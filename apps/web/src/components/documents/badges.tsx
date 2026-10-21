/**
 * The small marks of a document: its processing, index and semantic index status, its format,
 * whether search finds it.
 */

import { Icon } from "../chat/Icon";
import { indexLabel, indexTone, semanticLabel, semanticTone, statusLabel, statusTone } from "./labels";

/** An extraction status as a coloured badge: green when ready, red when it failed, amber while in progress. */
export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${statusTone(status)} doc-badge`}>{statusLabel(status)}</span>;
}

/** A search-index status as a badge, coloured like `StatusBadge`. */
export function IndexBadge({ status }: { status: string }) {
  return <span className={`badge badge-${indexTone(status)} doc-badge`}>{indexLabel(status)}</span>;
}

/** A semantic index status as a badge, coloured like `StatusBadge`. */
export function SemanticBadge({ status }: { status: string }) {
  return <span className={`badge badge-${semanticTone(status)} doc-badge`}>{semanticLabel(status)}</span>;
}

/** Shown on documents whose passages search can find. */
export function SearchableMark() {
  return (
    <span className="doc-searchable" title="Search can find this document's passages">
      <Icon name="search" />
      Searchable
    </span>
  );
}

/** A file icon with the format under it ("PDF", "DOCX", ...), coloured per format. */
export function FormatIcon({ format }: { format: string }) {
  return (
    <span className="doc-format" data-format={format}>
      <Icon name="file" />
      <span className="doc-format-label">{format.toUpperCase()}</span>
    </span>
  );
}
