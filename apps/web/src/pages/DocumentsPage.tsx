/**
 * The document library (`/documents`): upload files (with the picker or by dropping them),
 * follow them through processing, and find, retry or delete documents. Each organization has
 * its own library, so switching organization starts the page afresh (and ends its uploads).
 */

import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { useAuth } from "../auth/AuthContext";
import { Banner } from "../components/chat/Banner";
import { Icon } from "../components/chat/Icon";
import { FormatIcon, SearchableMark, StatusBadge } from "../components/documents/badges";
import { ConfirmDelete } from "../components/documents/ConfirmDelete";
import { formatBytes, listExtensions } from "../components/documents/files";
import { describeFailure, displayName } from "../components/documents/labels";
import { Alert } from "../components/forms";
import {
  DEFAULT_FILTERS,
  isActiveUpload,
  useDocuments,
  type DocumentFilters,
  type DocumentLibrary,
  type UploadItem,
  type UploadState,
} from "../hooks/useDocuments";
import type { DocumentFormat, DocumentRecord, DocumentStatus } from "../types/document";

const STATUSES: { value: DocumentStatus | ""; label: string }[] = [
  { value: "", label: "All statuses" },
  { value: "completed", label: "Ready" },
  { value: "processing", label: "Processing" },
  { value: "queued", label: "Queued" },
  { value: "failed", label: "Failed" },
  { value: "needs_ocr", label: "Needs OCR" },
  { value: "unsupported", label: "Unsupported" },
];
const FORMATS: { value: DocumentFormat | ""; label: string }[] = [
  { value: "", label: "All formats" },
  { value: "pdf", label: "PDF" },
  { value: "docx", label: "Word" },
  { value: "txt", label: "Text" },
  { value: "md", label: "Markdown" },
  { value: "csv", label: "CSV" },
];
const SEARCHABLE: { value: DocumentFilters["searchable"]; label: string }[] = [
  { value: "", label: "Searchable or not" },
  { value: "true", label: "Searchable" },
  { value: "false", label: "Not searchable" },
];
const UPLOAD_STATE_TEXT: Record<UploadState, string> = {
  waiting: "Waiting",
  uploading: "Uploading",
  done: "Uploaded, processing has started",
  failed: "Failed",
  rejected: "Not uploaded",
  cancelled: "Cancelled",
};

function when(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
}

/** The name of a document deleted from its own page, handed over in the navigation state. */
function deletedName(state: unknown): string | null {
  if (typeof state !== "object" || state === null || !("deleted" in state)) return null;
  return typeof state.deleted === "string" ? state.deleted : null;
}

export function DocumentsPage() {
  const { activeOrganizationId } = useAuth();
  return <Library key={activeOrganizationId ?? "none"} />;
}

function Library() {
  const library = useDocuments();
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [deleted] = useState(() => deletedName(location.state));
  const heading = useRef<HTMLHeadingElement>(null);

  // Shown once: the history entry forgets it, so a reload does not say it again.
  useEffect(() => {
    if (deleted) void navigate("/documents", { replace: true, state: null });
  }, [deleted, navigate]);

  // A file dropped beside the drop zone must not make the browser open it instead.
  useEffect(() => {
    const guard = (event: DragEvent) => {
      if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
    };
    window.addEventListener("dragover", guard);
    window.addEventListener("drop", guard);
    return () => {
      window.removeEventListener("dragover", guard);
      window.removeEventListener("drop", guard);
    };
  }, []);

  return (
    <div className="documents-page">
      <header className="header documents-header">
        <h1 ref={heading} tabIndex={-1}>
          Documents
        </h1>
        <p className="muted">
          Upload files and NEXA extracts their text, which you can read here. Documents are
          private unless you share them with your organization.
        </p>
      </header>

      {deleted && <Alert kind="success">“{deleted}” was deleted.</Alert>}
      {library.error && (
        <Banner
          message={library.error}
          onDismiss={library.dismissError}
          onRetry={library.failed ? library.reload : undefined}
        />
      )}

      <UploadPanel library={library} />

      <section className="doc-library" aria-labelledby="doc-list-title">
        <h2 id="doc-list-title" className="visually-hidden">
          Your documents
        </h2>
        <Toolbar library={library} />
        <DocumentList library={library} userId={user?.id ?? null} heading={heading} />
      </section>

      <div className="visually-hidden" aria-live="polite">
        {library.notice}
      </div>
    </div>
  );
}

