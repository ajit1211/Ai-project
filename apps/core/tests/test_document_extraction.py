"""The extraction engine on its own: format sniffing, every handler, normalization, limits and
failure codes. Fixtures are built in memory (nexa_core.testing), small and synthetic; nothing
external is read."""

import io
from pathlib import Path

import pytest
from docx import Document as new_docx

from nexa_core.documents.extraction import (
    Extraction,
    ExtractionError,
    Limits,
    extract,
    format_for_name,
    looks_like,
)
from nexa_core.documents.extraction.text import decode_bytes, normalize
from nexa_core.testing import encrypt_pdf, make_docx, make_pdf, make_zip

# --- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def write(tmp_path: Path):  # type: ignore[no-untyped-def]
    def _write(name: str, data: bytes) -> Path:
        path = tmp_path / name
        path.write_bytes(data)
        return path

    return _write


# --- sniffing ----------------------------------------------------------------------------------


class TestSniff:
    def test_the_extension_allowlist(self) -> None:
        assert format_for_name("Report.PDF") == "pdf"
        assert format_for_name("notes.markdown") == "md"
        assert format_for_name("data.csv") == "csv"
        assert format_for_name("tool.exe") is None
        assert format_for_name("archive.zip") is None
        assert format_for_name("noext") is None

    def test_signatures_decide_not_names(self) -> None:
        assert looks_like("pdf", b"\n  %PDF-1.7 rest")
        assert not looks_like("pdf", b"hello, this is text")
        assert not looks_like("txt", b"%PDF-1.4")  # a PDF renamed to .txt
        assert not looks_like("txt", b"MZ\x90\x00 program")
        assert not looks_like("txt", b"hello\x00world")  # binary with NUL
        assert looks_like("txt", "héllo wörld".encode())
        assert looks_like("txt", "héllo".encode("utf-16"))
        assert looks_like("csv", b"a,b,c\r\n1,2,3\r\n")
        assert looks_like("txt", b"")

    def test_docx_needs_a_word_archive(self) -> None:
        real = make_docx()
        assert looks_like("docx", real[:8], file=io.BytesIO(real))
        plain_zip = make_zip({"a.txt": b"x"})
        assert not looks_like("docx", plain_zip[:8], file=io.BytesIO(plain_zip))
        assert not looks_like("docx", b"%PDF-1.4")


# --- text --------------------------------------------------------------------------------------


class TestText:
    def test_decoding_prefers_utf8_and_falls_back(self) -> None:
        utf8 = decode_bytes("héllo — 日本語".encode())
        assert (utf8.text, utf8.encoding, utf8.replaced) == ("héllo — 日本語", "utf-8", False)
        bom = decode_bytes("héllo".encode("utf-16"))
        assert (bom.text, bom.encoding) == ("héllo", "utf-16")
        sig = decode_bytes(b"\xef\xbb\xbfhello")
        assert (sig.text, sig.encoding) == ("hello", "utf-8-sig")
        latin = decode_bytes(b"caf\xe9 au lait")
        assert (latin.text, latin.encoding, latin.replaced) == ("café au lait", "cp1252", False)
        mostly_utf8 = decode_bytes(("héllo wörld ünïcode " * 10).encode() + b"\xff")
        assert mostly_utf8.encoding == "utf-8"
        assert mostly_utf8.replaced is True

    def test_normalization_keeps_structure(self) -> None:
        raw = "# Title\r\n\r\n\r\n\r\nline one  \r\n\tindented\x00\x07 text\rnext\n\n\n\nend\n\n"
        assert normalize(raw) == "# Title\n\nline one\n\tindented text\nnext\n\nend"
        assert normalize("é") == "é"  # NFC


# --- handlers ----------------------------------------------------------------------------------


