import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import type { SemanticStatus } from "../../types/document";
import { checkFile, extensionOf, formatBytes } from "./files";
import {
  describeDocumentError,
  describeEmbeddingFailure,
  describeFailure,
  describeIndexFailure,
  describeSemanticUnavailable,
  describeWarning,
  indexLabel,
  indexTone,
  isEmbedding,
  isEmbeddingNow,
  semanticLabel,
  semanticTone,
  statusLabel,
  statusTone,
} from "./labels";

describe("describeWarning", () => {
  it.each([
    ["pages_without_text:3", "3 pages have no text; they may be scanned images."],
    ["pages_without_text:1", "1 page has no text; it may be a scanned image."],
    ["pages_unreadable:2", "2 pages could not be read."],
    ["text_truncated", "The text is longer than NEXA keeps; only the beginning was extracted."],
    ["cell_truncated", "Some table cells were very long and were shortened."],
    [
      "formula_cells_neutralized",
      "Cells that looked like spreadsheet formulas are kept as plain text, so they can never run.",
    ],
    [
      "encoding_fallback:cp1252",
      "The file is not UTF-8, so it was read as Windows-1252, a best guess: some characters may be wrong.",
    ],
    ["encoding_fallback:utf-16", "The file's text encoding was read as UTF-16."],
    ["encoding_replaced", "Some characters could not be decoded and were replaced with “�”."],
    ["rows_truncated", "The file has more rows than NEXA keeps; the rest were left out."],
    [
      "ragged_rows:2",
      "2 rows have a different number of columns than the header; they were padded or cut to fit.",
    ],
    ["no_text", "No text was found in this file."],
    ["no_rows", "The file has a header row but no data rows."],
    [
      "chunks_truncated",
      "The document is longer than NEXA indexes; only its beginning can be found by search.",
    ],
    // Unknown codes, and known ones with a malformed detail, are shown as sent.
    ["brand_new_warning:7", "brand_new_warning:7"],
    ["pages_unreadable:many", "pages_unreadable:many"],
    ["constructor", "constructor"],
  ])("%s", (warning, sentence) => {
    expect(describeWarning(warning)).toBe(sentence);
  });
});

describe("statuses", () => {
  it("labels and colours extraction and index statuses, and passes unknown ones through", () => {
    expect([statusLabel("completed"), statusTone("completed")]).toEqual(["Ready", "up"]);
    expect([statusLabel("needs_ocr"), statusTone("needs_ocr")]).toEqual(["Needs OCR", "down"]);
    expect([statusLabel("queued"), statusTone("queued")]).toEqual(["Queued", "waiting"]);
    expect([statusLabel("archived"), statusTone("archived")]).toEqual(["archived", "neutral"]);
    expect([indexLabel("indexed"), indexTone("indexed")]).toEqual(["Indexed", "up"]);
    expect([indexLabel("stale"), indexTone("stale")]).toEqual(["Outdated", "waiting"]);
    expect([indexLabel("none"), indexTone("none")]).toEqual(["Not indexed", "neutral"]);
  });

  it("explains failures with the server's message, else its code, else the status", () => {
    const failed = { status: "failed", error_code: null, error_message: null };
    expect(describeFailure({ ...failed, error_message: "The PDF is password protected." })).toBe(
      "The PDF is password protected.",
    );
    expect(describeFailure({ ...failed, error_code: "corrupted" })).toBe(
      "The file is damaged or could not be read.",
    );
    expect(describeFailure({ ...failed, status: "unsupported" })).toBe(
      "NEXA could not extract text from this file.",
    );
    expect(describeFailure({ ...failed, status: "completed" })).toBeNull();
    expect(describeIndexFailure({ ...failed, error_code: "timeout" })).toBe(
      "Indexing took too long and was stopped.",
    );
    expect(describeIndexFailure({ ...failed, status: "indexed" })).toBeNull();
  });
});

