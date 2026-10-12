/**
 * The memory manager (`/memories`): what NEXA remembers about you and your work. Candidates
 * wait here for approval; anything can be edited or deleted; automatic memory can be turned
 * off; and everything private can be cleared at once.
 */

import { useId, useState } from "react";
import { Link } from "react-router";

import { Alert, Field } from "../components/forms";
import { useMemories, type MemoryFilters } from "../hooks/useMemories";
import type { Memory, MemoryKind, MemoryScope, MemoryStatus } from "../types/memory";

const SCOPES: { value: MemoryScope | ""; label: string }[] = [
  { value: "", label: "All scopes" },
  { value: "user", label: "Mine" },
  { value: "conversation", label: "One conversation" },
  { value: "team", label: "Team" },
  { value: "organization", label: "Organization" },
];
const KINDS: { value: MemoryKind | ""; label: string }[] = [
  { value: "", label: "All kinds" },
  { value: "preference", label: "Preferences" },
  { value: "fact", label: "Facts" },
  { value: "decision", label: "Decisions" },
  { value: "episode", label: "Past conversations" },
];
const STATUSES: { value: MemoryStatus | ""; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "candidate", label: "Waiting for approval" },
  { value: "superseded", label: "Replaced" },
  { value: "expired", label: "Expired" },
  { value: "rejected", label: "Rejected" },
  { value: "", label: "Everything" },
];
const SCOPE_LABEL: Record<MemoryScope, string> = {
  conversation: "This conversation",
  user: "Private",
  team: "Team",
  organization: "Organization",
};
const SOURCE_LABEL: Record<Memory["source_type"], string> = {
  explicit: "You said so",
  inferred: "Inferred by the model",
  assistant: "Suggested by the assistant",
  manual: "Written by you",
  summary: "From a conversation summary",
};

function when(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
}

export function MemoriesPage() {
  const memories = useMemories();
  return (
    <div className="page memories-page">
      <header className="header">
        <h1>Memories</h1>
        <p className="muted">
          What NEXA remembers about you and your work, and uses to answer. Conversations are
          not memories: only selected statements are kept here, and you decide what stays.
        </p>
      </header>

      {memories.error && (
        <div className="chat-banner">
          <Alert kind="error">{memories.error}</Alert>
          <div className="chat-banner-actions">
            <button type="button" className="btn btn-quiet" onClick={memories.dismissError}>
              Dismiss
            </button>
          </div>
        </div>
      )}

      <div className="memories-layout">
        <aside className="memories-side">
          <SettingsCard memories={memories} />
          <AddMemoryCard memories={memories} />
        </aside>
        <section className="memories-main" aria-label="Memory list">
          <Toolbar filters={memories.filters} onChange={memories.setFilters} />
          <div aria-busy={memories.loading}>
            {memories.loading && memories.items.length === 0 && <p className="muted">Loading…</p>}
            {!memories.loading && memories.items.length === 0 && (
              <p className="muted memories-empty">
                Nothing here yet. Memories appear as you chat, or you can add one yourself.
              </p>
            )}
            {memories.items.length > 0 && (
              <ul className="memory-list">
                {memories.items.map((memory) => (
                  <MemoryItem key={memory.id} memory={memory} memories={memories} />
                ))}
              </ul>
            )}
            {memories.hasMore && (
              <button
                type="button"
                className="btn btn-quiet"
                onClick={memories.loadMore}
                disabled={memories.loadingMore}
              >
                {memories.loadingMore ? "Loading…" : "Load more"}
              </button>
            )}
          </div>
        </section>
      </div>
      <div className="visually-hidden" aria-live="polite">
        {memories.notice}
      </div>
    </div>
  );
}

type Memories = ReturnType<typeof useMemories>;

