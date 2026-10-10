"""Tokens, sessions, refresh rotation and rate limiting."""

import time
import uuid
from datetime import UTC, datetime, timedelta

import fakeredis
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from django.conf import settings as django_settings
from django.utils import timezone

from nexa_common.errors import RateLimited
from nexa_common.jwt import (
    TokenExpired,
    TokenInvalid,
    UnknownSigningKey,
    decode_access_token,
    encode_access_token,
    key_id,
)
from nexa_core.audit.models import AuditLog
from nexa_core.identity.devices import describe_device
from nexa_core.identity.models import AuthSession, RefreshToken
from nexa_core.identity.sessions import (
    RefreshConflict,
    SessionEnded,
    revoke_session,
    rotate_refresh_token,
    start_session,
)
from nexa_core.security import ratelimit
from nexa_core.security.ratelimit import _client as REAL_REDIS_CLIENT
from nexa_core.testing import UserFactory

KEY = Ed25519PrivateKey.generate()
KEYS = {key_id(KEY.public_key()): KEY.public_key()}


def _token(**overrides: object) -> str:
    fields: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "organization_id": None,
        "token_id": "t1",
    }
    fields.update(overrides)
    return encode_access_token(KEY, **fields)  # type: ignore[arg-type]


class TestAccessTokens:
    def test_round_trip(self) -> None:
        org = uuid.uuid4()
        claims = decode_access_token(_token(organization_id=org), KEYS)
        assert claims.organization_id == org

    def test_expired_token_is_rejected(self) -> None:
        old = datetime.now(UTC) - timedelta(hours=1)
        with pytest.raises(TokenExpired):
            decode_access_token(_token(now=old), KEYS)

    def test_token_from_an_unknown_key_is_rejected(self) -> None:
        other = Ed25519PrivateKey.generate()
        token = encode_access_token(
            other, user_id=uuid.uuid4(), session_id=uuid.uuid4(), organization_id=None, token_id="x"
        )
        with pytest.raises(UnknownSigningKey):
            decode_access_token(token, KEYS)

    def test_tampered_payload_is_rejected(self) -> None:
        header, _payload, signature = _token().split(".")
        forged = jwt.utils.base64url_encode(
            b'{"sub":"' + uuid.uuid4().hex.encode() + b'"}'
        ).decode()
        with pytest.raises(TokenInvalid):
            decode_access_token(f"{header}.{forged}.{signature}", KEYS)

    def test_unsigned_alg_none_token_is_rejected(self) -> None:
        unsigned = jwt.encode(
            {"sub": str(uuid.uuid4())},
            key="",
            algorithm="none",
            headers={"kid": next(iter(KEYS))},
        )
        with pytest.raises(TokenInvalid):
            decode_access_token(unsigned, KEYS)


def test_device_labels() -> None:
    chrome_windows = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    )
    edge = chrome_windows + " Edg/140.0"
    firefox_linux = "Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0"
    assert describe_device(chrome_windows) == "Chrome on Windows"
    assert describe_device(edge) == "Edge on Windows"
    assert describe_device(firefox_linux) == "Firefox on Linux"
    assert describe_device("") == "Unknown device"


@pytest.mark.django_db
class TestSessions:
    def test_start_session_issues_both_tokens(self, make_user: UserFactory) -> None:
        issued = start_session(make_user(), client_type="web")
        assert (
            decode_access_token(issued.access_token, _core_keys()).session_id == issued.session.id
        )
        assert RefreshToken.objects.filter(session=issued.session).count() == 1

    def test_rotation_spends_the_old_refresh_token(self, make_user: UserFactory) -> None:
        first = start_session(make_user(), client_type="web")
        second = rotate_refresh_token(first.refresh_token)
        assert second.refresh_token != first.refresh_token
        assert second.session.id == first.session.id

    def test_reuse_inside_the_grace_window_is_a_conflict_not_an_attack(
        self, make_user: UserFactory
    ) -> None:
        first = start_session(make_user(), client_type="web")
        rotate_refresh_token(first.refresh_token)
        with pytest.raises(RefreshConflict):
            rotate_refresh_token(first.refresh_token)
        assert AuthSession.objects.get(id=first.session.id).revoked_at is None

    def test_reuse_after_the_grace_window_revokes_the_session(self, make_user: UserFactory) -> None:
        first = start_session(make_user(), client_type="web")
        second = rotate_refresh_token(first.refresh_token)
        RefreshToken.objects.filter(session=first.session, used_at__isnull=False).update(
            used_at=timezone.now() - timedelta(minutes=5)
        )
        with pytest.raises(SessionEnded):
            rotate_refresh_token(first.refresh_token)

        session = AuthSession.objects.get(id=first.session.id)
        assert session.revoked_reason == AuthSession.RevokedReason.TOKEN_REUSE
        assert AuditLog.objects.filter(action="TOKEN_REUSE_DETECTED").exists()
        # The legitimate holder's newer token died with the session.
        with pytest.raises(SessionEnded):
            rotate_refresh_token(second.refresh_token)

    def test_revoked_session_cannot_refresh(self, make_user: UserFactory) -> None:
        issued = start_session(make_user(), client_type="web")
        revoke_session(issued.session, AuthSession.RevokedReason.LOGOUT)
        with pytest.raises(SessionEnded):
            rotate_refresh_token(issued.refresh_token)

    def test_expired_refresh_token_ends_the_session(self, make_user: UserFactory) -> None:
        issued = start_session(make_user(), client_type="web")
        RefreshToken.objects.filter(session=issued.session).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(SessionEnded):
            rotate_refresh_token(issued.refresh_token)


def _core_keys() -> dict[str, Ed25519PublicKey]:
    from nexa_core.identity.keys import verification_keys

    return verification_keys()


@pytest.mark.django_db
class TestRateLimits:
    LIMIT = ratelimit.Limit("test", max_hits=3, window_seconds=60)

    def test_requests_beyond_the_limit_are_refused_with_retry_after(self) -> None:
        for _ in range(3):
            ratelimit.consume(self.LIMIT, "203.0.113.7")
        with pytest.raises(RateLimited) as refused:
            ratelimit.consume(self.LIMIT, "203.0.113.7")
        assert 0 < refused.value.retry_after_seconds <= 60

    def test_subjects_are_counted_separately(self) -> None:
        for _ in range(3):
            ratelimit.consume(self.LIMIT, "203.0.113.7")
        ratelimit.consume(self.LIMIT, "198.51.100.1")

    def test_keys_never_contain_the_raw_subject(self, fake_redis: fakeredis.FakeRedis) -> None:
        ratelimit.consume(self.LIMIT, "ada@example.com")
        assert not any(b"ada@example.com" in key for key in fake_redis.scan_iter())

    def test_limits_fail_open_fast_when_redis_is_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing listens on port 1.
        monkeypatch.setattr(django_settings, "NEXA_REDIS_QUEUE_URL", "redis://127.0.0.1:1/0")
        REAL_REDIS_CLIENT.cache_clear()
        monkeypatch.setattr("nexa_core.security.ratelimit._client", REAL_REDIS_CLIENT)
        monkeypatch.setattr("nexa_core.security.ratelimit._unavailable_until", 0.0)
        started = time.monotonic()
        try:
            for _ in range(10):
                ratelimit.consume(self.LIMIT, "203.0.113.7")
        finally:
            REAL_REDIS_CLIENT.cache_clear()
        # No retries, and the breaker means only the first call pays a connection attempt.
        assert time.monotonic() - started < 3
