/**
 * A Server-Sent Events parser over a byte stream.
 *
 * The browser's EventSource cannot send a POST body or an Authorization header, so the SPA
 * reads `text/event-stream` responses from `fetch` and parses the frames itself. This
 * follows the WHATWG event stream format: frames are separated by a blank line, lines end in
 * LF, CRLF or CR, `:` lines are comments (the server's `: ping`), several `data:` lines are
 * joined with newlines, and `retry:` is ignored. Chunk boundaries may fall anywhere,
 * including inside a line or between the CR and LF of a line end. An unfinished frame at
 * the end of the stream is discarded, as the format requires.
 *
 * It knows nothing about NEXA event names; `src/api/ai.ts` types the frames.
 */

export interface SseFrame {
  /** The `event:` field, or "message" when the frame has none. */
  event: string;
  /** The `id:` field when the frame carried one. */
  id?: string;
  /** All `data:` lines of the frame, joined with "\n". */
  data: string;
}

const LINE_END = /\r\n|\r|\n/;

export async function* parseSSE(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SseFrame, void, undefined> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "message";
  let id: string | undefined;
  let data: string[] = [];

  /** The next complete line, or null until more bytes arrive (or the stream has ended). */
  const takeLine = (atEnd: boolean): string | null => {
    const match = LINE_END.exec(buffer);
    if (!match) {
      if (!atEnd || buffer === "") return null;
      const rest = buffer;
      buffer = "";
      return rest;
    }
    // A chunk ending in CR may be the first half of a CRLF: wait for the next byte.
    if (match[0] === "\r" && match.index === buffer.length - 1 && !atEnd) return null;
    const line = buffer.slice(0, match.index);
    buffer = buffer.slice(match.index + match[0].length);
    return line;
  };

  /** Feed one line; returns the frame it completes, if any. */
  const consume = (line: string): SseFrame | null => {
    if (line === "") {
      let frame: SseFrame | null = null;
      if (data.length > 0) {
        frame = id === undefined ? { event, data: data.join("\n") } : { event, id, data: data.join("\n") };
      }
      event = "message";
      id = undefined;
      data = [];
      return frame;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    switch (field) {
      case "event":
        event = value;
        break;
      case "data":
        data.push(value);
        break;
      case "id":
        if (!value.includes("\0")) id = value;
        break;
      default:
        // "retry" and unknown fields are ignored.
        break;
    }
    return null;
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      for (let line = takeLine(false); line !== null; line = takeLine(false)) {
        const frame = consume(line);
        if (frame) yield frame;
      }
    }
    buffer += decoder.decode();
    for (let line = takeLine(true); line !== null; line = takeLine(true)) {
      const frame = consume(line);
      if (frame) yield frame;
    }
  } finally {
    // Stops the producer when the consumer leaves early (break, return or throw); a no-op
    // once the stream has closed, and a rejection when it already errored (e.g. aborted).
    await reader.cancel().catch(() => undefined);
  }
}
