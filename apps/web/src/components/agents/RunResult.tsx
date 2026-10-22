/**
 * A completed task's result: the answer (the model's Markdown, through the safe renderer)
 * with its [R1]-style citations as buttons that lead to the source they cite, the structured
 * output as formatted JSON, and the sources. A source links to its document; once the
 * document is deleted or no longer shared with the reader, it says so and shows nothing else
 * about it. A citation button brings its source into view, focuses it and highlights it for a
 * moment; a citation no source has stays plain text.
 */

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { Link } from "react-router";

import type { Run, RunSource } from "../../types/agents";
import type { Source } from "../../types/chat";
import { CitationContext } from "../chat/citations";
import { Markdown } from "../chat/Markdown";

/** How long a source stays highlighted after its citation was followed. */
const HIGHLIGHT_MS = 2500;

/** A source in the shape the citation buttons read (their spoken name: "Source R1: refunds.md"). */
function citationSource(source: RunSource, index: number): Source {
  return {
    label: source.ref,
    rank: index + 1,
    cited: true,
    available: source.available,
    score: null,
    document_id: source.available ? source.document_id : null,
    chunk_id: source.available ? source.chunk_id : null,
    document_title: source.available ? source.title || null : null,
    filename: null,
    format: null,
    section: null,
    page_start: null,
    page_end: null,
    row_start: null,
    row_end: null,
    char_start: null,
    char_end: null,
    excerpt: null,
    excerpt_truncated: false,
  };
}

export function RunResult({ run }: { run: Run }) {
  const prefix = useId();
  const [highlight, setHighlight] = useState<{ ref: string } | null>(null);
  const byRef = useMemo(
    () => new Map(run.sources.map((source, index) => [source.ref, citationSource(source, index)])),
    [run.sources],
  );
  const onCite = useCallback((ref: string) => {
    setHighlight({ ref });
  }, []);
  const citations = useMemo(() => ({ sources: byRef, onCite }), [byRef, onCite]);

  // Bring the cited source into view and focus it; the highlight then fades.
  useEffect(() => {
    if (!highlight) return;
    const item = document.getElementById(`${prefix}-source-${highlight.ref}`);
    if (item) {
      if ("scrollIntoView" in item) item.scrollIntoView({ block: "nearest", behavior: "smooth" });
      item.focus();
    }
    const timer = window.setTimeout(() => {
      setHighlight(null);
    }, HIGHLIGHT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [highlight, prefix]);

  return (
    <section className="card section run-result" aria-labelledby={`${prefix}-title`}>
      <h2 id={`${prefix}-title`}>Answer</h2>
      {run.result ? (
        <CitationContext.Provider value={citations}>
          <Markdown text={run.result} cite="R" />
        </CitationContext.Provider>
      ) : (
        !run.output && <p className="muted">The agent finished without an answer.</p>
      )}

      {run.output && (
        <>
          <h3>Structured output</h3>
          <pre className="run-output" tabIndex={0} aria-label="Structured output as JSON">
            <code>{JSON.stringify(run.output, null, 2)}</code>
          </pre>
        </>
      )}

      {run.sources.length > 0 && (
        <>
          <h3 id={`${prefix}-sources`}>Sources</h3>
          <ol className="source-list run-sources" aria-labelledby={`${prefix}-sources`}>
            {run.sources.map((source) => (
              <li
                key={source.ref}
                id={`${prefix}-source-${source.ref}`}
                tabIndex={-1}
                className={`source-card run-source${highlight?.ref === source.ref ? " highlighted" : ""}`}
              >
                <span className="source-label">{source.ref}</span>
                {source.available ? (
                  <Link to={`/documents/${encodeURIComponent(source.document_id)}`} className="source-title">
                    {source.title || "Untitled document"}
                  </Link>
                ) : (
                  <span className="muted small">No longer available</span>
                )}
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
