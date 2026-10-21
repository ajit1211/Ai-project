"""Splitting extracted text into passages for search and citation (docs/architecture/rag.md).

The input is what Phase 07 stored: the normalized text of a document, the page spans of a PDF
and the document's format. The output is an ordered list of chunks, each a passage of that
text with everything a citation needs to point back at it: character offsets into the text,
the page range, the section path (from headings) and, for tables, the row range.

Rules, most important first:

1. A chunk never crosses a section (a heading starts a new one) or, in a PDF, a page.
2. Blocks (paragraphs, lists, code blocks, tables) are kept whole when they fit, and a chunk
   ends at a block boundary once it is reasonably full.
3. A block larger than the target is split between sentences (or lines), a sentence larger
   than the maximum between words, and a "word" larger than the maximum anywhere.
4. Where a block had to be split, the next chunk repeats up to `overlap_chars` of whole
   sentences from the end of the previous one, so a passage never starts mid-thought.
5. Table chunks repeat the table's header row, so every chunk of a CSV still says what its
   columns are; they record which data rows they hold.
6. Empty chunks and exact repeats (boilerplate on every page) are dropped.

Prose chunks are exact slices of the text: `text[char_start:char_end] == content`. Table chunks
are the header row plus the slice of their rows (`char_start:char_end` covers the rows).

Headings are only recognised where the extractor wrote them (`#` lines in Markdown and Word
documents). Plain text and PDF text carry no reliable structure, so their chunks have no
section rather than an invented one. Sizes are characters; the token figure is an estimate
(three characters per token, the ratio nexa_ai.tokens documents), since no tokenizer ships with
NEXA and counting characters is exact.
"""

import bisect
import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

STRATEGY_VERSION = "structured-chars/v1"
CHARS_PER_TOKEN = 3
MAX_SECTION_CHARS = 300

ChunkKind = Literal["text", "table"]

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_TABLE_SEPARATOR = re.compile(r"^\|(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*$")
# A sentence ends at . ! or ? (optionally followed by closing quotes or brackets) and space.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*[ \t]+")
_STRUCTURED = frozenset({"md", "docx"})
_TABULAR = frozenset({"md", "docx", "csv"})


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    target_chars: int = 1_200
    max_chars: int = 2_000
    overlap_chars: int = 200
    max_chunks: int = 20_000

    def __post_init__(self) -> None:
        if not 100 <= self.target_chars <= self.max_chars:
            raise ValueError("chunk sizes need 100 <= target_chars <= max_chars")
        if not 0 <= self.overlap_chars <= self.target_chars // 2:
            raise ValueError("overlap_chars must be between 0 and half of target_chars")
        if self.max_chunks < 1:
            raise ValueError("max_chunks must be positive")


