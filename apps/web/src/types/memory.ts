/**
 * Types of the memory API (docs/api/memories.md). They mirror the server's shapes field for
 * field; `src/api/memories.ts` is the only caller.
 */

export type MemoryScope = "conversation" | "user" | "team" | "organization";
export type MemoryKind = "preference" | "fact" | "decision" | "episode";
export type MemorySourceType = "explicit" | "inferred" | "assistant" | "manual" | "summary";
export type MemoryStatus = "candidate" | "active" | "superseded" | "expired" | "rejected";

export interface Memory {
  id: string;
  scope: MemoryScope;
  kind: MemoryKind;
  /** A normalized subject such as "preferred_language"; "" when none. */
  key: string;
  content: string;
  value: unknown;
  source_type: MemorySourceType;
  confidence: number;
  importance: number;
  status: MemoryStatus;
  owner_id: string;
  team_id: string | null;
  conversation_id: string | null;
  source_message_id: string | null;
  superseded_by_id: string | null;
  created_at: string;
  updated_at: string;
  last_accessed_at: string | null;
  expires_at: string | null;
  /** Whether the caller may edit, approve, reject or delete it (decided by the server). */
  editable: boolean;
}

export interface MemoryListMeta {
  next_cursor: string | null;
  has_more: boolean;
}

export interface MemoryPage {
  data: Memory[];
  meta: MemoryListMeta;
}

export interface SearchHit {
  memory: Memory;
  score: number;
}

export interface ListMemoriesParams {
  scope?: MemoryScope;
  kind?: MemoryKind;
  status?: MemoryStatus;
  conversation_id?: string;
  q?: string;
  limit?: number;
  cursor?: string;
}

export interface CreateMemoryBody {
  content: string;
  scope?: MemoryScope;
  kind?: MemoryKind;
  key?: string;
  importance?: number;
  team_id?: string;
  conversation_id?: string;
  expires_at?: string | null;
}

export interface UpdateMemoryBody {
  content?: string;
  kind?: MemoryKind;
  key?: string;
  importance?: number;
  expires_at?: string | null;
}

export interface MemorySettings {
  extraction_enabled: boolean;
  auto_save: boolean;
  retention_days: number | null;
}

export type ClearScope = "all" | "user" | "conversation" | "candidates";
