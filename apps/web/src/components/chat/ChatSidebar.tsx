/**
 * The conversation list: New conversation, title search, Active / Archived, the list itself
 * (newest activity first) and paging. Beside the chat on wide screens, in a drawer on narrow
 * ones.
 */

import { useEffect, useId, useRef, type ReactNode } from "react";

import type { ConversationList as ConversationListState } from "../../hooks/useConversations";
import type { Conversation } from "../../types/chat";
import { Banner } from "./Banner";
import type { ConversationActions } from "./ConversationItem";
import { ConversationList } from "./ConversationList";
import { Icon } from "./Icon";

export interface ChatSidebarProps extends Omit<ConversationActions, "onArchive" | "onDelete"> {
  /** Resolves to whether the conversation was archived (or restored). */
  onArchive: (conversation: Conversation, archived: boolean) => Promise<boolean>;
  /** Resolves to whether the conversation was deleted. */
  onDelete: (conversation: Conversation) => Promise<boolean>;
  list: ConversationListState;
  selectedId: string | undefined;
  creating: boolean;
  onNew: () => void;
  /** Shown beside New conversation (the drawer's close button). */
  headerAction?: ReactNode;
}

export function ChatSidebar({
  list,
  selectedId,
  creating,
  onNew,
  headerAction,
  onArchive,
  onDelete,
  ...actions
}: ChatSidebarProps) {
  const searchId = useId();
  const search = useRef<HTMLInputElement>(null);
  const refocus = useRef(false);

  // Archiving, restoring or deleting removes the item whose button had focus. Once the list
  // has re-rendered without it, keep keyboard and screen-reader users in the list.
  useEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    const active = document.activeElement;
    if (!active || active === document.body) search.current?.focus();
  }, [list.items]);

  const keepFocus = (done: Promise<boolean>) => {
    void done.then((removed) => {
      if (removed) refocus.current = true;
    });
  };

  let emptyText = "No conversations yet.";
  if (list.search) emptyText = `No conversations match “${list.search}”.`;
  else if (list.view === "archived") emptyText = "No archived conversations.";

  return (
    <nav className="chat-sidebar" aria-label="Conversations">
      <div className="chat-sidebar-header">
        <div className="chat-sidebar-top">
          <button type="button" className="btn btn-primary chat-new" onClick={onNew} disabled={creating}>
            <Icon name="plus" />
            New conversation
          </button>
          {headerAction}
        </div>
        <label htmlFor={searchId} className="visually-hidden">
          Search conversations
        </label>
        <input
          ref={search}
          id={searchId}
          type="search"
          placeholder="Search conversations"
          value={list.query}
          onChange={(event) => {
            list.setQuery(event.target.value);
          }}
        />
        <div className="chat-views" role="group" aria-label="Which conversations">
          {(["active", "archived"] as const).map((view) => (
            <button
              key={view}
              type="button"
              aria-pressed={list.view === view}
              onClick={() => {
                list.setView(view);
              }}
            >
              {view === "active" ? "Active" : "Archived"}
            </button>
          ))}
        </div>
      </div>

      {list.error && <Banner message={list.error} onDismiss={list.dismissError} onRetry={list.reload} />}

      <div className="chat-sidebar-body" aria-busy={list.loading}>
        {list.items.length === 0 && (
          <p className="chat-sidebar-note">{list.loading ? "Loading…" : emptyText}</p>
        )}
        {list.items.length > 0 && (
          <ConversationList
            conversations={list.items}
            selectedId={selectedId}
            {...actions}
            onArchive={(conversation, archived) => {
              keepFocus(onArchive(conversation, archived));
            }}
            onDelete={(conversation) => {
              keepFocus(onDelete(conversation));
            }}
          />
        )}
        {!list.loading && list.hasMore && (
          <button type="button" className="btn btn-quiet chat-more" onClick={list.loadMore} disabled={list.loadingMore}>
            {list.loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </div>
    </nav>
  );
}
