/**
 * One document (`/documents/:id`): where text extraction and the search index stand (with
 * their warnings, every extraction attempt, Retry and Re-index), its details and sharing, and
 * its extracted text. The text is untrusted
 * data: it is shown as plain text, never as HTML or Markdown, one slice at a time.
 *
 * Two query parameters open a passage directly, for links from citations: `?page=N` shows that
 * page, `?offset=N` the text from that character on. "Show whole document" drops them.
 */

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { useAuth } from "../auth/AuthContext";
import { Banner } from "../components/chat/Banner";
import { formatCount, formatDuration } from "../components/chat/format";
import { Icon } from "../components/chat/Icon";
import { FormatIcon, IndexBadge, StatusBadge } from "../components/documents/badges";
import { ConfirmDelete } from "../components/documents/ConfirmDelete";
import { formatBytes } from "../components/documents/files";
import {
  describeFailure,
  describeIndexFailure,
  describeWarning,
  displayName,
  formatName,
  isInFlight,
  isIndexing,
  visibilityLabel,
} from "../components/documents/labels";
import { Alert, Field } from "../components/forms";
import { useDocument, useDocumentText, type DocumentDetail, type SaveResult } from "../hooks/useDocument";
import type {
  DocumentRecord,
  DocumentVisibility,
  Ingestion,
  UpdateDocumentBody,
} from "../types/document";

/** The server's rule for categories (documents/serializers.py). */
const CATEGORY_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
const EDITABLE_FIELDS = ["title", "category", "visibility"];

