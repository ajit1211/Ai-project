import { describe, expect, it, vi } from "vitest";

import { parseSSE, type SseFrame } from "./sse";

const encoder = new TextEncoder();

function streamOfBytes(chunks: Uint8Array[], onCancel?: () => void): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
    cancel: onCancel,
  });
}

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  return streamOfBytes(chunks.map((chunk) => encoder.encode(chunk)));
}

async function collect(chunks: string[]): Promise<SseFrame[]> {
  const frames: SseFrame[] = [];
  for await (const frame of parseSSE(streamOf(chunks))) frames.push(frame);
  return frames;
}

describe("parseSSE", () => {
  it("parses a frame with event, id and data", async () => {
    const frames = await collect(['event: message.delta\nid: 1\ndata: {"text":"hi"}\n\n']);
    expect(frames).toEqual([{ event: "message.delta", id: "1", data: '{"text":"hi"}' }]);
  });

  it("reassembles frames split anywhere across chunks", async () => {
    const frames = await collect([
      "event: message.",
      "delta\nda",
      'ta: {"text":"a"}\n',
      "\nevent: usage\nid: 2\nd",
      "ata: {}\n\n",
    ]);
    expect(frames).toEqual([
      { event: "message.delta", data: '{"text":"a"}' },
      { event: "usage", id: "2", data: "{}" },
    ]);
  });

  it("ignores comment lines such as the server's ping", async () => {
    const frames = await collect([": ping\n\n", "event: usage\n: keepalive\ndata: {}\n\n", ": ping\n"]);
    expect(frames).toEqual([{ event: "usage", data: "{}" }]);
  });

  it("joins several data lines with newlines", async () => {
    const frames = await collect(["data: first\ndata: second\n\n"]);
    expect(frames).toEqual([{ event: "message", data: "first\nsecond" }]);
  });

  it("accepts CRLF line endings", async () => {
    const frames = await collect(["event: x\r\ndata: 1\r\n\r\nevent: y\r\ndata: 2\r\n\r\n"]);
    expect(frames).toEqual([
      { event: "x", data: "1" },
      { event: "y", data: "2" },
    ]);
  });

  it("handles a CRLF split between two chunks", async () => {
    const frames = await collect(["event: x\r", "\ndata: 1\r", "\n\r", "\n"]);
    expect(frames).toEqual([{ event: "x", data: "1" }]);
  });

  it("accepts lone CR line endings", async () => {
    const frames = await collect(["event: x\rdata: 1\r\r"]);
    expect(frames).toEqual([{ event: "x", data: "1" }]);
  });

  it("decodes multibyte characters split across chunks", async () => {
    const bytes = encoder.encode("data: héllo wörld\n\n");
    const cut = bytes.indexOf(0xc3) + 1; // inside the two-byte "é"
    const frames: SseFrame[] = [];
    for await (const frame of parseSSE(streamOfBytes([bytes.slice(0, cut), bytes.slice(cut)]))) {
      frames.push(frame);
    }
    expect(frames).toEqual([{ event: "message", data: "héllo wörld" }]);
  });

  it("reads 'data:' without a space and keeps further leading spaces", async () => {
    const frames = await collect(["data:x\n\n", "data:  y\n\n"]);
    expect(frames.map((f) => f.data)).toEqual(["x", " y"]);
  });

  it("does not dispatch a frame that carries no data", async () => {
    const frames = await collect(["event: x\nid: 7\n\n", "data: ok\n\n"]);
    expect(frames).toEqual([{ event: "message", data: "ok" }]);
  });

  it("ignores retry and unknown fields", async () => {
    const frames = await collect(["retry: 1000\nfoo: bar\ndata: ok\n\n"]);
    expect(frames).toEqual([{ event: "message", data: "ok" }]);
  });

  it("discards an unfinished frame at the end of the stream", async () => {
    const frames = await collect(["data: done\n\nevent: x\ndata: partial"]);
    expect(frames).toEqual([{ event: "message", data: "done" }]);
  });

  it("cancels the source when the consumer stops early", async () => {
    const onCancel = vi.fn();
    const body = streamOfBytes(
      [encoder.encode("data: 1\n\n"), encoder.encode("data: 2\n\n")],
      onCancel,
    );
    const seen: string[] = [];
    for await (const frame of parseSSE(body)) {
      seen.push(frame.data);
      break;
    }
    expect(seen).toEqual(["1"]);
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
