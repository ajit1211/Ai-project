/**
 * The sidebar's conversation list: one status at a time (active or archived), an optional
 * title search, cursor paging, and the actions on a conversation. Ordered like the server,
 * by activity (the last message, or the creation time before any): a conversation the chat
 * reports new activity for moves to the top.
 */

import { useCallback, useEffect, useState } from "react";

import {
  createConversation,
  deleteConversation,
  listConversations,
  updateConversation,
} from "../api/conversations";
import { describeError } from "../components/forms";
import type { Conversation, ConversationStatus, CreateConversationBody } from "../types/chat";

const PAGE_SIZE = 30;
const SEARCH_DELAY_MS = 300;

interface ListState {
  /** Which request produced this list; see `listKey`. */
  key: string;
  view: ConversationStatus;
  search: string;
  items: Conversation[];
  nextCursor: string | null;
  hasMore: boolean;
}

const listKey = (view: ConversationStatus, search: string, reloads: number): string =>
  JSON.stringify([view, search, reloads]);

function activityOf(conversation: Conversation): number {
  const at = Date.parse(conversation.last_message_at ?? conversation.created_at);
  return Number.isNaN(at) ? 0 : at;
}

/** Newest activity first; ties by id, newest first, as the server orders them. */
export function byActivity(a: Conversation, b: Conversation): number {
  const difference = activityOf(b) - activityOf(a);
  if (difference !== 0) return difference;
  if (a.id === b.id) return 0;
  return a.id < b.id ? 1 : -1;
}

/** The list with `conversation` updated, added when it belongs there, or removed. */
function place(state: ListState, conversation: Conversation): ListState {
  const others = state.items.filter((item) => item.id !== conversation.id);
  const known = others.length !== state.items.length;
  if (conversation.status !== state.view) return known ? { ...state, items: others } : state;
  // A new arrival must match the search; a renamed one stays where the user is looking.
  const matches = conversation.title.toLowerCase().includes(state.search.toLowerCase());
  if (!known && !matches) return state;
  return { ...state, items: [...others, conversation].sort(byActivity) };
}

export function useConversations() {
  const [view, setView] = useState<ConversationStatus>("active");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [reloads, setReloads] = useState(0);
  const [list, setList] = useState<ListState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

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
    const key = listKey(view, search, reloads);
    const controller = new AbortController();
    listConversations({ status: view, q: search, limit: PAGE_SIZE }, { signal: controller.signal })
      .then((page) => {
        setList({
          key,
          view,
          search,
          items: [...page.data].sort(byActivity),
          nextCursor: page.meta.next_cursor,
          hasMore: page.meta.has_more,
        });
        setError(null);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setList({ key, view, search, items: [], nextCursor: null, hasMore: false });
        setError(describeError(err));
      });
    return () => {
      controller.abort();
    };
  }, [view, search, reloads]);

  /** Take a newer description of a conversation: renamed, re-modelled, newly active. */
  const upsert = useCallback((conversation: Conversation): void => {
    setList((state) => (state ? place(state, conversation) : state));
  }, []);

  const loadMore = (): void => {
    if (!list?.hasMore || !list.nextCursor || loadingMore) return;
    const { key, view: forView, search: forSearch, nextCursor } = list;
    setLoadingMore(true);
    listConversations({ status: forView, q: forSearch, limit: PAGE_SIZE, cursor: nextCursor })
      .then((page) => {
        setList((state) => {
          if (state?.key !== key) return state;
          const known = new Set(state.items.map((item) => item.id));
          return {
            ...state,
            items: [...state.items, ...page.data.filter((item) => !known.has(item.id))].sort(byActivity),
            nextCursor: page.meta.next_cursor,
            hasMore: page.meta.has_more,
          };
        });
      })
      .catch((err: unknown) => {
        setError(describeError(err));
      })
      .finally(() => {
        setLoadingMore(false);
      });
  };

  /** Run one action; a failure goes to the banner and resolves to null. */
  const attempt = async <T>(action: () => Promise<T>): Promise<T | null> => {
    try {
      return await action();
    } catch (err) {
      setError(describeError(err));
      return null;
    }
  };

  const create = (body: CreateConversationBody = {}): Promise<Conversation | null> =>
    attempt(async () => {
      const conversation = await createConversation(body);
      // A new conversation is active: show that list, with it on top.
      setView("active");
      upsert(conversation);
      return conversation;
    });

  const rename = (id: string, title: string): Promise<Conversation | null> =>
    attempt(async () => {
      const conversation = await updateConversation(id, { title });
      upsert(conversation);
      return conversation;
    });

  const setArchived = (id: string, archived: boolean): Promise<Conversation | null> =>
    attempt(async () => {
      const conversation = await updateConversation(id, { status: archived ? "archived" : "active" });
      upsert(conversation);
      return conversation;
    });

  const remove = (id: string): Promise<boolean> =>
    attempt(async () => {
      await deleteConversation(id);
      setList((state) => state && { ...state, items: state.items.filter((item) => item.id !== id) });
      return true;
    }).then((done) => done === true);

  return {
    /** While a new search loads, the previous results stay (but never another status's). */
    items: list?.view === view ? list.items : [],
    /** The list for the current status and search has not arrived yet. */
    loading: list?.key !== listKey(view, search, reloads),
    error,
    dismissError: () => {
      setError(null);
    },
    reload: () => {
      setReloads((count) => count + 1);
    },
    view,
    setView,
    query,
    setQuery,
    /** The search the shown list answers (the query, once typing paused). */
    search,
    hasMore: list?.hasMore ?? false,
    loadingMore,
    loadMore,
    create,
    rename,
    setArchived,
    remove,
    upsert,
  };
}

export type ConversationList = ReturnType<typeof useConversations>;
