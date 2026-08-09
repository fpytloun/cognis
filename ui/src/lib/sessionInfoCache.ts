import type { ContextUsage, GenerationPerformanceSnapshot } from '$lib/types/api';

const MAX_ENTRIES = 16;
const MAX_PER_CONVERSATION = 8;
const TTL_MS = 120_000;
const HARD_EVICTION_MS = 300_000;

export interface SessionInfoData {
  intaris_session_id: string;
  intention: string | null;
  summary: string | null;
  status: string;
  total_calls: number;
  approved_count: number;
  denied_count: number;
  escalated_count: number;
  context_usage?: ContextUsage | null;
  last_generation?: GenerationPerformanceSnapshot | null;
}

type Entry = {
  conversationId: string;
  value: SessionInfoData;
  storedAt: number;
};

const entries = new Map<string, Entry>();

function key(conversationId: string, sessionId: string): string {
  return `${conversationId}:${sessionId}`;
}

function prune(now = Date.now()): void {
  for (const [entryKey, entry] of entries) {
    if (now - entry.storedAt >= HARD_EVICTION_MS) entries.delete(entryKey);
  }
}

export function getSessionInfo(conversationId: string, sessionId: string): SessionInfoData | null {
  const now = Date.now();
  prune(now);
  const entryKey = key(conversationId, sessionId);
  const entry = entries.get(entryKey);
  if (!entry || now - entry.storedAt >= TTL_MS) return null;
  entries.delete(entryKey);
  entries.set(entryKey, entry);
  return { ...entry.value };
}

export function setSessionInfo(conversationId: string, sessionId: string, value: SessionInfoData): void {
  const entryKey = key(conversationId, sessionId);
  entries.delete(entryKey);
  entries.set(entryKey, { conversationId, value: { ...value }, storedAt: Date.now() });
  const matching = [...entries].filter(([, entry]) => entry.conversationId === conversationId);
  while (matching.length > MAX_PER_CONVERSATION) {
    const oldest = matching.shift();
    if (oldest) entries.delete(oldest[0]);
  }
  while (entries.size > MAX_ENTRIES) {
    const oldest = entries.keys().next().value;
    if (typeof oldest !== 'string') break;
    entries.delete(oldest);
  }
}

export function clearSessionInfoCache(conversationId?: string): void {
  if (!conversationId) {
    entries.clear();
    return;
  }
  for (const [entryKey, entry] of entries) {
    if (entry.conversationId === conversationId) entries.delete(entryKey);
  }
}
