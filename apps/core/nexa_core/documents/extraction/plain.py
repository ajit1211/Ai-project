"""Plain text and Markdown: decoded, normalized, kept as written.

Markdown is not rendered: headings, lists and fenced code blocks are meaningful structure for
later chunking and are preserved as text. Rendering, if ever, happens in the browser with the
same sanitized renderer the chat uses.
"""

import re
from pathlib import Path

from nexa_core.documents.extraction.text import decode_bytes, normalize
from nexa_core.documents.extraction.types import Extraction, Limits, PageSpan

_HEADING = re.compile(r"^#{1,6} \S", re.MULTILINE)
_FENCE = re.compile(r"^```", re.MULTILINE)


def extract_plain(path: Path, limits: Limits, *, markdown: bool) -> Extraction:
    # Four bytes per character is the UTF-8 worst case: reading more could never fit the cap.
    with path.open("rb") as handle:
        data = handle.read(limits.max_text_chars * 4 + 1)
        truncated_bytes = bool(handle.read(1))
    decoded = decode_bytes(data)
    warnings: list[str] = []
    if decoded.encoding != "utf-8":
        warnings.append(f"encoding_fallback:{decoded.encoding}")
    if decoded.replaced:
        warnings.append("encoding_replaced")
    text = normalize(decoded.text)
    if truncated_bytes:
        warnings.append("text_truncated")
    if not text:
        warnings.append("no_text")
    metadata: dict[str, object] = {
        "encoding": decoded.encoding,
        "line_count": text.count("\n") + 1 if text else 0,
    }
    if markdown:
        metadata["heading_count"] = len(_HEADING.findall(text))
        metadata["code_block_count"] = len(_FENCE.findall(text)) // 2
    return Extraction(
        text=text,
        pages=[PageSpan(1, 0, len(text))] if text else [],
        metadata=metadata,
        warnings=warnings,
        page_count=None,
    )
