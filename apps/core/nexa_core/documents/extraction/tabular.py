"""CSV: columns, rows and a text form an AI can read, through the standard library.

Nothing is evaluated. Cells that a spreadsheet would treat as formulas are made inert in the
text form. Rows are streamed, never loaded all at once, and capped.
"""

import csv
import io
from pathlib import Path
from typing import Any

from nexa_core.documents.extraction.text import (
    cell_text,
    decode_bytes,
    markdown_table,
    normalize,
)
from nexa_core.documents.extraction.types import Extraction, ExtractionError, Limits, PageSpan

_SAMPLE_BYTES = 64 * 1024
_DELIMITERS = ",;\t|"


def extract_csv(path: Path, limits: Limits) -> Extraction:
    with path.open("rb") as handle:
        sample = handle.read(_SAMPLE_BYTES)
    if not sample.strip():
        raise ExtractionError("empty_file", "The CSV file is empty.")
    decoded = decode_bytes(sample)
    warnings: list[str] = []
    if decoded.encoding != "utf-8":
        warnings.append(f"encoding_fallback:{decoded.encoding}")
    if decoded.replaced:
        warnings.append("encoding_replaced")
    delimiter = _delimiter(decoded.text)

    csv.field_size_limit(max(limits.max_cell_chars * 8, 131_072))
    header: list[str] = []
    rows: list[list[str]] = []
    row_count = 0
    ragged = 0
    try:
        with path.open("r", encoding=decoded.encoding, errors="replace", newline="") as stream:
            reader = csv.reader(stream, delimiter=delimiter)
            for raw in reader:
                if not header:
                    if not any(cell.strip() for cell in raw):
                        continue  # leading blank lines
                    header = [cell_text(cell, limits.max_cell_chars, warnings) for cell in raw]
                    continue
                if not any(cell.strip() for cell in raw):
                    continue
                row_count += 1
                if row_count > limits.max_csv_rows:
                    warnings.append("rows_truncated")
                    row_count = limits.max_csv_rows
                    break
                if len(raw) != len(header):
                    ragged += 1
                    raw = (raw + [""] * len(header))[: len(header)]
                rows.append([cell_text(cell, limits.max_cell_chars, warnings) for cell in raw])
    except csv.Error as exc:
        raise ExtractionError("corrupted", "The CSV file is malformed.") from exc
    except (UnicodeDecodeError, LookupError) as exc:
        raise ExtractionError("corrupted", "The CSV file could not be decoded.") from exc

    if not header:
        raise ExtractionError("empty_file", "The CSV file has no rows.")
    if ragged:
        warnings.append(f"ragged_rows:{ragged}")
    if row_count == 0:
        warnings.append("no_rows")

    text = normalize(markdown_table(header, rows))
    metadata: dict[str, Any] = {
        "columns": header,
        "column_count": len(header),
        "row_count": row_count,
        "delimiter": delimiter,
        "encoding": decoded.encoding,
    }
    return Extraction(
        text=text,
        pages=[PageSpan(1, 0, len(text))] if text else [],
        metadata=metadata,
        warnings=warnings,
        page_count=None,
    )


def _delimiter(sample: str) -> str:
    """The delimiter the sniffer finds, or the one that splits the first line most."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        first = io.StringIO(sample).readline()
        counts = {d: first.count(d) for d in _DELIMITERS}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] else ","
