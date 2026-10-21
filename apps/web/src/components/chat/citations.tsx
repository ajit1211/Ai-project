/**
 * Citation markers of a grounded reply ([S1], [S1][S3], [S1, S2]) as small buttons that lead
 * to the cited passage.
 *
 * `rehypeCitations` is a step of the reply's Markdown pipeline. It looks at text nodes only,
 * never inside code, pre or links, and wraps each marker in a `cite` carrier element that
 * keeps the marker's exact text. `CitationMarker` (the `cite` component) then shows a button
 * for every label the reply's sources know, and leaves anything else as the text it was: a
 * label no source has is unverified, and is never made to look verified. Nothing is parsed
 * as HTML; the carrier is a node built here, not markup from the reply.
 */

import { createContext, useContext, type ReactNode } from "react";
import type { ExtraProps } from "react-markdown";

import type { Source } from "../../types/chat";

type HastElement = NonNullable<ExtraProps["node"]>;
type HastChild = HastElement["children"][number];
interface HastParent {
  children: HastChild[];
}

const MARKER = /\[(S\d+(?:\s*,\s*S\d+)*)\]/g;
/** Code shows markers as typed; links and buttons cannot hold another button. */
const SKIPPED: ReadonlySet<string> = new Set(["code", "pre", "a", "button"]);

function split(value: string): HastChild[] {
  const parts: HastChild[] = [];
  let last = 0;
  for (const match of value.matchAll(MARKER)) {
    const raw = match[0];
    const labels = (match[1] ?? "").split(",").map((label) => label.trim());
    if (match.index > last) parts.push({ type: "text", value: value.slice(last, match.index) });
    parts.push({
      type: "element",
      tagName: "cite",
      properties: { dataLabels: labels.join(" "), dataRaw: raw },
      children: [{ type: "text", value: raw }],
    });
    last = match.index + raw.length;
  }
  if (parts.length === 0) return [{ type: "text", value }];
  if (last < value.length) parts.push({ type: "text", value: value.slice(last) });
  return parts;
}

function transform(parent: HastParent): void {
  parent.children = parent.children.flatMap((child) => {
    if (child.type === "text") return split(child.value);
    if (child.type === "element" && !SKIPPED.has(child.tagName)) transform(child);
    return [child];
  });
}

/** The rehype step: citation markers in text become `cite` carriers (see the module comment). */
export function rehypeCitations() {
  return (tree: HastParent): void => {
    transform(tree);
  };
}

export interface CitationContextValue {
  /** The reply's sources, by label. */
  sources: ReadonlyMap<string, Source>;
  /** A citation was activated: show its source. */
  onCite: (label: string) => void;
}

/** Provided around a grounded reply; without it every marker stays plain text. */
export const CitationContext = createContext<CitationContextValue | null>(null);

/** The document a source comes from, as the user knows it. */
export function sourceName(source: Source): string {
  return source.document_title?.trim() || source.filename || "Untitled document";
}

function range(start: number, end: number | null): string {
  return end !== null && end !== start ? `${String(start)}–${String(end)}` : String(start);
}

/**
 * Where in its document a passage is: "p. 3–4", "rows 1–20" or the section path (short), or
 * "pages 3–4", "rows 1–20", the section (long, for spoken labels). Null when nothing is known.
 */
export function sourceLocation(source: Source, style: "short" | "long" = "short"): string | null {
  const parts: string[] = [];
  if (source.page_start !== null) {
    const pages = range(source.page_start, source.page_end);
    if (style === "short") parts.push(`p. ${pages}`);
    else parts.push(pages.includes("–") ? `pages ${pages}` : `page ${pages}`);
  } else if (source.row_start !== null) {
    const rows = range(source.row_start, source.row_end);
    parts.push(rows.includes("–") ? `rows ${rows}` : `row ${rows}`);
  }
  if (source.section) parts.push(source.section);
  return parts.length > 0 ? parts.join(", ") : null;
}

/** The spoken name of a citation button: "Source S1: Q4 report, page 3". */
export function citationLabel(source: Source): string {
  if (!source.available) return `Source ${source.label}: no longer available`;
  const where = sourceLocation(source, "long");
  return `Source ${source.label}: ${sourceName(source)}${where ? `, ${where}` : ""}`;
}

function stringProperty(node: HastElement | undefined, name: string): string {
  const value = node?.properties[name];
  return typeof value === "string" ? value : "";
}

/** The `cite` component of the Markdown renderer: buttons for known labels, text otherwise. */
export function CitationMarker({ node, children }: { node?: HastElement; children?: ReactNode }) {
  const context = useContext(CitationContext);
  const labels = stringProperty(node, "dataLabels").split(" ").filter(Boolean);
  if (!context || !labels.some((label) => context.sources.has(label))) return <>{children}</>;
  return (
    <sup className="citation">
      {labels.map((label, index) => {
        const source = context.sources.get(label);
        const key = `${String(index)}:${label}`;
        if (!source) {
          return (
            <span key={key} className="citation-unverified">
              [{label}]
            </span>
          );
        }
        return (
          <button
            key={key}
            type="button"
            className="citation-ref"
            aria-label={citationLabel(source)}
            title={citationLabel(source)}
            onClick={() => {
              context.onCite(label);
            }}
          >
            {label.replace(/^S/, "")}
          </button>
        );
      })}
    </sup>
  );
}
