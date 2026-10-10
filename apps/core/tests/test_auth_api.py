"""Authentication API: registration, verification, login, refresh, logout, passwords, sessions."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from nexa_common.jwt import encode_access_token
from nexa_core.audit.models import AuditLog
from nexa_core.celery import app as celery_app
from nexa_core.identity.keys import signing_key
from nexa_core.identity.models import AuthSession, OneTimeToken, User
from nexa_core.organizations.models import Organization
from nexa_core.testing import PASSWORD, UserFactory

pytestmark = pytest.mark.django_db
CSRF = {"X-NEXA-CSRF": "1"}
NEW_PASSWORD = "an-entirely-new-passphrase"


@pytest.fixture(autouse=True)
def eager_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.fixture
def commit(django_capture_on_commit_callbacks: Any) -> Callable[[], Any]:
    """Run on-commit callbacks (emails) as if the transaction had committed."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


def _token_from_last_email() -> str:
    match = re.search(r"#token=([\w-]+)", str(mail.outbox[-1].body))
    assert match, mail.outbox[-1].body
    return match.group(1)


def _register(api: APIClient, commit: Callable[[], Any], email: str = "ada@example.com") -> Any:
    with commit():
        return api.post(
            "/api/v1/auth/register/",
            {"email": email, "password": PASSWORD, "full_name": "Ada Lovelace"},
            format="json",
        )


def _login(api: APIClient, email: str, password: str = PASSWORD, **extra: Any) -> Any:
    return api.post(
        "/api/v1/auth/login/", {"email": email, "password": password, **extra}, format="json"
    )