function UploadPanel({ library }: { library: DocumentLibrary }) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const hintId = useId();
  const { accepted_extensions: accepted, max_upload_bytes: maxBytes } = library.rules;

  const choose = () => {
    input.current?.click();
  };

  return (
    <section className="card section doc-upload" aria-labelledby="doc-upload-title">
      <div className="doc-upload-header">
        <h2 id="doc-upload-title">Upload</h2>
        <button type="button" className="btn btn-primary doc-icon-button" onClick={choose}>
          <Icon name="upload" />
          Upload files
        </button>
      </div>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        accept={accepted.join(",")}
        aria-label="Choose files to upload"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = ""; // so that choosing the same file again still counts
          if (files.length > 0) library.addFiles(files);
        }}
      />
      <button
        type="button"
        className={`doc-dropzone${dragging ? " dragging" : ""}`}
        aria-describedby={hintId}
        onClick={choose}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
        }}
        onDragLeave={() => {
          setDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const files = Array.from(event.dataTransfer.files);
          if (files.length > 0) library.addFiles(files);
        }}
      >
        <Icon name="upload" />
        <span>Drop files here, or choose files</span>
      </button>
      <p id={hintId} className="muted small doc-upload-hint">
        {listExtensions(accepted)}
        {maxBytes === null ? "" : `, up to ${formatBytes(maxBytes)} each`}.
      </p>
      {library.uploads.length > 0 && <UploadList library={library} />}
    </section>
  );
}

function UploadList({ library }: { library: DocumentLibrary }) {
  const finished = library.uploads.some((item) => !isActiveUpload(item));
  return (
    <div className="doc-uploads">
      <ul className="doc-upload-list" aria-label="Uploads">
        {library.uploads.map((item) => (
          <UploadRow key={item.id} item={item} library={library} />
        ))}
      </ul>
      {finished && (
        <button type="button" className="btn btn-quiet" onClick={library.clearFinishedUploads}>
          Clear finished
        </button>
      )}
    </div>
  );
}

function UploadRow({ item, library }: { item: UploadItem; library: DocumentLibrary }) {
  const active = isActiveUpload(item);
  const stateText =
    item.state === "uploading" ? `${String(item.percent)}%` : UPLOAD_STATE_TEXT[item.state];
  return (
    <li className={`doc-upload-row doc-upload-${item.state}`}>
      <div className="doc-upload-line">
        <span className="doc-upload-name">{item.name}</span>
        <span className="muted small">{formatBytes(item.size)}</span>
        <span className="doc-upload-state small">{stateText}</span>
        {item.state === "done" && item.documentId && (
          <Link to={`/documents/${item.documentId}`} className="small" aria-label={`Open ${item.name}`}>
            Open
          </Link>
        )}
        {active ? (
          <button
            type="button"
            className="btn btn-quiet"
            aria-label={`Cancel uploading ${item.name}`}
            onClick={() => {
              library.cancelUpload(item.id);
            }}
          >
            Cancel
          </button>
        ) : (
          <button
            type="button"
            className="icon-button"
            aria-label={`Dismiss ${item.name}`}
            title="Dismiss"
            onClick={() => {
              library.dismissUpload(item.id);
            }}
          >
            <Icon name="close" />
          </button>
        )}
      </div>
      {item.state === "uploading" && (
        <progress
          className="doc-progress"
          max={100}
          value={item.percent}
          aria-label={`Uploading ${item.name}`}
        />
      )}
      {item.message && <p className="doc-upload-message small">{item.message}</p>}
    </li>
  );
}

