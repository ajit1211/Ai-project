/**
 * The chat workspace (`/chat` and `/chat/:conversationId`): the conversation list beside the
 * open conversation. At 800 px wide or less there is one panel, and a menu button opens the
 * list as a drawer. Writing a message with no conversation open starts one.
 */

import { useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router";

import { Banner } from "../components/chat/Banner";
import { ChatSidebar } from "../components/chat/ChatSidebar";
import { conversationTitle } from "../components/chat/ConversationItem";
import { Drawer } from "../components/chat/Drawer";
import { EmptyState } from "../components/chat/EmptyState";
import { Icon } from "../components/chat/Icon";
import { MessageComposer, type ComposerHandle } from "../components/chat/MessageComposer";
import { MessageList } from "../components/chat/MessageList";
import { ModelSelector } from "../components/chat/ModelSelector";
import { describeChatError } from "../hooks/chatThread";
import { useChat } from "../hooks/useChat";
import { useConversations } from "../hooks/useConversations";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { useModels } from "../hooks/useModels";
import type { Conversation } from "../types/chat";

/** Matches the CSS breakpoint of the chat layout. */
const NARROW_QUERY = "(max-width: 800px)";
/** The draft written before any conversation is open. */
const NEW_DRAFT = "";

export function ChatPage() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const list = useConversations();
  const chat = useChat(conversationId, { onConversationChange: list.upsert });
  const { models, error: modelsError } = useModels();
  const narrow = useMediaQuery(NARROW_QUERY);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drafts, setDrafts] = useState<Readonly<Record<string, string>>>({});
  const [creating, setCreating] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const composer = useRef<ComposerHandle>(null);

  const showDrawer = narrow && drawerOpen;
  const draftKey = conversationId ?? NEW_DRAFT;
  const draft = drafts[draftKey] ?? "";
  const setDraft = (key: string, value: string) => {
    setDrafts((all) => ({ ...all, [key]: value }));
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
    if (conversationId) {
      if (chat.send(content)) setDraft(conversationId, "");
      return;
    }
    // Nothing open yet: the first message starts a conversation.
    void create().then((conversation) => {
      if (!conversation) return;
      setDraft(NEW_DRAFT, "");
      void navigate(`/chat/${conversation.id}`);
      chat.send(content, conversation.id);
    });
  };

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
          />
        )}
      </section>

      <div className="visually-hidden" aria-live="polite">
        {chat.announcement}
      </div>
    </div>
  );
}
