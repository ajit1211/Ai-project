"""Email dispatch: queued when the broker is up, sent inline (once-tripped breaker) when not."""

from typing import Any

import pytest
from django.core import mail

from nexa_core.identity import emails
from nexa_core.identity.tasks import send_email


@pytest.fixture
def broker_down(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def failing_apply_async(**kwargs: Any) -> None:
        attempts.append(kwargs["kwargs"]["subject"])
        raise OSError("connection refused")

    monkeypatch.setattr(send_email, "apply_async", failing_apply_async)
    monkeypatch.setattr(emails, "_broker_down_until", 0.0)
    return attempts


def test_emails_are_sent_inline_when_the_queue_is_down(broker_down: list[str]) -> None:
    emails._dispatch("a@example.com", "First", "body")
    emails._dispatch("b@example.com", "Second", "body")

    assert [m.subject for m in mail.outbox] == ["First", "Second"]
    # Only the first send paid for a broker connection attempt; the breaker skipped the second.
    assert broker_down == ["First"]
