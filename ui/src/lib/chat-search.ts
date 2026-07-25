import type { ConversationFlatSearchMatch } from '$lib/types/api';
export interface SearchTimelineItem {
  id: string;
  kind: string;
  role?: 'user' | 'assistant' | 'system';
  content?: string | null;
  created_at?: string | null;
}

export interface LocalChatMatch {
  id: string;
  label: string;
  snippet: string;
}

export type ChatSearchResult =
  | { source: 'local'; local: LocalChatMatch; targetId: string }
  | { source: 'server'; server: ConversationFlatSearchMatch; targetId: string };

export function stripMarks(value: string): string {
  return value.replace(/<\/?mark>/g, '');
}

export function cleanSearchSnippet(value: string): string {
  return stripMarks(value).replace(/^(user|assistant|system)\s+message:\s*/i, '');
}

function normalizeSearchText(value: string): string {
  return stripMarks(value)
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toLocaleLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

function messageTimestampDelta(item: SearchTimelineItem, match: ConversationFlatSearchMatch): number {
  if (item.kind !== 'message' || !item.created_at || !match.match.ts) return Number.POSITIVE_INFINITY;
  const itemTime = Date.parse(item.created_at);
  const matchTime = Date.parse(match.match.ts);
  if (!Number.isFinite(itemTime) || !Number.isFinite(matchTime)) return Number.POSITIVE_INFINITY;
  return Math.abs(itemTime - matchTime);
}

function roleFromSnippet(value: string): 'user' | 'assistant' | 'system' | null {
  const role = value.match(/^\s*(user|assistant|system)\s+message\s*:/i)?.[1]?.toLocaleLowerCase();
  if (role === 'user' || role === 'assistant' || role === 'system') return role;
  return null;
}

function expectedMessageRole(match: ConversationFlatSearchMatch): 'user' | 'assistant' | 'system' | null {
  const role = match.match.role?.toLocaleLowerCase();
  if (role === 'user' || role === 'assistant' || role === 'system') return role;
  return roleFromSnippet(match.match.snippet);
}

export function serverSearchFallbackTargetId(match: ConversationFlatSearchMatch): string {
  return `server:${match.intaris_session_id}:${match.match.kind}:${match.match.ref_id ?? match.match.ts ?? match.match.snippet}`;
}

export function findVisibleServerSearchTarget(
  items: SearchTimelineItem[],
  match: ConversationFlatSearchMatch
): string | null {
  const snippetNeedle = normalizeSearchText(cleanSearchSnippet(match.match.snippet).replace(/\.{3,}|…/g, ' '));
  const expectedRole = expectedMessageRole(match);
  let best: { id: string; score: number; delta: number; index: number } | null = null;

  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (!item) continue;
    if (item.kind !== 'message') continue;
    if (expectedRole && item.role !== expectedRole) continue;
    const content = normalizeSearchText(item.content ?? '');
    if (!content) continue;

    const snippetMatches = snippetNeedle.length >= 12 && content.includes(snippetNeedle);
    if (!snippetMatches) continue;

    const delta = messageTimestampDelta(item, match);
    if (
      best === null ||
      delta < best.delta ||
      (delta === best.delta && index < best.index)
    ) {
      best = { id: item.id, score: 1, delta, index };
    }
  }

  return best?.id ?? null;
}

export function resultLabel(result: ChatSearchResult): string {
  if (result.source === 'local') return result.local.label;
  return 'Search match';
}

export function resultSnippet(result: ChatSearchResult): string {
  if (result.source === 'local') return result.local.snippet;
  return cleanSearchSnippet(result.server.match.snippet);
}

function resultScore(result: ChatSearchResult): number {
  if (result.source === 'local') return Number.POSITIVE_INFINITY;
  return result.server.match.score;
}

function shouldReplaceResult(current: ChatSearchResult, next: ChatSearchResult): boolean {
  if (current.source !== 'local' && next.source === 'local') return true;
  if (current.source === 'local' && next.source !== 'local') return false;
  return resultScore(next) > resultScore(current);
}

export function mergeSearchResultsByTarget(results: ChatSearchResult[]): ChatSearchResult[] {
  const byTarget = new Map<string, ChatSearchResult>();
  for (const result of results) {
    if (!result.targetId) continue;
    const current = byTarget.get(result.targetId);
    if (!current || shouldReplaceResult(current, result)) {
      byTarget.set(result.targetId, result);
    }
  }
  return [...byTarget.values()];
}

export function findLocalChatMatches(items: SearchTimelineItem[], query: string): LocalChatMatch[] {
  const q = query.trim().toLocaleLowerCase();
  if (!q) return [];
  const matches: LocalChatMatch[] = [];
  for (const item of items) {
    if (item.kind !== 'message') continue;
    const content = item.content ?? '';
    const index = content.toLocaleLowerCase().indexOf(q);
    if (index < 0) continue;
    const start = Math.max(0, index - 48);
    const end = Math.min(content.length, index + q.length + 72);
    const prefix = start > 0 ? '...' : '';
    const suffix = end < content.length ? '...' : '';
    matches.push({
      id: item.id,
      label: item.role === 'user' ? 'User message' : 'Assistant message',
      snippet: `${prefix}${content.slice(start, end)}${suffix}`
    });
  }
  return matches;
}
