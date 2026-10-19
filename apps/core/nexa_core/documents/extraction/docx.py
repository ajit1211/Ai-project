"""DOCX: paragraphs and tables in reading order, through python-docx.

The archive is inspected before it is opened: a Word file is a zip, and a small zip can
inflate to gigabytes. Headings become Markdown headings, list paragraphs become list items,
tables become pipe tables with their cells made inert, so the structure survives for later
chunking. Nothing embedded (macros, images, objects) is ever executed or extracted.
"""

import zipfile
from pathlib import Path
from typing import Any

from docx import Document as open_document
from docx.table import Table
from docx.text.paragraph import Paragraph

from nexa_core.documents.extraction.text import cell_text, markdown_table, normalize
from nexa_core.documents.extraction.types import Extraction, ExtractionError, Limits, PageSpan

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def check_archive(path: Path, limits: Limits) -> None:
    """Refuse archives that would not fit in memory or that are not Word documents."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_archive_entries:
                raise ExtractionError("archive_too_large", "The file has too many parts.")
            uncompressed = sum(info.file_size for info in entries)
            compressed = sum(info.compress_size for info in entries)
            suspicious = (
                uncompressed > limits.ratio_check_bytes
                and uncompressed > limits.max_compression_ratio * max(compressed, 1)
            )
            if uncompressed > limits.max_uncompressed_bytes or suspicious:
                raise ExtractionError("archive_too_large", "The file inflates to too much data.")
            names = {info.filename for info in entries}
            if "word/document.xml" not in names:
                raise ExtractionError("corrupted", "The file is not a Word document.")
            if any(name.startswith("word/vbaProject") for name in names):
                # Macros are never run; they are also a sign this is not a plain document.
                raise ExtractionError(
                    "unsupported_content", "Macro-enabled documents are not supported."
                )
    except zipfile.BadZipFile as exc:
        raise ExtractionError("corrupted", "The file is not a valid Word document.") from exc


def extract_docx(path: Path, limits: Limits) -> Extraction:
    check_archive(path, limits)
    try:
        document = open_document(str(path))
    except ExtractionError:
        raise
    except Exception as exc:  # python-docx raises several types for broken packages
        raise ExtractionError("corrupted", "The Word document could not be read.") from exc

    warnings: list[str] = []
    blocks: list[str] = []
    paragraphs = 0
    tables = 0
    try:
        for element in document.element.body.iterchildren():
            if element.tag == f"{_W}p":
                paragraph = Paragraph(element, document)
                text = _paragraph(paragraph)
                if text:
                    paragraphs += 1
                    blocks.append(text)
            elif element.tag == f"{_W}tbl":
                tables += 1
                rendered = _table(Table(element, document), limits, warnings)
                if rendered:
                    blocks.append(rendered)
            if sum(len(block) for block in blocks) > limits.max_text_chars:
                break
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("corrupted", "The Word document could not be read.") from exc

    text = normalize("\n\n".join(blocks))
    if not text:
        warnings.append("no_text")
    return Extraction(
        text=text,
        pages=[PageSpan(1, 0, len(text))] if text else [],
        metadata={**_properties(document), "paragraphs": paragraphs, "tables": tables},
        warnings=warnings,
        page_count=None,  # pages are decided by the renderer, not stored in the file
    )


def _paragraph(paragraph: Paragraph) -> str:
    text = " ".join(paragraph.text.split())
    if not text:
        return ""
    style = (paragraph.style.name if paragraph.style is not None else "") or ""
    if style.startswith("Heading"):
        level = "".join(ch for ch in style if ch.isdigit())
        depth = min(max(int(level or "1"), 1), 6)
        return "#" * depth + " " + text
    if style == "Title":
        return "# " + text
    if "List" in style:
        return "- " + text
    return text


def _table(table: Table, limits: Limits, warnings: list[str]) -> str:
    rows: list[list[str]] = []
    for index, row in enumerate(table.rows):
        if index >= limits.max_table_rows:
            if "table_rows_truncated" not in warnings:
                warnings.append("table_rows_truncated")
            break
        cells = [cell_text(cell.text, limits.max_cell_chars, warnings) for cell in row.cells]
        rows.append(cells)
    if not rows:
        return ""
    header, body = rows[0], rows[1:]
    return markdown_table(header, body)


def _safe_attr(props: Any, attr: str) -> Any:
    """A core property, or None when the file's metadata part is malformed."""
    try:
        return getattr(props, attr)
    except Exception:
        return None


def _properties(document: Any) -> dict[str, Any]:
    """Core properties as plain strings; absent values are null."""
    found: dict[str, Any] = {"title": None, "author": None, "created": None, "modified": None}
    try:
        props = document.core_properties
    except Exception:
        return found
    for key, attr in (
        ("title", "title"),
        ("author", "author"),
        ("created", "created"),
        ("modified", "modified"),
    ):
        value = _safe_attr(props, attr)
        if value is None or value == "":
            continue
        found[key] = value.isoformat() if hasattr(value, "isoformat") else str(value)[:500]
    return found
