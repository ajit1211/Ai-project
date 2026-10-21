/**
 * A reply answered from documents: a "From your documents" badge, the answer with its
 * citations as buttons, notices about how well it is supported, and its sources (the cited
 * passages as cards, the others folded under "Other passages consulted"). A citation button
 * brings its card into view, focuses it and highlights it for a moment.
 *
 * Excerpts are untrusted document text: they are shown as plain text, never as HTML or
 * Markdown. A source whose document is gone or no longer shared says so and links nowhere.
 */

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { Link } from "react-router";

import { listMessageSources } from "../../api/conversations";
import type { Retrieval } from "../../hooks/chatThread";
import type { Grounding, Message, Source } from "../../types/chat";
import { CitationContext, sourceLocation, sourceName } from "./citations";
import { Markdown } from "./Markdown";

/** How long a card stays highlighted after its citation was followed. */
const HIGHLIGHT_MS = 2500;

/** Where to read a passage in its document: its page, else its first character. */
export function documentHref(source: Source): string | null {
  if (!source.document_id) return null;
  const base = `/documents/${encodeURIComponent(source.document_id)}`;
  if (source.page_start !== null) return `${base}?page=${String(source.page_start)}`;
  if (source.char_start !== null) return `${base}?offset=${String(source.char_start)}`;
  return base;
}

/** Where the document search stands while a grounded reply has no text yet. */
export function RetrievalStatus({ retrieval }: { retrieval: Retrieval }) {
  let text: string;
  if (retrieval.state === "searching") {
    const { documents } = retrieval;
    if (documents === null) text = "Searching your documents…";
    else text = documents === 1 ? "Searching 1 document…" : `Searching ${String(documents)} documents…`;
  } else if (retrieval.found === 0) {
    text = "No matching passages found";
  } else {
    text = retrieval.found === 1 ? "Found 1 passage" : `Found ${String(retrieval.found)} passages`;
  }
  return <p className="grounding-status muted small">{text}</p>;
}

interface SourceCardProps {
  source: Source;
  id: string;
  highlighted: boolean;
  /** The whole passage, once fetched and asked for. */
  passage: string | null;
  loadingPassage: boolean;
  onExpand: () => void;
}

function SourceCard({ source, id, highlighted, passage, loadingPassage, onExpand }: SourceCardProps) {
  const className = `source-card${highlighted ? " highlighted" : ""}`;
  if (!source.available) {
    return (
      <li id={id} tabIndex={-1} className={`${className} source-unavailable`}>
        <span className="source-label">{source.label}</span>
        <p className="muted small">This source is no longer available.</p>
      </li>
    );
  }
  const name = sourceName(source);
  const where = sourceLocation(source);
  const href = documentHref(source);
  const text = passage ?? source.excerpt;
  return (
    <li id={id} tabIndex={-1} className={className}>
      <div className="source-head">
        <span className="source-label">{source.label}</span>
        <span className="source-title">{name}</span>
        {source.format && <span className="source-format">{source.format.toUpperCase()}</span>}
        {where && <span className="muted small">{where}</span>}
      </div>
      {text && <p className="source-excerpt">{text}</p>}
      <div className="source-actions">
        {source.excerpt_truncated && passage === null && (
          <button type="button" className="btn btn-quiet" disabled={loadingPassage} onClick={onExpand}>
            {loadingPassage ? "Loading…" : "Expand passage"}
          </button>
        )}
        {href && (
          <Link to={href} className="small" aria-label={`Open document: ${name}`}>
            Open document
          </Link>
        )}
      </div>
    </li>
  );
}

export interface GroundedReplyProps {
  message: Message;
  grounding: Grounding;
  streaming: boolean;
}

export function GroundedReply({ message, grounding, streaming }: GroundedReplyProps) {
  const sources = useMemo(() => message.sources ?? [], [message.sources]);
  const prefix = useId();
  const [highlight, setHighlight] = useState<{ label: string } | null>(null);
  const [othersOpen, setOthersOpen] = useState(false);
  /** Whole passages by label, from the sources endpoint; fetched once, when first asked. */
  const [passages, setPassages] = useState<Readonly<Record<string, string>> | null>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [loadingPassages, setLoadingPassages] = useState(false);
  const [passageError, setPassageError] = useState<string | null>(null);

  const byLabel = useMemo(() => new Map(sources.map((source) => [source.label, source])), [sources]);
  const onCite = useCallback(
    (label: string) => {
      if (byLabel.get(label)?.cited === false) setOthersOpen(true);
      setHighlight({ label });
    },
    [byLabel],
  );
  const citations = useMemo(() => ({ sources: byLabel, onCite }), [byLabel, onCite]);

  // Bring the cited card into view and focus it; the highlight then fades.
  useEffect(() => {
    if (!highlight) return;
    const card = document.getElementById(`${prefix}-source-${highlight.label}`);
    if (card) {
      if ("scrollIntoView" in card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      card.focus();
    }
    const timer = window.setTimeout(() => {
      setHighlight(null);
    }, HIGHLIGHT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [highlight, prefix]);

  const expand = (label: string) => {
    setExpanded((current) => new Set(current).add(label));
    if (passages || loadingPassages) return;
    setLoadingPassages(true);
    setPassageError(null);
    listMessageSources(message.conversation_id, message.id)
      .then((whole) => {
        const found: Record<string, string> = {};
        for (const source of whole) {
          if (source.available && source.excerpt !== null) found[source.label] = source.excerpt;
        }
        setPassages(found);
      })
      .catch(() => {
        setExpanded(new Set());
        setPassageError("The whole passage could not be loaded. Try again in a moment.");
      })
      .finally(() => {
        setLoadingPassages(false);
      });
  };

  const card = (source: Source) => (
    <SourceCard
      key={source.label}
      source={source}
      id={`${prefix}-source-${source.label}`}
      highlighted={highlight?.label === source.label}
      passage={expanded.has(source.label) ? (passages?.[source.label] ?? null) : null}
      loadingPassage={loadingPassages && expanded.has(source.label)}
      onExpand={() => {
        expand(source.label);
      }}
    />
  );
  const cited = sources.filter((source) => source.cited);
  const others = sources.filter((source) => !source.cited);

  return (
    <CitationContext.Provider value={citations}>
      <p className="grounding-badge">From your documents</p>
      {message.content && <Markdown text={message.content} cite />}
      {!streaming && grounding.outcome === "uncited" && (
        <p className="grounding-note grounding-warning">
          This answer does not cite the selected documents; treat it as unverified.
        </p>
      )}
      {!streaming && grounding.outcome === "no_evidence" && (
        <p className="grounding-note">No matching passages were found in the selected documents.</p>
      )}
      {!streaming && grounding.invalid_citations.length > 0 && (
        <p className="grounding-note small">
          Some citations could not be verified and are shown as plain text.
        </p>
      )}
      {cited.length > 0 && (
        <div className="sources">
          <p className="sources-title">Sources</p>
          <ol className="source-list" aria-label="Sources">
            {cited.map(card)}
          </ol>
        </div>
      )}
      {others.length > 0 && (
        <details
          className="sources-other"
          open={othersOpen}
          onToggle={(event) => {
            setOthersOpen(event.currentTarget.open);
          }}
        >
          <summary>Other passages consulted ({others.length})</summary>
          <ol className="source-list" aria-label="Other passages consulted">
            {others.map(card)}
          </ol>
        </details>
      )}
      {passageError && <p className="grounding-note grounding-warning small">{passageError}</p>}
    </CitationContext.Provider>
  );
}
