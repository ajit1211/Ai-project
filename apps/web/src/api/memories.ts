/**
 * The memory endpoints (docs/api/memories.md). This is the only module that calls
 * `/memories/...`; the memories page goes through `src/hooks/useMemories.ts`.
 */

import type {
  ClearScope,
  CreateMemoryBody,
  ListMemoriesParams,
  Memory,
  MemoryPage,
  MemorySettings,
  SearchHit,
  UpdateMemoryBody,
} from "../types/memory";
import { api } from "./client";

export interface RequestControl {
  signal?: AbortSignal;
}

const memoryPath = (id: string): string => `/memories/${encodeURIComponent(id)}/`;

/** Your memories and the ones shared with you, newest first, one page at a time. */
export async function listMemories(
  params: ListMemoriesParams = {},
  control: RequestControl = {},
): Promise<MemoryPage> {
  const query = new URLSearchParams();
  if (params.scope) query.set("scope", params.scope);
  if (params.kind) query.set("kind", params.kind);
  if (params.status) query.set("status", params.status);
  if (params.conversation_id) query.set("conversation_id", params.conversation_id);
  if (params.q) query.set("q", params.q);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.cursor) query.set("cursor", params.cursor);
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const { data, meta } = await api<Memory[]>(`/memories/${suffix}`, { signal: control.signal });
  const nextCursor = typeof meta?.next_cursor === "string" ? meta.next_cursor : null;
  return { data, meta: { next_cursor: nextCursor, has_more: meta?.has_more === true } };
}

/** The memories most relevant to `q`, best first. */
export async function searchMemories(
  q: string,
  limit = 10,
  control: RequestControl = {},
): Promise<SearchHit[]> {
  const query = new URLSearchParams({ q, limit: String(limit) });
  const { data } = await api<SearchHit[]>(`/memories/search/?${query.toString()}`, {
    signal: control.signal,
  });
  return data;
}

export async function createMemory(body: CreateMemoryBody): Promise<Memory> {
  const { data } = await api<Memory>("/memories/", { method: "POST", body });
  return data;
}

export async function getMemory(id: string, control: RequestControl = {}): Promise<Memory> {
  const { data } = await api<Memory>(memoryPath(id), { signal: control.signal });
  return data;
}

export async function updateMemory(id: string, body: UpdateMemoryBody): Promise<Memory> {
  const { data } = await api<Memory>(memoryPath(id), { method: "PATCH", body });
  return data;
}

export async function deleteMemory(id: string): Promise<void> {
  await api(memoryPath(id), { method: "DELETE" });
}

export async function approveMemory(id: string): Promise<Memory> {
  const { data } = await api<Memory>(`${memoryPath(id)}approve/`, { method: "POST" });
  return data;
}

export async function rejectMemory(id: string): Promise<Memory> {
  const { data } = await api<Memory>(`${memoryPath(id)}reject/`, { method: "POST" });
  return data;
}

export async function getMemorySettings(control: RequestControl = {}): Promise<MemorySettings> {
  const { data } = await api<MemorySettings>("/memories/settings/", { signal: control.signal });
  return data;
}

export async function updateMemorySettings(
  body: Partial<MemorySettings>,
): Promise<MemorySettings> {
  const { data } = await api<MemorySettings>("/memories/settings/", { method: "PATCH", body });
  return data;
}

/** Deletes the caller's own private memories; returns how many went. */
export async function clearMemories(scope: ClearScope = "all"): Promise<number> {
  const { data } = await api<{ deleted: number }>("/memories/clear/", {
    method: "POST",
    body: { scope },
  });
  return data.deleted;
}