function Toolbar({ library }: { library: DocumentLibrary }) {
  const id = useId();
  const { filters, setFilters } = library;
  return (
    <div className="doc-toolbar" role="search">
      <div className="doc-search">
        <Icon name="search" />
        <label htmlFor={`${id}-q`} className="visually-hidden">
          Search documents by file name
        </label>
        <input
          id={`${id}-q`}
          type="search"
          placeholder="Search by file name"
          value={library.query}
          onChange={(event) => {
            library.setQuery(event.target.value);
          }}
        />
      </div>
      <label>
        <span className="visually-hidden">Filter by status</span>
        <select
          value={filters.status}
          onChange={(event) => {
            setFilters({ status: event.target.value as DocumentFilters["status"] });
          }}
        >
          {STATUSES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="visually-hidden">Filter by format</span>
        <select
          value={filters.format}
          onChange={(event) => {
            setFilters({ format: event.target.value as DocumentFilters["format"] });
          }}
        >
          {FORMATS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="visually-hidden">Filter by search index</span>
        <select
          value={filters.searchable}
          onChange={(event) => {
            setFilters({ searchable: event.target.value as DocumentFilters["searchable"] });
          }}
        >
          {SEARCHABLE.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label className="check doc-mine">
        <input
          type="checkbox"
          checked={filters.mine}
          onChange={(event) => {
            setFilters({ mine: event.target.checked });
          }}
        />
        Only mine
      </label>
    </div>
  );
}

function DocumentList({
  library,
  userId,
  heading,
}: {
  library: DocumentLibrary;
  userId: string | null;
  heading: RefObject<HTMLHeadingElement | null>;
}) {
  const list = useRef<HTMLUListElement>(null);
  /** After a deletion, focus goes to the document now in its place (or the heading). */
  const [focusAfterDelete, setFocusAfterDelete] = useState<{ index: number } | null>(null);

  useEffect(() => {
    if (!focusAfterDelete) return;
    const links = list.current?.querySelectorAll<HTMLAnchorElement>("a.doc-name");
    const next = links?.[Math.min(focusAfterDelete.index, links.length - 1)];
    (next ?? heading.current)?.focus();
  }, [focusAfterDelete, heading]);

  const filtered =
    library.search !== "" ||
    library.filters.status !== DEFAULT_FILTERS.status ||
    library.filters.format !== DEFAULT_FILTERS.format ||
    library.filters.searchable !== DEFAULT_FILTERS.searchable ||
    library.filters.mine !== DEFAULT_FILTERS.mine;

  let empty = null;
  if (library.loading && library.items.length === 0) {
    empty = <p className="muted doc-empty">Loading documents…</p>;
  } else if (!library.loading && library.failed) {
    empty = <p className="muted doc-empty">The documents could not be loaded.</p>;
  } else if (!library.loading && library.items.length === 0) {
    empty = filtered ? (
      <div className="doc-empty">
        <p className="muted">No documents match this search and these filters.</p>
        <button
          type="button"
          className="btn btn-quiet"
          onClick={() => {
            library.setQuery("");
            library.setFilters(DEFAULT_FILTERS);
          }}
        >
          Clear search and filters
        </button>
      </div>
    ) : (
      <div className="doc-empty">
        <p>No documents yet.</p>
        <p className="muted">
          Upload a PDF, Word, text, Markdown or CSV file above; its text is extracted in the
          background.
        </p>
      </div>
    );
  }

  return (
    <div aria-busy={library.loading}>
      {empty}
      {library.items.length > 0 && (
        <ul ref={list} className="doc-list" aria-label="Documents">
          {library.items.map((record, index) => (
            <DocumentItem
              key={record.id}
              record={record}
              mine={record.owner?.id === userId}
              library={library}
              onDeleted={() => {
                setFocusAfterDelete({ index });
              }}
            />
          ))}
        </ul>
      )}
      {library.hasMore && (
        <button
          type="button"
          className="btn doc-more"
          onClick={library.loadMore}
          disabled={library.loadingMore}
        >
          {library.loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}

function DocumentItem({
  record,
  mine,
  library,
  onDeleted,
}: {
  record: DocumentRecord;
  mine: boolean;
  library: DocumentLibrary;
  onDeleted: () => void;
}) {
  const [retrying, setRetrying] = useState(false);
  const link = useRef<HTMLAnchorElement>(null);
  const name = displayName(record);
  const failure = describeFailure(record);

  const retry = () => {
    setRetrying(true);
    void library.retry(record.id).then((retried) => {
      setRetrying(false);
      // The Retry button goes away with the failure: keep focus on the document.
      if (retried) link.current?.focus();
    });
  };

  let owner = "Unknown owner";
  if (mine) owner = "You";
  else if (record.owner) owner = record.owner.email;

  return (
    <li className="doc-item">
      <FormatIcon format={record.format} />
      <div className="doc-item-main">
        <Link ref={link} to={`/documents/${record.id}`} className="doc-name">
          {name}
        </Link>
        {name !== record.filename && <span className="doc-filename muted small">{record.filename}</span>}
        <p className="doc-item-meta muted small">
          <span>{formatBytes(record.size_bytes)}</span>
          <span>{owner}</span>
          <span>
            Uploaded <time dateTime={record.created_at}>{when(record.created_at)}</time>
          </span>
        </p>
        {failure && <p className="doc-item-failure small">{failure}</p>}
      </div>
      <div className="doc-item-status">
        <StatusBadge status={record.status} />
        {record.index.searchable && <SearchableMark />}
      </div>
      <div className="doc-item-actions">
        {record.can_retry && (
          <button
            type="button"
            className="btn btn-quiet"
            disabled={retrying}
            aria-label={`Retry processing “${name}”`}
            onClick={retry}
          >
            {retrying ? "Retrying…" : "Retry"}
          </button>
        )}
        {record.deletable && (
          <ConfirmDelete
            label={`Delete “${name}”`}
            question={`Delete “${name}”? This cannot be undone.`}
            onConfirm={() =>
              library.remove(record.id).then((removed) => {
                if (removed) onDeleted();
                return removed;
              })
            }
          />
        )}
      </div>
    </li>
  );
}
