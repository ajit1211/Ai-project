/**
 * The memories page's state: the filtered list with paging, the actions on one memory, the
 * member's settings, and clearing. Every change is confirmed by the server's answer; the
 * list is updated from it rather than refetched.
 */

import { useCallback, useEffect, useState } from "react";

import {
  approveMemory,
  clearMemories,
  createMemory,
  deleteMemory,
  getMemorySettings,
  listMemories,
  rejectMemory,
  updateMemory,
  updateMemorySettings,
} from "../api/memories";
import { describeError } from "../components/forms";
import type {
  ClearScope,
  CreateMemoryBody,
  Memory,
  MemoryKind,
  MemoryScope,
  MemorySettings,
  MemoryStatus,
  UpdateMemoryBody,
} from "../types/memory";

export const PAGE_SIZE = 30;

export interface MemoryFilters {
  scope: MemoryScope | "";
  kind: MemoryKind | "";
  status: MemoryStatus | "";
  q: string;
}

export const DEFAULT_FILTERS: MemoryFilters = { scope: "", kind: "", status: "active", q: "" };

/** Identifies one list request: loading is "the newest answer is for an older key". */
function listKey(filters: MemoryFilters, reloads: number): string {
  return `${filters.scope}|${filters.kind}|${filters.status}|${filters.q}|${String(reloads)}`;
}

export interface MemoriesState {
  items: Memory[];
  filters: MemoryFilters;
  setFilters: (patch: Partial<MemoryFilters>) => void;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  error: string | null;
  dismissError: () => void;
  reload: () => void;
  loadMore: () => void;
  create: (body: CreateMemoryBody) => Promise<Memory | null>;
  update: (id: string, body: UpdateMemoryBody) => Promise<Memory | null>;
  remove: (id: string) => Promise<boolean>;
  approve: (id: string) => Promise<Memory | null>;
  reject: (id: string) => Promise<Memory | null>;
  clear: (scope: ClearScope) => Promise<number | null>;
  settings: MemorySettings | null;
  saveSettings: (patch: Partial<MemorySettings>) => Promise<boolean>;
  /** The last action's outcome, for the live region. */
  notice: string | null;
}

export function useMemories(): MemoriesState {
  const [items, setItems] = useState<Memory[]>([]);
  const [filters, setFiltersState] = useState<MemoryFilters>(DEFAULT_FILTERS);
  const [settledKey, setSettledKey] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  const [settings, setSettings] = useState<MemorySettings | null>(null);

  useEffect(() => {
    const key = listKey(filters, reloads);
    const controller = new AbortController();
    listMemories(
      {
        scope: filters.scope || undefined,
        kind: filters.kind || undefined,
        status: filters.status || undefined,
        q: filters.q || undefined,
        limit: PAGE_SIZE,
      },
      { signal: controller.signal },
    )
      .then((page) => {
        if (controller.signal.aborted) return; // a newer request is on its way
        setItems(page.data);
        setCursor(page.meta.next_cursor);
        setError(null);
        setSettledKey(key);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(describeError(err));
        setSettledKey(key);
      });
    return () => {
      controller.abort();
    };
  }, [filters, reloads]);

  useEffect(() => {
    const controller = new AbortController();
    getMemorySettings({ signal: controller.signal })
      .then(setSettings)
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(describeError(err));
      });
    return () => {
      controller.abort();
    };
  }, []);

  const setFilters = useCallback((patch: Partial<MemoryFilters>) => {
    setFiltersState((current) => ({ ...current, ...patch }));
  }, []);

  const reload = useCallback(() => {
    setReloads((count) => count + 1);
  }, []);

  const loadMore = () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    listMemories({
      scope: filters.scope || undefined,
      kind: filters.kind || undefined,
      status: filters.status || undefined,
      q: filters.q || undefined,
      limit: PAGE_SIZE,
      cursor,
    })
      .then((page) => {
        setItems((current) => {
          const known = new Set(current.map((m) => m.id));
          return [...current, ...page.data.filter((m) => !known.has(m.id))];
        });
        setCursor(page.meta.next_cursor);
      })
      .catch((err: unknown) => {
        setError(describeError(err));
      })
      .finally(() => {
        setLoadingMore(false);
      });
  };

  /** Put the server's version of a memory in the list, or drop it when the filter excludes it. */
  const place = (memory: Memory) => {
    setItems((current) => {
      const matches = !filters.status || memory.status === filters.status;
      const without = current.filter((m) => m.id !== memory.id);
      if (!matches) return without;
      const index = current.findIndex((m) => m.id === memory.id);
      if (index === -1) return [memory, ...without];
      return current.map((m) => (m.id === memory.id ? memory : m));
    });
  };

  const attempt = async <T>(action: () => Promise<T>, done: string): Promise<T | null> => {
    setError(null);
    try {
      const result = await action();
      setNotice(done);
      return result;
    } catch (err) {
      setError(describeError(err));
      return null;
    }
  };

  const create = (body: CreateMemoryBody) =>
    attempt(async () => {
      const memory = await createMemory(body);
      place(memory);
      return memory;
    }, "Memory saved.");

  const update = (id: string, body: UpdateMemoryBody) =>
    attempt(async () => {
      const memory = await updateMemory(id, body);
      place(memory);
      return memory;
    }, "Memory updated.");

  const remove = (id: string) =>
    attempt(async () => {
      await deleteMemory(id);
      setItems((current) => current.filter((m) => m.id !== id));
      return true;
    }, "Memory deleted.").then((result) => result === true);

  const approve = (id: string) =>
    attempt(async () => {
      const memory = await approveMemory(id);
      place(memory);
      return memory;
    }, "Memory approved.");

  const reject = (id: string) =>
    attempt(async () => {
      const memory = await rejectMemory(id);
      place(memory);
      return memory;
    }, "Memory rejected.");

  const clear = (scope: ClearScope) =>
    attempt(async () => {
      const deleted = await clearMemories(scope);
      reload();
      return deleted;
    }, "Memories cleared.");

  const saveSettings = (patch: Partial<MemorySettings>) =>
    attempt(async () => {
      setSettings(await updateMemorySettings(patch));
      return true;
    }, "Memory settings saved.").then((result) => result === true);

  return {
    items,
    filters,
    setFilters,
    loading: settledKey !== listKey(filters, reloads),
    loadingMore,
    hasMore: cursor !== null,
    error,
    dismissError: () => {
      setError(null);
    },
    reload,
    loadMore,
    create,
    update,
    remove,
    approve,
    reject,
    clear,
    settings,
    saveSettings,
    notice,
  };
}