function dateTime(iso: string | null): string {
  return iso === null
    ? "—"
    : new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function plural(count: number, one: string, many: string): string {
  return `${formatCount(count)} ${count === 1 ? one : many}`;
}

/** A whole number from the query string, at least `min`; null when absent or malformed. */
function intParam(value: string | null, min: number): number | null {
  if (value === null || !/^\d{1,12}$/.test(value)) return null;
  const number = Number(value);
  return number >= min ? number : null;
}

export function DocumentDetailPage() {
  const { id = "" } = useParams();
  const { activeOrganizationId } = useAuth();
  const navigate = useNavigate();
  const shown = useRef(activeOrganizationId);

  // A document belongs to the organization it was opened in: switching leaves it.
  useEffect(() => {
    if (shown.current === activeOrganizationId) return;
    shown.current = activeOrganizationId;
    void navigate("/documents", { replace: true });
  }, [activeOrganizationId, navigate]);

  return <DocumentView key={id} id={id} />;
}

function BackLink() {
  return (
    <p className="doc-back">
      <Link to="/documents" className="doc-icon-link">
        <Icon name="back" />
        All documents
      </Link>
    </p>
  );
}

function DocumentView({ id }: { id: string }) {
  const detail = useDocument(id);
  const record = detail.record;

  if (!record) {
    if (detail.missing) {
      return (
        <div className="documents-page doc-detail">
          <BackLink />
          <h1>Document not found</h1>
          <p className="muted">
            It may have been deleted, or it is not shared with you in this organization.
          </p>
        </div>
      );
    }
    if (detail.loadError) {
      return (
        <div className="documents-page doc-detail">
          <BackLink />
          <h1>Document</h1>
          <Alert kind="error">{detail.loadError}</Alert>
          <button type="button" className="btn" onClick={detail.reload}>
            Try again
          </button>
        </div>
      );
    }
    return (
      <div className="documents-page doc-detail">
        <BackLink />
        <p className="muted">Loading document…</p>
      </div>
    );
  }

  const name = displayName(record);
  return (
    <div className="documents-page doc-detail">
      <BackLink />
      <header className="doc-detail-header">
        <FormatIcon format={record.format} />
        <div className="doc-detail-title">
          <h1>{name}</h1>
          {name !== record.filename && <p className="muted small">{record.filename}</p>}
        </div>
        <StatusBadge status={record.status} />
      </header>

      <Actions detail={detail} record={record} />
      {detail.actionError && (
        <Banner message={detail.actionError} onDismiss={detail.dismissActionError} />
      )}

      <div className="doc-detail-layout">
        <div className="doc-detail-main">
          <ProcessingCard detail={detail} record={record} />
          <TextCard record={record} />
          {record.ingestions && <HistoryCard attempts={record.ingestions} />}
        </div>
        <aside className="doc-detail-side" aria-label="Document details">
          <DetailsCard detail={detail} record={record} />
        </aside>
      </div>

      <div className="visually-hidden" aria-live="polite">
        {detail.notice}
      </div>
    </div>
  );
}

function Actions({ detail, record }: { detail: DocumentDetail; record: DocumentRecord }) {
  const navigate = useNavigate();
  const [downloading, setDownloading] = useState(false);
  const name = displayName(record);

  const download = () => {
    setDownloading(true);
    void detail.download().finally(() => {
      setDownloading(false);
    });
  };

  return (
    <div className="doc-detail-actions">
      <button type="button" className="btn doc-icon-button" disabled={downloading} onClick={download}>
        <Icon name="download" />
        {downloading ? "Downloading…" : "Download"}
      </button>
      {record.deletable && (
        <ConfirmDelete
          label={`Delete “${name}”`}
          question="Delete this document? Its file and extracted text are removed; this cannot be undone."
          onConfirm={() =>
            detail.remove().then((removed) => {
              if (removed) void navigate("/documents", { state: { deleted: name } });
              return removed;
            })
          }
        />
      )}
    </div>
  );
}

/** One pipeline's state: what it is, its badge, and a line about it. */
function StatusRow({ label, badge, children }: { label: string; badge: ReactNode; children: ReactNode }) {
  return (
    <div className="doc-status-row">
      <dt>{label}</dt>
      <dd>
        {badge}
        <span className="muted small">{children}</span>
      </dd>
    </div>
  );
}

function indexSummary(record: DocumentRecord): string {
  const index = record.index;
  const passages = `${plural(index.chunk_count, "passage", "passages")}, about ${plural(index.token_estimate, "token", "tokens")}`;
  switch (index.status) {
    case "none":
      return record.has_text ? "Not indexed yet." : "Indexing starts once the text is extracted.";
    case "pending":
      return "Waiting for its turn.";
    case "indexing":
      return `Started ${dateTime(index.started_at)}.`;
    case "indexed":
      return `${passages}; indexed ${dateTime(index.indexed_at)}.`;
    case "stale":
      return `${passages}. Built by an older method: still searchable, and re-indexing rebuilds it.`;
    case "failed":
      return index.searchable
        ? `Stopped ${dateTime(index.finished_at)}; the previous index can still be searched.`
        : `Stopped ${dateTime(index.finished_at)}.`;
  }
}

function extractionSummary(record: DocumentRecord): string {
  const attempt = record.attempt_count > 1 ? ` (attempt ${String(record.attempt_count)})` : "";
  switch (record.status) {
    case "uploaded":
    case "queued":
      return `Waiting for its turn${attempt}.`;
    case "processing":
      return `Started ${dateTime(record.processing_started_at)}${attempt}.`;
    case "completed":
      return `Finished ${dateTime(record.processed_at)}${attempt}.`;
    default:
      return `Stopped ${dateTime(record.processed_at)}${attempt}.`;
  }
}

function Warnings({ title, warnings }: { title: string; warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <>
      <h3>{title}</h3>
      <ul className="doc-warnings">
        {warnings.map((warning, index) => (
          <li key={`${String(index)}:${warning}`}>{describeWarning(warning)}</li>
        ))}
      </ul>
    </>
  );
}

function ProcessingCard({ detail, record }: { detail: DocumentDetail; record: DocumentRecord }) {
  const [retrying, setRetrying] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const failure = describeFailure(record);
  const indexFailure = describeIndexFailure(record.index);

  const retry = () => {
    setRetrying(true);
    void detail.retry().then((retried) => {
      setRetrying(false);
      // The Retry button goes away once processing is queued: keep focus in this card.
      if (retried) heading.current?.focus();
    });
  };

  const reindex = () => {
    setReindexing(true);
    void detail.reindex().then((queued) => {
      setReindexing(false);
      // Re-index is disabled while the index is built: keep focus in this card.
      if (queued) heading.current?.focus();
    });
  };

  return (
    <section className="card section doc-processing" aria-labelledby="doc-processing-title">
      <h2 id="doc-processing-title" ref={heading} tabIndex={-1}>
        Processing
      </h2>
      <dl className="doc-status-rows">
        <StatusRow label="Text extraction" badge={<StatusBadge status={record.status} />}>
          {extractionSummary(record)}
        </StatusRow>
        <StatusRow label="Search index" badge={<IndexBadge status={record.index.status} />}>
          {indexSummary(record)}
        </StatusRow>
      </dl>
      {failure && <p className="doc-failure">{failure}</p>}
      {indexFailure && <p className="doc-failure">{indexFailure}</p>}
      {(record.can_retry || record.can_reindex) && (
        <div className="doc-processing-actions">
          {record.can_retry && (
            <button type="button" className="btn" disabled={retrying} onClick={retry}>
              {retrying ? "Retrying…" : "Retry processing"}
            </button>
          )}
          {record.can_reindex && (
            <button
              type="button"
              className="btn"
              disabled={reindexing || isIndexing(record.index.status)}
              onClick={reindex}
            >
              {reindexing || isIndexing(record.index.status) ? "Indexing…" : "Re-index"}
            </button>
          )}
        </div>
      )}
      <Warnings title="Extraction warnings" warnings={record.warnings} />
      <Warnings title="Index warnings" warnings={record.index.warnings} />
    </section>
  );
}

function TextCard({ record }: { record: DocumentRecord }) {
  const [params, setParams] = useSearchParams();
  const page = intParam(params.get("page"), 1);
  const offset = page === null ? (intParam(params.get("offset"), 0) ?? 0) : 0;
  const focused = page !== null || offset > 0;
  const text = useDocumentText(record.id, record.has_text, page, offset);
  const block = useRef<HTMLPreElement>(null);
  const shown = text.shown;
  const shownKey = shown?.key ?? null;

  // A new passage starts at the top of the text block; a linked one is also brought into view.
  useEffect(() => {
    const element = block.current;
    if (shownKey === null || !element) return;
    element.scrollTop = 0;
    if (focused && "scrollIntoView" in element) element.scrollIntoView({ block: "nearest" });
  }, [shownKey, focused]);

  const pageCount = Math.max(record.page_count ?? 0, ...(shown?.pages.map((span) => span.page) ?? []));
  const goToPage = (target: number) => {
    setParams({ page: String(target) });
  };
  const showWhole = () => {
    setParams({});
  };

  let body: ReactNode;
  if (!record.has_text) {
    body = (
      <p className="muted">
        {isInFlight(record.status)
          ? "The text appears here once processing finishes."
          : "No text could be extracted from this file."}
      </p>
    );
  } else if (!shown) {
    body = <p className="muted">Loading text…</p>;
  } else if (shown.error) {
    body = (
      <>
        <Alert kind="error">{shown.error}</Alert>
        <button type="button" className="btn" onClick={text.reload}>
          Try again
        </button>
      </>
    );
  } else {
    let position: string;
    if (page !== null) {
      position = pageCount >= page ? `Page ${String(page)} of ${String(pageCount)}` : `Page ${String(page)}`;
    } else if (shown.start === 0 && shown.nextOffset === null) {
      position = plural(shown.totalChars, "character", "characters");
    } else {
      const end = shown.nextOffset ?? shown.totalChars;
      position = `Characters ${formatCount(shown.start + 1)}–${formatCount(end)} of ${formatCount(shown.totalChars)}`;
    }
    body = (
      <>
        <div className="doc-text-tools">
          <p className="muted small doc-text-position">{position}</p>
          {pageCount > 1 && (
            <PageJump key={page ?? 0} page={page} pageCount={pageCount} onGo={goToPage} />
          )}
        </div>
        {shown.text === "" ? (
          <p className="muted">{page === null ? "This document has no text." : "This page has no text."}</p>
        ) : (
          <pre ref={block} className="doc-text" tabIndex={0} role="region" aria-label="Document text">
            {shown.text}
          </pre>
        )}
        {shown.moreError && <Alert kind="error">{shown.moreError}</Alert>}
        {page === null && shown.nextOffset !== null && (
          <button type="button" className="btn" disabled={text.loadingMore} onClick={text.loadMore}>
            {text.loadingMore ? "Loading…" : "Load more text"}
          </button>
        )}
      </>
    );
  }

  return (
    <section className="card section doc-text-card" aria-labelledby="doc-text-title">
      <div className="doc-card-header">
        <h2 id="doc-text-title">Extracted text</h2>
        {focused && (
          <button type="button" className="btn btn-quiet" onClick={showWhole}>
            Show whole document
          </button>
        )}
      </div>
      {body}
    </section>
  );
}

function PageJump({
  page,
  pageCount,
  onGo,
}: {
  page: number | null;
  pageCount: number;
  onGo: (page: number) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState(String(page ?? 1));
  const target = Number(draft);
  const valid = Number.isInteger(target) && target >= 1 && target <= pageCount;
  return (
    <form
      className="doc-page-jump"
      aria-label="Pages"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) onGo(target);
      }}
    >
      {page !== null && (
        <button
          type="button"
          className="btn btn-quiet"
          disabled={page <= 1}
          onClick={() => {
            onGo(page - 1);
          }}
        >
          Previous page
        </button>
      )}
      <label htmlFor={`${id}-page`} className="small">
        Go to page
      </label>
      <input
        id={`${id}-page`}
        type="number"
        inputMode="numeric"
        min={1}
        max={pageCount}
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
        }}
      />
      <span className="muted small">of {pageCount}</span>
      <button type="submit" className="btn" disabled={!valid}>
        Go
      </button>
      {page !== null && (
        <button
          type="button"
          className="btn btn-quiet"
          disabled={page >= pageCount}
          onClick={() => {
            onGo(page + 1);
          }}
        >
          Next page
        </button>
      )}
    </form>
  );
}

