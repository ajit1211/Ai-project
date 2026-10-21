/**
 * The chat workspace (`/chat` and `/chat/:conversationId`): the conversation list beside the
 * open conversation. At 800 px wide or less there is one panel, and a menu button opens the
 * list as a drawer. Writing a message with no conversation open starts one.
 *
 * The composer's Documents control answers the next questions from chosen documents (or all
 * of them); the choice is kept per conversation for this visit. With nothing chosen, the
 * chat is the ordinary chat and sends exactly what it always did.
 */

import { useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router";

import { ApiError } from "../api/client";
import { getDocument } from "../api/documents";
import { Banner } from "../components/chat/Banner";
import { ChatSidebar } from "../components/chat/ChatSidebar";
import { conversationTitle } from "../components/chat/ConversationItem";
import { DocumentPicker } from "../components/chat/DocumentPicker";
import { Drawer } from "../components/chat/Drawer";
import { EmptyState } from "../components/chat/EmptyState";
import {
  describeGroundingRefusal,
  groundingFor,
  isGroundingRefusal,
  MAX_GROUNDED_CHARS,
  type DocumentSelection,
  type PickedDocument,
} from "../components/chat/grounding";
import { GroundingBar } from "../components/chat/GroundingBar";
import { Icon } from "../components/chat/Icon";
import { MessageComposer, type ComposerHandle } from "../components/chat/MessageComposer";
import { MessageList } from "../components/chat/MessageList";
import { ModelSelector } from "../components/chat/ModelSelector";
import { describeChatError, type Submission } from "../hooks/chatThread";
import { useChat } from "../hooks/useChat";
import { useConversations } from "../hooks/useConversations";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { useModels } from "../hooks/useModels";
import type { Conversation } from "../types/chat";

/** Matches the CSS breakpoint of the chat layout. */
const NARROW_QUERY = "(max-width: 800px)";
/** The draft written before any conversation is open. */
const NEW_DRAFT = "";

/** `record` without `key`. */
function without<T>(record: Readonly<Record<string, T>>, key: string): Readonly<Record<string, T>> {
  if (!(key in record)) return record;
  return Object.fromEntries(Object.entries(record).filter(([name]) => name !== key));
}

/** Whether a failed lookup means the document is gone for this user. */
const isGone = (err: unknown): boolean => err instanceof ApiError && (err.status === 404 || err.status === 403);

function listNames(names: readonly string[]): string {
  return new Intl.ListFormat("en", { style: "long", type: "conjunction" }).format(names);
}

export function ChatPage() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const list = useConversations();
  const [drafts, setDrafts] = useState<Readonly<Record<string, string>>>({});
  // What questions are answered from, and why the last grounded one was refused, both kept
  // per conversation (by draft key) for this visit only.
  const [selections, setSelections] = useState<Readonly<Record<string, DocumentSelection>>>({});
  const [groundingAlerts, setGroundingAlerts] = useState<Readonly<Record<string, string>>>({});
  const [pickerOpen, setPickerOpen] = useState(false);
  const documentsButton = useRef<HTMLButtonElement>(null);

  const setGroundingAlert = (key: string, text: string | null) => {
    setGroundingAlerts((all) => (text ? { ...all, [key]: text } : without(all, key)));
  };

  /**
   * After "not available" or "not indexed", find out which selected documents are the
   * problem: the ones gone are dropped from the selection, the ones still being indexed named.
   */
  const checkDocuments = async (key: string, refusal: ApiError, picked: readonly PickedDocument[]) => {
    const checks = await Promise.all(
      picked.map((document) =>
        getDocument(document.id).then(
          (record) => ({ document, gone: false, preparing: !record.index.searchable }),
          (err: unknown) => ({ document, gone: isGone(err), preparing: false }),
        ),
      ),
    );
    const gone = checks.filter((check) => check.gone).map((check) => check.document);
    const preparing = checks.filter((check) => check.preparing).map((check) => check.document.name);
    const parts: string[] = [];
    if (gone.length > 0) {
      const goneIds = new Set(gone.map((document) => document.id));
      const kept = picked.filter((document) => !goneIds.has(document.id));
      setSelections((all) => {
        const current = all[key];
        if (current?.kind !== "documents") return all;
        const remaining = current.documents.filter((document) => !goneIds.has(document.id));
        return remaining.length > 0 ? { ...all, [key]: { kind: "documents", documents: remaining } } : without(all, key);
      });
      const names = listNames(gone.map((document) => document.name));
      parts.push(
        gone.length === 1
          ? `${names} is no longer available and was removed from the selection.`
          : `${names} are no longer available and were removed from the selection.`,
      );
      parts.push(kept.length > 0 ? "Check the selection and send again." : "Choose other documents, or send it as an ordinary question.");
    }
    if (preparing.length > 0) {
      parts.push(`Still being prepared for search: ${listNames(preparing)}. Try again once indexing finishes.`);
    }
    if (parts.length === 0) return; // Nothing more specific than the server's own words.
    const lead = refusal.code === "DOCUMENT_NOT_INDEXED" ? `${refusal.message} ` : "";
    setGroundingAlert(key, `${lead}${parts.join(" ")}`);
  };

  /** A refused question about documents goes back into the composer, with why. */
  const onGroundedSendRefused = (id: string, submission: Submission, error: ApiError): boolean => {
    if (!isGroundingRefusal(error)) return false;
    setDrafts((all) => (all[id] ? all : { ...all, [id]: submission.content }));
    setGroundingAlert(id, describeGroundingRefusal(error));
    const picked = selections[id];
    if ((error.code === "DOCUMENT_UNAVAILABLE" || error.code === "DOCUMENT_NOT_INDEXED") && picked?.kind === "documents") {
      void checkDocuments(id, error, picked.documents);
    }
    return true;
  };

  const chat = useChat(conversationId, { onConversationChange: list.upsert, onGroundedSendRefused });
  const { models, error: modelsError } = useModels();
  const narrow = useMediaQuery(NARROW_QUERY);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const composer = useRef<ComposerHandle>(null);

  const showDrawer = narrow && drawerOpen;
  const draftKey = conversationId ?? NEW_DRAFT;
  const draft = drafts[draftKey] ?? "";
  const setDraft = (key: string, value: string) => {
    setDrafts((all) => ({ ...all, [key]: value }));
  };
  const selection = selections[draftKey] ?? null;
  const setSelection = (key: string, next: DocumentSelection | null) => {
    setSelections((all) => (next ? { ...all, [key]: next } : without(all, key)));
    setGroundingAlert(key, null);
  };

  const closeDrawer = () => {
    setDrawerOpen(false);
  };

  const create = async (): Promise<Conversation | null> => {
    setCreating(true);
    try {
      return await list.create();
    } finally {
      setCreating(false);
    }
  };

  const startNew = () => {
    void create().then((conversation) => {
      if (!conversation) return;
      closeDrawer();
      void navigate(`/chat/${conversation.id}`);
    });
  };

  const send = () => {
    const content = draft;
    if (!content.trim() || creating) return;
    const grounding = selection ? groundingFor(selection) : undefined;
    if (grounding && content.length > MAX_GROUNDED_CHARS) {
      setGroundingAlert(
        draftKey,
        `Questions about documents are limited to ${MAX_GROUNDED_CHARS.toLocaleString()} characters; this one has ${content.length.toLocaleString()}. Shorten it, or switch to ordinary chat.`,
      );
      return;
    }
    setGroundingAlert(draftKey, null);
    if (conversationId) {
      if (chat.send(content, conversationId, grounding)) setDraft(conversationId, "");
      return;
    }
    // Nothing open yet: the first message starts a conversation.
    void create().then((conversation) => {
      if (!conversation) return;
      setDraft(NEW_DRAFT, "");
      // A selection made before the conversation existed now belongs to it.
      if (selection) {
        setSelections((all) => ({ ...without(all, NEW_DRAFT), [conversation.id]: selection }));
      }
      void navigate(`/chat/${conversation.id}`);
      chat.send(content, conversation.id, grounding);
    });
  };

  const removeDocument = (documentId: string) => {
    if (selection?.kind !== "documents") return;
    const documents = selection.documents.filter((document) => document.id !== documentId);
    setSelection(draftKey, documents.length > 0 ? { kind: "documents", documents } : null);
    // The chip and its button are gone: carry on from the Documents button.
    documentsButton.current?.focus();
  };

  const groundingBar = (
    <GroundingBar
      selection={selection}
      pickerOpen={pickerOpen}
      onOpenPicker={() => {
        setPickerOpen(true);
      }}
      onRemove={removeDocument}
      onClear={() => {
        setSelection(draftKey, null);
        documentsButton.current?.focus();
      }}
      alert={groundingAlerts[draftKey] ?? null}
      onDismissAlert={() => {
        setGroundingAlert(draftKey, null);
      }}
      openerRef={documentsButton}
    />
  );

  const fillPrompt = (prompt: string) => {
    setDraft(draftKey, prompt);
    composer.current?.focus();
  };

  const rename = (conversation: Conversation, title: string) => {
    void list.rename(conversation.id, title).then((updated) => {
      if (updated) chat.syncConversation(updated);
    });
  };

  const archive = (conversation: Conversation, archived: boolean): Promise<boolean> =>
    list.setArchived(conversation.id, archived).then((updated) => {
      if (updated) chat.syncConversation(updated);
      return updated !== null;
    });

  const remove = (conversation: Conversation): Promise<boolean> =>
    list.remove(conversation.id).then((removed) => {
      if (removed && conversation.id === conversationId) void navigate("/chat");
      return removed;
    });

  /** Regenerate, Retry and Stop remove or disable the button that was pressed: carry on in the composer. */
  const thenType =
    <A extends unknown[]>(action: (...args: A) => void) =>
    (...args: A) => {
      action(...args);
      composer.current?.focus();
    };

  const sidebar = (
    <ChatSidebar
      list={list}
      selectedId={conversationId}
      creating={creating}
      onNew={startNew}
      onSelect={closeDrawer}
      onRename={rename}
      onArchive={archive}
      onDelete={remove}
      headerAction={
        narrow ? (
          <button type="button" className="icon-button" aria-label="Close conversations" onClick={closeDrawer}>
            <Icon name="close" />
          </button>
        ) : undefined
      }
    />
  );

  const conversation = chat.conversation;
  const archived = conversation?.status === "archived";
  let title = "Chat";
  if (conversation) title = conversationTitle(conversation);
  else if (conversationId) {
    const listed = list.items.find((item) => item.id === conversationId);
    title = listed ? conversationTitle(listed) : "Conversation";
  }

  let body: ReactNode = null;
  if (!conversationId) {
    body = <EmptyState variant="no-conversation" onPrompt={fillPrompt} />;
  } else if (chat.loading) {
    body = <p className="chat-loading muted">Loading conversation…</p>;
  } else if (!chat.loadError && chat.items.length === 0) {
    body = <EmptyState variant="no-messages" onPrompt={fillPrompt} />;
  } else if (!chat.loadError) {
    body = (
      <MessageList
        conversationId={conversationId}
        items={chat.items}
        hasOlder={chat.hasOlder}
        loadingOlder={chat.loadingOlder}
        onLoadOlder={chat.loadOlder}
        idle={!chat.streaming && !archived}
        onRegenerate={thenType(chat.regenerate)}
        onRetry={thenType(chat.retry)}
      />
    );
  }

  return (
    <div className="chat-page">
      {!narrow && sidebar}
      {showDrawer && (
        <Drawer label="Conversations" onClose={closeDrawer} returnFocusRef={menuButton}>
          {sidebar}
        </Drawer>
      )}

      <section className="chat-panel" aria-label="Conversation" inert={showDrawer}>
        <header className="chat-header">
          {narrow && (
            <button
              ref={menuButton}
              type="button"
              className="icon-button"
              aria-label="Open conversations"
              aria-haspopup="dialog"
              aria-expanded={drawerOpen}
              onClick={() => {
                setDrawerOpen(true);
              }}
            >
              <Icon name="menu" />
            </button>
          )}
          <h1 className="chat-title">{title}</h1>
          {conversation && (
            <ModelSelector
              models={models}
              error={modelsError}
              value={conversation.model}
              onChange={chat.setModel}
              disabled={chat.savingModel || chat.streaming || archived}
            />
          )}
        </header>

        {/* On narrow screens the list (and its errors) hide in the drawer: surface them here. */}
        {narrow && list.error && (
          <Banner message={list.error} onDismiss={list.dismissError} onRetry={list.reload} />
        )}
        {chat.loadError && (
          <Banner
            message={chat.loadError}
            onRetry={chat.reload}
            onDismiss={() => {
              void navigate("/chat");
            }}
          />
        )}
        {chat.notice && <Banner message={describeChatError(chat.notice)} onDismiss={chat.dismissNotice} />}

        {body}

        {/* A reply still running in a conversation archived meanwhile keeps its Stop button. */}
        {archived && !chat.streaming ? (
          <div className="chat-archived">
            <p>This conversation is archived. Restore it to continue.</p>
            <button
              type="button"
              className="btn"
              onClick={() => {
                void archive(conversation, false);
              }}
            >
              Restore
            </button>
          </div>
        ) : (
          <MessageComposer
            ref={composer}
            value={draft}
            onChange={(value) => {
              setDraft(draftKey, value);
            }}
            onSend={send}
            onStop={thenType(chat.stop)}
            streaming={chat.streaming}
            stopping={chat.stopping}
            disabled={creating || (Boolean(conversationId) && (chat.loading || Boolean(chat.loadError)))}
            header={groundingBar}
            placeholder={selection ? "Ask about your documents…" : undefined}
          />
        )}
        {pickerOpen && (
          <DocumentPicker
            selection={selection}
            onApply={(next) => {
              setSelection(draftKey, next);
              setPickerOpen(false);
            }}
            onClose={() => {
              setPickerOpen(false);
            }}
            returnFocusRef={documentsButton}
          />
        )}
      </section>

      <div className="visually-hidden" aria-live="polite">
        {chat.announcement}
      </div>
    </div>
  );
}
