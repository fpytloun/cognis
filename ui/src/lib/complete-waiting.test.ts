import { describe, expect, it, vi } from 'vitest';
import { loadCompleteWaiting, mergeWaitingConversations, reconcileWaitingSnapshot, WaitingActivity } from './complete-waiting';
import { partitionSidebarConversationLanes } from './conversation-sidebar';
import type { Conversation, WaitingConversationPage } from './types/api';

const conversation = (id: string, fields: Partial<Conversation> = {}): Conversation => ({
  conversation_id: id, has_unread: true, has_active_turn: false,
  last_message_at: '2026-07-01T00:00:00Z', last_read_at: null,
  updated_at: '2026-07-01T00:00:00Z',
  pending_notification_types: [], ...fields,
} as Conversation);
const page = (items: Conversation[], cursor: string | null = null, active: string[] = []): WaitingConversationPage => ({
  conversations: { items, cursor, has_more: cursor !== null },
  active_conversation_ids: active,
});

describe('complete Waiting projection', () => {
  it('remembers reported work without unread candidates until telemetry confirms idle', async () => {
    const activity = new WaitingActivity();
    const running = conversation('running-without-unread');
    await loadCompleteWaiting(async () => page([], null, [running.conversation_id]), activity, 'owner');
    await loadCompleteWaiting(async () => ({
      ...page([]), activity_complete: false,
    }), activity, 'owner');
    expect(await loadCompleteWaiting(async () => ({
      ...page([running, conversation('new-unread')]), activity_complete: false,
    }), activity, 'owner')).toEqual([conversation('new-unread')]);
    expect(await loadCompleteWaiting(async () => page([running]), activity, 'owner')).toEqual([running]);
  });

  it('classifies earlier candidates using activity reported by later pages', async () => {
    const running = conversation('later-reported-work');
    const question = conversation('later-reported-question', {
      pending_notification_types: ['step_question'],
    });
    const fetch = vi.fn()
      .mockResolvedValueOnce(page([running, question], 'next'))
      .mockResolvedValueOnce(page([], null, [running.conversation_id, question.conversation_id]));
    expect(await loadCompleteWaiting(fetch)).toEqual([question]);
    expect(fetch.mock.calls).toEqual([[null], ['next']]);
  });

  it('updates unread and actionable rows during missing telemetry without declaring known work idle', async () => {
    const activity = new WaitingActivity();
    const running = conversation('running-shell');
    expect(await loadCompleteWaiting(async () => page([running], null, ['running-shell']), activity, 'owner')).toEqual([]);
    const incomplete = async () => ({
      ...page([running, conversation('new-unread'), conversation('running-question', {
        pending_notification_types: ['step_question'],
      })], null, ['running-question']),
      activity_complete: false,
    });
    expect((await loadCompleteWaiting(incomplete, activity, 'owner')).map(row => row.conversation_id))
      .toEqual(['new-unread', 'running-question']);
    expect((await loadCompleteWaiting(async () => page([running]), activity, 'owner')).map(row => row.conversation_id))
      .toEqual(['running-shell']);
  });

  it('does not commit partial activity knowledge or leak it across scopes', async () => {
    const activity = new WaitingActivity();
    const running = conversation('running-shell');
    await loadCompleteWaiting(async () => page([running], null, ['running-shell']), activity, 'owner-a');
    const failed = vi.fn().mockResolvedValueOnce(page([running], 'next'))
      .mockRejectedValueOnce(new Error('second page offline'));
    await expect(loadCompleteWaiting(failed, activity, 'owner-a')).rejects.toThrow('second page offline');
    const incomplete = async () => ({ ...page([running]), activity_complete: false });
    expect(await loadCompleteWaiting(incomplete, activity, 'owner-a')).toEqual([]);
    expect(await loadCompleteWaiting(incomplete, activity, 'owner-b')).toEqual([running]);
    expect(await loadCompleteWaiting(incomplete, activity, 'owner-a')).toEqual([]);
  });

  it('fences overlapping activity loads and removes read rows during an outage', async () => {
    const activity = new WaitingActivity();
    let resolve!: (value: WaitingConversationPage) => void;
    const delayed = loadCompleteWaiting(() => new Promise<WaitingConversationPage>(done => { resolve = done; }), activity, 'owner');
    await loadCompleteWaiting(async () => page([conversation('work')], null, ['work']), activity, 'owner');
    resolve(page([conversation('work')]));
    await delayed;
    expect(await loadCompleteWaiting(async () => ({
      ...page([conversation('work'), conversation('already-read', { has_unread: false })]),
      activity_complete: false,
    }), activity, 'owner')).toEqual([]);
  });

  it('loads old channel and managed rows independently of recent history, with badge parity', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(page([conversation('old-matrix')], 'next'))
      .mockResolvedValueOnce(page([conversation('old-managed'), conversation('active-unread'), conversation('active-question', {
        pending_notification_types: ['step_question'],
      })], null, ['active-unread', 'active-question']));
    const waiting = await loadCompleteWaiting(fetch);
    expect(fetch.mock.calls).toEqual([[null], ['next']]);
    expect(waiting.map(x => x.conversation_id)).toEqual(['old-matrix', 'old-managed', 'active-question']);
    const history = Array.from({ length: 50 }, (_, i) => conversation(`recent-${i}`, { has_unread: false }));
    expect(partitionSidebarConversationLanes(mergeWaitingConversations(history, waiting)).waiting).toHaveLength(3);
  });
  it('deduplicates and replaces a full snapshot after read/resolve', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(page([conversation('overlap')], 'next'))
      .mockResolvedValueOnce(page([conversation('overlap', { has_unread: false })]));
    expect(await loadCompleteWaiting(fetch)).toEqual([]);
    expect(mergeWaitingConversations([conversation('overlap', { title: 'stale' })], [conversation('overlap', { title: 'fresh' })]))
      .toMatchObject([conversation('overlap', { title: 'fresh' })]);
  });
  it('does not resurrect a local read or newer runtime when a deferred response arrives', () => {
    const current = conversation('read-during-flight', {
      has_unread: false, last_read_at: '2026-07-01T00:01:00Z',
      updated_at: '2026-07-01T00:01:00Z', has_active_turn: true,
      pending_notification_types: [],
    });
    const stale = conversation('read-during-flight', { pending_notification_types: ['step_question'] });
    const reconciled = reconcileWaitingSnapshot([current], [stale]);
    expect(reconciled[0]).toMatchObject({
      has_unread: false, last_read_at: current.last_read_at,
      has_active_turn: true, pending_notification_types: [],
    });
    expect(partitionSidebarConversationLanes(reconciled).waiting).toEqual([]);
    expect(reconcileWaitingSnapshot(reconciled, [])).toEqual([]);
    expect(mergeWaitingConversations([current], [stale])[0].has_unread).toBe(false);
  });
  it('does not publish partial results on page failures or broken cursors', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(page([conversation('first')], 'repeat'))
      .mockResolvedValueOnce(page([], 'repeat'));
    await expect(loadCompleteWaiting(fetch)).rejects.toThrow('did not advance');
    const failed = vi.fn()
      .mockResolvedValueOnce(page([conversation('first')], 'next'))
      .mockRejectedValueOnce(new Error('offline'));
    await expect(loadCompleteWaiting(failed)).rejects.toThrow('offline');
  });
});
