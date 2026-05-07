import type { ConversationFlatSearchMatch } from '$lib/types/api';
import type { TimelineItem } from '$lib/chat';

export interface LocalChatMatch {
  id: string;
  label: string;
  snippet: string;
}

export type ChatSearchResult =
  | { source: 'local'; local: LocalChatMatch }
  | { source: 'server'; server: ConversationFlatSearchMatch };

const KIND_LABELS: Record<string, string> = {
  reasoning: 'Reasoning',
  intention: 'Intention',
  summary: 'Summary'
};

export function stripMarks(value: string): string {
  return value.replace(/<\/?mark>/g, '');
}

export function resultLabel(result: ChatSearchResult): string {
  if (result.source === 'local') return result.local.label;
  return KIND_LABELS[result.server.match.kind] ?? result.server.match.kind;
}

export function resultSnippet(result: ChatSearchResult): string {
  if (result.source === 'local') return result.local.snippet;
  return stripMarks(result.server.match.snippet);
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
