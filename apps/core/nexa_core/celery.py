"""Celery application for Core workers (doc 21).

Workers run the same code as the API with a different command; see the root package.json.
"""

import os
from typing import Any

from celery import Celery
from celery.signals import setup_logging, task_postrun, task_prerun

from nexa_common.context import bind_request_context, clear_request_context
from nexa_common.logs import configure_logging
from nexa_core.config import get_settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nexa_core.settings")

app = Celery("nexa_core")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@setup_logging.connect
def _use_nexa_logging(**_: Any) -> None:
    """Connecting this signal stops Celery from replacing NEXA's log configuration."""
    env = get_settings()
    configure_logging(
        service="core-worker", env=env.nexa_env, level=env.log_level, json_logs=env.log_json
    )


@task_prerun.connect
def _bind_task_context(task_id: str | None = None, task: Any = None, **_: Any) -> None:
    bind_request_context(task_id=task_id, task=getattr(task, "name", None))


@task_postrun.connect
def _clear_task_context(**_: Any) -> None:
    clear_request_context()
