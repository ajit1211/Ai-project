"""The chunker on its own: exact offsets, sections, pages, sizes and overlap, tables, limits,
and the hand-off from the extraction engine. No database."""

from pathlib import Path

import pytest

from nexa_core.documents.chunking import (
    STRATEGY_VERSION,
    Chunk,
    ChunkingConfig,
    PageSpan,
    chunk_text,
)
from nexa_core.documents.extraction import extract
from nexa_core.documents.extraction.text import markdown_table
from nexa_core.testing import make_docx, make_pdf

SMALL = ChunkingConfig(target_chars=200, max_chars=300, overlap_chars=60)


def sentences(count: int, prefix: str = "Sentence") -> str:
    return " ".join(f"{prefix} number {i} talks about refunds and invoices." for i in range(count))


def assert_exact(text: str, chunks: list[Chunk]) -> None:
    for chunk in chunks:
        if chunk.kind == "text":
            assert text[chunk.char_start : chunk.char_end] == chunk.content


class TestProse:
    def test_short_text_is_one_chunk(self) -> None:
        result = chunk_text("One paragraph.\n\nAnother one.", fmt="txt")
        assert [c.content for c in result.chunks] == ["One paragraph.\n\nAnother one."]
        chunk = result.chunks[0]
        assert (chunk.index, chunk.char_start, chunk.section, chunk.page_start) == (0, 0, "", None)
        assert chunk.token_estimate == (len(chunk.content) + 2) // 3
        assert result.warnings == []

    def test_paragraphs_are_kept_whole_and_sizes_respected(self) -> None:
        paragraphs = [sentences(2, f"P{i}") for i in range(8)]
        text = "\n\n".join(paragraphs)
        result = chunk_text(text, fmt="txt", config=SMALL)
        assert len(result.chunks) > 1
        assert_exact(text, result.chunks)
        for chunk in result.chunks:
            assert len(chunk.content) <= SMALL.max_chars
            # Every chunk starts where a paragraph starts: no paragraph was cut.
            assert any(text.startswith(p, chunk.char_start) for p in paragraphs)
        # No overlap between chunks that end at paragraph boundaries.
        for before, after in zip(result.chunks, result.chunks[1:], strict=False):
            assert after.char_start >= before.char_end

    def test_a_long_paragraph_is_split_between_sentences_with_overlap(self) -> None:
        text = sentences(12)
        result = chunk_text(text, fmt="txt", config=SMALL)
        assert len(result.chunks) >= 3
        assert_exact(text, result.chunks)
        for chunk in result.chunks:
            assert chunk.content.startswith("Sentence number")
            assert chunk.content.endswith(".")
            assert len(chunk.content) <= SMALL.max_chars
        for before, after in zip(result.chunks, result.chunks[1:], strict=False):
            assert after.char_start < before.char_end  # the overlap
            assert before.char_end - after.char_start <= SMALL.overlap_chars
        covered: set[int] = set()
        for chunk in result.chunks:
            covered.update(range(chunk.char_start, chunk.char_end))
        assert covered >= {i for i, ch in enumerate(text) if not ch.isspace()}

    def test_a_sentence_without_spaces_is_cut_hard(self) -> None:
        text = "".join(chr(97 + i % 26) for i in range(1_000))
        result = chunk_text(text, fmt="txt", config=SMALL)
        assert [len(c.content) for c in result.chunks] == [300, 300, 300, 100]
        assert_exact(text, result.chunks)

    def test_overlap_can_be_switched_off(self) -> None:
        config = ChunkingConfig(target_chars=200, max_chars=300, overlap_chars=0)
        result = chunk_text(sentences(12), fmt="txt", config=config)
        for before, after in zip(result.chunks, result.chunks[1:], strict=False):
            assert after.char_start >= before.char_end

    def test_empty_and_blank_text(self) -> None:
        for text in ("", "   \n\n  \n"):
            result = chunk_text(text, fmt="txt")
            assert result.chunks == []
            assert result.warnings == ["no_text"]

    def test_exact_repeats_are_dropped(self) -> None:
        # The same footer alone on two pages is one passage, not two.
        pages = [
            "Intro page.",
            "Confidential - do not share.",
            "Body page.",
            "Confidential - do not share.",
        ]
        text = "\n\n".join(pages)
        spans, offset = [], 0
        for number, page in enumerate(pages, start=1):
            spans.append(PageSpan(number, offset, offset + len(page)))
            offset += len(page) + 2
        result = chunk_text(text, fmt="pdf", pages=spans)
        assert [(c.index, c.page_start, c.content) for c in result.chunks] == [
            (0, 1, "Intro page."),
            (1, 2, "Confidential - do not share."),
            (2, 3, "Body page."),
        ]

    def test_the_chunk_limit_is_reported(self) -> None:
        text = "\n\n".join(sentences(1, f"P{i}") for i in range(20))
        config = ChunkingConfig(target_chars=100, max_chars=200, overlap_chars=0, max_chunks=3)
        result = chunk_text(text, fmt="txt", config=config)
        assert len(result.chunks) == 3
        assert result.warnings == ["chunks_truncated"]

    def test_deterministic(self) -> None:
        text = "\n\n".join(sentences(5, f"P{i}") for i in range(6))
        first = chunk_text(text, fmt="md", config=SMALL)
        second = chunk_text(text, fmt="md", config=SMALL)
        assert first.chunks == second.chunks
        assert STRATEGY_VERSION

    def test_invalid_configuration(self) -> None:
        with pytest.raises(ValueError, match="target_chars"):
            ChunkingConfig(target_chars=50, max_chars=60)
        with pytest.raises(ValueError, match="overlap"):
            ChunkingConfig(target_chars=200, max_chars=300, overlap_chars=150)