@dataclass(frozen=True, slots=True)
class PageSpan:
    page: int
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    content: str
    char_start: int
    char_end: int
    section: str
    page_start: int | None
    page_end: int | None
    kind: ChunkKind = "text"
    row_start: int | None = None
    row_end: int | None = None

    @property
    def char_count(self) -> int:
        return len(self.content)

    @property
    def token_estimate(self) -> int:
        return (len(self.content) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Chunking:
    chunks: list[Chunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def token_estimate(self) -> int:
        return sum(chunk.token_estimate for chunk in self.chunks)


# --- Blocks ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Line:
    start: int
    end: int  # exclusive, without the newline


@dataclass(slots=True)
class _Block:
    kind: Literal["text", "table", "heading"]
    lines: list[_Line]
    level: int = 0
    title: str = ""

    @property
    def start(self) -> int:
        return self.lines[0].start

    @property
    def end(self) -> int:
        return self.lines[-1].end


def _lines(text: str) -> Iterator[_Line]:
    start = 0
    while start <= len(text):
        end = text.find("\n", start)
        if end == -1:
            end = len(text)
        yield _Line(start, end)
        start = end + 1


def _blocks(text: str, fmt: str) -> list[_Block]:
    """Paragraphs, headings, tables and code blocks, in reading order."""
    structured = fmt in _STRUCTURED
    tabular = fmt in _TABULAR
    blocks: list[_Block] = []
    current: _Block | None = None
    fence: str | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current.lines:
            blocks.append(current)
        current = None

    for line in _lines(text):
        raw = text[line.start : line.end]
        if fence is not None and current is not None:  # inside a fenced code block
            current.lines.append(line)
            if raw.strip().startswith(fence):
                fence = None
                flush()
            continue
        if not raw.strip():
            flush()
            continue
        if fmt == "md" and (match := _FENCE.match(raw)):
            flush()
            fence = match.group(1)
            current = _Block("text", [line])
            continue
        if structured and (match := _HEADING.match(raw)):
            flush()
            title = " ".join(match.group(2).split())
            blocks.append(_Block("heading", [line], level=len(match.group(1)), title=title))
            continue
        if tabular and raw.lstrip().startswith("|"):
            if current is None or current.kind != "table":
                flush()
                current = _Block("table", [])
            current.lines.append(line)
            continue
        if current is None or current.kind != "text":
            flush()
            current = _Block("text", [])
        current.lines.append(line)
    flush()
    return blocks


# --- Pages -------------------------------------------------------------------------------------


class _Pages:
    def __init__(self, spans: Sequence[PageSpan]) -> None:
        self._spans = sorted(spans, key=lambda s: s.char_start)
        self._starts = [s.char_start for s in self._spans]

    def __bool__(self) -> bool:
        return bool(self._spans)

    def at(self, offset: int) -> int | None:
        """The page holding character `offset`; None when no page does (the gap between)."""
        index = bisect.bisect_right(self._starts, offset) - 1
        if index < 0:
            return None
        span = self._spans[index]
        return (
            span.page
            if span.char_start <= offset < max(span.char_end, span.char_start + 1)
            else None
        )

    def nearest(self, offset: int) -> int | None:
        index = max(0, bisect.bisect_right(self._starts, offset) - 1)
        return self._spans[index].page if self._spans else None


# --- Splitting long blocks ---------------------------------------------------------------------


def _units(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """Sentence- or line-sized pieces of text[start:end], none longer than max_chars."""
    pieces: list[tuple[int, int]] = []
    for line_start, line_end in _split_lines(text, start, end):
        cursor = line_start
        for match in _SENTENCE_END.finditer(text, line_start, line_end):
            if match.start() > cursor:
                pieces.append((cursor, match.start()))
            cursor = match.end()
        if cursor < line_end:
            pieces.append((cursor, line_end))
    out: list[tuple[int, int]] = []
    for piece_start, piece_end in pieces:
        out.extend(_hard_split(text, piece_start, piece_end, max_chars))
    return out


def _split_lines(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    cursor = start
    while cursor < end:
        newline = text.find("\n", cursor, end)
        stop = end if newline == -1 else newline
        if text[cursor:stop].strip():
            yield cursor, stop
        cursor = stop + 1


def _hard_split(text: str, start: int, end: int, max_chars: int) -> Iterator[tuple[int, int]]:
    """Cut an over-long piece at the last space before the limit, else at the limit."""
    while end - start > max_chars:
        cut = text.rfind(" ", start + max_chars // 2, start + max_chars)
        stop = cut if cut > start else start + max_chars
        yield start, stop
        start = stop
        while start < end and text[start] == " ":
            start += 1
    if end > start:
        yield start, end


# --- Packing -----------------------------------------------------------------------------------


class _Builder:
    def __init__(self, text: str, config: ChunkingConfig, pages: _Pages, result: Chunking) -> None:
        self.text = text
        self.config = config
        self.pages = pages
        self.result = result
        self.seen: set[str] = set()
        self.full = False

    def page_range(self, start: int, end: int) -> tuple[int | None, int | None]:
        if not self.pages:
            return None, None
        first = self.pages.at(start) or self.pages.nearest(start)
        last = self.pages.at(max(start, end - 1)) or first
        return first, last

    def emit(
        self,
        content: str,
        start: int,
        end: int,
        section: str,
        *,
        kind: ChunkKind = "text",
        rows: tuple[int, int] | None = None,
    ) -> None:
        if self.full or not content.strip():
            return
        digest = hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()
        if digest in self.seen:
            return
        if len(self.result.chunks) >= self.config.max_chunks:
            self.full = True
            self.result.warnings.append("chunks_truncated")
            return
        self.seen.add(digest)
        page_start, page_end = self.page_range(start, end)
        self.result.chunks.append(
            Chunk(
                index=len(self.result.chunks),
                content=content,
                char_start=start,
                char_end=end,
                section=section,
                page_start=page_start,
                page_end=page_end,
                kind=kind,
                row_start=rows[0] if rows else None,
                row_end=rows[1] if rows else None,
            )
        )

    # Prose: units are sentences (or lines) with their offsets; a chunk is the slice from its
    # first unit's start to its last unit's end.
    def prose(self, blocks: list[_Block], section: str) -> None:
        target, overlap = self.config.target_chars, self.config.overlap_chars
        units: list[tuple[int, int, bool]] = []  # (start, end, first unit of its block)
        for block in blocks:
            if block.end - block.start <= target:
                units.append((block.start, block.end, True))
            else:
                pieces = _units(self.text, block.start, block.end, self.config.max_chars)
                units.extend((s, e, i == 0) for i, (s, e) in enumerate(pieces))
        chunk: list[tuple[int, int, bool]] = []
        for unit in units:
            start, end, first = unit
            if chunk:
                size = end - chunk[0][0]
                current = chunk[-1][1] - chunk[0][0]
                at_boundary = first and current >= target * 0.6
                if size > target or at_boundary:
                    self.emit_slice(chunk, section)
                    # Repeat the end of the previous chunk only when a block was split.
                    chunk = [] if first else self.tail(chunk, overlap, limit=start)
            chunk.append(unit)
        if chunk:
            self.emit_slice(chunk, section)

    def emit_slice(self, units: list[tuple[int, int, bool]], section: str) -> None:
        start, end = units[0][0], units[-1][1]
        self.emit(self.text[start:end], start, end, section)

    @staticmethod
    def tail(
        units: list[tuple[int, int, bool]], overlap: int, *, limit: int
    ) -> list[tuple[int, int, bool]]:
        kept: list[tuple[int, int, bool]] = []
        for unit in reversed(units):
            if limit - unit[0] > overlap:
                break
            kept.insert(0, (unit[0], unit[1], False))
        return kept if len(kept) < len(units) else []

    def table(self, block: _Block, section: str) -> None:
        lines = block.lines
        header_lines: list[_Line] = []
        if len(lines) >= 2 and _TABLE_SEPARATOR.match(self.text[lines[1].start : lines[1].end]):
            header_lines = lines[:2]
        rows = lines[len(header_lines) :]
        header = "\n".join(self.text[ln.start : ln.end] for ln in header_lines)
        # A header that would crowd out its rows is shown once, not repeated.
        repeat = header if len(header) <= self.config.max_chars // 2 else ""
        budget = self.config.target_chars - len(repeat)
        if not rows:
            self.emit(header, block.start, block.end, section, kind="table")
            return
        group: list[tuple[int, _Line]] = []

        def flush() -> None:
            if not group:
                return
            first, last = group[0], group[-1]
            body = self.text[first[1].start : last[1].end]
            content = f"{repeat}\n{body}" if repeat else body
            self.emit(
                content,
                first[1].start,
                last[1].end,
                section,
                kind="table",
                rows=(first[0], last[0]),
            )
            group.clear()

        for number, line in enumerate(rows, start=1):
            length = line.end - line.start
            if length > self.config.max_chars - len(repeat):
                flush()
                limit = max(100, self.config.max_chars - len(repeat))
                for piece_start, piece_end in _hard_split(self.text, line.start, line.end, limit):
                    piece = self.text[piece_start:piece_end]
                    content = f"{repeat}\n{piece}" if repeat else piece
                    self.emit(
                        content,
                        piece_start,
                        piece_end,
                        section,
                        kind="table",
                        rows=(number, number),
                    )
                continue
            if group and line.end - group[0][1].start > budget:
                flush()
            group.append((number, line))
        flush()


def chunk_text(
    text: str,
    *,
    fmt: str,
    pages: Sequence[PageSpan] = (),
    config: ChunkingConfig | None = None,
) -> Chunking:
    """Chunk a document's extracted text. Deterministic: the same input gives the same chunks."""
    config = config or ChunkingConfig()
    result = Chunking()
    if not text.strip():
        result.warnings.append("no_text")
        return result
    page_index = _Pages(pages if fmt == "pdf" else ())
    builder = _Builder(text, config, page_index, result)

    headings: list[tuple[int, str]] = []
    section = ""
    run: list[_Block] = []
    run_page: int | None = None

    def flush_run() -> None:
        if run:
            builder.prose(list(run), section)
            run.clear()

    for block in _blocks(text, fmt):
        if builder.full:
            break
        if block.kind == "heading":
            flush_run()
            while headings and headings[-1][0] >= block.level:
                headings.pop()
            headings.append((block.level, block.title))
            section = " > ".join(title for _, title in headings)[:MAX_SECTION_CHARS]
            continue
        page = page_index.at(block.start) if page_index else None
        if run and page != run_page:
            flush_run()  # a chunk never crosses a page
        run_page = page
        if block.kind == "table":
            flush_run()
            builder.table(block, section)
            continue
        run.append(block)
    flush_run()
    if not result.chunks and "no_text" not in result.warnings:
        result.warnings.append("no_text")
    return result
