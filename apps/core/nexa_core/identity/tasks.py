from smtplib import SMTPException

from celery import shared_task
from django.core.mail import send_mail


@shared_task(
    name="core.identity.send_email",
    autoretry_for=(SMTPException, OSError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_email(to: str, subject: str, body: str) -> None:
    send_mail(subject, body, None, [to])
