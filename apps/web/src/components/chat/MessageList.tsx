/**
 * The messages of the open conversation, oldest first, with "Load older" at the top. While a
 * reply streams the list follows its end, unless the user scrolled up to read; scrolling back
 * to the bottom resumes following. Loading older messages keeps the reading position.
 */

import { useLayoutEffect, useRef } from "react";

import type { ChatItem } from "../../hooks/chatThread";
import { MessageBubble } from "./MessageBubble";

/** Within this many pixels of the bottom, the list follows new text. */
const FOLLOW_THRESHOLD_PX = 48;

function canRegenerate(item: ChatItem): boolean {
  return item.delivery === "sent" && item.message.role === "assistant" && item.message.status === "completed";
}

function canRetry(item: ChatItem): boolean {
  if (item.delivery === "unsent") return true;
  if (item.delivery !== "sent") return false;
  const { role, status } = item.message;
  // A user message without a reply (it failed before being stored) can be answered again too.
  return role === "user" || status === "failed" || status === "cancelled";
}

export interface MessageListProps {
  conversationId: string;
  items: ChatItem[];
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  /** Nothing is being generated and the conversation takes messages. */
  idle: boolean;
  onRegenerate: () => void;
  onRetry: (key: string) => void;
}

export function MessageList({
  conversationId,
  items,
  hasOlder,
  loadingOlder,
  onLoadOlder,
  idle,
  onRegenerate,
  onRetry,
}: MessageListProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  /** Set while older messages load: where the reader was, measured from the bottom. */
  const anchor = useRef<{ fromBottom: number; firstKey: string | undefined } | null>(null);

  // A conversation opens at its newest message.
  useLayoutEffect(() => {
    following.current = true;
    anchor.current = null;
  }, [conversationId]);

  useLayoutEffect(() => {
    const element = scroller.current;
    if (!element) return;
    const saved = anchor.current;
    if (saved && saved.firstKey !== items[0]?.key) {
      // Older messages arrived above: keep the same message under the reader's eyes.
      anchor.current = null;
      element.scrollTop = element.scrollHeight - saved.fromBottom;
      return;
    }
    if (saved && !loadingOlder) anchor.current = null; // The load ended without adding any.
    if (following.current) element.scrollTop = element.scrollHeight;
  }, [items, loadingOlder]);

  const onScroll = () => {
    const element = scroller.current;
    if (!element) return;
    following.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < FOLLOW_THRESHOLD_PX;
  };

  const loadOlder = () => {
    const element = scroller.current;
    if (element) {
      anchor.current = {
        fromBottom: element.scrollHeight - element.scrollTop,
        firstKey: items[0]?.key,
      };
    }
    onLoadOlder();
  };

  const last = items.at(-1);
  return (
    <div ref={scroller} className="chat-messages" onScroll={onScroll}>
      <div className="chat-thread">
        {hasOlder && (
          <div className="chat-older">
            <button type="button" className="btn btn-quiet" onClick={loadOlder} disabled={loadingOlder}>
              {loadingOlder ? "Loading…" : "Load older messages"}
            </button>
          </div>
        )}
        <ol className="message-list" aria-label="Messages">
          {items.map((item) => {
            const offered = idle && item === last;
            return (
              <MessageBubble
                key={item.key}
                item={item}
                canRegenerate={offered && canRegenerate(item)}
                canRetry={offered && canRetry(item)}
                onRegenerate={onRegenerate}
                onRetry={onRetry}
              />
            );
          })}
        </ol>
      </div>
    </div>
  );
}
