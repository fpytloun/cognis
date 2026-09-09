import { describe, expect, it, vi } from 'vitest';

import type { Conversation } from '$lib/types/api';
import {
  DashboardConversationProjectionGate,
  DashboardConversationReadTracker,
} from './conversation-read';

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    conversation_id: 'conversation-1',
    agent_id: 'agent',
    status: 'active',
    has_unread: true,
    last_message_at: '2026-08-24T12:00:00Z',
    updated_at: '2026-08-24T12:00:00Z',
    ...overrides,
  } as Conversation;
}

function tracker(markRead = vi.fn().mockResolvedValue({ ok: true })) {
  const optimisticRead = vi.fn();
  const reconcile = vi.fn().mockResolvedValue(undefined);
  return {
    markRead,
    optimisticRead,
    reconcile,
    value: new DashboardConversationReadTracker({
      markRead,
      optimisticRead,
      reconcile,
      isRetryable: (error) => error instanceof Error && error.message === 'transient',
    }),
  };
}

describe('DashboardConversationReadTracker', () => {
  it('does not mark on open and marks exactly once after the initial scope load', async () => {
    const subject = tracker();
    const item = conversation();

    subject.value.open(item.conversation_id);
    expect(subject.markRead).not.toHaveBeenCalled();
    await subject.value.initialLoaded(item);
    expect(subject.markRead).toHaveBeenCalledOnce();
    expect(subject.optimisticRead).toHaveBeenCalledWith(
      item.conversation_id,
      item.last_message_at,
    );
    expect(subject.value.initialLoaded(item)).toBeNull();
  });

  it('does not mark a stale or failed-load scope that never signals initial load', () => {
    const subject = tracker();
    subject.value.open('conversation-1');

    expect(subject.value.initialLoaded(conversation({ conversation_id: 'conversation-2' }))).toBeNull();
    expect(subject.markRead).not.toHaveBeenCalled();
  });

  it('defers a hidden initial load until its conversation opens', async () => {
    const subject = tracker();
    const item = conversation({ conversation_id: 'agent-direct' });

    expect(subject.value.initialLoaded(item)).toBeNull();
    expect(subject.markRead).not.toHaveBeenCalled();

    subject.value.open(item.conversation_id);
    await vi.waitFor(() => expect(subject.markRead).toHaveBeenCalledOnce());
    expect(subject.markRead).toHaveBeenCalledWith(item.conversation_id);
  });

  it('discards a hidden initial load when its conversation closes', async () => {
    const subject = tracker();
    const item = conversation({ conversation_id: 'agent-direct' });

    subject.value.initialLoaded(item);
    subject.value.close(item.conversation_id);
    subject.value.open(item.conversation_id);
    await Promise.resolve();

    expect(subject.markRead).not.toHaveBeenCalled();
  });

  it('retries one transient failure and reconciles one hard failure', async () => {
    const transient = vi.fn()
      .mockRejectedValueOnce(new Error('transient'))
      .mockResolvedValueOnce({ ok: true });
    const retried = tracker(transient);
    retried.value.open('conversation-1');
    await retried.value.initialLoaded(conversation());
    expect(transient).toHaveBeenCalledTimes(2);
    expect(retried.reconcile).not.toHaveBeenCalled();

    const hard = tracker(vi.fn().mockRejectedValue(new Error('hard')));
    hard.value.open('conversation-1');
    await hard.value.initialLoaded(conversation());
    expect(hard.markRead).toHaveBeenCalledOnce();
    expect(hard.reconcile).toHaveBeenCalledOnce();
  });

  it('re-marks a newer unread version that arrives while the modal stays open', async () => {
    let resolveFirst!: () => void;
    const markRead = vi.fn()
      .mockReturnValueOnce(new Promise<void>((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce({ ok: true });
    const subject = tracker(markRead);
    subject.value.open('conversation-1');
    const first = subject.value.initialLoaded(conversation());
    subject.value.observe([conversation({
      last_message_at: '2026-08-24T12:01:00Z',
      updated_at: '2026-08-24T12:01:00Z',
    })]);

    resolveFirst();
    await first;
    await vi.waitFor(() => expect(markRead).toHaveBeenCalledTimes(2));
    expect(subject.optimisticRead).toHaveBeenLastCalledWith(
      'conversation-1',
      '2026-08-24T12:01:00Z',
    );
  });

  it('does not re-mark unread updates after the modal closes', async () => {
    const subject = tracker();
    subject.value.open('conversation-1');
    await subject.value.initialLoaded(conversation());
    subject.value.close();

    expect(subject.value.observe([conversation({
      last_message_at: '2026-08-24T12:02:00Z',
    })])).toBeNull();
    expect(subject.markRead).toHaveBeenCalledOnce();
  });

  it('tracks multiple open conversation windows independently', async () => {
    const subject = tracker();
    const second = conversation({
      conversation_id: 'conversation-2',
      last_message_at: '2026-08-24T12:02:00Z',
    });
    subject.value.open('conversation-1');
    subject.value.open('conversation-2');
    await subject.value.initialLoaded(conversation());
    await subject.value.initialLoaded(second);
    expect(subject.markRead).toHaveBeenCalledTimes(2);

    subject.value.close('conversation-1');
    const observed = subject.value.observe([
      conversation({ last_message_at: '2026-08-24T12:03:00Z' }),
      { ...second, last_message_at: '2026-08-24T12:04:00Z' },
    ]);
    expect(observed).not.toBeNull();
    await observed;
    expect(subject.markRead).toHaveBeenCalledTimes(3);
    expect(subject.markRead).toHaveBeenLastCalledWith('conversation-2');
  });

  it('keeps workspace reads tracked when one blocking conversation closes', async () => {
    const subject = tracker();
    subject.value.open('workspace-conversation');
    subject.value.open('blocking-conversation');
    subject.value.close('blocking-conversation');
    const observed = subject.value.observe([conversation({
      conversation_id: 'workspace-conversation',
      last_message_at: '2026-08-24T12:05:00Z',
    })]);
    expect(observed).not.toBeNull();
    await observed;
    expect(subject.markRead).toHaveBeenCalledWith('workspace-conversation');
  });
});

describe('DashboardConversationProjectionGate', () => {
  it('rejects stale reconciliation after a newer load or modal scope change', () => {
    const gate = new DashboardConversationProjectionGate();
    const staleReconciliation = gate.begin();
    const newerRealtimeLoad = gate.begin();

    expect(gate.isCurrent(staleReconciliation)).toBe(false);
    expect(gate.isCurrent(newerRealtimeLoad)).toBe(true);

    gate.invalidate();
    expect(gate.isCurrent(newerRealtimeLoad)).toBe(false);
  });
});
