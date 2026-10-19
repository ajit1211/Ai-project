"""What extraction produces, and how it fails (docs/architecture/documents.md#extraction).

A handler turns one file into an `Extraction`: normalized text, the span of each page in
that text, deterministic metadata, and warnings. Anything it cannot do becomes an
`ExtractionError` with a code the API shows and a message safe for the user; the exception
text never carries file content.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

Format = Literal["pdf", "docx", "txt", "md", "csv"]

FORMATS: tuple[Format, ...] = ("pdf", "docx", "txt", "md", "csv")

# One parser version for the whole pipeline: a change to any handler bumps it, so a document
# processed by an older version can be told apart and re-processed deliberately.
PARSER_VERSION = "2026.10.1"


@dataclass(frozen=True, slots=True)
class Limits:
    """Caps that keep one document from exhausting a worker. All configurable."""

    max_text_chars: int = 5_000_000
    max_pages: int = 500
    max_csv_rows: int = 50_000
    max_cell_chars: int = 2_000
    max_table_rows: int = 2_000
    # Office files are zip archives: a small file may inflate to a huge one. XML inflates
    # ten to twenty times legitimately, so the ratio rule only applies once the inflated
    # size is worth worrying about; the absolute cap always applies.
    max_uncompressed_bytes: int = 200 * 1024 * 1024
    max_compression_ratio: int = 50
    ratio_check_bytes: int = 8 * 1024 * 1024
    max_archive_entries: int = 10_000


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True, slots=True)
class PageSpan:
    """Where one page sits in the extracted text, so later phases can cite it."""

    page: int
    char_start: int
    char_end: int


@dataclass(slots=True)
class Extraction:
    text: str
    pages: list[PageSpan] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    page_count: int | None = None

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


# Error codes a user may see. `needs_ocr` and `unsupported_content` are not failures of the
# pipeline but facts about the file; the lifecycle maps them to their own statuses.
ErrorCode = Literal[
    "unsupported_format",
    "type_mismatch",
    "empty_file",
    "corrupted",
    "encrypted",
    "too_many_pages",
    "archive_too_large",
    "needs_ocr",
    "unsupported_content",
    "extraction_failed",
]


class ExtractionError(Exception):
    """The file could not be turned into text. `message` is written for the user."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
