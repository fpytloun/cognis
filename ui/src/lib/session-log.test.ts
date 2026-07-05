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
      timeline_items: [
        {
          id: 'thinking:msg_think:think_1',
          kind: 'thinking',
          messageId: 'msg_think',
          turnId: 'turn_1',
          blocks: [
            {
              block_id: 'think_1',
              title: 'Thinking',
              content: 'checking',
              source: 'reasoning',
              complete: false
            }
          ],
          streaming: true,
          activeTitle: 'Thinking',
          timestamp: '2026-01-01T00:00:02Z'
        }
      ],
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

  it('does not duplicate items when a projection refresh follows an events-built timeline', async () => {
    // The events builder and the server projection use different item id
    // schemes. A refresh carrying the full projection must REPLACE the
    // timeline, not upsert into it — otherwise every already-rendered
    // message re-appears at the bottom under its projection id.
    const initial = await loadSessionLog('sess_child', async () => response([event(1)]));
    expect(initial.timelineSource).toBe('events');

    const refreshed = await refreshSessionLog(initial, async () => ({
      session_id: 'sess_child',
      items: [event(2, 'assistant_message', 'the reply')],
      timeline_items: [
        {
          id: 'message:sess_child:1',
          kind: 'message',
          role: 'user',
          content: 'message 1',
          timestamp: '2026-01-01T00:00:01Z'
        },
        {
          id: 'message:turn-1:phase:0',
          kind: 'message',
          role: 'assistant',
          content: 'the reply',
          timestamp: '2026-01-01T00:00:02Z'
        }
      ],
      last_seq: 2,
      has_more: false,
      active_thinking: []
    }));

    expect(refreshed.timelineSource).toBe('projection');
    const contents = refreshed.timeline
      .filter((item) => item.kind === 'message')
      .map((item) => (item.kind === 'message' ? item.content : ''));
    // Each message appears exactly once.
    expect(contents.filter((content) => content === 'message 1')).toHaveLength(1);
    expect(contents.filter((content) => content === 'the reply')).toHaveLength(1);
  });
});
