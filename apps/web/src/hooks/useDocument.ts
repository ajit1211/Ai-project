/**
 * One document's page state. `useDocument` holds the record (polled while it is processed or
 * indexed) and the actions on it; `useDocumentText` holds its extracted text: the first slice and the
 * ones appended after it, a slice from a given character, or a single page. The text is
 * untrusted data and stays a plain string all the way to the screen.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../api/client";
import {
  deleteDocument,
  downloadDocument,
  getDocument,
  getDocumentContent,
  requestReindex,
  retryDocument,
  updateDocument,
} from "../api/documents";
import { describeDocumentError } from "../components/documents/labels";
import type {
  ContentRequest,
  DocumentRecord,
  PageSpan,
  UpdateDocumentBody,
} from "../types/document";
import { changeNotice, useProcessingPoll } from "./useDocuments";

interface RecordState {
  /** Which request produced this state (the id and the reload count). */
  key: string;
  record: DocumentRecord | null;
  /** The document does not exist, or the caller cannot see it. */
  missing: boolean;
  error: string | null;
}

/** A newer record keeps the attempts it came without (PATCH, retry and status answers omit them). */
function keepAttempts(next: DocumentRecord, previous: DocumentRecord | null): DocumentRecord {
  return next.ingestions || !previous?.ingestions ? next : { ...next, ingestions: previous.ingestions };
}

export type SaveResult =
  | { ok: true }
  | { ok: false; message: string; fields: Partial<Record<string, string>> };

export function useDocument(id: string) {
  const [state, setState] = useState<RecordState | null>(null);
  const [reloads, setReloads] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const key = `${id}|${String(reloads)}`;
    const controller = new AbortController();
    getDocument(id, { signal: controller.signal })
      .then((record) => {
        if (controller.signal.aborted) return;
        setState({ key, record, missing: false, error: null });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const missing = err instanceof ApiError && err.status === 404;
        setState({ key, record: null, missing, error: missing ? null : describeDocumentError(err) });
      });
    return () => {
      controller.abort();
    };
  }, [id, reloads]);

  const record = state?.record ?? null;

  const replace = (next: DocumentRecord) => {
    setState((current) =>
      current?.record ? { ...current, record: keepAttempts(next, current.record) } : current,
    );
  };

  useProcessingPoll(record ? [record] : [], (outcomes) => {
    for (const outcome of outcomes) {
      if (outcome.kind === "gone") {
        setState((current) => current && { ...current, record: null, missing: true });
        continue;
      }
      replace(outcome.document);
      const change = changeNotice(outcome.previous, outcome.document);
      if (change) setNotice(change);
    }
  });

  const reload = useCallback(() => {
    setReloads((count) => count + 1);
  }, []);

  const save = async (body: UpdateDocumentBody): Promise<SaveResult> => {
    setActionError(null);
    try {
      replace(await updateDocument(id, body));
      setNotice("Changes saved.");
      return { ok: true };
    } catch (err) {
      const fields: Partial<Record<string, string>> = {};
      if (err instanceof ApiError) {
        for (const detail of err.details) fields[detail.field] = detail.message;
      }
      return { ok: false, message: describeDocumentError(err), fields };
    }
  };

  const retry = async (): Promise<boolean> => {
    setActionError(null);
    try {
      const queued = await retryDocument(id);
      // Fetched again for the attempts list, which now has the new attempt.
      replace(await getDocument(id).catch(() => queued));
      setNotice("Processing has been queued again.");
      return true;
    } catch (err) {
      setActionError(describeDocumentError(err));
      return false;
    }
  };

  const reindex = async (): Promise<boolean> => {
    setActionError(null);
    try {
      const index = await requestReindex(id);
      setState((current) =>
        current?.record ? { ...current, record: { ...current.record, index } } : current,
      );
      setNotice("Indexing has been queued.");
      return true;
    } catch (err) {
      setActionError(describeDocumentError(err));
      return false;
    }
  };

  const remove = async (): Promise<boolean> => {
    setActionError(null);
    try {
      await deleteDocument(id);
      return true;
    } catch (err) {
      setActionError(describeDocumentError(err));
      return false;
    }
  };

  const download = async (): Promise<void> => {
    if (!record) return;
    setActionError(null);
    try {
      await downloadDocument(record);
      setNotice(`Downloading ${record.filename}.`);
    } catch (err) {
      setActionError(describeDocumentError(err));
    }
  };

  const current = state?.key === `${id}|${String(reloads)}` ? state : null;
  return {
    record,
    loading: current === null,
    missing: current?.missing === true,
    /** Why the document could not be loaded (not found is `missing`). */
    loadError: current?.error ?? null,
    reload,
    actionError,
    dismissActionError: () => {
      setActionError(null);
    },
    save,
    retry,
    reindex,
    remove,
    download,
    /** The last outcome, for the live region. */
    notice,
  };
}

