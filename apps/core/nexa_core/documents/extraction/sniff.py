"""What a file really is. The client's filename and MIME type are claims; the bytes decide.

A format is accepted only when the extension is on the allowlist and the content looks like
that format: PDF and DOCX by their signatures, text formats by decoding cleanly without
control bytes. A `.pdf` that is really text, or a `.txt` that is really a zip, is refused.
"""

import zipfile
from pathlib import Path
from typing import IO

from nexa_core.documents.extraction.types import Format

EXTENSIONS: dict[str, Format] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "txt",
    ".md": "md",
    ".markdown": "md",
    ".csv": "csv",
}

MEDIA_TYPES: dict[Format, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
}

HEAD_BYTES = 8 * 1024
_TEXT_CONTROL = bytes(set(range(0x20)) - {0x09, 0x0A, 0x0D, 0x0C})


def format_for_name(filename: str) -> Format | None:
    """The format the extension claims, or None when the extension is not allowed."""
    suffix = Path(filename).suffix.lower()
    return EXTENSIONS.get(suffix)


def looks_like(fmt: Format, head: bytes, *, file: IO[bytes] | None = None) -> bool:
    """Whether the first bytes (and, for DOCX, the archive index) match `fmt`."""
    if fmt == "pdf":
        return head.lstrip(b"\r\n\t \x00").startswith(b"%PDF-")
    if fmt == "docx":
        if not head.startswith(b"PK\x03\x04"):
            return False
        if file is None:
            return True
        try:
            with zipfile.ZipFile(file) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError, ValueError):
            return False
        return "[Content_Types].xml" in names and "word/document.xml" in names
    return _looks_like_text(head)


def _looks_like_text(head: bytes) -> bool:
    if not head:
        return True  # an empty file is refused for being empty, not for its type
    if head.startswith((b"%PDF-", b"PK\x03\x04", b"\x7fELF", b"MZ")):
        return False
    sample = head
    for bom in (b"\xff\xfe", b"\xfe\xff"):
        if sample.startswith(bom):
            # UTF-16 text: every other byte may be zero; check the decoded text instead.
            try:
                sample.decode("utf-16")
            except UnicodeDecodeError:
                return len(sample) % 2 == 1  # a split code unit at the end is fine
            return True
    return not any(byte in _TEXT_CONTROL for byte in sample)
