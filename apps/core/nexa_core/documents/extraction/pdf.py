"""PDF: the text layer of each page, with page spans, through pypdf.

A PDF with no text layer (scans, exported images) is reported as `needs_ocr`; nothing here
pretends to read pixels. Encrypted files are tried with the empty user password, which is how
most "protected" PDFs are made, and refused otherwise.
"""

import logging
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError, PdfStreamError

from nexa_core.documents.extraction.text import normalize
from nexa_core.documents.extraction.types import Extraction, ExtractionError, Limits, PageSpan

# pypdf logs every malformed object it repairs; that is noise, and never ours to act on.
logging.getLogger("pypdf").setLevel(logging.ERROR)

_METADATA_KEYS = {
    "/Title": "title",
    "/Author": "author",
    "/Subject": "subject",
    "/Creator": "creator",
    "/Producer": "producer",
    "/CreationDate": "created",
    "/ModDate": "modified",
}


def extract_pdf(path: Path, limits: Limits) -> Extraction:
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    raise ExtractionError("encrypted", "The PDF is password protected.")
            except ExtractionError:
                raise
            except Exception as exc:  # pypdf raises several types for bad encryption
                raise ExtractionError("encrypted", "The PDF is password protected.") from exc
        page_count = len(reader.pages)
    except ExtractionError:
        raise
    except (PdfReadError, PdfStreamError, ValueError, TypeError, KeyError, OSError) as exc:
        raise ExtractionError("corrupted", "The PDF could not be read.") from exc

    if page_count == 0:
        raise ExtractionError("corrupted", "The PDF has no pages.")
    if page_count > limits.max_pages:
        raise ExtractionError(
            "too_many_pages",
            f"The PDF has {page_count} pages; at most {limits.max_pages} are processed.",
        )

    warnings: list[str] = []
    parts: list[str] = []
    spans: list[PageSpan] = []
    pages_without_text = 0
    unreadable = 0
    offset = 0
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = normalize(page.extract_text() or "")
        except Exception:  # one broken page must not lose the rest
            unreadable += 1
            text = ""
        if not text:
            pages_without_text += 1
        if parts:
            offset += 2  # the "\n\n" between pages
        parts.append(text)
        spans.append(PageSpan(number, offset, offset + len(text)))
        offset += len(text)
        if offset > limits.max_text_chars:
            break  # the limit is applied afterwards; stop reading pages beyond it

    if pages_without_text == page_count:
        raise ExtractionError(
            "needs_ocr",
            "The PDF has no text layer; it is probably scanned. OCR is not available yet.",
        )
    if unreadable:
        warnings.append(f"pages_unreadable:{unreadable}")
    if pages_without_text:
        warnings.append(f"pages_without_text:{pages_without_text}")

    return Extraction(
        text="\n\n".join(parts),
        pages=spans,
        metadata=_metadata(reader),
        warnings=warnings,
        page_count=page_count,
    )


def _safe_get(info: Any, key: str) -> Any:
    """A value from a possibly malformed information dictionary, or None."""
    try:
        return info.get(key)
    except Exception:
        return None


def _metadata(reader: PdfReader) -> dict[str, Any]:
    """The document information dictionary, as plain strings; absent keys are null."""
    found: dict[str, Any] = dict.fromkeys(_METADATA_KEYS.values())
    try:
        info = reader.metadata
    except Exception:
        return found
    if info is None:
        return found
    for raw_key, key in _METADATA_KEYS.items():
        value = _safe_get(info, raw_key)
        if value is None:
            continue
        text = " ".join(str(value).split())[:500]
        found[key] = text or None
    return found
