"""Structured logging for every NEXA process (doc 27.8).

structlog renders both our own events and standard-library records (Django, Celery, uvicorn),
so every line has the same shape: JSON in deployed environments, readable text locally.
"""

import importlib.util
import logging
import re
import sys
from typing import Any

import structlog
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, Processor, WrappedLogger

# Libraries that install their own handlers; their records are routed through ours instead.
_ADOPTED_LOGGERS = ("uvicorn", "uvicorn.error", "celery", "django")
# Access logs duplicate the one line per request that NEXA's own middleware writes.
_SILENCED_LOGGERS = ("uvicorn.access",)

# Credential-shaped strings, redacted from every rendered value (architecture doc 23.5).
_SECRET_PATTERNS = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"
)
_REDACTED = "[REDACTED]"


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Replace API keys, bearer tokens and JWTs anywhere in a log event."""
    return {key: _redact(value) for key, value in event_dict.items()}


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_PATTERNS.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact(v) for v in value)
    return value


def configure_logging(
    *, service: str, env: str, level: str = "INFO", json_logs: bool = True
) -> None:
    def add_service(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("env", env)
        return event_dict

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_service,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderers: list[Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if json_logs:
        # Never render frame locals: they would carry prompts, request bodies and SDK headers.
        tracebacks = structlog.processors.ExceptionRenderer(
            ExceptionDictTransformer(show_locals=False)
        )
        renderers += [tracebacks, redact_secrets, structlog.processors.JSONRenderer()]
    else:
        renderers += [
            redact_secrets,
            structlog.dev.ConsoleRenderer(
                exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False)
                if _rich_available()
                else structlog.dev.plain_traceback
            ),
        ]

    # Piped output on Windows defaults to a legacy code page; never let a log line crash a process.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="backslashreplace")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared, processors=renderers)
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in _ADOPTED_LOGGERS:
        adopted = logging.getLogger(name)
        adopted.handlers = []
        adopted.propagate = True
    for name in _SILENCED_LOGGERS:
        silenced = logging.getLogger(name)
        silenced.handlers = []
        silenced.propagate = False


def _rich_available() -> bool:
    return importlib.util.find_spec("rich") is not None


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
