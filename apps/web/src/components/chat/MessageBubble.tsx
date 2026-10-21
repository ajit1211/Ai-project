/**
 * One message. The user's text is shown as typed (line breaks kept); a reply is rendered as
 * Markdown, with its status, its details (tokens, cost, latency) and its actions. A reply
 * answered from documents adds its citations and sources (GroundedReply.tsx).
 */

import { useEffect, useState } from "react";

import type { ChatItem } from "../../hooks/chatThread";
import type { Message } from "../../types/chat";
import { GroundedReply, RetrievalStatus } from "./GroundedReply";
import { Icon } from "./Icon";
import { Markdown } from "./Markdown";
import { StreamingIndicator } from "./StreamingIndicator";
import { formatCount, formatDuration } from "./format";

/** How long "Copied" stays on the button. */
const COPIED_MS = 1500;

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => {
      setState("idle");
    }, COPIED_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [state]);

  const copy = () => {
    let copying: Promise<void>;
    try {
      copying = navigator.clipboard.writeText(text);
    } catch (err) {
      // No clipboard API (an insecure context, an old browser).
      copying = Promise.reject(err instanceof Error ? err : new Error(String(err)));
    }
    void copying.then(
      () => {
        setState("copied");
      },
      () => {
        setState("failed");
      },
    );
  };

  const label = { idle: "Copy", copied: "Copied", failed: "Copy failed" }[state];
  return (
    <button type="button" className="chip" onClick={copy}>
      <Icon name={state === "copied" ? "check" : "copy"} />
      {label}
    </button>
  );
}

/** Tokens, cost and timings of a finished reply, and why it ended early. */
function ReplyDetails({ message }: { message: Message }) {
  const details: string[] = [];
  if (message.input_tokens + message.output_tokens > 0) {
    details.push(`${formatCount(message.input_tokens)} in · ${formatCount(message.output_tokens)} out tokens`);
  }
  if (message.cost_usd !== null) details.push(`$${message.cost_usd}`);
  if (message.latency_ms > 0) details.push(`latency ${formatDuration(message.latency_ms)}`);
  if (message.ttft_ms !== null) details.push(`first token ${formatDuration(message.ttft_ms)}`);

  let note: string | null = null;
  if (message.finish_reason === "length") {
    note = "The reply was cut off: it reached the model's output limit.";
  } else if (message.finish_reason === "content_filter") {
    note = "The model stopped for safety reasons.";
  }
  if (details.length === 0 && !note) return null;

  return (
    <div className="message-footer">
      {details.length > 0 && (
        <ul className="message-details" aria-label="Reply details">
          {details.map((detail) => (
            <li key={detail}>{detail}</li>
          ))}
        </ul>
      )}
      {note && <p className="message-note">{note}</p>}
    </div>
  );
}

const describe = (message: string, hint: string | null): string => (hint ? `${message} ${hint}` : message);

function StatusLine({ item }: { item: ChatItem }) {
  const { message, streaming, delivery, error } = item;
  if (delivery === "unsent") {
    return (
      <p className="message-status">
        <span className="badge badge-down">Not sent</span>
        {error && <span className="message-error">{describe(error.message, error.retryHint)}</span>}
      </p>
    );
  }
  if (message.role !== "assistant" || streaming) return null;
  switch (message.status) {
    case "failed":
      return (
        <p className="message-status">
          <span className="badge badge-down">Failed</span>
          {error && <span className="message-error">{describe(error.message, error.retryHint)}</span>}
        </p>
      );
    case "cancelled":
      return (
        <p className="message-status">
          <span className="badge badge-waiting">Cancelled</span>
          <span className="muted">The reply was stopped before it finished.</span>
        </p>
      );
    case "pending":
    case "streaming":
      return (
        <p className="message-status">
          <span className="badge badge-waiting">In progress</span>
          <span className="muted">This reply is still being generated elsewhere.</span>
        </p>
      );
    case "completed":
      return null;
  }
}

export interface MessageBubbleProps {
  item: ChatItem;
  /** Offer Regenerate: the last reply, finished, with nothing running. */
  canRegenerate: boolean;
  /** Offer Retry: a failed or stopped last reply, or a submission that never got through. */
  canRetry: boolean;
  onRegenerate: () => void;
  onRetry: (key: string) => void;
}

export function MessageBubble({ item, canRegenerate, canRetry, onRegenerate, onRetry }: MessageBubbleProps) {
  const { message, streaming, delivery } = item;
  const isUser = message.role === "user";
  const finishedReply =
    !isUser &&
    !streaming &&
    delivery === "sent" &&
    (message.status === "completed" || message.status === "failed" || message.status === "cancelled");

  const grounding = isUser ? null : (message.grounding ?? null);
  let body = null;
  if (isUser) body = <p className="message-text">{message.content}</p>;
  else if (grounding) body = <GroundedReply message={message} grounding={grounding} streaming={streaming} />;
  else if (message.content) body = <Markdown text={message.content} />;

  return (
    <li className={`message message-${message.role} message-${delivery}${grounding ? " message-grounded" : ""}`}>
      <div className="message-bubble">
        <span className="visually-hidden">{isUser ? "You said:" : "NEXA replied:"}</span>
        {body}
        {item.retrieval && <RetrievalStatus retrieval={item.retrieval} />}
        {streaming && <StreamingIndicator />}
      </div>
      <StatusLine item={item} />
      {finishedReply && <ReplyDetails message={message} />}
      {!streaming && delivery !== "sending" && (
        <div className="message-actions">
          {message.content && <CopyButton text={message.content} />}
          {canRegenerate && (
            <button type="button" className="chip" onClick={onRegenerate}>
              <Icon name="refresh" />
              Regenerate
            </button>
          )}
          {canRetry && (
            <button
              type="button"
              className="chip"
              onClick={() => {
                onRetry(item.key);
              }}
            >
              <Icon name="refresh" />
              Retry
            </button>
          )}
        </div>
      )}
    </li>
  );
}
