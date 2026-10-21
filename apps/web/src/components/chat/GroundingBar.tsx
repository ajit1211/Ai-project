/**
 * The composer's document controls: the Documents button that opens the picker, what the
 * next question will be answered from (removable chips, or "all your documents"), how to
 * search them (only where semantic search runs), the way back to ordinary chat, and why the
 * last grounded question was refused.
 */

import { useId, type Ref } from "react";

import type { RetrievalChoice, RetrievalMode } from "../../types/chat";
import { Alert } from "../forms";
import { retrievalLabel, selectionSummary, type DocumentSelection } from "./grounding";
import { Icon } from "./Icon";

/** The search control: the modes the server offers, and the one chosen. */
export interface RetrievalControl {
  modes: readonly RetrievalMode[];
  value: RetrievalChoice;
  onChange: (choice: RetrievalChoice) => void;
}

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
  /**
   * How to search the documents; null hides the control (keyword search is the only way
   * here, or the options could not be loaded), and questions then use the server's default.
   */
  retrieval?: RetrievalControl | null;
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
  retrieval = null,
}: GroundingBarProps) {
  const id = useId();
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
        {selection && retrieval && (
          <span className="grounding-mode">
            <label htmlFor={`${id}-retrieval`}>Search by</label>
            <select
              id={`${id}-retrieval`}
              value={retrieval.value}
              onChange={(event) => {
                retrieval.onChange(event.target.value as RetrievalChoice);
              }}
            >
              <option value="auto">{retrievalLabel("auto")}</option>
              {retrieval.modes.map((mode) => (
                <option key={mode} value={mode}>
                  {retrievalLabel(mode)}
                </option>
              ))}
            </select>
          </span>
        )}
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
