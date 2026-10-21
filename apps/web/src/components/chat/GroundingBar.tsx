/**
 * The composer's document controls: the Documents button that opens the picker, what the
 * next question will be answered from (removable chips, or "all your documents"), the way
 * back to ordinary chat, and why the last grounded question was refused.
 */

import type { Ref } from "react";

import { Alert } from "../forms";
import { selectionSummary, type DocumentSelection } from "./grounding";
import { Icon } from "./Icon";

export interface GroundingBarProps {
  selection: DocumentSelection | null;
  onOpenPicker: () => void;
  pickerOpen: boolean;
  /** Remove one document; removing the last goes back to ordinary chat. */
  onRemove: (documentId: string) => void;
  onClear: () => void;
  /** Why the last question about documents could not be sent. */
  alert: string | null;
  onDismissAlert: () => void;
  openerRef: Ref<HTMLButtonElement>;
}

export function GroundingBar({
  selection,
  onOpenPicker,
  pickerOpen,
  onRemove,
  onClear,
  alert,
  onDismissAlert,
  openerRef,
}: GroundingBarProps) {
  return (
    <div className="grounding-bar">
      <div className="grounding-row">
        <button
          ref={openerRef}
          type="button"
          className={`chip${selection ? " chip-active" : ""}`}
          aria-haspopup="dialog"
          aria-expanded={pickerOpen}
          onClick={onOpenPicker}
        >
          <Icon name="file" />
          Documents
        </button>
        {selection && <span className="grounding-indicator">{selectionSummary(selection)}</span>}
        {selection && (
          <button type="button" className="btn btn-quiet grounding-clear" onClick={onClear}>
            Use ordinary chat
          </button>
        )}
      </div>
      {selection?.kind === "documents" && (
        <ul className="grounding-chips" aria-label="Selected documents">
          {selection.documents.map((document) => (
            <li key={document.id} className="chip grounding-chip">
              <span className="grounding-chip-name">{document.name}</span>
              <button
                type="button"
                className="grounding-chip-remove"
                aria-label={`Remove ${document.name}`}
                onClick={() => {
                  onRemove(document.id);
                }}
              >
                <Icon name="close" />
              </button>
            </li>
          ))}
        </ul>
      )}
      {alert && (
        <div className="chat-banner grounding-alert">
          <Alert kind="error">{alert}</Alert>
          <div className="chat-banner-actions">
            <button type="button" className="icon-button" aria-label="Dismiss" onClick={onDismissAlert}>
              <Icon name="close" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