function Toolbar({
  filters,
  onChange,
}: {
  filters: MemoryFilters;
  onChange: (patch: Partial<MemoryFilters>) => void;
}) {
  const id = useId();
  return (
    <div className="memories-toolbar" role="search">
      <label htmlFor={`${id}-q`} className="visually-hidden">
        Search memories
      </label>
      <input
        id={`${id}-q`}
        type="search"
        placeholder="Search memories"
        value={filters.q}
        onChange={(event) => {
          onChange({ q: event.target.value });
        }}
      />
      <label>
        <span className="visually-hidden">Filter by scope</span>
        <select
          value={filters.scope}
          onChange={(event) => {
            onChange({ scope: event.target.value as MemoryFilters["scope"] });
          }}
        >
          {SCOPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="visually-hidden">Filter by kind</span>
        <select
          value={filters.kind}
          onChange={(event) => {
            onChange({ kind: event.target.value as MemoryFilters["kind"] });
          }}
        >
          {KINDS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="visually-hidden">Filter by status</span>
        <select
          value={filters.status}
          onChange={(event) => {
            onChange({ status: event.target.value as MemoryFilters["status"] });
          }}
        >
          {STATUSES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

function MemoryItem({ memory, memories }: { memory: Memory; memories: Memories }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(memory.content);
  const [busy, setBusy] = useState(false);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    if (!draft.trim()) return;
    void run(async () => {
      const updated = await memories.update(memory.id, { content: draft.trim() });
      if (updated) setEditing(false);
    });
  };

  return (
    <li className={`memory-item memory-${memory.status}`}>
      {editing ? (
        <form
          className="memory-edit"
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          <Field label="Memory">
            <textarea
              value={draft}
              rows={3}
              maxLength={2000}
              onChange={(event) => {
                setDraft(event.target.value);
              }}
            />
          </Field>
          <div className="memory-actions">
            <button type="submit" className="btn btn-primary" disabled={busy}>
              Save
            </button>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={() => {
                setDraft(memory.content);
                setEditing(false);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <p className="memory-content">{memory.content}</p>
      )}
      <div className="memory-meta">
        <span className="chip">{SCOPE_LABEL[memory.scope]}</span>
        <span className="chip">{memory.kind}</span>
        {memory.status !== "active" && <span className="chip memory-status">{memory.status}</span>}
        <span className="muted small">{SOURCE_LABEL[memory.source_type]}</span>
        {memory.source_type !== "manual" && memory.confidence < 1 && (
          <span className="muted small">confidence {Math.round(memory.confidence * 100)}%</span>
        )}
        <span className="muted small">
          added {when(memory.created_at)}
          {memory.updated_at !== memory.created_at && `, updated ${when(memory.updated_at)}`}
        </span>
        {memory.expires_at && <span className="muted small">expires {when(memory.expires_at)}</span>}
        {memory.conversation_id && (
          <Link to={`/chat/${memory.conversation_id}`} className="small">
            From a conversation
          </Link>
        )}
      </div>
      {memory.editable && !editing && (
        <div className="memory-actions">
          {memory.status === "candidate" && (
            <>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void run(() => memories.approve(memory.id))}
                aria-label={`Approve “${memory.content}”`}
              >
                Approve
              </button>
              <button
                type="button"
                className="btn btn-quiet"
                disabled={busy}
                onClick={() => void run(() => memories.reject(memory.id))}
                aria-label={`Reject “${memory.content}”`}
              >
                Reject
              </button>
            </>
          )}
          <button
            type="button"
            className="btn btn-quiet"
            disabled={busy}
            onClick={() => {
              setDraft(memory.content);
              setEditing(true);
            }}
            aria-label={`Edit “${memory.content}”`}
          >
            Edit
          </button>
          <button
            type="button"
            className="btn btn-quiet btn-danger"
            disabled={busy}
            onClick={() => {
              if (window.confirm("Delete this memory? NEXA will no longer use it.")) {
                void run(() => memories.remove(memory.id));
              }
            }}
            aria-label={`Delete “${memory.content}”`}
          >
            Delete
          </button>
        </div>
      )}
    </li>
  );
}

function SettingsCard({ memories }: { memories: Memories }) {
  const settings = memories.settings;
  return (
    <section className="card section" aria-labelledby="memory-settings-title">
      <h2 id="memory-settings-title">Automatic memory</h2>
      {settings ? (
        <>
          <label className="check">
            <input
              type="checkbox"
              checked={settings.extraction_enabled}
              onChange={(event) => {
                void memories.saveSettings({ extraction_enabled: event.target.checked });
              }}
            />
            Remember things from my conversations
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={settings.auto_save}
              disabled={!settings.extraction_enabled}
              onChange={(event) => {
                void memories.saveSettings({ auto_save: event.target.checked });
              }}
            />
            Save what I state myself without asking
          </label>
          <p className="muted small">
            Anything the model only inferred always waits for your approval. Passwords, keys,
            health, finances, identity and contact details are never remembered automatically.
          </p>
        </>
      ) : (
        <p className="muted small">Loading settings…</p>
      )}
      <button
        type="button"
        className="btn btn-danger"
        onClick={() => {
          if (
            window.confirm(
              "Delete every private memory, including those waiting for approval? Shared team and organization memories stay.",
            )
          ) {
            void memories.clear("all");
          }
        }}
      >
        Clear my memories
      </button>
    </section>
  );
}

function AddMemoryCard({ memories }: { memories: Memories }) {
  const [content, setContent] = useState("");
  const [kind, setKind] = useState<MemoryKind>("fact");
  const [busy, setBusy] = useState(false);

  const submit = () => {
    const text = content.trim();
    if (!text || busy) return;
    setBusy(true);
    void memories
      .create({ content: text, kind, scope: "user" })
      .then((created) => {
        if (created) setContent("");
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <form
      className="card section"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      aria-labelledby="add-memory-title"
    >
      <h2 id="add-memory-title">Add a memory</h2>
      <Field label="What should NEXA remember?">
        <textarea
          value={content}
          rows={3}
          maxLength={2000}
          placeholder="For example: I prefer short answers with code examples."
          onChange={(event) => {
            setContent(event.target.value);
          }}
        />
      </Field>
      <Field label="Kind">
        <select
          value={kind}
          onChange={(event) => {
            setKind(event.target.value as MemoryKind);
          }}
        >
          <option value="fact">Fact</option>
          <option value="preference">Preference</option>
          <option value="decision">Decision</option>
        </select>
      </Field>
      <button type="submit" className="btn btn-primary" disabled={busy || !content.trim()}>
        Save memory
      </button>
    </form>
  );
}
