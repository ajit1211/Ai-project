"""The extraction engine: one entry point, one handler per format.

    extract(path, "pdf", limits)  ──►  Extraction(text, pages, metadata, warnings)
                                  ──►  ExtractionError(code, message)

Adding a format is a handler and a line in `HANDLERS` plus its signature in `sniff`; the
upload, the lifecycle and the API do not change.
"""

from collections.abc import Callable
from pathlib import Path

from nexa_core.documents.extraction.docx import extract_docx
from nexa_core.documents.extraction.pdf import extract_pdf
from nexa_core.documents.extraction.plain import extract_plain
from nexa_core.documents.extraction.sniff import (
    EXTENSIONS,
    HEAD_BYTES,
    MEDIA_TYPES,
    format_for_name,
    looks_like,
)
from nexa_core.documents.extraction.tabular import extract_csv
from nexa_core.documents.extraction.text import apply_text_limit
from nexa_core.documents.extraction.types import (
    DEFAULT_LIMITS,
    FORMATS,
    PARSER_VERSION,
    Extraction,
    ExtractionError,
    Format,
    Limits,
    PageSpan,
)

Handler = Callable[[Path, Limits], Extraction]

HANDLERS: dict[Format, Handler] = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "txt": lambda path, limits: extract_plain(path, limits, markdown=False),
    "md": lambda path, limits: extract_plain(path, limits, markdown=True),
    "csv": extract_csv,
}


def extract(path: Path, fmt: Format, limits: Limits = DEFAULT_LIMITS) -> Extraction:
    """Text, page spans, metadata and warnings for one file, within the limits."""
    handler = HANDLERS.get(fmt)
    if handler is None:
        raise ExtractionError("unsupported_format", f"'{fmt}' files are not supported.")
    try:
        extraction = handler(path, limits)
    except ExtractionError:
        raise
    except MemoryError as exc:
        raise ExtractionError("archive_too_large", "The file is too large to process.") from exc
    except Exception as exc:
        raise ExtractionError("extraction_failed", "The file could not be processed.") from exc
    return apply_text_limit(extraction, limits.max_text_chars)


__all__ = [
    "DEFAULT_LIMITS",
    "EXTENSIONS",
    "FORMATS",
    "HANDLERS",
    "HEAD_BYTES",
    "MEDIA_TYPES",
    "PARSER_VERSION",
    "Extraction",
    "ExtractionError",
    "Format",
    "Limits",
    "PageSpan",
    "extract",
    "format_for_name",
    "looks_like",
]
