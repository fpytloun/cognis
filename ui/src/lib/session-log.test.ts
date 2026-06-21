import { describe, expect, it } from 'vitest';

import { loadSessionLog, refreshSessionLog } from '$lib/session-log';
import type { MessageEvent, SessionEventsResponse } from '$lib/types/api';

function event(seq: number, type = 'user_message', content = `message ${seq}`): MessageEvent {
  return {
    seq,
    type,
    data: { content },
    timestamp: `2026-01-01T00:00:${String(seq).padStart(2, '0')}Z`
  };
}

function response(
  items: MessageEvent[],
  options: { hasMore?: boolean; lastSeq?: number } = {},
): SessionEventsResponse {
  return {
    session_id: 'sess_child',
    items,
    last_seq: options.lastSeq ?? items.at(-1)?.seq ?? 0,
    has_more: options.hasMore ?? false,
    active_thinking: []
  };
}

describe('session log helpers', () => {
  it('bootstraps multiple pages and advances after_seq from the returned events', async () => {
    const afterSeqs: number[] = [];
    const pages = [
      response([event(1), event(2)], { hasMore: true }),
      response([event(3)], { hasMore: false })
    ];

    const state = await loadSessionLog('sess_child', async (afterSeq) => {
      afterSeqs.push(afterSeq);
      return pages.shift() ?? response([]);
    }, { pageSize: 2, maxPages: 5 });

    expect(afterSeqs).toEqual([0, 2]);
    expect(state.events.map((item) => item.seq)).toEqual([1, 2, 3]);
    expect(state.lastSeq).toBe(3);
    expect(state.truncated).toBe(false);
    expect(state.timeline).toHaveLength(3);
  });

  it('adds a history gap when bootstrap hits the page cap', async () => {
    const state = await loadSessionLog('sess_child', async (afterSeq) => (
      response([event(afterSeq + 1)], { hasMore: true })
    ), { pageSize: 1, maxPages: 2 });

    expect(state.truncated).toBe(true);
    expect(state.events.map((item) => item.type)).toEqual(['user_message', 'user_message', 'history_gap']);
    expect(state.events.at(-1)?.data).toMatchObject({
      reason: 'bootstrap_cap_reached',
      session_id: 'sess_child'
    });
  });

  it('refreshes from the current lastSeq and appends new events', async () => {
    const initial = await loadSessionLog('sess_child', async () => response([event(1)]));
    const afterSeqs: number[] = [];

    const refreshed = await refreshSessionLog(initial, async (afterSeq) => {
      afterSeqs.push(afterSeq);
      return response([event(2, 'assistant_message')]);
    });

    expect(afterSeqs).toEqual([1]);
    expect(refreshed.events.map((item) => item.seq)).toEqual([1, 2]);
    expect(refreshed.lastSeq).toBe(2);
    expect(refreshed.timeline.map((item) => item.kind)).toEqual(['message', 'message']);
  });

  it('keeps existing events when refresh returns only active thinking snapshots', async () => {
    const initial = await loadSessionLog('sess_child', async () => response([event(1)]));

    const refreshed = await refreshSessionLog(initial, async () => ({
      session_id: 'sess_child',
      items: [],
      last_seq: 1,
      has_more: false,
      active_thinking: [
        {
          session_id: 'sess_child',
          message_id: 'msg_think',
          turn_id: 'turn_1',
          blocks: [
            {
              block_id: 'think_1',
              title: 'Thinking',
              content: 'checking',
              source: 'reasoning',
              complete: false
            }
          ],
          updated_at: '2026-01-01T00:00:02Z'
        }
      ]
    }));

    expect(refreshed.events).toHaveLength(1);
    expect(refreshed.timeline.some((item) => item.kind === 'thinking' && item.streaming)).toBe(true);
  });
});
