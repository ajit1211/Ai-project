/**
 * The result of one tool call, as the model received it. NEXA's built-in tools get a
 * readable form (the calculation, the time, documents and passages with links to them, the
 * workspace, the saved memory); any other tool, or a result that does not have the shape its
 * tool promises, is shown as its JSON. Results are untrusted text: everything is rendered as
 * plain text, never as HTML or Markdown.
 */

import type { ReactNode } from "react";
import { Link } from "react-router";

import type { ToolActivity } from "../../types/tools";
import { statusLabel } from "../documents/labels";
import { formatCount } from "./format";
import { lookup } from "./tools";

type Json = Record<string, unknown>;

const isRecord = (value: unknown): value is Json =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** A non-empty string, or null. */
const text = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

const number = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/** An array of objects, or null. */
const records = (value: unknown): Json[] | null =>
  Array.isArray(value) && value.every(isRecord) ? value : null;

function plural(count: number, one: string, many = `${one}s`): string {
  return `${formatCount(count)} ${count === 1 ? one : many}`;
}

/** "p. 3", "p. 3–4", or null. */
function pages(start: number | null, end: number | null): string | null {
  if (start === null) return null;
  return end !== null && end !== start ? `p. ${String(start)}–${String(end)}` : `p. ${String(start)}`;
}

const documentPath = (id: string): string => `/documents/${encodeURIComponent(id)}`;

/** One item of a fact list (`dl`). */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function calculatorResult(result: Json): ReactNode {
  const expression = text(result.expression);
  const value = number(result.result);
  const answer = text(result.result_text) ?? (value === null ? null : String(value));
  if (expression === null || answer === null) return null;
  return (
    <p className="tool-calculation">
      <code>{expression}</code> = <strong>{answer}</strong>
    </p>
  );
}

function timeResult(result: Json): ReactNode {
  const utc = text(result.utc);
  if (utc === null) return null;
  const weekday = text(result.weekday);
  const local = isRecord(result.local) ? result.local : null;
  const localTime = local ? text(local.time) : null;
  const localWeekday = local ? text(local.weekday) : null;
  return (
    <dl className="tool-facts">
      <Fact label="UTC">{weekday ? `${weekday}, ${utc}` : utc}</Fact>
      {local && localTime && (
        <Fact label={text(local.timezone) ?? "Local time"}>
          {localWeekday ? `${localWeekday}, ${localTime}` : localTime}
        </Fact>
      )}
    </dl>
  );
}

function documentsResult(result: Json): ReactNode {
  const documents = records(result.documents);
  if (!documents) return null;
  const total = number(result.total);
  const notFound = number(result.not_found);
  return (
    <>
      {documents.length === 0 ? (
        <p className="tool-note">No documents.</p>
      ) : (
        <ul className="tool-list" aria-label="Documents">
          {documents.map((document, index) => {
            const id = text(document.id);
            const name = text(document.title) ?? text(document.filename) ?? "Untitled document";
            const format = text(document.format);
            const status = text(document.status);
            const pageCount = number(document.page_count);
            const words = number(document.word_count);
            const facts = [
              format?.toUpperCase(),
              status === null ? null : statusLabel(status),
              pageCount === null ? null : plural(pageCount, "page"),
              words === null ? null : plural(words, "word"),
              document.searchable === false ? "not searchable yet" : null,
            ].filter((fact): fact is string => Boolean(fact));
            return (
              <li key={id ?? index}>
                {id ? <Link to={documentPath(id)}>{name}</Link> : <span>{name}</span>}
                {facts.length > 0 && <span className="muted small"> · {facts.join(" · ")}</span>}
              </li>
            );
          })}
        </ul>
      )}
      {result.more === true && (
        <p className="tool-note">
          {total === null
            ? "More documents match than are listed."
            : `Listed ${formatCount(documents.length)} of ${plural(total, "document")}.`}
        </p>
      )}
      {notFound !== null && notFound > 0 && (
        <p className="tool-note">
          {notFound === 1
            ? "1 requested document was not found or is not available to you."
            : `${formatCount(notFound)} requested documents were not found or are not available to you.`}
        </p>
      )}
    </>
  );
}