export type DocumentDetail = ReturnType<typeof useDocument>;

// --- The extracted text -----------------------------------------------------------------------

interface TextState {
  /** Which request produced this state; see `textKey`. */
  key: string;
  text: string;
  /** Where `text` starts in the whole document. */
  start: number;
  /** Where the next slice starts; null when the text shown runs to the end (or is one page). */
  nextOffset: number | null;
  totalChars: number;
  /** The pages the text shown overlaps. */
  pages: PageSpan[];
  /** The first request failed: there is no text to show. */
  error: string | null;
  /** "Load more" failed; the text shown stays. */
  moreError: string | null;
}

const textKey = (id: string, page: number | null, offset: number, reloads: number): string =>
  JSON.stringify([id, page, offset, reloads]);

function mergePages(known: PageSpan[], more: PageSpan[]): PageSpan[] {
  const seen = new Set(known.map((span) => span.page));
  return [...known, ...more.filter((span) => !seen.has(span.page))];
}

/**
 * The text of a document that has some (`ready`): one page when `page` is set, otherwise the
 * slice starting at `offset` (0 for the beginning), extended slice by slice with `loadMore`.
 */
export function useDocumentText(id: string, ready: boolean, page: number | null, offset: number) {
  const [state, setState] = useState<TextState | null>(null);
  const [reloads, setReloads] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => {
    if (!ready) return;
    const key = textKey(id, page, offset, reloads);
    const controller = new AbortController();
    let request: ContentRequest = {};
    if (page !== null) request = { page };
    else if (offset > 0) request = { offset };
    getDocumentContent(id, request, { signal: controller.signal })
      .then((slice) => {
        if (controller.signal.aborted) return;
        setState({
          key,
          text: slice.content,
          start: slice.offset,
          nextOffset: slice.has_more ? slice.next_offset : null,
          totalChars: slice.total_chars,
          pages: slice.pages,
          error: null,
          moreError: null,
        });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        // The document was just loaded, so a 404 here is about the page asked for.
        const noSuchPage = page !== null && err instanceof ApiError && err.status === 404;
        setState({
          key,
          text: "",
          start: 0,
          nextOffset: null,
          totalChars: 0,
          pages: [],
          error: noSuchPage ? `This document has no page ${String(page)}.` : describeDocumentError(err),
          moreError: null,
        });
      });
    return () => {
      controller.abort();
    };
  }, [id, ready, page, offset, reloads]);

  const shown = ready && state?.key === textKey(id, page, offset, reloads) ? state : null;

  const loadMore = () => {
    if (!shown || shown.nextOffset === null || loadingMore) return;
    const { key, nextOffset } = shown;
    setLoadingMore(true);
    getDocumentContent(id, { offset: nextOffset })
      .then((slice) => {
        setState((current) => {
          if (current?.key !== key) return current;
          return {
            ...current,
            text: current.text + slice.content,
            nextOffset: slice.has_more ? slice.next_offset : null,
            totalChars: slice.total_chars,
            pages: mergePages(current.pages, slice.pages),
            moreError: null,
          };
        });
      })
      .catch((err: unknown) => {
        setState((current) =>
          current?.key === key ? { ...current, moreError: describeDocumentError(err) } : current,
        );
      })
      .finally(() => {
        setLoadingMore(false);
      });
  };

  return {
    /** The text shown, or null while it loads (or while the document has none). */
    shown,
    loading: ready && shown === null,
    loadingMore,
    loadMore,
    reload: () => {
      setReloads((count) => count + 1);
    },
  };
}
