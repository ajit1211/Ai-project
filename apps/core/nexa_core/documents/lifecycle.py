"""The processing lifecycle of a document (docs/architecture/documents.md#lifecycle).

    uploaded ──► queued ──► processing ──► completed
                   ▲            │
                   │            ├──► failed        (retry ──► queued)
                   │            ├──► needs_ocr     (retry ──► queued)
                   │            └──► unsupported   (retry ──► queued)
                   └── a queued or processing attempt that went stale is queued again

Every change of status goes through `transition`, which refuses anything not drawn above.
The attempt itself (`DocumentIngestion`) has the same states plus `cancelled`, for an attempt
that found its document deleted.
"""

from datetime import datetime
from typing import Any

UPLOADED = "uploaded"
QUEUED = "queued"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"
NEEDS_OCR = "needs_ocr"
UNSUPPORTED = "unsupported"
CANCELLED = "cancelled"

STATUSES: tuple[str, ...] = (
    UPLOADED,
    QUEUED,
    PROCESSING,
    COMPLETED,
    FAILED,
    NEEDS_OCR,
    UNSUPPORTED,
)
# Outcomes of an attempt. `cancelled` belongs to the attempt only, never to the document.
ATTEMPT_STATUSES: tuple[str, ...] = (
    QUEUED,
    PROCESSING,
    COMPLETED,
    FAILED,
    NEEDS_OCR,
    UNSUPPORTED,
    CANCELLED,
)
TERMINAL: frozenset[str] = frozenset({COMPLETED, FAILED, NEEDS_OCR, UNSUPPORTED})
# The user may ask for another attempt from these.
RETRYABLE: frozenset[str] = frozenset({FAILED, NEEDS_OCR, UNSUPPORTED})
# An attempt that has been in one of these longer than the processing timeout is stale.
IN_FLIGHT: frozenset[str] = frozenset({QUEUED, PROCESSING})

TRANSITIONS: dict[str, frozenset[str]] = {
    UPLOADED: frozenset({QUEUED, FAILED}),
    QUEUED: frozenset({PROCESSING, FAILED, QUEUED}),
    PROCESSING: frozenset({COMPLETED, FAILED, NEEDS_OCR, UNSUPPORTED, QUEUED}),
    COMPLETED: frozenset(),
    FAILED: frozenset({QUEUED}),
    NEEDS_OCR: frozenset({QUEUED}),
    UNSUPPORTED: frozenset({QUEUED}),
}


class InvalidTransition(ValueError):
    def __init__(self, current: str, to: str) -> None:
        super().__init__(f"A document cannot go from '{current}' to '{to}'.")
        self.current = current
        self.to = to


def can_transition(current: str, to: str) -> bool:
    return to in TRANSITIONS.get(current, frozenset())


def transition(document: Any, to: str, *, now: datetime) -> None:
    """Move `document` to `to`, keeping its timestamps in step. Raises InvalidTransition."""
    if not can_transition(document.status, to):
        raise InvalidTransition(document.status, to)
    document.status = to
    if to == PROCESSING:
        document.processing_started_at = now
        document.processed_at = None
    elif to in TERMINAL:
        document.processed_at = now
    elif to == QUEUED:
        document.processed_at = None
        document.error_code = ""
        document.error_message = ""