function searchResult(result: Json): ReactNode {
  const query = text(result.query);
  const results = records(result.results);
  if (query === null || !results) return null;
  const searched = number(result.searched_documents);
  const note = text(result.note);
  return (
    <>
      <p className="tool-note">
        {searched === null
          ? `Searched for “${query}”.`
          : `Searched ${plural(searched, "document")} for “${query}”.`}
      </p>
      {results.length > 0 && (
        <ol className="tool-list tool-passages" aria-label="Passages found">
          {results.map((hit, index) => {
            const id = text(hit.document_id);
            const name = text(hit.title) ?? text(hit.filename) ?? "Untitled document";
            const page = number(hit.page_start);
            const where = [pages(page, number(hit.page_end)), text(hit.section)].filter(Boolean).join(", ");
            const excerpt = text(hit.excerpt);
            const href = id ? `${documentPath(id)}${page === null ? "" : `?page=${String(page)}`}` : null;
            return (
              <li key={text(hit.chunk_id) ?? index} className="tool-passage">
                <div className="tool-passage-head">
                  {href ? <Link to={href}>{name}</Link> : <span>{name}</span>}
                  {where && <span className="muted small">{where}</span>}
                </div>
                {excerpt && <p className="tool-excerpt">{excerpt}</p>}
                {hit.excerpt_truncated === true && <p className="muted small">Excerpt shortened.</p>}
              </li>
            );
          })}
        </ol>
      )}
      {note && <p className="tool-note">{note}</p>}
    </>
  );
}

function workspaceResult(result: Json): ReactNode {
  const organization = isRecord(result.organization) ? text(result.organization.name) : null;
  if (organization === null) return null;
  const role = isRecord(result.role) ? text(result.role.name) : null;
  const teams = (records(result.teams) ?? []).map((team) => text(team.name)).filter(Boolean);
  const members = number(result.member_count);
  return (
    <dl className="tool-facts">
      <Fact label="Organization">{organization}</Fact>
      {role && <Fact label="Your role">{role}</Fact>}
      <Fact label="Your teams">{teams.length > 0 ? teams.join(", ") : "None"}</Fact>
      {members !== null && <Fact label="Members">{formatCount(members)}</Fact>}
    </dl>
  );
}

function memoryResult(result: Json): ReactNode {
  const content = text(result.content);
  if (content === null) return null;
  const kind = text(result.kind);
  return (
    <>
      <p className="tool-quote">{content}</p>
      <p className="tool-note">
        {kind ? `Saved to your memories as a ${kind}. ` : "Saved to your memories. "}
        <Link to="/memories">Manage your memories</Link>
      </p>
    </>
  );
}

/** The readable form of each built-in tool's result; null when the result is not that shape. */
const BUILT_IN: Readonly<Record<string, (result: Json) => ReactNode>> = {
  calculator: calculatorResult,
  current_time: timeResult,
  document_metadata: documentsResult,
  search_documents: searchResult,
  workspace_info: workspaceResult,
  save_memory: memoryResult,
};

/** Any result, as indented JSON in plain text. */
function JsonResult({ value }: { value: unknown }) {
  return (
    <pre className="tool-json">
      <code>{JSON.stringify(value, null, 2)}</code>
    </pre>
  );
}

export function ToolResult({ activity }: { activity: ToolActivity }) {
  const { result } = activity;
  if (result === null) return null;
  const render = lookup(BUILT_IN, activity.tool_id);
  const readable = render ? render(result) : null;
  return (
    <div className="tool-result">
      {readable ?? <JsonResult value={result} />}
      {activity.result_truncated && (
        <p className="tool-note">This result was too long and was truncated before the model read it.</p>
      )}
    </div>
  );
}
