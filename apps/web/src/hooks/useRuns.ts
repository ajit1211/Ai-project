/**
 * The task history's state: the caller's tasks, newest first, filtered by status and agent,
 * with cursor paging ("Load more"). A change of filter starts the list afresh; an older answer
 * arriving late never replaces a newer one.
 */

import { useCallback, useEffect, useState } from "react";

import { listRuns } from "../api/runs";
import { describeAgentError } from "../components/agents/labels";
import type { ListRunsParams, Run, RunStatusFilter } from "../types/agents";

export const PAGE_SIZE = 30;

export interface RunFilters {
  status: RunStatusFilter | "";
  /** "" for every agent. */
  agentId: string;
}

export const DEFAULT_RUN_FILTERS: RunFilters = { status: "", agentId: "" };

interface ListState {
  /** Which request produced this list; see `listKey`. */
  key: string;
  runs: Run[];
  nextCursor: string | null;
  failed: boolean;
}

const listKey = (filters: RunFilters, reloads: number): string =>
  JSON.stringify([filters.status, filters.agentId, reloads]);

function listParams(filters: RunFilters): ListRunsParams {
  return {
    status: filters.status || undefined,
    agent_id: filters.agentId || undefined,
    limit: PAGE_SIZE,
  };
}

export interface RunHistory {
  runs: Run[];
  filters: RunFilters;
  setFilters: (patch: Partial<RunFilters>) => void;
  loading: boolean;
  failed: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;
  reload: () => void;
  error: string | null;
  dismissError: () => void;
}

export function useRuns(): RunHistory {
  const [filters, setFiltersState] = useState<RunFilters>(DEFAULT_RUN_FILTERS);
  const [reloads, setReloads] = useState(0);
  const [state, setState] = useState<ListState | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = listKey(filters, reloads);

  useEffect(() => {
    const controller = new AbortController();
    const requested = listKey(filters, reloads);
    listRuns(listParams(filters), { signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return;
        setState({ key: requested, runs: page.runs, nextCursor: page.nextCursor, failed: false });
        setError(null);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setState({ key: requested, runs: [], nextCursor: null, failed: true });
        setError(describeAgentError(err));
      });
    return () => {
      controller.abort();
    };
  }, [filters, reloads]);

  const setFilters = useCallback((patch: Partial<RunFilters>) => {
    setFiltersState((current) => ({ ...current, ...patch }));
  }, []);

  const reload = useCallback(() => {
    setReloads((count) => count + 1);
  }, []);

  const current = state?.key === key ? state : null;

  const loadMore = () => {
    if (!current?.nextCursor || loadingMore) return;
    const requested = current.key;
    setLoadingMore(true);
    listRuns({ ...listParams(filters), cursor: current.nextCursor })
      .then((page) => {
        setState((latest) => {
          // The filters changed meanwhile: this page belongs to a list that is gone.
          if (latest?.key !== requested) return latest;
          const known = new Set(latest.runs.map((run) => run.id));
          return {
            ...latest,
            runs: [...latest.runs, ...page.runs.filter((run) => !known.has(run.id))],
            nextCursor: page.nextCursor,
          };
        });
      })
      .catch((err: unknown) => {
        setError(describeAgentError(err));
      })
      .finally(() => {
        setLoadingMore(false);
      });
  };

  return {
    // While a new filter loads, the previous list stays visible (marked busy).
    runs: state?.runs ?? [],
    filters,
    setFilters,
    loading: current === null,
    failed: current?.failed === true,
    hasMore: Boolean(current?.nextCursor),
    loadingMore,
    loadMore,
    reload,
    error,
    dismissError: () => {
      setError(null);
    },
  };
}
