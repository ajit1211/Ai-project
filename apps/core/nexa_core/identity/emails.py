"""Transactional emails for accounts and invitations.

Links carry their one-time token in the URL fragment (`#token=...`). Browsers never send the
fragment to servers, so tokens stay out of access logs, proxies and Referer headers.

Emails are queued after the database transaction commits. If the queue is unreachable, the
email is sent inline instead: a Redis outage must not silently swallow a verification link,
and a short breaker keeps later requests from paying the broker's connection timeout.
"""

import time

from django.conf import settings
from django.db import transaction

from nexa_common.logs import get_logger
from nexa_core.identity.tasks import send_email

log = get_logger("nexa.core.emails")


def _link(path: str, token: str) -> str:
    return f"{settings.NEXA_FRONTEND_URL}{path}#token={token}"


_BROKER_RETRY_SECONDS = 30.0
_broker_down_until = 0.0


def _dispatch(to: str, subject: str, body: str) -> None:
    global _broker_down_until
    if time.monotonic() < _broker_down_until:
        send_email.run(to, subject, body)
        return
    try:
        send_email.apply_async(kwargs={"to": to, "subject": subject, "body": body}, retry=False)
    except Exception:  # broker unreachable: deliver now rather than never
        _broker_down_until = time.monotonic() + _BROKER_RETRY_SECONDS
        log.warning("email.queue_unavailable", subject=subject)
        send_email.run(to, subject, body)


def _send_after_commit(to: str, subject: str, body: str) -> None:
    transaction.on_commit(lambda: _dispatch(to, subject, body))


def verification_email(to: str, name: str, token: str) -> None:
    _send_after_commit(
        to,
        "Verify your NEXA email address",
        f"Hi {name or 'there'},\n\n"
        f"Confirm your email address to finish creating your NEXA account:\n\n"
        f"{_link('/verify-email', token)}\n\n"
        f"The link expires in 24 hours. If you did not sign up, ignore this email.\n",
    )


def account_exists_email(to: str) -> None:
    _send_after_commit(
        to,
        "You already have a NEXA account",
        "Someone tried to create a NEXA account with this email address, which already has "
        f"one.\n\nSign in at {settings.NEXA_FRONTEND_URL}/login, or reset your password at "
        f"{settings.NEXA_FRONTEND_URL}/forgot-password.\n\n"
        "If this was not you, no action is needed.\n",
    )


def password_reset_email(to: str, token: str) -> None:
    _send_after_commit(
        to,
        "Reset your NEXA password",
        "Use this link to choose a new password:\n\n"
        f"{_link('/reset-password', token)}\n\n"
        "The link expires in 30 minutes and works once. If you did not ask for this, ignore "
        "this email; your password has not changed.\n",
    )


def password_changed_email(to: str) -> None:
    _send_after_commit(
        to,
        "Your NEXA password was changed",
        "The password for your NEXA account was just changed, and other devices were signed "
        "out.\n\nIf this was not you, reset your password immediately at "
        f"{settings.NEXA_FRONTEND_URL}/forgot-password.\n",
    )


def invitation_email(to: str, *, organization: str, inviter: str, role: str, token: str) -> None:
    _send_after_commit(
        to,
        f"{inviter} invited you to {organization} on NEXA",
        f"{inviter} invited you to join {organization} on NEXA as {role}.\n\n"
        f"{_link('/invitations/accept', token)}\n\n"
        "The invitation expires in 7 days. If you do not know the sender, ignore this email.\n",
    )
