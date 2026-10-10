/**
 * One conversation in the sidebar: a link that opens it, and Rename (inline: Enter saves,
 * Escape cancels, leaving the field saves), Archive or Restore, and Delete (after a
 * confirmation).
 */

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import type { Conversation } from "../../types/chat";
import { Icon } from "./Icon";

export interface ConversationActions {
  /** Called when the link is followed (closes the drawer on narrow screens). */
  onSelect: () => void;
  /** An empty title asks the server for the automatic one again. */
  onRename: (conversation: Conversation, title: string) => void;
  onArchive: (conversation: Conversation, archived: boolean) => void;
  onDelete: (conversation: Conversation) => void;
}

export interface ConversationItemProps extends ConversationActions {
  conversation: Conversation;
  selected: boolean;
}

export const conversationTitle = (conversation: Conversation): string =>
  conversation.title || "New conversation";

export function ConversationItem({
  conversation,
  selected,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: ConversationItemProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const renameButton = useRef<HTMLButtonElement>(null);
  /** The field is going away on purpose: its blur must not save again. */
  const closing = useRef(false);
  /** Finished from the keyboard: put focus back on Rename. */
  const refocus = useRef(false);
  const title = conversationTitle(conversation);
  const archived = conversation.status === "archived";

  useEffect(() => {
    if (editing || !refocus.current) return;
    refocus.current = false;
    renameButton.current?.focus();
  }, [editing]);

  const startEditing = () => {
    closing.current = false;
    setDraft(conversation.title);
    setEditing(true);
  };

  const finishEditing = (save: boolean, fromKeyboard: boolean) => {
    if (closing.current) return;
    closing.current = true;
    refocus.current = fromKeyboard;
    setEditing(false);
    const next = draft.trim();
    if (save && next !== conversation.title) onRename(conversation, next);
  };

  return (
    <li className={`conversation-item${selected ? " selected" : ""}`}>
      {editing ? (
        <form
          className="conversation-rename"
          onSubmit={(event) => {
            event.preventDefault();
            finishEditing(true, true);
          }}
        >
          <input
            aria-label="Conversation title"
            value={draft}
            maxLength={200}
            placeholder="Automatic title"
            autoFocus
            onChange={(event) => {
              setDraft(event.target.value);
            }}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              event.stopPropagation(); // Cancels the rename only, not the drawer around it.
              finishEditing(false, true);
            }}
            onBlur={() => {
              finishEditing(true, false);
            }}
          />
        </form>
      ) : (
        <>
          <Link
            to={`/chat/${conversation.id}`}
            className="conversation-link"
            aria-current={selected ? "page" : undefined}
            title={title}
            onClick={onSelect}
          >
            {title}
          </Link>
          <div className="conversation-actions">
            <button
              ref={renameButton}
              type="button"
              className="icon-button"
              aria-label={`Rename “${title}”`}
              title="Rename"
              onClick={startEditing}
            >
              <Icon name="pencil" />
            </button>
            <button
              type="button"
              className="icon-button"
              aria-label={`${archived ? "Restore" : "Archive"} “${title}”`}
              title={archived ? "Restore" : "Archive"}
              onClick={() => {
                onArchive(conversation, !archived);
              }}
            >
              <Icon name={archived ? "unarchive" : "archive"} />
            </button>
            <button
              type="button"
              className="icon-button icon-button-danger"
              aria-label={`Delete “${title}”`}
              title="Delete"
              onClick={() => {
                if (window.confirm(`Delete “${title}” and all of its messages?`)) onDelete(conversation);
              }}
            >
              <Icon name="trash" />
            </button>
          </div>
        </>
      )}
    </li>
  );
}
