import { Component, lazy, Suspense, useEffect, useRef, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router";

import { useAuth } from "../auth/AuthContext";

// The chat brings Markdown and syntax highlighting: loaded only when the chat is opened.
const ChatPage = lazy(() => import("./ChatPage").then((module) => ({ default: module.ChatPage })));

/**
 * The chat workspace of the active organization. Switching organization starts a fresh
 * workspace, because its conversation list, models and any running reply belong to the
 * organization just left; a conversation open in the URL belongs to it too, so it is closed.
 */
export function ChatRoute() {
  const { activeOrganizationId } = useAuth();
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const shown = useRef(activeOrganizationId);

  useEffect(() => {
    if (shown.current === activeOrganizationId) return;
    shown.current = activeOrganizationId;
    if (conversationId) void navigate("/chat", { replace: true });
  }, [activeOrganizationId, conversationId, navigate]);

  return (
    <ChatFailure>
      <Suspense fallback={<p className="muted center">Loading…</p>}>
        <ChatPage key={activeOrganizationId ?? "none"} />
      </Suspense>
    </ChatFailure>
  );
}

interface ChatFailureState {
  failed: boolean;
}

/**
 * Without this, a chat that cannot load (its code bundle replaced by a new deployment, the
 * network gone) would take the whole app down with it, top bar included.
 */
class ChatFailure extends Component<{ children: ReactNode }, ChatFailureState> {
  override state: ChatFailureState = { failed: false };

  static getDerivedStateFromError(): ChatFailureState {
    return { failed: true };
  }

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="chat-unavailable" role="alert">
        <p>The chat could not be loaded. Check your connection, then reload the page.</p>
        <button
          type="button"
          className="btn"
          onClick={() => {
            window.location.reload();
          }}
        >
          Reload
        </button>
      </div>
    );
  }
}
