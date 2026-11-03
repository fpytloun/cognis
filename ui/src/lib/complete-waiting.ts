import { isSidebarConversationWaiting } from '$lib/conversation-sidebar';
import { mergeConversationPreservingActivity } from '$lib/chat-page';
import type { Conversation, WaitingConversationPage } from '$lib/types/api';

/** Workspace-owned knowledge; never infer idle from an incomplete lookup. */
export class WaitingActivity {
  private readonly snapshots = new Map<string, ReadonlySet<string>>();
  private generation = 0;

  begin(scope: string): { generation: number; activeIds: ReadonlySet<string> } {
    return { generation: ++this.generation, activeIds: this.snapshots.get(scope) ?? new Set() };
  }

  commit(scope: string, generation: number, activeIds: Set<string>): void {
    if (generation !== this.generation) return;
    this.snapshots.delete(scope);
    this.snapshots.set(scope, activeIds);
    if (this.snapshots.size > 12) this.snapshots.delete(this.snapshots.keys().next().value!);
  }
}

/** Finish the candidate feed before publishing a replacement Waiting snapshot. */
export async function loadCompleteWaiting(
  fetchPage: (cursor: string | null) => Promise<WaitingConversationPage>,
  activity?: WaitingActivity,
  scope = '',
): Promise<Conversation[]> {
  const previous = activity?.begin(scope);
  const nextActiveIds = new Set<string>();
  const items = new Map<string, Conversation>();
  const cursors = new Set<string>();
  let cursor: string | null = null;
  do {
    const page = await fetchPage(cursor);
    for (const id of page.active_conversation_ids) nextActiveIds.add(id);
    if (page.activity_complete === false) {
      for (const id of previous?.activeIds ?? []) nextActiveIds.add(id);
    }
    for (const item of page.conversations.items) {
      items.set(item.conversation_id, item);
    }
    if (!page.conversations.has_more) break;
    const next = page.conversations.cursor;
    if (!next || cursors.has(next)) throw new Error('Waiting pagination did not advance.');
    cursors.add(next);
    cursor = next;
  } while (true);
  if (previous) activity!.commit(scope, previous.generation, nextActiveIds);
  return [...items.values()].filter(item =>
    isSidebarConversationWaiting(item, nextActiveIds.has(item.conversation_id)));
}

/** Waiting metadata overrides overlapping history; history cursors are untouched. */
export function mergeWaitingConversations(
  history: readonly Conversation[],
  waiting: readonly Conversation[],
): Conversation[] {
  const items = new Map(history.map((item) => [item.conversation_id, item]));
  for (const item of waiting) items.set(item.conversation_id,
    mergeConversationPreservingActivity(items.get(item.conversation_id), item));
  return [...items.values()];
}

/** Replace membership while keeping newer local reads/runtime over delayed REST. */
export function reconcileWaitingSnapshot(
  current: readonly Conversation[],
  incoming: readonly Conversation[],
): Conversation[] {
  const existing = new Map(current.map(item => [item.conversation_id, item]));
  return incoming.map(item => mergeConversationPreservingActivity(existing.get(item.conversation_id), item));
}