class TestPdf:
    def test_text_pages_and_metadata(self, write) -> None:  # type: ignore[no-untyped-def]
        path = write(
            "report.pdf",
            make_pdf(["First page text", "Second (page) text"], title="Quarterly report"),
        )
        result = extract(path, "pdf")
        assert result.page_count == 2
        assert result.text == "First page text\n\nSecond (page) text"
        assert [(s.page, s.char_start, s.char_end) for s in result.pages] == [
            (1, 0, 15),
            (2, 17, 35),
        ]
        assert (
            result.text[result.pages[1].char_start : result.pages[1].char_end]
            == "Second (page) text"
        )
        assert result.metadata["title"] == "Quarterly report"
        assert result.metadata["author"] == "Test Author"
        assert result.metadata["subject"] is None  # unavailable, not invented
        assert result.warnings == []
        assert (result.word_count, result.char_count) == (6, 35)

    def test_pages_without_text_are_reported(self, write) -> None:  # type: ignore[no-untyped-def]
        result = extract(write("mixed.pdf", make_pdf(["Has text", None])), "pdf")
        assert result.warnings == ["pages_without_text:1"]
        assert result.pages[1].char_start == result.pages[1].char_end

    def test_image_only_pdf_needs_ocr(self, write) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ExtractionError) as caught:
            extract(write("scan.pdf", make_pdf([None, None])), "pdf")
        assert caught.value.code == "needs_ocr"

    def test_encrypted_pdfs(self, write) -> None:  # type: ignore[no-untyped-def]
        plain = make_pdf(["Secret text"])
        owner_only = encrypt_pdf(plain, user_password="", owner_password="owner")
        assert extract(write("owner.pdf", owner_only), "pdf").text == "Secret text"
        locked = encrypt_pdf(plain, user_password="user", owner_password="owner")
        with pytest.raises(ExtractionError) as caught:
            extract(write("locked.pdf", locked), "pdf")
        assert caught.value.code == "encrypted"

    def test_corrupted_pdf(self, write) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ExtractionError) as caught:
            extract(
                write("broken.pdf", b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF"), "pdf"
            )
        assert caught.value.code == "corrupted"

    def test_page_limit(self, write) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ExtractionError) as caught:
            extract(write("long.pdf", make_pdf(["p"] * 4)), "pdf", Limits(max_pages=3))
        assert caught.value.code == "too_many_pages"

    def test_text_limit_keeps_spans_consistent(self, write) -> None:  # type: ignore[no-untyped-def]
        result = extract(
            write("r.pdf", make_pdf(["First page text", "Second"])),
            "pdf",
            Limits(max_text_chars=20),
        )
        assert result.text == "First page text\n\nSec"
        assert [(s.page, s.char_start, s.char_end) for s in result.pages] == [
            (1, 0, 15),
            (2, 17, 20),
        ]
        assert "text_truncated" in result.warnings


class TestDocx:
    def test_headings_lists_and_tables_in_order(self, write) -> None:  # type: ignore[no-untyped-def]
        result = extract(write("notes.docx", make_docx()), "docx")
        assert result.text.startswith(
            "# Platform notes\n\nNEXA stores documents privately.\n\n## Limits\n\n- Fifty"
        )
        assert (
            "| Format | Status |\n| --- | --- |\n| PDF | '=SUM(A1) |\n| CSV | ready \\| now |"
            in result.text
        )
        assert result.text.endswith("After the table.")
        assert result.metadata["title"] == "Platform notes"
        assert result.metadata["author"] == "Test Author"
        assert result.metadata["tables"] == 1
        assert result.metadata["paragraphs"] == 6
        assert "formula_cells_neutralized" in result.warnings
        assert result.page_count is None

    def test_an_empty_document_is_not_an_error(self, write) -> None:  # type: ignore[no-untyped-def]
        document = new_docx()
        out = io.BytesIO()
        document.save(out)
        result = extract(write("empty.docx", out.getvalue()), "docx")
        assert result.text == ""
        assert "no_text" in result.warnings

    def test_an_inflating_archive_is_refused(self, write) -> None:  # type: ignore[no-untyped-def]
        bomb = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b" " * 3_000_000})
        with pytest.raises(ExtractionError) as caught:  # over the absolute cap
            extract(write("bomb.docx", bomb), "docx", Limits(max_uncompressed_bytes=1_000_000))
        assert caught.value.code == "archive_too_large"
        # Under the cap but inflating a thousandfold: a bomb by ratio.
        big = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b" " * 20_000_000})
        with pytest.raises(ExtractionError) as caught:
            extract(write("ratio.docx", big), "docx")
        assert caught.value.code == "archive_too_large"
        # A real, small document inflates a lot too, and is fine.
        assert extract(write("real.docx", make_docx()), "docx").text

    def test_macros_and_broken_files(self, write) -> None:  # type: ignore[no-untyped-def]
        macro = make_zip(
            {
                "[Content_Types].xml": b"<Types/>",
                "word/document.xml": b"<w/>",
                "word/vbaProject.bin": b"x",
            }
        )
        with pytest.raises(ExtractionError) as caught:
            extract(write("macro.docx", macro), "docx")
        assert caught.value.code == "unsupported_content"
        with pytest.raises(ExtractionError) as caught:
            extract(write("notzip.docx", b"PK\x03\x04 not really"), "docx")
        assert caught.value.code == "corrupted"
        with pytest.raises(ExtractionError) as caught:
            extract(write("noword.docx", make_zip({"a.txt": b"x"})), "docx")
        assert caught.value.code == "corrupted"