describe("the semantic index", () => {
  it.each<[SemanticStatus, string, string, boolean, boolean]>([
    // status, label, tone, polled, re-embed disabled
    ["unavailable", "Semantic search unavailable", "neutral", false, false],
    ["none", "Not embedded", "neutral", false, false],
    ["pending", "Embedding queued", "waiting", true, true],
    ["embedding", "Embedding…", "waiting", true, true],
    ["ready", "Semantic search ready", "up", false, false],
    ["failed", "Embedding failed", "down", false, false],
    ["stale", "Updating embeddings", "waiting", true, false],
  ])("%s is “%s”", (status, label, tone, polled, busy) => {
    expect(semanticLabel(status)).toBe(label);
    expect(semanticTone(status)).toBe(tone);
    expect(isEmbedding(status)).toBe(polled);
    expect(isEmbeddingNow(status)).toBe(busy);
  });

  it("passes an unknown status through, uncoloured and not polled", () => {
    expect([semanticLabel("archived"), semanticTone("archived"), isEmbedding("archived")]).toEqual([
      "archived",
      "neutral",
      false,
    ]);
    expect(semanticLabel("constructor")).toBe("constructor");
  });

  it("explains a failure with the server's message, else its code", () => {
    const failed = { status: "failed", error_code: "provider_error", error_message: null };
    expect(describeEmbeddingFailure({ ...failed, error_message: "The embedding provider refused the request." })).toBe(
      "The embedding provider refused the request.",
    );
    expect(describeEmbeddingFailure({ ...failed, error_code: "timeout" })).toBe(
      "Embedding took too long and was stopped.",
    );
    expect(describeEmbeddingFailure(failed)).toBe("The document could not be prepared for semantic search.");
    expect(describeEmbeddingFailure({ status: "ready" })).toBeNull();
  });

  it("says why semantic search is unavailable without naming its internals", () => {
    expect(describeSemanticUnavailable("disabled")).toBe(
      "Semantic search is turned off here; documents are found by their keywords.",
    );
    for (const reason of ["pgvector_missing", "provider_not_configured", "no_model", null]) {
      expect(describeSemanticUnavailable(reason)).toBe(
        "Semantic search is not available here right now; documents are found by their keywords.",
      );
    }
  });
});

describe("describeDocumentError", () => {
  it("never shows the message of an unexpected refusal", () => {
    const denied = new ApiError(403, { code: "PERMISSION_DENIED", message: "Role viewer lacks documents.update" }, null);
    expect(describeDocumentError(denied)).toBe("You do not have permission to do that.");
    const conflict = new ApiError(409, { code: "CONFLICT", message: "The document has no extracted text to index yet." }, null);
    expect(describeDocumentError(conflict)).toBe("The document has no extracted text to index yet.");
    expect(describeDocumentError(new TypeError("Failed to fetch"))).toBe(
      "Something went wrong. Check your connection and try again.",
    );
  });
});

describe("file checks", () => {
  const rules = { accepted_extensions: [".pdf", ".txt"], max_upload_bytes: 1024 };

  it("formats sizes in human units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(2_516_582)).toBe("2.4 MB");
    expect(formatBytes(50 * 1024 * 1024)).toBe("50 MB");
  });

  it("reads extensions case-insensitively", () => {
    expect(extensionOf("Report.PDF")).toBe(".pdf");
    expect(extensionOf("archive.tar.gz")).toBe(".gz");
    expect(extensionOf("README")).toBe("");
    expect(extensionOf(".bashrc")).toBe("");
  });

  it("refuses unsupported, empty and oversize files", () => {
    expect(checkFile({ name: "a.pdf", size: 10 }, rules)).toBeNull();
    expect(checkFile({ name: "setup.exe", size: 10 }, rules)).toBe(
      "This file type is not supported. Upload a .pdf or .txt file.",
    );
    expect(checkFile({ name: "a.txt", size: 0 }, rules)).toBe("The file is empty.");
    expect(checkFile({ name: "a.pdf", size: 2048 }, rules)).toBe("The file is 2 KB; the limit is 1 KB.");
    expect(checkFile({ name: "a.pdf", size: 10 ** 9 }, { ...rules, max_upload_bytes: null })).toBeNull();
  });
});
