/**
 * Where the user writes. The textarea grows with its text; Enter sends, Shift+Enter starts a
 * new line, and an Enter that confirms an IME composition (Chinese, Japanese, Korean input)
 * never sends. While a reply streams the composer is disabled and offers Stop instead of
 * Send. It is made read-only (and aria-disabled) rather than `disabled`, so focus stays in it
 * and the next message can be written as soon as the reply ends.
 */

import {
  useId,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  type KeyboardEvent,
  type Ref,
} from "react";

import { Icon } from "./Icon";

/** The textarea grows with its text up to this height, then scrolls. */
const MAX_HEIGHT_PX = 200;

export interface ComposerHandle {
  focus: () => void;
}

export interface MessageComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  stopping: boolean;
  /** Nothing can be sent right now (the conversation is loading or could not be). */
  disabled?: boolean;
  ref?: Ref<ComposerHandle>;
}

export function MessageComposer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  stopping,
  disabled = false,
  ref,
}: MessageComposerProps) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const id = useId();
  const hintId = useId();
  const locked = streaming || disabled;
  const canSend = !locked && value.trim() !== "";

  useImperativeHandle(ref, () => ({
    focus: () => {
      textarea.current?.focus();
    },
  }));

  // Grow with the text (also when it is set from outside: cleared, an example prompt).
  useLayoutEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "auto";
    const borders = element.offsetHeight - element.clientHeight;
    element.style.height = `${String(Math.min(element.scrollHeight + borders, MAX_HEIGHT_PX))}px`;
  }, [value]);

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    // This Enter picks an IME candidate. Safari reports the composition only as keyCode 229.
    // eslint-disable-next-line @typescript-eslint/no-deprecated -- keyCode 229 is the only composition signal some browsers give
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (canSend) onSend();
  };

  return (
    <form
      className="chat-composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSend) onSend();
      }}
    >
      <div className="chat-composer-inner">
        <label htmlFor={id} className="visually-hidden">
          Message
        </label>
        <textarea
          ref={textarea}
          id={id}
          rows={1}
          value={value}
          placeholder="Message NEXA…"
          readOnly={locked}
          aria-disabled={locked}
          aria-describedby={hintId}
          onChange={(event) => {
            onChange(event.target.value);
          }}
          onKeyDown={onKeyDown}
        />
        {streaming ? (
          <button type="button" className="btn chat-stop" onClick={onStop} disabled={stopping}>
            <Icon name="stop" />
            {stopping ? "Stopping…" : "Stop"}
          </button>
        ) : (
          <button type="submit" className="btn btn-primary chat-send" disabled={!canSend}>
            <Icon name="send" />
            Send
          </button>
        )}
      </div>
      <p id={hintId} className="chat-composer-hint">
        Enter to send · Shift+Enter for a new line
      </p>
    </form>
  );
}