class TestPlainText:
    def test_unicode_text(self, write) -> None:  # type: ignore[no-untyped-def]
        result = extract(
            write("notes.txt", "héllo wörld — 日本語\r\nsecond line\r\n".encode()), "txt"
        )
        assert result.text == "héllo wörld — 日本語\nsecond line"
        assert result.metadata == {"encoding": "utf-8", "line_count": 2}
        assert result.pages[0].char_end == len(result.text)

    def test_other_encodings_are_reported(self, write) -> None:  # type: ignore[no-untyped-def]
        result = extract(write("latin.txt", b"caf\xe9 au lait"), "txt")
        assert result.text == "café au lait"
        assert result.warnings == ["encoding_fallback:cp1252"]

    def test_markdown_keeps_headings_and_code(self, write) -> None:  # type: ignore[no-untyped-def]
        source = "# Title\n\nSome *text*.\n\n## Usage\n\n```python\nprint('hi')\n```\n"
        result = extract(write("readme.md", source.encode()), "md")
        assert result.text == source.strip()
        assert result.metadata["heading_count"] == 2
        assert result.metadata["code_block_count"] == 1

    def test_text_limit(self, write) -> None:  # type: ignore[no-untyped-def]
        result = extract(write("big.txt", b"0123456789" * 10), "txt", Limits(max_text_chars=25))
        assert len(result.text) == 25
        assert "text_truncated" in result.warnings
        assert result.pages[0].char_end == 25


class TestCsv:
    def test_columns_rows_and_text(self, write) -> None:  # type: ignore[no-untyped-def]
        data = b'name,amount,note\nAlice,10,=HYPERLINK("x")\nBob,-5,fine | ok\n'
        result = extract(write("data.csv", data), "csv")
        assert result.metadata["columns"] == ["name", "amount", "note"]
        assert result.metadata["row_count"] == 2
        assert result.metadata["delimiter"] == ","
        assert result.text == (
            "| name | amount | note |\n| --- | --- | --- |\n"
            '| Alice | 10 | \'=HYPERLINK("x") |\n| Bob | -5 | fine \\| ok |'
        )
        assert "formula_cells_neutralized" in result.warnings

    def test_other_delimiters_ragged_rows_and_caps(self, write) -> None:  # type: ignore[no-untyped-def]
        data = b"a;b;c\n1;2\n3;4;5;6\n7;8;9\n"
        result = extract(write("semi.csv", data), "csv", Limits(max_csv_rows=2, max_cell_chars=4))
        assert result.metadata["delimiter"] == ";"
        assert result.metadata["row_count"] == 2
        assert "rows_truncated" in result.warnings
        assert "ragged_rows:2" in result.warnings
        long = extract(
            write("long.csv", b"h\n" + b"x" * 50 + b"\n"), "csv", Limits(max_cell_chars=4)
        )
        assert "cell_truncated" in long.warnings
        assert "xxxx…" in long.text

    def test_empty_and_header_only(self, write) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ExtractionError) as caught:
            extract(write("empty.csv", b""), "csv")
        assert caught.value.code == "empty_file"
        with pytest.raises(ExtractionError) as caught:
            extract(write("blank.csv", b"\n\n  \n"), "csv")
        assert caught.value.code == "empty_file"
        header_only = extract(write("header.csv", b"a,b,c\n"), "csv")
        assert header_only.metadata["row_count"] == 0
        assert "no_rows" in header_only.warnings

    def test_malformed_csv(self, write) -> None:  # type: ignore[no-untyped-def]
        huge_field = b"a,b\n" + b"x" * 200_000 + b",1\n"
        with pytest.raises(ExtractionError) as caught:
            extract(write("huge.csv", huge_field), "csv")
        assert caught.value.code == "corrupted"


def test_unknown_formats_and_unexpected_errors(write) -> None:  # type: ignore[no-untyped-def]
    path = write("x.xlsx", b"PK\x03\x04")
    with pytest.raises(ExtractionError) as caught:
        extract(path, "xlsx")  # type: ignore[arg-type]
    assert caught.value.code == "unsupported_format"
    with pytest.raises(ExtractionError) as caught:
        extract(path.with_name("missing.txt"), "txt")
    assert caught.value.code == "extraction_failed"


def test_extraction_counts() -> None:
    result = Extraction(text="one two  three\nfour")
    assert (result.word_count, result.char_count) == (4, 19)
