/**
 * The document library's state: the filtered list with cursor paging, the upload queue with
 * per-file progress, and the polling that follows documents through processing and indexing.
 * Changes are confirmed by the server's answer and placed in the list rather than refetched.
 *
 * `useProcessingPoll` is shared with the document page (`useDocument.ts`).
 */

import { useCallback, useEffect, useEffectEvent, useRef, useState } from "react";

import { ApiError } from "../api/client";
import {
  deleteDocument,
  getDocument,
  getDocumentStatus,
  getIndexStatus,
  isAbortError,
  listDocuments,
  retryDocument,
  uploadDocument,
} from "../api/documents";
import { checkFile, DEFAULT_ACCEPTED_EXTENSIONS, type UploadRules } from "../components/documents/files";
import {
  describeDocumentError,
  displayName,
  isInFlight,
  isIndexing,
} from "../components/documents/labels";
import type {
  DocumentFormat,
  DocumentIndex,
  DocumentRecord,
  DocumentStatus,
  DocumentStatusInfo,
  ListDocumentsParams,
  UploadLimits,
} from "../types/document";

export const PAGE_SIZE = 30;
export const SEARCH_DELAY_MS = 300;
export const POLL_INTERVAL_MS = 2_500;
/** Status requests in flight at once, however many documents are being processed. */
export const MAX_PARALLEL_POLLS = 3;
export const MAX_PARALLEL_UPLOADS = 2;

// --- Polling ---------------------------------------------------------------------------------

export type PollOutcome =
  /**
   * The document's newer state (status fields or the index merged in, or the full record once
   * extraction finished), with the state it had before, for announcing what changed.
   */
  | { kind: "changed"; document: DocumentRecord; previous: DocumentRecord }
  /** It was deleted meanwhile (or is no longer visible). */
  | { kind: "gone"; id: string };

/** What to ask the server about a document: its extraction, its index, or nothing. */
type PollKind = "status" | "index";

function pollKind(record: DocumentRecord): PollKind | null {
  if (isInFlight(record.status)) return "status";
  if (isIndexing(record.index.status)) return "index";
  return null;
}

function sameIndex(a: DocumentIndex, b: DocumentIndex): boolean {
  return (
    a.status === b.status &&
    a.attempt_count === b.attempt_count &&
    a.requested_at === b.requested_at &&
    a.started_at === b.started_at &&
    a.finished_at === b.finished_at
  );
}

function withStatus(record: DocumentRecord, info: DocumentStatusInfo): DocumentRecord {
  return {
    ...record,
    status: info.status,
    error_code: info.error_code,
    error_message: info.error_message,
    attempt_count: info.attempt_count,
    processing_started_at: info.processing_started_at,
    processed_at: info.processed_at,
    updated_at: info.updated_at,
  };
}

/**
 * Follows the documents still being processed (uploaded, queued, processing) and the ones
 * whose search index is being built (pending, indexing). Every POLL_INTERVAL_MS,
 * MAX_PARALLEL_POLLS of them at most, taking turns, are asked where they stand: the status
 * endpoint during extraction, the index-status endpoint during indexing. Rounds are skipped
 * while the tab is hidden. A change is reported merged into the document; a finished
 * extraction is fetched again in full, for its counts, warnings, attempts and index (whose
 * building then starts, and is followed in turn). Polling stops when nothing is in flight
 * and when the component unmounts.
 */