class TestSections:
    def test_headings_become_section_paths_and_split_chunks(self) -> None:
        text = (
            "# Handbook\n\nIntro text.\n\n## Refunds\n\nRefunds take five days.\n\n"
            "### Enterprise\n\nEnterprise refunds need approval.\n\n## Travel\n\nBook early."
        )
        result = chunk_text(text, fmt="md")
        assert [(c.section, c.content) for c in result.chunks] == [
            ("Handbook", "Intro text."),
            ("Handbook > Refunds", "Refunds take five days."),
            ("Handbook > Refunds > Enterprise", "Enterprise refunds need approval."),
            ("Handbook > Travel", "Book early."),
        ]
        assert_exact(text, result.chunks)

    def test_plain_text_and_pdf_never_invent_sections(self) -> None:
        text = "# not a heading in a text file\n\nBody."
        assert {c.section for c in chunk_text(text, fmt="txt").chunks} == {""}
        assert {c.section for c in chunk_text(text, fmt="pdf").chunks} == {""}

    def test_hashes_inside_code_blocks_are_not_headings(self) -> None:
        text = "# Setup\n\n```bash\n# install\npip install nexa\n```\n\nDone."
        result = chunk_text(text, fmt="md")
        assert {c.section for c in result.chunks} == {"Setup"}
        assert "# install" in result.chunks[0].content


class TestPages:
    def test_pdf_chunks_keep_their_page_and_never_cross_one(self) -> None:
        pages = ["Page one talks about refunds.", "Page two talks about travel.", "Page three."]
        text = "\n\n".join(pages)
        spans, offset = [], 0
        for number, page in enumerate(pages, start=1):
            spans.append(PageSpan(number, offset, offset + len(page)))
            offset += len(page) + 2
        result = chunk_text(text, fmt="pdf", pages=spans)
        assert [(c.page_start, c.page_end, c.content) for c in result.chunks] == [
            (1, 1, pages[0]),
            (2, 2, pages[1]),
            (3, 3, pages[2]),
        ]

    def test_a_long_page_is_split_but_keeps_its_number(self) -> None:
        page = sentences(12)
        result = chunk_text(page, fmt="pdf", pages=[PageSpan(7, 0, len(page))], config=SMALL)
        assert len(result.chunks) > 1
        assert {(c.page_start, c.page_end) for c in result.chunks} == {(7, 7)}

    def test_pages_are_ignored_for_other_formats(self) -> None:
        result = chunk_text("Text.", fmt="txt", pages=[PageSpan(1, 0, 5)])
        assert result.chunks[0].page_start is None


class TestTables:
    def table(self, rows: int) -> str:
        return markdown_table(
            ["Region", "Price", "Notes"],
            [[f"R{i}", f"{i * 10}", f"Row {i} pricing note"] for i in range(1, rows + 1)],
        )

    def test_csv_rows_are_grouped_with_the_header_repeated(self) -> None:
        text = self.table(30)
        result = chunk_text(text, fmt="csv", config=SMALL)
        assert len(result.chunks) > 1
        header = "| Region | Price | Notes |\n| --- | --- | --- |"
        expected_row = 1
        for chunk in result.chunks:
            assert chunk.kind == "table"
            assert chunk.content.startswith(header + "\n")
            assert chunk.row_start == expected_row
            assert chunk.row_end is not None
            expected_row = chunk.row_end + 1
            body = chunk.content[len(header) + 1 :]
            assert text[chunk.char_start : chunk.char_end] == body
            assert len(chunk.content) <= SMALL.max_chars
        assert expected_row == 31  # every row exactly once

    def test_a_huge_row_is_split(self) -> None:
        text = markdown_table(["A", "B"], [["x " * 400, "y"]])
        result = chunk_text(text, fmt="csv", config=SMALL)
        assert len(result.chunks) > 1
        assert {(c.row_start, c.row_end) for c in result.chunks} == {(1, 1)}
        assert all(len(c.content) <= SMALL.max_chars + 1 for c in result.chunks)

    def test_tables_inside_documents_end_the_prose_chunk(self) -> None:
        text = "# Prices\n\nOur prices follow.\n\n" + self.table(2) + "\n\nThat is all."
        result = chunk_text(text, fmt="md")
        assert [c.kind for c in result.chunks] == ["text", "table", "text"]
        assert {c.section for c in result.chunks} == {"Prices"}


class TestFromExtraction:
    def test_docx_headings_lists_and_tables(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.docx"
        path.write_bytes(make_docx())
        extraction = extract(path, "docx")
        result = chunk_text(extraction.text, fmt="docx")
        sections = [c.section for c in result.chunks]
        assert "Platform notes" in sections
        assert "Platform notes > Limits" in sections
        table = next(c for c in result.chunks if c.kind == "table")
        assert table.content.startswith("| Format | Status |")
        assert table.section == "Platform notes > Limits"

    def test_pdf_page_spans_flow_through(self, tmp_path: Path) -> None:
        path = tmp_path / "two.pdf"
        path.write_bytes(make_pdf(["Alpha page text.", None, "Gamma page text."]))
        extraction = extract(path, "pdf")
        spans = [PageSpan(s.page, s.char_start, s.char_end) for s in extraction.pages]
        result = chunk_text(extraction.text, fmt="pdf", pages=spans)
        assert [(c.page_start, c.content) for c in result.chunks] == [
            (1, "Alpha page text."),
            (3, "Gamma page text."),
        ]
