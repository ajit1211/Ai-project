/**
 * An action confirmed in place, as `ConfirmDelete` does for deletions: the button turns into
 * the question with the action and a way out. Focus moves to the way out, the safe answer;
 * it and Escape put focus back on the first button. `onConfirm` resolves to whether the action
 * happened: when it did, the caller moves on; when not, the question closes.
 */

import { useEffect, useId, useRef, useState, type ReactNode } from "react";

export interface ConfirmActionProps {
  /** The first button's text ("Cancel task"). */
  label: string;
  /** Its icon, if any. */
  icon?: ReactNode;
  /** The question shown while confirming. */
  question: string;
  /** The confirming button's text, and its text while the action runs. */
  confirm: string;
  busyText: string;
  /** The button that keeps things as they are ("Keep running"). */
  dismiss: string;
  disabled?: boolean;
  onConfirm: () => Promise<boolean>;
}

export function ConfirmAction({
  label,
  icon,
  question,
  confirm,
  busyText,
  dismiss,
  disabled = false,
  onConfirm,
}: ConfirmActionProps) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const firstButton = useRef<HTMLButtonElement>(null);
  const dismissButton = useRef<HTMLButtonElement>(null);
  /** The question closed without acting: focus returns to the first button. */
  const refocus = useRef(false);
  const questionId = useId();

  useEffect(() => {
    if (confirming) {
      dismissButton.current?.focus();
    } else if (refocus.current) {
      refocus.current = false;
      firstButton.current?.focus();
    }
  }, [confirming]);

  const close = () => {
    refocus.current = true;
    setConfirming(false);
  };

  const run = () => {
    setBusy(true);
    void onConfirm().then((done) => {
      setBusy(false);
      if (done) setConfirming(false);
      else close();
    });
  };

  if (!confirming) {
    return (
      <button
        ref={firstButton}
        type="button"
        className="btn btn-danger agent-icon-button"
        disabled={disabled}
        onClick={() => {
          setConfirming(true);
        }}
      >
        {icon}
        {label}
      </button>
    );
  }

  return (
    <div
      className="doc-confirm"
      role="group"
      aria-labelledby={questionId}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || busy) return;
        event.preventDefault();
        event.stopPropagation();
        close();
      }}
    >
      <span id={questionId} className="small">
        {question}
      </span>
      <button type="button" className="btn btn-danger" disabled={busy} onClick={run}>
        {busy ? busyText : confirm}
      </button>
      <button ref={dismissButton} type="button" className="btn btn-quiet" disabled={busy} onClick={close}>
        {dismiss}
      </button>
    </div>
  );
}
