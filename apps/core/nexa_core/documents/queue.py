"""Handing document work to the worker (`core.documents` queue), after the commit.

When the broker is unreachable the task runs here and now rather than never, the choice the
email flow makes; a breaker then skips the broker for a while, so a dead broker costs one
timeout, not one per upload. Tasks are idempotent, so a run that also arrives later through
a recovered broker does nothing the second time.
"""

from __future__ import annotations  # celery's Task is generic only to the type checker

import time
from typing import Any

from celery import Task

from nexa_common.logs import get_logger

log = get_logger("nexa.core.documents")

BROKER_RETRY_SECONDS = 30.0
_broker_down_until = 0.0


def dispatch(task: Task[Any, Any], kwargs: dict[str, Any]) -> None:
    global _broker_down_until
    if time.monotonic() < _broker_down_until:
        task.run(**kwargs)
        return
    try:
        task.apply_async(kwargs=kwargs, retry=False)
    except Exception as exc:  # broker unreachable: do the work now rather than never
        _broker_down_until = time.monotonic() + BROKER_RETRY_SECONDS
        log.warning("documents.queue_unavailable", task=task.name, error=type(exc).__name__)
        task.run(**kwargs)
