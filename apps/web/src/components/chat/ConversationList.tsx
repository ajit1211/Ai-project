import type { Conversation } from "../../types/chat";
import { ConversationItem, type ConversationActions } from "./ConversationItem";

export interface ConversationListProps extends ConversationActions {
  conversations: Conversation[];
  selectedId: string | undefined;
}

export function ConversationList({ conversations, selectedId, ...actions }: ConversationListProps) {
  return (
    <ul className="conversation-list">
      {conversations.map((conversation) => (
        <ConversationItem
          key={conversation.id}
          conversation={conversation}
          selected={conversation.id === selectedId}
          {...actions}
        />
      ))}
    </ul>
  );
}
