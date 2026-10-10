from celery import shared_task


@shared_task(name="core.system.ping")
def ping() -> str:
    """Round trip through the core queue, used to check that a worker is consuming."""
    return "pong"
