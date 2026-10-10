"""Authentication, account and session endpoints (docs/api/authentication.md)."""

from typing import Any
from uuid import UUID

from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from nexa_common.context import current_request_id
from nexa_common.errors import AuthRequired, NexaError, NotFound, error_body_from
from nexa_core.access.drf import AuthenticatedView, current_auth, current_user
from nexa_core.api import ok
from nexa_core.audit.actions import AuditAction
from nexa_core.audit.service import record
from nexa_core.identity import services
from nexa_core.identity.cookies import (
    clear_refresh_cookie,
    read_refresh_cookie,
    set_refresh_cookie,
)
from nexa_core.identity.keys import jwks
from nexa_core.identity.models import AuthSession
from nexa_core.identity.serializers import (
    ChangePasswordInput,
    EmailInput,
    LoginInput,
    PasswordInput,
    ProfileUpdateInput,
    RefreshInput,
    RegisterInput,
    ResetPasswordInput,
    SwitchOrganizationInput,
    TokenInput,
    session_payload,
    token_payload,
    user_payload,
)
from nexa_core.identity.sessions import (
    IssuedTokens,
    RefreshConflict,
    revoke_all_sessions,
    revoke_session,
    rotate_refresh_token,
    switch_organization,
)


def _validated(serializer_class: type[serializers.Serializer[Any]], data: Any) -> dict[str, Any]:
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def _tokens_response(
    issued: IssuedTokens, *, deliver_in_body: bool, message: str | None = None
) -> Response:
    data = token_payload(issued.access_token, issued.access_expires_in, issued.session)
    data["user"] = user_payload(issued.session.user)
    if deliver_in_body:
        data["refresh_token"] = issued.refresh_token
        data["refresh_expires_at"] = issued.refresh_expires_at.isoformat()
    response = ok(data, message=message)
    if not deliver_in_body:
        set_refresh_cookie(response, issued.refresh_token, issued.refresh_expires_at)
    return response


class PublicView(APIView):
    """No credential needed; a stale Authorization header is ignored rather than rejected."""

    authentication_classes = ()
    permission_classes = (AllowAny,)


class RegisterView(PublicView):
    def post(self, request: Request) -> Response:
        services.register(**_validated(RegisterInput, request.data))
        return ok(message="Check your email to verify your account.", status=202)


class LoginView(PublicView):
    def post(self, request: Request) -> Response:
        data = _validated(LoginInput, request.data)
        issued = services.login(
            email=data["email"], password=data["password"], client_type=data["client"]
        )
        return _tokens_response(
            issued,
            deliver_in_body=data["client"] == AuthSession.ClientType.MOBILE,
            message="Signed in.",
        )


class RefreshView(PublicView):
    """Web clients send the cookie; mobile clients send `refresh_token` in the body."""

    def post(self, request: Request) -> Response:
        body_token = _validated(RefreshInput, request.data).get("refresh_token")
        raw = body_token or read_refresh_cookie(request)
        if not raw:
            raise AuthRequired
        try:
            issued = rotate_refresh_token(raw)
        except NexaError as exc:
            if body_token or isinstance(exc, RefreshConflict):
                raise
            # The cookie is dead: tell the browser to drop it.
            response = Response(
                error_body_from(exc, request_id=current_request_id()), status=exc.http_status
            )
            clear_refresh_cookie(response)
            return response
        return _tokens_response(issued, deliver_in_body=bool(body_token))


class LogoutView(AuthenticatedView):
    def post(self, request: Request) -> Response:
        session = current_auth(request).session
        revoke_session(session, AuthSession.RevokedReason.LOGOUT)
        record(AuditAction.USER_LOGOUT, actor_id=session.user_id)
        response = ok(message="Signed out.")
        clear_refresh_cookie(response)
        return response


class VerifyEmailView(PublicView):
    def post(self, request: Request) -> Response:
        services.verify_email(_validated(TokenInput, request.data)["token"])
        return ok(message="Email verified. You can sign in now.")


class ResendVerificationView(PublicView):
    def post(self, request: Request) -> Response:
        services.resend_verification(_validated(EmailInput, request.data)["email"])
        return ok(message="If that account needs verifying, we have sent a new link.", status=202)


class ForgotPasswordView(PublicView):
    def post(self, request: Request) -> Response:
        services.request_password_reset(_validated(EmailInput, request.data)["email"])
        return ok(
            message="If an account exists for that email, we have sent a reset link.", status=202
        )


class ResetPasswordView(PublicView):
    def post(self, request: Request) -> Response:
        data = _validated(ResetPasswordInput, request.data)
        services.reset_password(data["token"], data["password"])
        return ok(message="Password updated. Sign in with your new password.")


class ChangePasswordView(AuthenticatedView):
    def post(self, request: Request) -> Response:
        data = _validated(ChangePasswordInput, request.data)
        services.change_password(
            current_user(request),
            current_password=data["current_password"],
            new_password=data["new_password"],
            session=current_auth(request).session,
        )
        return ok(message="Password changed. Your other devices were signed out.")


class SwitchOrganizationView(AuthenticatedView):
    """Make another organization the active one and get an access token scoped to it.

    The ID from the client is only a request: switching succeeds only if the user is an
    active member of a live organization with that ID.
    """

    def post(self, request: Request) -> Response:
        organization_id = _validated(SwitchOrganizationInput, request.data)["organization_id"]
        session = current_auth(request).session
        access_token, expires_in = switch_organization(session, organization_id)
        return ok(token_payload(access_token, expires_in, session))


class MeView(AuthenticatedView):
    def get(self, request: Request) -> Response:
        return ok(user_payload(current_user(request)))

    def patch(self, request: Request) -> Response:
        changes = _validated(ProfileUpdateInput, request.data)
        user = services.update_profile(current_user(request), changes)
        user.refresh_from_db()
        return ok(user_payload(user), message="Profile updated.")


class DeactivateAccountView(AuthenticatedView):
    def post(self, request: Request) -> Response:
        services.deactivate_account(
            current_user(request), _validated(PasswordInput, request.data)["password"]
        )
        response = ok(message="Your account has been deactivated.")
        clear_refresh_cookie(response)
        return response


class SessionListView(AuthenticatedView):
    def get(self, request: Request) -> Response:
        current = current_auth(request).session.id
        sessions = AuthSession.objects.filter(
            user=current_user(request), revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).order_by("-last_seen_at")
        return ok([session_payload(session, current) for session in sessions])


class SessionDetailView(AuthenticatedView):
    def delete(self, request: Request, session_id: UUID) -> Response:
        # Filtering by the caller's own user means another user's session ID is simply not found.
        session = AuthSession.objects.filter(
            id=session_id, user=current_user(request), revoked_at__isnull=True
        ).first()
        if session is None:
            raise NotFound
        revoke_session(session, AuthSession.RevokedReason.REVOKED_BY_USER)
        response = ok(message="Session signed out.")
        if session.id == current_auth(request).session.id:
            clear_refresh_cookie(response)
        return response


class LogoutAllView(AuthenticatedView):
    def post(self, request: Request) -> Response:
        count = revoke_all_sessions(current_user(request), AuthSession.RevokedReason.LOGOUT_ALL)
        response = ok({"sessions_revoked": count}, message="Signed out on every device.")
        clear_refresh_cookie(response)
        return response


@require_GET
def jwks_view(request: HttpRequest) -> JsonResponse:
    response = JsonResponse(jwks())
    response["Cache-Control"] = "public, max-age=600"
    return response