function attemptResult(attempt: Ingestion): string {
  const failure = describeFailure(attempt);
  if (failure) return failure;
  if (attempt.status === "cancelled") return "Abandoned for a newer attempt.";
  if (attempt.status !== "completed") return "—";
  const parts = [plural(attempt.word_count, "word", "words")];
  if (attempt.page_count !== null) parts.push(plural(attempt.page_count, "page", "pages"));
  if (attempt.warnings.length > 0) parts.push(plural(attempt.warnings.length, "warning", "warnings"));
  return parts.join(", ");
}

function HistoryCard({ attempts }: { attempts: Ingestion[] }) {
  return (
    <section className="card section" aria-labelledby="doc-history-title">
      <h2 id="doc-history-title">Processing history</h2>
      {attempts.length === 0 ? (
        <p className="muted small">No attempts yet.</p>
      ) : (
        <div className="doc-table-scroll" tabIndex={0} role="region" aria-labelledby="doc-history-title">
          <table className="table doc-history">
            <thead>
              <tr>
                <th scope="col">Attempt</th>
                <th scope="col">Status</th>
                <th scope="col">Started</th>
                <th scope="col">Took</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {attempts.map((attempt) => (
                <tr key={attempt.id}>
                  <td>{attempt.attempt}</td>
                  <td>
                    <StatusBadge status={attempt.status} />
                  </td>
                  <td>{dateTime(attempt.started_at ?? attempt.created_at)}</td>
                  <td>{attempt.finished_at ? formatDuration(attempt.duration_ms) : "—"}</td>
                  <td>{attemptResult(attempt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="doc-fact">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function DetailsCard({ detail, record }: { detail: DocumentDetail; record: DocumentRecord }) {
  const [editing, setEditing] = useState(false);
  const editButton = useRef<HTMLButtonElement>(null);
  /** The form closed: focus returns to Edit. */
  const refocus = useRef(false);

  useEffect(() => {
    if (editing || !refocus.current) return;
    refocus.current = false;
    editButton.current?.focus();
  }, [editing]);

  const finish = () => {
    refocus.current = true;
    setEditing(false);
  };

  let visibleTo = visibilityLabel(record.visibility);
  if (record.visibility === "private") visibleTo = "Private: only the owner";
  else if (record.visibility === "organization") visibleTo = "Everyone in the organization";
  else if (record.team_id) visibleTo = `Team ${record.team_id.slice(0, 8)}`;

  return (
    <section className="card section doc-details" aria-labelledby="doc-details-title">
      <div className="doc-card-header">
        <h2 id="doc-details-title">Details</h2>
        {record.editable && !editing && (
          <button
            ref={editButton}
            type="button"
            className="btn btn-quiet"
            onClick={() => {
              setEditing(true);
            }}
          >
            Edit
          </button>
        )}
      </div>
      {editing && <EditForm record={record} onSave={detail.save} onDone={finish} />}
      <dl className="doc-facts">
        <Fact term="File name">{record.filename}</Fact>
        {record.title && <Fact term="Title">{record.title}</Fact>}
        <Fact term="Format">{formatName(record.format)}</Fact>
        <Fact term="Size">{formatBytes(record.size_bytes)}</Fact>
        <Fact term="SHA-256">
          <code title={record.sha256}>{record.sha256.slice(0, 12)}…</code>
        </Fact>
        <Fact term="Owner">{record.owner?.email ?? "Unknown"}</Fact>
        <Fact term="Visible to">{visibleTo}</Fact>
        <Fact term="Category">{record.category || "None"}</Fact>
        <Fact term="Uploaded">{dateTime(record.created_at)}</Fact>
        <Fact term="Processed">{dateTime(record.processed_at)}</Fact>
        <Fact term="Pages">{record.page_count === null ? "—" : formatCount(record.page_count)}</Fact>
        <Fact term="Words">{formatCount(record.word_count)}</Fact>
        <Fact term="Characters">{formatCount(record.char_count)}</Fact>
      </dl>
    </section>
  );
}

interface Problem {
  /** For the form as a whole; null when every error belongs to a field. */
  message: string | null;
  fields: Partial<Record<string, string>>;
}

function EditForm({
  record,
  onSave,
  onDone,
}: {
  record: DocumentRecord;
  onSave: (body: UpdateDocumentBody) => Promise<SaveResult>;
  onDone: () => void;
}) {
  const [title, setTitle] = useState(record.title);
  const [category, setCategory] = useState(record.category);
  const [visibility, setVisibility] = useState<DocumentVisibility>(record.visibility);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);

  const submit = () => {
    const nextTitle = title.trim();
    const nextCategory = category.trim();
    if (nextCategory && !CATEGORY_PATTERN.test(nextCategory)) {
      setProblem({
        message: null,
        fields: {
          category: "Use lowercase letters, digits, - and _, starting with a letter or a digit.",
        },
      });
      return;
    }
    const body: UpdateDocumentBody = {};
    if (nextTitle !== record.title) body.title = nextTitle;
    if (nextCategory !== record.category) body.category = nextCategory;
    if (visibility !== record.visibility) body.visibility = visibility;
    if (Object.keys(body).length === 0) {
      onDone();
      return;
    }
    setBusy(true);
    setProblem(null);
    void onSave(body).then((result) => {
      setBusy(false);
      if (result.ok) {
        onDone();
        return;
      }
      const onField = Object.keys(result.fields).some((field) => EDITABLE_FIELDS.includes(field));
      setProblem({ message: onField ? null : result.message, fields: result.fields });
    });
  };

  return (
    <form
      className="doc-edit"
      aria-label="Edit details"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || busy) return;
        event.preventDefault();
        onDone();
      }}
    >
      {problem?.message && <Alert kind="error">{problem.message}</Alert>}
      <Field label="Title" error={problem?.fields.title}>
        <input
          value={title}
          maxLength={200}
          placeholder={record.filename}
          autoFocus
          onChange={(event) => {
            setTitle(event.target.value);
          }}
        />
      </Field>
      <Field label="Category" error={problem?.fields.category}>
        <input
          value={category}
          maxLength={32}
          placeholder="For example: contracts"
          autoCapitalize="none"
          spellCheck={false}
          onChange={(event) => {
            setCategory(event.target.value);
          }}
        />
      </Field>
      {record.visibility === "team" ? (
        <div className="field">
          <span className="field-label">Visible to</span>
          <p className="doc-readonly">Team members only. Team sharing cannot be changed here.</p>
        </div>
      ) : (
        <Field label="Visible to" error={problem?.fields.visibility}>
          <select
            value={visibility}
            onChange={(event) => {
              setVisibility(event.target.value as DocumentVisibility);
            }}
          >
            <option value="private">Private: only you</option>
            <option value="organization">Everyone in the organization</option>
          </select>
        </Field>
      )}
      <div className="actions">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" className="btn btn-quiet" disabled={busy} onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}