export function useProcessingPoll(
  documents: readonly DocumentRecord[],
  onOutcomes: (outcomes: PollOutcome[]) => void,
): void {
  const pollKey = documents
    .flatMap((record) => {
      const kind = pollKind(record);
      return kind ? [`${record.id}:${kind}`] : [];
    })
    .join(" ");
  const known = useEffectEvent((id: string) => documents.find((record) => record.id === id));
  const report = useEffectEvent((outcomes: PollOutcome[]) => {
    onOutcomes(outcomes);
  });

  useEffect(() => {
    if (!pollKey) return;
    const targets = pollKey.split(" ").map((entry) => {
      const colon = entry.lastIndexOf(":");
      return { id: entry.slice(0, colon), kind: entry.slice(colon + 1) as PollKind };
    });
    const controller = new AbortController();
    const { signal } = controller;
    let timer = 0;
    let turn = 0;

    const pollStatus = async (id: string): Promise<PollOutcome | null> => {
      const info = await getDocumentStatus(id, { signal });
      const record = known(id);
      if (!record) return null;
      if (info.status === record.status && info.updated_at === record.updated_at) return null;
      const next = isInFlight(info.status) ? withStatus(record, info) : await getDocument(id, { signal });
      return { kind: "changed", document: next, previous: record };
    };

    const pollIndex = async (id: string): Promise<PollOutcome | null> => {
      const index = await getIndexStatus(id, { signal });
      const record = known(id);
      if (!record || sameIndex(index, record.index)) return null;
      return { kind: "changed", document: { ...record, index }, previous: record };
    };

    const pollOne = async ({ id, kind }: { id: string; kind: PollKind }) => {
      try {
        return await (kind === "status" ? pollStatus(id) : pollIndex(id));
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return { kind: "gone" as const, id };
        return null; // Cancelled, or a passing failure: the next round asks again.
      }
    };

    const round = async () => {
      if (document.visibilityState !== "hidden") {
        const batch: { id: string; kind: PollKind }[] = [];
        for (let i = 0; i < Math.min(MAX_PARALLEL_POLLS, targets.length); i += 1) {
          const target = targets[(turn + i) % targets.length];
          if (target) batch.push(target);
        }
        turn = (turn + batch.length) % targets.length;
        const outcomes = await Promise.all(batch.map(pollOne));
        if (signal.aborted) return;
        const found = outcomes.filter((outcome) => outcome !== null);
        if (found.length > 0) report(found);
      }
      if (!signal.aborted) timer = window.setTimeout(() => void round(), POLL_INTERVAL_MS);
    };

    timer = window.setTimeout(() => void round(), POLL_INTERVAL_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [pollKey]);
}

/** The sentence to announce for a change polling found, or null when it is not worth one. */
export function changeNotice(previous: DocumentRecord, next: DocumentRecord): string | null {
  const name = displayName(next);
  if (isInFlight(previous.status) && !isInFlight(next.status)) {
    if (next.status === "completed") return `${name} is ready.`;
    if (next.status === "needs_ocr") return `${name} has no text layer; it needs text recognition (OCR).`;
    return `${name} could not be processed.`;
  }
  if (isIndexing(previous.index.status) && !isIndexing(next.index.status)) {
    if (next.index.status !== "failed") return `${name} is now searchable.`;
    return next.index.searchable
      ? `Re-indexing ${name} failed; its previous index can still be searched.`
      : `${name} could not be indexed.`;
  }
  return null;
}

function applyOutcomes(items: DocumentRecord[], outcomes: readonly PollOutcome[]): DocumentRecord[] {
  let next = items;
  for (const outcome of outcomes) {
    next =
      outcome.kind === "gone"
        ? next.filter((item) => item.id !== outcome.id)
        : next.map((item) => (item.id === outcome.document.id ? outcome.document : item));
  }
  return next;
}

// --- The list ---------------------------------------------------------------------------------

export interface DocumentFilters {
  status: DocumentStatus | "";
  format: DocumentFormat | "";
  /** Whether search can find the document's passages ("" for either). */
  searchable: "" | "true" | "false";
  /** Only the caller's own uploads (`owner=me`). */
  mine: boolean;
}

export const DEFAULT_FILTERS: DocumentFilters = {
  status: "",
  format: "",
  searchable: "",
  mine: false,
};

interface ListState {
  /** Which request produced this list; see `listKey`. */
  key: string;
  filters: DocumentFilters;
  search: string;
  items: DocumentRecord[];
  nextCursor: string | null;
  /** The request failed: there is no list to show, which is not the same as an empty one. */
  failed: boolean;
}

const listKey = (filters: DocumentFilters, search: string, reloads: number): string =>
  JSON.stringify([filters.status, filters.format, filters.searchable, filters.mine, search, reloads]);

function listParams(filters: DocumentFilters, search: string): ListDocumentsParams {
  return {
    status: filters.status || undefined,
    format: filters.format || undefined,
    owner: filters.mine ? "me" : undefined,
    searchable: filters.searchable === "" ? undefined : filters.searchable === "true",
    q: search || undefined,
    limit: PAGE_SIZE,
  };
}

/** Whether a new document belongs in the list (the search matches filenames, as on the server). */
function belongs(record: DocumentRecord, state: ListState): boolean {
  if (state.filters.status && record.status !== state.filters.status) return false;
  if (state.filters.format && record.format !== state.filters.format) return false;
  if (state.filters.searchable && String(record.index.searchable) !== state.filters.searchable) {
    return false;
  }
  return !state.search || record.filename.toLowerCase().includes(state.search.toLowerCase());
}

// --- Uploads ----------------------------------------------------------------------------------

export type UploadState = "waiting" | "uploading" | "done" | "failed" | "rejected" | "cancelled";

export interface UploadItem {
  /** Local to the page: a file has no server id until it is accepted. */
  id: number;
  name: string;
  size: number;
  state: UploadState;
  /** 0 to 100. */
  percent: number;
  /** Why it was refused before sending, or why it failed. */
  message: string | null;
  /** The document the server created, once it accepted the file. */
  documentId: string | null;
}

interface UploadJob {
  file: File;
  controller: AbortController;
  /** The last percentage reported, so progress re-renders only when it moves. */
  percent: number;
}

export const isActiveUpload = (item: UploadItem): boolean =>
  item.state === "waiting" || item.state === "uploading";

const NO_DOCUMENTS: DocumentRecord[] = [];

export function useDocuments() {
  const [filters, setFiltersState] = useState<DocumentFilters>(DEFAULT_FILTERS);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [reloads, setReloads] = useState(0);
  const [list, setList] = useState<ListState | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [limits, setLimits] = useState<UploadLimits | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const jobs = useRef(new Map<number, UploadJob>());
  const queue = useRef<number[]>([]);
  const running = useRef(0);
  const lastUploadId = useRef(0);
  const closed = useRef(false);

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
    const key = listKey(filters, search, reloads);
    const controller = new AbortController();
    listDocuments(listParams(filters, search), { signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return; // a newer request is on its way
        setList({
          key,
          filters,
          search,
          items: page.data,
          nextCursor: page.meta.has_more ? page.meta.next_cursor : null,
          failed: false,
        });
        if (page.meta.limits) setLimits(page.meta.limits);
        setError(null);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setList({ key, filters, search, items: [], nextCursor: null, failed: true });
        setError(describeDocumentError(err));
      });
    return () => {
      controller.abort();
    };
  }, [filters, search, reloads]);

  // Uploads end with the page: nothing goes on sending once it is gone.
  useEffect(() => {
    closed.current = false;
    const active = jobs.current;
    const waiting = queue.current;
    return () => {
      closed.current = true;
      waiting.length = 0;
      for (const job of active.values()) job.controller.abort();
    };
  }, []);

  useProcessingPoll(list?.items ?? NO_DOCUMENTS, (outcomes) => {
    setList((state) => state && { ...state, items: applyOutcomes(state.items, outcomes) });
    const notices = outcomes.flatMap((outcome) => {
      const notice = outcome.kind === "changed" ? changeNotice(outcome.previous, outcome.document) : null;
      return notice ? [notice] : [];
    });
    if (notices.length > 0) setNotice(notices.join(" "));
  });

  const setFilters = useCallback((patch: Partial<DocumentFilters>) => {
    setFiltersState((current) => ({ ...current, ...patch }));
  }, []);

  const reload = useCallback(() => {
    setReloads((count) => count + 1);
  }, []);

  const currentKey = listKey(filters, search, reloads);

  const loadMore = () => {
    if (list?.key !== currentKey || !list.nextCursor || loadingMore) return;
    const { key, nextCursor } = list;
    setLoadingMore(true);
    listDocuments({ ...listParams(list.filters, list.search), cursor: nextCursor })
      .then((page) => {
        setList((state) => {
          if (state?.key !== key) return state;
          const known = new Set(state.items.map((item) => item.id));
          return {
            ...state,
            items: [...state.items, ...page.data.filter((item) => !known.has(item.id))],
            nextCursor: page.meta.has_more ? page.meta.next_cursor : null,
          };
        });
      })
      .catch((err: unknown) => {
        setError(describeDocumentError(err));
      })
      .finally(() => {
        setLoadingMore(false);
      });
  };

  const rules: UploadRules = {
    accepted_extensions: limits?.accepted_extensions ?? DEFAULT_ACCEPTED_EXTENSIONS,
    max_upload_bytes: limits?.max_upload_bytes ?? null,
  };

  const patchUpload = (id: number, patch: Partial<UploadItem>) => {
    setUploads((items) => items.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  };

  /** A new document goes on top of the list, when the list's filters let it in. */
  const prepend = (record: DocumentRecord) => {
    setList((state) => {
      if (!state || !belongs(record, state)) return state;
      return { ...state, items: [record, ...state.items.filter((item) => item.id !== record.id)] };
    });
  };

  /** Send waiting files, MAX_PARALLEL_UPLOADS at a time; each one finished starts the next. */
  const startUploads = () => {
    while (!closed.current && running.current < MAX_PARALLEL_UPLOADS) {
      const id = queue.current.shift();
      if (id === undefined) return;
      const job = jobs.current.get(id);
      if (!job) continue;
      running.current += 1;
      patchUpload(id, { state: "uploading" });
      const name = job.file.name;
      uploadDocument(
        job.file,
        {},
        {
          signal: job.controller.signal,
          onProgress: (sent, total) => {
            const percent = total > 0 ? Math.min(100, Math.floor((sent / total) * 100)) : 0;
            if (percent === job.percent) return;
            job.percent = percent;
            patchUpload(id, { percent });
          },
        },
      )
        .then((record) => {
          patchUpload(id, { state: "done", percent: 100, documentId: record.id });
          prepend(record);
          setNotice(`${name} uploaded. Processing has started.`);
        })
        .catch((err: unknown) => {
          if (isAbortError(err)) {
            patchUpload(id, { state: "cancelled" });
            return;
          }
          const message = describeDocumentError(err);
          patchUpload(id, { state: "failed", message });
          setNotice(`${name} was not uploaded. ${message}`);
        })
        .finally(() => {
          running.current -= 1;
          jobs.current.delete(id);
          startUploads();
        });
    }
  };

  /** Check the chosen files, list them all, and queue the acceptable ones. */
  const addFiles = (files: readonly File[]) => {
    const added: UploadItem[] = [];
    const notes: string[] = [];
    for (const file of files) {
      lastUploadId.current += 1;
      const id = lastUploadId.current;
      const problem = checkFile(file, rules);
      const item: UploadItem = {
        id,
        name: file.name,
        size: file.size,
        state: problem ? "rejected" : "waiting",
        percent: 0,
        message: problem,
        documentId: null,
      };
      added.push(item);
      if (problem) {
        notes.push(`${file.name} cannot be uploaded: ${problem}`);
        continue;
      }
      jobs.current.set(id, { file, controller: new AbortController(), percent: 0 });
      queue.current.push(id);
    }
    if (added.length === 0) return;
    const queued = added.length - notes.length;
    if (queued > 0) notes.unshift(queued === 1 ? "Uploading 1 file." : `Uploading ${String(queued)} files.`);
    setUploads((items) => [...items, ...added]);
    setNotice(notes.join(" "));
    startUploads();
  };

  const cancelUpload = (id: number) => {
    const job = jobs.current.get(id);
    if (!job) return;
    const waitingAt = queue.current.indexOf(id);
    if (waitingAt === -1) {
      job.controller.abort(); // the upload's own handler marks it cancelled
    } else {
      queue.current.splice(waitingAt, 1);
      jobs.current.delete(id);
      patchUpload(id, { state: "cancelled" });
    }
    setNotice(`Upload of ${job.file.name} cancelled.`);
  };

  const dismissUpload = (id: number) => {
    setUploads((items) => items.filter((item) => item.id !== id || isActiveUpload(item)));
  };

  const clearFinishedUploads = () => {
    setUploads((items) => items.filter(isActiveUpload));
  };

  const retry = async (id: string): Promise<boolean> => {
    setError(null);
    try {
      const record = await retryDocument(id);
      setList(
        (state) =>
          state && { ...state, items: state.items.map((item) => (item.id === id ? record : item)) },
      );
      setNotice(`Processing ${displayName(record)} again.`);
      return true;
    } catch (err) {
      setError(describeDocumentError(err));
      return false;
    }
  };

  const remove = async (id: string): Promise<boolean> => {
    setError(null);
    const listed = list?.items.find((item) => item.id === id);
    try {
      await deleteDocument(id);
      setList((state) => state && { ...state, items: state.items.filter((item) => item.id !== id) });
      setNotice(`${listed ? displayName(listed) : "The document"} deleted.`);
      return true;
    } catch (err) {
      setError(describeDocumentError(err));
      return false;
    }
  };

  const shown = list?.key === currentKey ? list : null;
  return {
    /** While a new filter loads, the previous results stay on screen. */
    items: list?.items ?? NO_DOCUMENTS,
    /** The list for the current filters and search has not arrived yet. */
    loading: shown === null,
    /** The current list could not be loaded (the error says why). */
    failed: shown?.failed === true,
    hasMore: Boolean(shown?.nextCursor),
    loadingMore,
    loadMore,
    filters,
    setFilters,
    query,
    setQuery,
    /** The search the shown list answers (the query, once typing paused). */
    search,
    error,
    dismissError: () => {
      setError(null);
    },
    reload,
    /** What the file picker accepts and the client checks before sending. */
    rules,
    uploads,
    addFiles,
    cancelUpload,
    dismissUpload,
    clearFinishedUploads,
    retry,
    remove,
    /** The last outcome, for the live region. */
    notice,
  };
}

export type DocumentLibrary = ReturnType<typeof useDocuments>;
