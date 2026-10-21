/**
 * The dialog that chooses what questions are answered from: up to 20 documents, found by name
 * among those search can use, or every document the user can access. Only searchable
 * documents are listed (the list asks Core for `searchable=true`), and a document that is not
 * searchable is never selectable. Choices apply when confirmed; Cancel and Escape leave the
 * selection as it was. Focus stays in the dialog and returns to the Documents button.
 */

import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import { Link } from "react-router";

import { listDocuments } from "../../api/documents";
import { formatBytes } from "../documents/files";
import { describeDocumentError, displayName, formatName } from "../documents/labels";
import type { DocumentRecord } from "../../types/document";
import { MAX_DOCUMENTS, type DocumentSelection, type PickedDocument } from "./grounding";
import { useDialogFocus } from "./useDialogFocus";

const PAGE_SIZE = 20;
const SEARCH_DELAY_MS = 300;

interface Results {
  /** The search these results answer. */
  search: string;
  items: DocumentRecord[];
  nextCursor: string | null;
  error: string | null;
}

export interface DocumentPickerProps {
  selection: DocumentSelection | null;
  /** The new selection; null goes back to ordinary chat. */
  onApply: (selection: DocumentSelection | null) => void;
  onClose: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
}

export function DocumentPicker({ selection, onApply, onClose, returnFocusRef }: DocumentPickerProps) {
  const panel = useRef<HTMLDivElement>(null);
  useDialogFocus(panel, returnFocusRef, onClose);
  const id = useId();
  const [all, setAll] = useState(selection?.kind === "all");
  const [picked, setPicked] = useState<PickedDocument[]>(
    selection?.kind === "documents" ? selection.documents : [],
  );
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [results, setResults] = useState<Results | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  // Search once the user pauses typing.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearch(query.trim());
    }, SEARCH_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    listDocuments({ searchable: true, q: search || undefined, limit: PAGE_SIZE }, { signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return;
        setResults({
          search,
          items: page.data,
          nextCursor: page.meta.has_more ? page.meta.next_cursor : null,
          error: null,
        });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setResults({ search, items: [], nextCursor: null, error: describeDocumentError(err) });
      });
    return () => {
      controller.abort();
    };
  }, [search]);

  const shown = results?.search === search ? results : null;

  const loadMore = () => {
    if (!shown?.nextCursor || loadingMore) return;
    const { nextCursor } = shown;
    setLoadingMore(true);
    listDocuments({ searchable: true, q: search || undefined, limit: PAGE_SIZE, cursor: nextCursor })
      .then((page) => {
        setResults((current) => {
          if (current?.search !== search) return current;
          const known = new Set(current.items.map((item) => item.id));
          return {
            ...current,
            items: [...current.items, ...page.data.filter((item) => !known.has(item.id))],
            nextCursor: page.meta.has_more ? page.meta.next_cursor : null,
          };
        });
      })
      .catch((err: unknown) => {
        setResults((current) => current && { ...current, error: describeDocumentError(err) });
      })
      .finally(() => {
        setLoadingMore(false);
      });
  };

  const pickedIds = new Set(picked.map((document) => document.id));
  const full = picked.length >= MAX_DOCUMENTS;

  const toggle = (record: DocumentRecord, on: boolean) => {
    setPicked((current) =>
      on
        ? [...current.filter((document) => document.id !== record.id), { id: record.id, name: displayName(record) }]
        : current.filter((document) => document.id !== record.id),
    );
  };

  const apply = () => {
    if (all) onApply({ kind: "all" });
    else onApply(picked.length > 0 ? { kind: "documents", documents: picked } : null);
  };

  let list: ReactNode;
  if (!shown) {
    list = <p className="muted small">Loading documents…</p>;
  } else if (shown.error && shown.items.length === 0) {
    list = <p className="picker-error small">{shown.error}</p>;
  } else if (shown.items.length === 0) {
    list = (
      <p className="muted small">
        {search ? `No searchable documents match “${search}”.` : "You have no searchable documents yet."}
      </p>
    );
  } else {
    list = (
      <>
        <ul className="picker-list" aria-label="Searchable documents">
          {shown.items.map((record) => {
            const checked = pickedIds.has(record.id);
            const searchable = record.index.searchable;
            return (
              <li key={record.id}>
                <label className="check picker-item">
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={!searchable || (full && !checked)}
                    onChange={(event) => {
                      toggle(record, event.target.checked);
                    }}
                  />
                  <span className="picker-name">{displayName(record)}</span>
                  <span className="muted small">
                    {searchable
                      ? `${formatName(record.format)} · ${formatBytes(record.size_bytes)}`
                      : "Not searchable yet"}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
        {shown.error && <p className="picker-error small">{shown.error}</p>}
        {shown.nextCursor && (
          <button type="button" className="btn btn-quiet" disabled={loadingMore} onClick={loadMore}>
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </>
    );
  }

  return (
    <div className="picker-layer">
      <div className="picker-backdrop" onClick={onClose} />
      <div
        ref={panel}
        className="picker"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        tabIndex={-1}
      >
        <h2 id={`${id}-title`}>Answer from documents</h2>
        <fieldset className="picker-scope">
          <legend className="visually-hidden">What to answer from</legend>
          <label className="check">
            <input
              type="radio"
              name={`${id}-scope`}
              checked={!all}
              onChange={() => {
                setAll(false);
              }}
            />
            Selected documents
          </label>
          <label className="check">
            <input
              type="radio"
              name={`${id}-scope`}
              checked={all}
              onChange={() => {
                setAll(true);
              }}
            />
            All documents I can access
          </label>
        </fieldset>
        <fieldset className="picker-documents" disabled={all}>
          <legend className="visually-hidden">Choose documents</legend>
          <label htmlFor={`${id}-search`} className="visually-hidden">
            Search documents
          </label>
          <input
            id={`${id}-search`}
            type="search"
            placeholder="Search documents by name"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
          />
          <div className="picker-results">{list}</div>
        </fieldset>
        <p className="muted small picker-hint">
          Documents appear here once they are indexed. <Link to="/documents">Manage documents</Link>
        </p>
        <p className="muted small" aria-live="polite">
          {all
            ? "Every document you can access will be searched."
            : `${String(picked.length)} of ${String(MAX_DOCUMENTS)} selected${full ? " (the most a question can use)" : ""}`}
        </p>
        <div className="picker-actions">
          <button type="button" className="btn btn-primary" disabled={!all && picked.length === 0} onClick={apply}>
            {all ? "Use all documents" : "Use selected documents"}
          </button>
          {selection && (
            <button
              type="button"
              className="btn btn-quiet"
              onClick={() => {
                onApply(null);
              }}
            >
              Use ordinary chat
            </button>
          )}
          <button type="button" className="btn btn-quiet" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
