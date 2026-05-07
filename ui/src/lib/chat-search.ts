import type { ConversationFlatSearchMatch } from '$lib/types/api';
import type { TimelineItem } from '$lib/chat';

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

export function findLocalChatMatches(items: TimelineItem[], query: string): LocalChatMatch[] {
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