def _signed_in(api: APIClient, user: User) -> str:
    response = _login(api, user.email)
    assert response.status_code == 200, response.data
    token: str = response.data["data"]["access_token"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return token


class TestRegistrationAndVerification:
    def test_register_creates_unverified_user_with_personal_workspace(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        response = _register(api, commit)

        assert response.status_code == 202
        assert response.data == {
            "success": True,
            "data": None,
            "message": "Check your email to verify your account.",
        }
        user = User.objects.get(email="ada@example.com")
        assert not user.is_verified
        assert Organization.objects.filter(memberships__user=user, kind="personal").exists()
        assert mail.outbox[-1].subject == "Verify your NEXA email address"
        assert AuditLog.objects.filter(action="USER_REGISTERED", actor_id=user.id).exists()

    def test_duplicate_email_gets_an_identical_response(
        self, api: APIClient, commit: Callable[[], Any], make_user: UserFactory
    ) -> None:
        make_user("ada@example.com")
        response = _register(api, commit, email="ADA@example.com")
        assert response.status_code == 202
        assert response.data["message"] == "Check your email to verify your account."
        assert User.objects.filter(email="ada@example.com").count() == 1
        assert mail.outbox[-1].subject == "You already have a NEXA account"

    def test_weak_password_is_rejected_with_field_details(self, api: APIClient) -> None:
        response = api.post(
            "/api/v1/auth/register/",
            {"email": "a@example.com", "password": "password"},
            format="json",
        )
        assert response.status_code == 422
        assert {d["field"] for d in response.data["error"]["details"]} == {"password"}

    def test_unverified_user_cannot_sign_in(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        _register(api, commit)
        response = _login(api, "ada@example.com")
        assert response.status_code == 403
        assert response.data["error"]["code"] == "EMAIL_NOT_VERIFIED"

    def test_verification_link_works_once(self, api: APIClient, commit: Callable[[], Any]) -> None:
        _register(api, commit)
        token = _token_from_last_email()

        first = api.post("/api/v1/auth/verify-email/", {"token": token}, format="json")
        again = api.post("/api/v1/auth/verify-email/", {"token": token}, format="json")

        assert first.status_code == 200
        assert User.objects.get(email="ada@example.com").is_verified
        assert again.status_code == 400
        assert again.data["error"]["code"] == "TOKEN_INVALID"
        assert _login(api, "ada@example.com").status_code == 200

    def test_unknown_and_expired_verification_tokens(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        _register(api, commit)
        token = _token_from_last_email()
        OneTimeToken.objects.update(expires_at=timezone.now() - timedelta(minutes=1))

        expired = api.post("/api/v1/auth/verify-email/", {"token": token}, format="json")
        unknown = api.post("/api/v1/auth/verify-email/", {"token": "not-a-token"}, format="json")

        assert expired.data["error"]["code"] == "TOKEN_EXPIRED"
        assert unknown.data["error"]["code"] == "TOKEN_INVALID"

    def test_resend_replaces_the_previous_link(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        _register(api, commit)
        old_token = _token_from_last_email()
        with commit():
            response = api.post(
                "/api/v1/auth/resend-verification/", {"email": "ada@example.com"}, format="json"
            )
        new_token = _token_from_last_email()

        assert response.status_code == 202
        assert new_token != old_token
        assert (
            api.post("/api/v1/auth/verify-email/", {"token": old_token}, format="json").status_code
            == 400
        )
        assert (
            api.post("/api/v1/auth/verify-email/", {"token": new_token}, format="json").status_code
            == 200
        )

    def test_resend_for_unknown_email_looks_the_same(self, api: APIClient) -> None:
        response = api.post(
            "/api/v1/auth/resend-verification/", {"email": "nobody@example.com"}, format="json"
        )
        assert response.status_code == 202
        assert mail.outbox == []


class TestLoginAndTokens:
    def test_login_returns_access_token_and_secure_refresh_cookie(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        user = make_user()
        response = _login(api, user.email)

        assert response.status_code == 200
        data = response.data["data"]
        assert data["token_type"] == "Bearer"
        assert data["expires_in"] == 600
        assert "refresh_token" not in data
        assert data["user"]["email"] == user.email
        assert "password" not in str(data)
        cookie = response.cookies["nexa_refresh"]
        assert cookie["httponly"]
        assert cookie["secure"]
        assert cookie["samesite"] == "Strict"
        assert cookie["path"] == "/api/v1/auth"

    def test_mobile_clients_receive_the_refresh_token_in_the_body(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        response = _login(api, make_user().email, client="mobile")
        assert "refresh_token" in response.data["data"]
        assert "nexa_refresh" not in response.cookies

    def test_wrong_password_and_unknown_email_look_the_same(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        user = make_user()
        wrong = _login(api, user.email, "not-the-password")
        unknown = _login(api, "nobody@example.com")
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.data["error"] == unknown.data["error"]
        assert AuditLog.objects.filter(action="USER_LOGIN_FAILED", category="security").count() == 2

    def test_protected_endpoint_requires_a_token(self, api: APIClient) -> None:
        response = api.get("/api/v1/users/me/")
        assert response.status_code == 401
        assert response.data["error"]["code"] == "AUTH_REQUIRED"
        assert response["WWW-Authenticate"].startswith("Bearer")

    def test_access_token_grants_access(self, api: APIClient, make_user: UserFactory) -> None:
        user = make_user()
        _signed_in(api, user)
        assert api.get("/api/v1/users/me/").data["data"]["email"] == user.email

    def test_expired_and_forged_tokens_are_rejected(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        user = make_user()
        session = AuthSession.objects.create(
            user=user, client_type="web", expires_at=timezone.now() + timedelta(days=1)
        )
        expired = encode_access_token(
            signing_key(),
            user_id=user.id,
            session_id=session.id,
            organization_id=None,
            token_id="t",
            now=datetime.now(UTC) - timedelta(hours=1),
        )
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {expired}")
        assert api.get("/api/v1/users/me/").data["error"]["code"] == "AUTH_TOKEN_EXPIRED"
        api.credentials(HTTP_AUTHORIZATION="Bearer not.a.jwt")
        assert api.get("/api/v1/users/me/").data["error"]["code"] == "AUTH_TOKEN_INVALID"

    def test_refresh_with_cookie_requires_the_csrf_header(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        _login(api, make_user().email)
        refused = api.post("/api/v1/auth/refresh/")
        cross_site = api.post(
            "/api/v1/auth/refresh/", headers={**CSRF, "Origin": "https://evil.example"}
        )
        allowed = api.post("/api/v1/auth/refresh/", headers=CSRF)

        assert refused.status_code == 403
        assert cross_site.status_code == 403
        assert allowed.status_code == 200
        assert allowed.data["data"]["access_token"]

    def test_mobile_refresh_rotates_the_token(self, api: APIClient, make_user: UserFactory) -> None:
        first = _login(api, make_user().email, client="mobile").data["data"]["refresh_token"]
        response = api.post("/api/v1/auth/refresh/", {"refresh_token": first}, format="json")
        assert response.status_code == 200
        assert response.data["data"]["refresh_token"] != first

    def test_logout_revokes_the_session_immediately(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        _signed_in(api, make_user())
        assert api.post("/api/v1/auth/logout/").status_code == 200

        after = api.get("/api/v1/users/me/")
        assert after.status_code == 401
        assert after.data["error"]["code"] == "AUTH_SESSION_REVOKED"
        assert api.post("/api/v1/auth/refresh/", headers=CSRF).status_code == 401


class TestPasswords:
    def test_reset_flow_changes_password_and_ends_every_session(
        self, api: APIClient, commit: Callable[[], Any], make_user: UserFactory
    ) -> None:
        user = make_user()
        _signed_in(api, user)
        with commit():
            assert (
                api.post(
                    "/api/v1/auth/forgot-password/", {"email": user.email}, format="json"
                ).status_code
                == 202
            )
        token = _token_from_last_email()

        with commit():
            reset = api.post(
                "/api/v1/auth/reset-password/",
                {"token": token, "password": NEW_PASSWORD},
                format="json",
            )
        reused = api.post(
            "/api/v1/auth/reset-password/",
            {"token": token, "password": NEW_PASSWORD + "2"},
            format="json",
        )

        assert reset.status_code == 200
        assert api.get("/api/v1/users/me/").status_code == 401  # old session revoked
        assert reused.data["error"]["code"] == "TOKEN_INVALID"
        assert _login(APIClient(), user.email, NEW_PASSWORD).status_code == 200
        assert mail.outbox[-1].subject == "Your NEXA password was changed"

    def test_forgot_password_for_unknown_email_sends_nothing(self, api: APIClient) -> None:
        response = api.post(
            "/api/v1/auth/forgot-password/", {"email": "nobody@example.com"}, format="json"
        )
        assert response.status_code == 202
        assert mail.outbox == []

    def test_expired_reset_link(
        self, api: APIClient, commit: Callable[[], Any], make_user: UserFactory
    ) -> None:
        user = make_user()
        with commit():
            api.post("/api/v1/auth/forgot-password/", {"email": user.email}, format="json")
        OneTimeToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        response = api.post(
            "/api/v1/auth/reset-password/",
            {"token": _token_from_last_email(), "password": NEW_PASSWORD},
            format="json",
        )
        assert response.data["error"]["code"] == "TOKEN_EXPIRED"

    def test_change_password_keeps_this_session_and_ends_the_others(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        user = make_user()
        other_device = APIClient()
        _signed_in(other_device, user)
        _signed_in(api, user)

        wrong = api.post(
            "/api/v1/auth/change-password/",
            {"current_password": "nope", "new_password": NEW_PASSWORD},
            format="json",
        )
        right = api.post(
            "/api/v1/auth/change-password/",
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            format="json",
        )

        assert wrong.status_code == 422
        assert right.status_code == 200
        assert api.get("/api/v1/users/me/").status_code == 200
        assert other_device.get("/api/v1/users/me/").status_code == 401


class TestSessionsAndProfile:
    def test_list_and_revoke_sessions(self, api: APIClient, make_user: UserFactory) -> None:
        user = make_user()
        other_device = APIClient(HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) Firefox/142.0")
        _signed_in(other_device, user)
        _signed_in(api, user)

        sessions = api.get("/api/v1/sessions/").data["data"]
        assert len(sessions) == 2
        assert [s["is_current"] for s in sessions].count(True) == 1
        other = next(s for s in sessions if not s["is_current"])
        assert other["device"] == "Firefox on Linux"

        assert api.delete(f"/api/v1/sessions/{other['id']}/").status_code == 200
        assert other_device.get("/api/v1/users/me/").status_code == 401
        assert api.get("/api/v1/users/me/").status_code == 200

    def test_cannot_revoke_another_users_session(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        victim = make_user()
        victim_client = APIClient()
        _signed_in(victim_client, victim)
        victim_session = AuthSession.objects.get(user=victim)

        _signed_in(api, make_user())
        response = api.delete(f"/api/v1/sessions/{victim_session.id}/")
        assert response.status_code == 404
        assert victim_client.get("/api/v1/users/me/").status_code == 200

    def test_logout_all_ends_every_session(self, api: APIClient, make_user: UserFactory) -> None:
        user = make_user()
        other_device = APIClient()
        _signed_in(other_device, user)
        _signed_in(api, user)
        assert api.post("/api/v1/sessions/logout-all/").data["data"]["sessions_revoked"] == 2
        assert api.get("/api/v1/users/me/").status_code == 401
        assert other_device.get("/api/v1/users/me/").status_code == 401

    def test_profile_update_validates_input(self, api: APIClient, make_user: UserFactory) -> None:
        _signed_in(api, make_user())
        bad_zone = api.patch("/api/v1/users/me/", {"timezone": "Mars/Olympus"}, format="json")
        bad_pref = api.patch(
            "/api/v1/users/me/", {"preferences": {"is_admin": True}}, format="json"
        )
        good = api.patch(
            "/api/v1/users/me/",
            {"full_name": "Ada", "timezone": "Asia/Kolkata", "preferences": {"theme": "dark"}},
            format="json",
        )
        assert bad_zone.status_code == 422
        assert bad_pref.status_code == 422
        profile = good.data["data"]["profile"]
        assert (profile["timezone"], profile["preferences"]["theme"]) == ("Asia/Kolkata", "dark")

    def test_deactivated_account_cannot_sign_in(
        self, api: APIClient, make_user: UserFactory
    ) -> None:
        user = make_user()
        _signed_in(api, user)
        assert (
            api.post(
                "/api/v1/users/me/deactivate/", {"password": PASSWORD}, format="json"
            ).status_code
            == 200
        )
        assert api.get("/api/v1/users/me/").status_code == 401
        assert _login(APIClient(), user.email).status_code == 401


class TestRateLimits:
    def test_registration_is_limited_per_ip(
        self, api: APIClient, commit: Callable[[], Any]
    ) -> None:
        for number in range(5):
            assert _register(api, commit, email=f"user{number}@example.com").status_code == 202
        response = _register(api, commit, email="one-more@example.com")
        assert response.status_code == 429
        assert int(response["Retry-After"]) > 0
        assert AuditLog.objects.filter(action="RATE_LIMIT_EXCEEDED").count() == 1

    def test_repeated_failures_lock_the_account_temporarily(
        self, api: APIClient, make_user: UserFactory, fake_redis: Any
    ) -> None:
        user = make_user()
        for _ in range(10):
            assert _login(api, user.email, "wrong-password").status_code == 401
        locked = _login(api, user.email)  # even the right password waits out the window
        assert locked.status_code == 429

        fake_redis.flushall()  # the window passes
        assert _login(api, user.email).status_code == 200
