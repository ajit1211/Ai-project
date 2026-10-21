/**
 * Delete, confirmed in place instead of in a dialog: the button turns into the question with
 * Delete and Cancel. Focus moves to Cancel, the safe answer; Cancel and Escape put it back on
 * the first Delete. `onConfirm` resolves to whether the deletion happened: when it did, the
 * caller moves on (the row goes away, the page navigates); when not, the question closes.
 */

import { useEffect, useId, useRef, useState } from "react";

export interface ConfirmDeleteProps {
  /** The accessible name of the first button, naming what is deleted. */
  label: string;
  /** The question shown while confirming. */
  question: string;
  onConfirm: () => Promise<boolean>;
}

export function ConfirmDelete({ label, question, onConfirm }: ConfirmDeleteProps) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  /** The question closed without deleting: focus returns to Delete. */
  const refocus = useRef(false);
  const questionId = useId();

  useEffect(() => {
    if (confirming) {
      cancelButton.current?.focus();
    } else if (refocus.current) {
      refocus.current = false;
      deleteButton.current?.focus();
    }
  }, [confirming]);

  const close = () => {
    refocus.current = true;
    setConfirming(false);
  };

  const confirm = () => {
    setBusy(true);
    void onConfirm().then((deleted) => {
      if (deleted) return;
      setBusy(false);
      close();
    });
  };

  if (!confirming) {
    return (
      <button
        ref={deleteButton}
        type="button"
        className="btn btn-quiet btn-danger"
        aria-label={label}
        onClick={() => {
          setConfirming(true);
        }}
      >
        Delete
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
      <button type="button" className="btn btn-danger" disabled={busy} onClick={confirm}>
        {busy ? "Deleting…" : "Delete"}
      </button>
      <button ref={cancelButton} type="button" className="btn btn-quiet" disabled={busy} onClick={close}>
        Cancel
      </button>
    </div>
  );
}
