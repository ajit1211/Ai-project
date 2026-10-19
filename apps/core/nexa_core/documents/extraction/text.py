"""Text shared by every handler: decoding bytes, normalizing text, applying the size cap."""

import re
import unicodedata
from dataclasses import dataclass

from nexa_core.documents.extraction.types import Extraction

_UTF8_BOM = bytes([0xEF, 0xBB, 0xBF])
_UTF16_BOMS = (bytes([0xFF, 0xFE]), bytes([0xFE, 0xFF]))
_NON_ASCII = re.compile(r"[^\x00-\x7f]")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class Decoded:
    text: str
    encoding: str
    replaced: bool  # some bytes could not be decoded and became U+FFFD


def decode_bytes(data: bytes) -> Decoded:
    """UTF-8 first (with or without a byte order mark), then UTF-16 with one, then UTF-8 with
    a few bad bytes replaced when the text is mostly valid UTF-8, and Windows-1252 last: it
    decodes any byte, so it can only ever be a guess, and it is reported as one."""
    if data.startswith(_UTF8_BOM):
        try:
            return Decoded(data.decode("utf-8-sig"), "utf-8-sig", False)
        except UnicodeDecodeError:
            pass
    try:
        return Decoded(data.decode("utf-8"), "utf-8", False)
    except UnicodeDecodeError:
        pass
    if data.startswith(_UTF16_BOMS):
        try:
            return Decoded(data.decode("utf-16"), "utf-16", False)
        except UnicodeDecodeError:
            pass
    text = data.decode("utf-8", errors="replace")
    replacements = text.count("\ufffd")
    valid_non_ascii = len(_NON_ASCII.findall(text)) - replacements
    if valid_non_ascii > replacements:
        return Decoded(text, "utf-8", True)
    try:
        return Decoded(data.decode("cp1252"), "cp1252", False)
    except UnicodeDecodeError:
        return Decoded(data.decode("cp1252", errors="replace"), "cp1252", True)


def normalize(text: str) -> str:
    """Unix line endings, no control characters, NFC, no trailing spaces, at most one blank
    line in a row. Headings, paragraphs and indentation survive."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n\n")
    text = _CONTROL.sub("", text)
    text = unicodedata.normalize("NFC", text)
    text = _TRAILING_SPACE.sub("", text)
    text = _MANY_BLANK_LINES.sub("\n\n", text)
    return text.strip("\n")


def apply_text_limit(extraction: Extraction, max_chars: int) -> Extraction:
    """Cut the text at the limit, keeping page spans consistent, and say so."""
    if len(extraction.text) <= max_chars:
        return extraction
    extraction.text = extraction.text[:max_chars]
    extraction.pages = [
        span if span.char_end <= max_chars else type(span)(span.page, span.char_start, max_chars)
        for span in extraction.pages
        if span.char_start < max_chars
    ]
    extraction.warnings.append("text_truncated")
    return extraction


def cell_text(value: str, max_chars: int, warnings: list[str]) -> str:
    """A table cell: one line, bounded, with spreadsheet formulas made inert. A cell that
    begins with = + - @ (or a tab or return) would be executed by a spreadsheet that opens
    the extracted text as CSV; a leading apostrophe is how spreadsheets mark it as text."""
    text = " ".join(value.split())
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
        if "cell_truncated" not in warnings:
            warnings.append("cell_truncated")
    if text[:1] in ("=", "+", "-", "@") and len(text) > 1 and not _NUMBER.match(text):
        text = "'" + text
        if "formula_cells_neutralized" not in warnings:
            warnings.append("formula_cells_neutralized")
    return text.replace("|", "\\|")


_NUMBER = re.compile(r"^[-+]?\d[\d,]*(\.\d+)?%?$")


def markdown_table(header: list[str], rows: list[list[str]]) -> str:
    """A pipe table; the first row is the header."""
    width = max([len(header), *(len(row) for row in rows)]) if rows or header else 0
    if width == 0:
        return ""

    def line(cells: list[str]) -> str:
        padded = [*cells, *([""] * (width - len(cells)))]
        return "| " + " | ".join(padded) + " |"

    lines = [line(header), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(line(row) for row in rows)
    return "\n".join(lines)
