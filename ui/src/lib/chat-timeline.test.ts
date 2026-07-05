/**
 * Tests for ChatTimeline — the server-authoritative timeline store.
 *
 * Tests use toArray() to inspect state rather than the reactive list property,
 * since the reactive $derived runs in the Svelte compiler context.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { ChatTimeline } from '$lib/chat-timeline.svelte';
import { checkPhaseOrder } from '$lib/test-support/timeline-invariants';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeProjectedMessage(overrides: Record<string, unknown> = {}) {
  return {
    id: 'msg_1',
    kind: 'message',
    role: 'assistant',
    content: 'Hello',
    seq: 1,
    timestamp: '2026-01-01T00:00:00Z',
    messageId: 'msg_1',
    turnId: 'turn_1',
    assistantPhaseIndex: 0,
    orderKey: '9998:000000000000001:000000:02:000000000',
    sessionId: 'sess_1',
    ...overrides,
  };
}

function makeProjectedToolCall(overrides: Record<string, unknown> = {}) {
  return {
    id: 'tool:call_1',
    kind: 'tool_call',
    callId: 'call_1',
    toolName: 'bash',
    status: 'started',
    timestamp: '2026-01-01T00:00:01Z',
    turnId: 'turn_1',
    sessionId: 'sess_1',
    orderKey: '9998:000000000000002:000000:03:000000000',
    ...overrides,
  };
}

function makeProjectedUserMessage(overrides: Record<string, unknown> = {}) {
  return {
    id: 'user_1',
    kind: 'message',
    role: 'user',
    content: 'Hello',
    seq: 0,
    timestamp: '2026-01-01T00:00:00Z',
    orderKey: '9998:000000000000000:000000:00:000000000',
    sessionId: 'sess_1',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// replaceAll
// ---------------------------------------------------------------------------

describe('ChatTimeline.replaceAll', () => {
  it('populates the store from a projection', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage()]);
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    expect(items[0]!.id).toBe('msg_1');
    expect(items[0]!.kind).toBe('message');
  });

  it('clears existing items before replacing', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage({ id: 'old_1' })]);
    ct.replaceAll([makeProjectedMessage({ id: 'new_1' })]);
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    expect(items[0]!.id).toBe('new_1');
  });

  it('sorts items by orderKey', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([
      makeProjectedToolCall({ orderKey: '9998:000000000000002:000000:03:000000000' }),
      makeProjectedUserMessage({ orderKey: '9998:000000000000000:000000:00:000000000' }),
      makeProjectedMessage({ orderKey: '9998:000000000000001:000000:02:000000000' }),
    ]);
    const items = ct.toArray();
    expect(items[0]!.id).toBe('user_1');
    expect(items[1]!.id).toBe('msg_1');
    expect(items[2]!.id).toBe('tool:call_1');
  });

  it('takes content verbatim — no merge or concat', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage({ content: 'First content' })]);
    ct.replaceAll([makeProjectedMessage({ content: 'Second content' })]);
    const items = ct.toArray();
    expect(items[0]!.kind).toBe('message');
    if (items[0]!.kind === 'message') {
      expect(items[0]!.content).toBe('Second content');
    }
  });

  it('preserves canonical user messages omitted from a sparse refresh projection', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([makeProjectedUserMessage()]);
    ct.flushPending();

    ct.replaceAll([]);

    const items = ct.toArray();
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ id: 'user_1', kind: 'message', role: 'user' });
  });
});

// ---------------------------------------------------------------------------
// upsert via enqueuePatch (flushed synchronously via flushPending)
// ---------------------------------------------------------------------------

describe('ChatTimeline.enqueuePatch / flushPending', () => {
  it('upserts items by id', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage({ content: 'v1' })]);
    ct.enqueuePatch([makeProjectedMessage({ content: 'v2' })]);
    ct.flushPending();
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    expect(items[0]!.kind).toBe('message');
    if (items[0]!.kind === 'message') {
      expect(items[0]!.content).toBe('v2');
    }
  });

  it('adds new items', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage()]);
    ct.enqueuePatch([makeProjectedToolCall()]);
    ct.flushPending();
    expect(ct.toArray()).toHaveLength(2);
  });

  it('removes ids', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage(), makeProjectedToolCall()]);
    ct.enqueuePatch([], ['tool:call_1']);
    ct.flushPending();
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    expect(items[0]!.id).toBe('msg_1');
  });

  it('batches multiple enqueues into one flush', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage({ content: 'v1' })]);
    ct.enqueuePatch([makeProjectedMessage({ content: 'v2' })]);
    ct.enqueuePatch([makeProjectedMessage({ content: 'v3' })]);
    ct.flushPending();
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    if (items[0]!.kind === 'message') {
      // Last patch wins (cumulative content)
      expect(items[0]!.content).toBe('v3');
    }
  });

  it('server content is authoritative — later patch overwrites earlier', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([]);
    ct.enqueuePatch([makeProjectedMessage({ content: 'partial' })]);
    ct.enqueuePatch([makeProjectedMessage({ content: 'full response text' })]);
    ct.flushPending();
    const items = ct.toArray();
    if (items[0]!.kind === 'message') {
      expect(items[0]!.content).toBe('full response text');
    }
  });
});

// ---------------------------------------------------------------------------
// prependOlder
// ---------------------------------------------------------------------------

describe('ChatTimeline.prependOlder', () => {
  it('adds older items without overwriting live items', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage({ id: 'live_1', content: 'live' })]);
    ct.prependOlder([
      makeProjectedUserMessage({ id: 'old_1', orderKey: '9997:000000000000001:000000:00:000000000' }),
    ]);
    const items = ct.toArray();
    expect(items).toHaveLength(2);
    // old_1 sorts before live_1 due to lower lineage
    expect(items[0]!.id).toBe('old_1');
    expect(items[1]!.id).toBe('live_1');
  });

  it('does not overwrite existing items with same id', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage({ content: 'live streaming' })]);
    ct.prependOlder([makeProjectedMessage({ content: 'stale history' })]);
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    if (items[0]!.kind === 'message') {
      expect(items[0]!.content).toBe('live streaming');
    }
  });
});

// ---------------------------------------------------------------------------
// remove
// ---------------------------------------------------------------------------

describe('ChatTimeline.remove', () => {
  it('removes items by id', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage(), makeProjectedToolCall()]);
    ct.remove(['msg_1']);
    expect(ct.toArray()).toHaveLength(1);
    expect(ct.toArray()[0]!.id).toBe('tool:call_1');
  });

  it('is a no-op for unknown ids', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage()]);
    ct.remove(['nonexistent']);
    expect(ct.toArray()).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// clear
// ---------------------------------------------------------------------------

describe('ChatTimeline.clear', () => {
  it('removes all items', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage(), makeProjectedToolCall()]);
    ct.clear();
    expect(ct.toArray()).toHaveLength(0);
    expect(ct.size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Optimistic user message
// ---------------------------------------------------------------------------

describe('ChatTimeline.addOptimisticUser', () => {
  it('adds an optimistic user message', () => {
    const ct = new ChatTimeline();
    ct.addOptimisticUser('Hello', [], 'cmsg_1');
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    expect(items[0]!.kind).toBe('message');
    if (items[0]!.kind === 'message') {
      expect(items[0]!.role).toBe('user');
      expect(items[0]!.content).toBe('Hello');
      expect(items[0]!.optimistic).toBe(true);
      expect(items[0]!.clientMessageId).toBe('cmsg_1');
    }
  });

  it('does not add duplicate optimistic message for same clientMessageId', () => {
    const ct = new ChatTimeline();
    ct.addOptimisticUser('Hello', [], 'cmsg_1');
    ct.addOptimisticUser('Hello', [], 'cmsg_1');
    expect(ct.toArray()).toHaveLength(1);
  });

  it('optimistic message sorts after existing items', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([
      makeProjectedMessage({ orderKey: '9998:999999999999999:000000:02:000000000' }),
    ]);
    ct.addOptimisticUser('New message', [], 'cmsg_1');
    const items = ct.toArray();
    expect(items).toHaveLength(2);
    // Optimistic user message should be last (tail orderKey)
    expect(items[items.length - 1]!.kind).toBe('message');
    if (items[items.length - 1]!.kind === 'message') {
      expect((items[items.length - 1] as { role: string }).role).toBe('user');
    }
  });
});

// ---------------------------------------------------------------------------
// removeQueuedUser
// ---------------------------------------------------------------------------

describe('ChatTimeline.removeQueuedUser', () => {
  it('removes optimistic user messages that are queued', () => {
    const ct = new ChatTimeline();
    ct.addOptimisticUser('Hello', [], 'cmsg_1');
    const items = ct.toArray();
    const optimisticId = items[0]!.id;
    // Simulate a queued message snapshot that includes this clientMessageId
    ct.removeQueuedUser([
      {
        queue_id: 'q_1',
        client_message_id: 'cmsg_1',
        content: 'Hello',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        attachments: [],
        position: 0,
      },
    ]);
    // The optimistic item should be removed (it's sending/optimistic)
    const remaining = ct.toArray();
    expect(remaining.every((item) => item.id !== optimisticId)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// reconcileOptimisticDrafts
// ---------------------------------------------------------------------------

describe('ChatTimeline.reconcileOptimisticDrafts', () => {
  it('removes optimistic items whose server echo has arrived', () => {
    const ct = new ChatTimeline();
    // Add optimistic message
    ct.addOptimisticUser('Hello', [], 'cmsg_1');
    // Add server echo (canonical user message with same clientMessageId)
    ct.replaceAll([
      makeProjectedUserMessage({
        id: 'user-msg:msg_echo',
        clientMessageId: 'cmsg_1',
        content: 'Hello',
        seq: 5,
      }),
    ]);
    // Reconcile with empty drafts (all settled)
    const { settledClientMessageIds } = ct.reconcileOptimisticDrafts([]);
    expect(settledClientMessageIds).toHaveLength(0);
    // Only the canonical message should remain
    expect(ct.toArray()).toHaveLength(1);
    expect(ct.toArray()[0]!.id).toBe('user-msg:msg_echo');
  });
});

// ---------------------------------------------------------------------------
// size
// ---------------------------------------------------------------------------

describe('ChatTimeline.size', () => {
  it('returns the number of items', () => {
    const ct = new ChatTimeline();
    expect(ct.size).toBe(0);
    ct.replaceAll([makeProjectedMessage(), makeProjectedToolCall()]);
    expect(ct.size).toBe(2);
    ct.clear();
    expect(ct.size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// restoreFromArray
// ---------------------------------------------------------------------------

describe('ChatTimeline.restoreFromArray', () => {
  it('restores items from a cached array', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage(), makeProjectedToolCall()]);
    const snapshot = ct.toArray();

    const ct2 = new ChatTimeline();
    ct2.restoreFromArray(snapshot);
    expect(ct2.toArray()).toHaveLength(2);
    expect(ct2.toArray()[0]!.id).toBe(snapshot[0]!.id);
  });
});

// ---------------------------------------------------------------------------
// applyEvent — timeline_patch
// ---------------------------------------------------------------------------

describe('ChatTimeline.applyEvent (timeline_patch)', () => {
  it('enqueues timeline_patch items for rAF flush', () => {
    const ct = new ChatTimeline();
    const result = ct.applyEvent({
      type: 'timeline_patch',
      conversation_id: 'conv_1',
      source: 'live',
      items: [makeProjectedMessage()],
      last_seq: 1,
      remove_ids: [],
    });
    expect(result).toBe(true);
    // Before flush, items are pending
    // After flush, items are in the store
    ct.flushPending();
    expect(ct.toArray()).toHaveLength(1);
  });

  it('filters items by activeSessionId', () => {
    const ct = new ChatTimeline();
    const result = ct.applyEvent(
      {
        type: 'timeline_patch',
        conversation_id: 'conv_1',
        source: 'live',
        items: [
          makeProjectedMessage({ sessionId: 'sess_1' }),
          makeProjectedToolCall({ sessionId: 'sess_2', id: 'tool:other' }),
        ],
        last_seq: 1,
        remove_ids: [],
      },
      'sess_1',
    );
    expect(result).toBe(true);
    ct.flushPending();
    // Only sess_1 item should be included
    expect(ct.toArray()).toHaveLength(1);
    expect(ct.toArray()[0]!.id).toBe('msg_1');
  });

  it('returns false for empty patch with no removes', () => {
    const ct = new ChatTimeline();
    const result = ct.applyEvent({
      type: 'timeline_patch',
      conversation_id: 'conv_1',
      source: 'live',
      items: [],
      last_seq: 1,
      remove_ids: [],
    });
    expect(result).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// applyEvent — system_message
// ---------------------------------------------------------------------------

describe('ChatTimeline.applyEvent (system_message)', () => {
  it('adds a system message synchronously', () => {
    const ct = new ChatTimeline();
    ct.applyEvent({ type: 'system_message', text: 'Session started.' });
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    expect(items[0]!.kind).toBe('system_message');
  });
});

// ---------------------------------------------------------------------------
// applyEvent — user_message
// ---------------------------------------------------------------------------

describe('ChatTimeline.applyEvent (user_message)', () => {
  it('adds a user message synchronously', () => {
    const ct = new ChatTimeline();
    ct.applyEvent({
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'Hello',
      timestamp: '2026-01-01T00:00:00Z',
      seq: 1,
      message_id: 'msg_u1',
    });
    const items = ct.toArray();
    expect(items).toHaveLength(1);
    expect(items[0]!.kind).toBe('message');
    if (items[0]!.kind === 'message') {
      expect(items[0]!.role).toBe('user');
      expect(items[0]!.content).toBe('Hello');
    }
  });

  it('reconciles optimistic user message on server echo', () => {
    const ct = new ChatTimeline();
    ct.addOptimisticUser('Hello', [], 'cmsg_1');
    expect(ct.size).toBe(1);

    ct.applyEvent({
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'Hello',
      timestamp: '2026-01-01T00:00:00Z',
      seq: 1,
      message_id: 'msg_u1',
      client_message_id: 'cmsg_1',
    });

    // Should still be 1 item (optimistic replaced by canonical)
    expect(ct.size).toBe(1);
    const items = ct.toArray();
    if (items[0]!.kind === 'message') {
      expect(items[0]!.optimistic).toBeFalsy();
    }
  });
});

// ---------------------------------------------------------------------------
// Ordering invariant: no duplicate ids
// ---------------------------------------------------------------------------

describe('ChatTimeline ordering invariants', () => {
  it('never produces duplicate ids', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage(), makeProjectedToolCall()]);
    ct.enqueuePatch([makeProjectedMessage({ content: 'updated' })]);
    ct.flushPending();
    const ids = ct.toArray().map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('maintains orderKey sort after multiple upserts', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([
      makeProjectedMessage({ orderKey: '9998:000000000000001:000000:02:000000000' }),
      makeProjectedToolCall({ orderKey: '9998:000000000000002:000000:03:000000000' }),
    ]);
    ct.enqueuePatch([
      makeProjectedMessage({
        orderKey: '9998:000000000000001:000000:02:000000000',
        content: 'updated',
      }),
    ]);
    ct.flushPending();
    const items = ct.toArray();
    expect(items[0]!.id).toBe('msg_1');
    expect(items[1]!.id).toBe('tool:call_1');
  });
});

// ---------------------------------------------------------------------------
// Upsert merge: partial patches must not clobber existing fields
// ---------------------------------------------------------------------------
// These tests reproduce the real live-patch sequence:
//   on_tool_call  → patch with arguments + toolName + orderKey
//   on_tool_result → patch with result + status, NO arguments (isolated projection)
// The store must merge, not replace, so arguments/streamedOutput/evaluation/
// orderKey are preserved across follow-up patches.

describe('ChatTimeline upsert merge — partial tool patches', () => {
  it('preserves arguments when a tool_result patch omits them', () => {
    const ct = new ChatTimeline();
    // Simulate on_tool_call patch (has arguments)
    ct.enqueuePatch([
      makeProjectedToolCall({
        arguments: { command: 'ls -la' },
        status: 'started',
        orderKey: '9998:000000000000010:000000:03:000000000',
      }),
    ]);
    ct.flushPending();

    // Simulate on_tool_result patch (no arguments — isolated projection)
    ct.enqueuePatch([
      makeProjectedToolCall({
        // arguments intentionally absent
        status: 'completed',
        result: 'file1.txt\nfile2.txt',
        orderKey: '9998:000000000000011:000000:03:000000000',
      }),
    ]);
    ct.flushPending();

    const items = ct.toArray();
    expect(items).toHaveLength(1);
    const tool = items[0]!;
    expect(tool.kind).toBe('tool_call');
    if (tool.kind === 'tool_call') {
      // Arguments must be preserved from the first patch
      expect(tool.arguments).toEqual({ command: 'ls -la' });
      // Result must be from the second patch
      expect(tool.result).toBe('file1.txt\nfile2.txt');
      expect(tool.status).toBe('completed');
    }
  });

  it('preserves orderKey from the first patch (lower key wins, no position jump)', () => {
    const ct = new ChatTimeline();
    const firstOrderKey = '9998:000000000000010:000000:03:000000000';
    const recomputedOrderKey = '9998:000000000000099:000000:03:000000000'; // higher = later

    ct.enqueuePatch([makeProjectedToolCall({ orderKey: firstOrderKey, status: 'started' })]);
    ct.flushPending();

    // Follow-up patch with a recomputed (higher) orderKey — must NOT replace
    ct.enqueuePatch([makeProjectedToolCall({ orderKey: recomputedOrderKey, status: 'running' })]);
    ct.flushPending();

    const items = ct.toArray();
    expect(items[0]!.orderKey).toBe(firstOrderKey);
  });

  it('does not reopen a terminal tool call with a stale running patch', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([makeProjectedToolCall({ status: 'completed', result: 'done' })]);
    ct.flushPending();

    // Stale runtime snapshot arrives with status: running
    ct.enqueuePatch([makeProjectedToolCall({ status: 'running' })]);
    ct.flushPending();

    const items = ct.toArray();
    expect(items[0]!.kind).toBe('tool_call');
    if (items[0]!.kind === 'tool_call') {
      expect(items[0]!.status).toBe('completed');
    }
  });

  it('terminalizes stale running tool calls from refresh projections after runtime settles', () => {
    const ct = new ChatTimeline();
    ct.applyRuntimeSnapshot([], false);
    ct.replaceAll([makeProjectedToolCall({ status: 'running' })]);

    const items = ct.toArray();
    expect(items[0]!.kind).toBe('tool_call');
    if (items[0]!.kind === 'tool_call') {
      expect(items[0]!.status).toBe('completed');
    }
  });

  it('terminalizes stale streaming thinking from refresh projections after runtime settles', () => {
    const ct = new ChatTimeline();
    ct.applyRuntimeSnapshot([], false);
    ct.replaceAll([
      {
        id: 'thinking:msg_1',
        kind: 'thinking',
        messageId: 'msg_1',
        role: 'assistant',
        blocks: [{ id: 'block_1', title: 'Thinking', content: 'working', complete: false }],
        streaming: true,
        activeTitle: 'Thinking',
        orderKey: '000001',
      },
    ]);

    const items = ct.toArray();
    expect(items[0]!.kind).toBe('thinking');
    if (items[0]!.kind === 'thinking') {
      expect(items[0]!.streaming).toBe(false);
      expect(items[0]!.activeTitle).toBeNull();
    }
  });

  it('preserves streamedOutput when a truncated result patch arrives', () => {
    const ct = new ChatTimeline();
    // First patch: live output streaming
    ct.enqueuePatch([
      makeProjectedToolCall({
        status: 'running',
        streamedOutput: 'very long output that exceeds the truncation limit...',
        liveOutputAvailable: true,
      }),
    ]);
    ct.flushPending();

    // Second patch: truncated result (shorter than streamedOutput)
    ct.enqueuePatch([
      makeProjectedToolCall({
        status: 'completed',
        result: '[truncated]',
        truncated: true,
      }),
    ]);
    ct.flushPending();

    const items = ct.toArray();
    if (items[0]!.kind === 'tool_call') {
      // streamedOutput from the first patch must be preserved
      expect(items[0]!.streamedOutput).toBe('very long output that exceeds the truncation limit...');
    }
  });

  it('preserves evaluation set by an escalation event when a tool_result patch arrives', () => {
    const ct = new ChatTimeline();
    // Tool call arrives first
    ct.enqueuePatch([makeProjectedToolCall({ status: 'started' })]);
    ct.flushPending();

    // Escalation annotates the item via applyWebSocketEvent (sync path)
    ct.applyEvent({
      type: 'escalation',
      conversation_id: 'conv_1',
      call_id: 'esc_1',
      tool_call_id: 'call_1',
      tool_name: 'bash',
      reasoning: 'Risky command',
      risk: 'high',
      timeout_seconds: 300,
    });

    // Verify evaluation was set
    const afterEscalation = ct.toArray();
    expect(afterEscalation[0]!.kind).toBe('tool_call');
    if (afterEscalation[0]!.kind === 'tool_call') {
      expect(afterEscalation[0]!.evaluation).toBeDefined();
    }

    // tool_result patch arrives without evaluation field
    ct.enqueuePatch([
      makeProjectedToolCall({
        status: 'completed',
        result: 'ok',
        // evaluation intentionally absent
      }),
    ]);
    ct.flushPending();

    const items = ct.toArray();
    if (items[0]!.kind === 'tool_call') {
      // Evaluation must survive the tool_result patch
      expect(items[0]!.evaluation).toBeDefined();
      expect(items[0]!.status).toBe('completed');
    }
  });

  it('preserves assistantPhaseIndex when a follow-up patch omits it', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      makeProjectedToolCall({ assistantPhaseIndex: 2, status: 'started' }),
    ]);
    ct.flushPending();

    // Follow-up patch without assistantPhaseIndex
    ct.enqueuePatch([
      makeProjectedToolCall({ status: 'running' }),
    ]);
    ct.flushPending();

    const items = ct.toArray();
    if (items[0]!.kind === 'tool_call') {
      expect(items[0]!.assistantPhaseIndex).toBe(2);
    }
  });

  it('message content is taken verbatim from the latest patch (server authoritative)', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([makeProjectedMessage({ content: 'Hello' })]);
    ct.flushPending();

    ct.enqueuePatch([makeProjectedMessage({ content: 'Hello world' })]);
    ct.flushPending();

    const items = ct.toArray();
    if (items[0]!.kind === 'message') {
      expect(items[0]!.content).toBe('Hello world');
    }
  });

  it('replaceAll bypasses merge — snapshot wins over pending patches', () => {
    const ct = new ChatTimeline();
    // Pending patch with arguments
    ct.enqueuePatch([makeProjectedToolCall({ arguments: { cmd: 'ls' }, status: 'started' })]);
    // replaceAll flushes pending and re-applies them on top of snapshot
    // The snapshot has the full item (from /view), pending patches merge on top
    ct.replaceAll([
      makeProjectedToolCall({ status: 'completed', result: 'done', arguments: { cmd: 'ls' } }),
    ]);
    const items = ct.toArray();
    // After replaceAll + pending re-apply, item should be completed
    if (items[0]!.kind === 'tool_call') {
      expect(items[0]!.status).toBe('completed');
    }
  });
});

// ---------------------------------------------------------------------------
// No-clear mutation policy: sync events must not remount unchanged items
// ---------------------------------------------------------------------------

describe('ChatTimeline no-clear policy — sync events preserve item identity', () => {
  it('untouched items keep their object reference after a sync event', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage(), makeProjectedToolCall()]);

    // Capture references before the sync event
    const msgBefore = ct.toArray().find((i) => i.id === 'msg_1');
    const toolBefore = ct.toArray().find((i) => i.id === 'tool:call_1');

    // Apply a system_message sync event (does not touch msg_1 or tool:call_1)
    ct.applyEvent({ type: 'system_message', text: 'Turn started.' });

    const msgAfter = ct.toArray().find((i) => i.id === 'msg_1');
    const toolAfter = ct.toArray().find((i) => i.id === 'tool:call_1');

    // Unchanged items must be the same object — no remount
    expect(msgAfter).toBe(msgBefore);
    expect(toolAfter).toBe(toolBefore);
  });

  it('replaceAll with a new snapshot only removes absent items, keeps present ones', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([makeProjectedMessage(), makeProjectedToolCall()]);
    expect(ct.size).toBe(2);

    // New snapshot has both items — neither should be removed
    ct.replaceAll([makeProjectedMessage({ content: 'updated' }), makeProjectedToolCall()]);
    expect(ct.size).toBe(2);
    // Content updated
    const msg = ct.toArray().find((i) => i.id === 'msg_1');
    if (msg?.kind === 'message') {
      expect(msg.content).toBe('updated');
    }
  });

  it('replaceAll removes persisted/terminal items absent from the new snapshot', () => {
    const ct = new ChatTimeline();
    // Use a terminal (completed), persisted (real-seq orderKey) tool call so the
    // removal reflects a genuine server-side removal — not an unconfirmed live
    // item, which replaceAll intentionally preserves (symptom 1 guard).
    ct.replaceAll([
      makeProjectedMessage(),
      makeProjectedToolCall({
        status: 'completed',
        orderKey: '0000:000000000000002:000000:03:000000000',
      }),
    ]);
    expect(ct.size).toBe(2);

    // New snapshot has only the message
    ct.replaceAll([makeProjectedMessage()]);
    expect(ct.size).toBe(1);
    expect(ct.toArray()[0]!.id).toBe('msg_1');
  });
});

// ---------------------------------------------------------------------------
// message_complete: no duplicate, no hanging spinner
// ---------------------------------------------------------------------------

describe('ChatTimeline message_complete handling', () => {
  it('finalizes a streaming assistant item in place (no duplicate, no clear)', () => {
    const ct = new ChatTimeline();
    // Streaming assistant item arrives via timeline_patch
    ct.enqueuePatch([
      makeProjectedMessage({
        id: 'message:msg_1:phase:0',
        messageId: 'msg_1',
        turnId: 'turn_1',
        streaming: true,
        content: 'Hello world',
        orderKey: '9998:999999999999999:000000:02:000000000',
      }),
    ]);
    ct.flushPending();
    expect(ct.size).toBe(1);

    // live.assistant_complete patch arrives (rAF-queued, sets streaming:false)
    ct.enqueuePatch([
      makeProjectedMessage({
        id: 'message:msg_1:phase:0',
        messageId: 'msg_1',
        turnId: 'turn_1',
        streaming: false,
        content: 'Hello world',
        orderKey: '9998:000000000000042:000000:02:000000000',
      }),
    ]);

    // message_complete arrives — flushes pending (applies the patch above),
    // then runs the streaming-off fallback (no-op since patch already did it)
    const changed = ct.applyEvent({
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'Hello world',
      seq: 42,
      queued_count: 0,
      messages: [],
      completed_at: '2026-01-01T00:00:01Z',
      context_usage: null,
      token_usage: null,
      attachments: [],
      chat_mode: undefined,
      chat_mode_source: undefined,
      partial: false,
      finish_reason: null,
      assistant_phase_index: 0,
      runtime: null,
    });

    // message_complete returns false (no timeline mutation from the store)
    expect(changed).toBe(false);
    // Exactly one assistant item — no duplicate
    expect(ct.size).toBe(1);
    const items = ct.toArray();
    if (items[0]!.kind === 'message') {
      // Spinner must be off
      expect(items[0]!.streaming).toBe(false);
    }
  });

  it('fallback finalizes streaming assistant when live.assistant_complete patch was not received', () => {
    const ct = new ChatTimeline();
    // Streaming assistant item — no completion patch queued
    ct.enqueuePatch([
      makeProjectedMessage({
        id: 'message:msg_1:phase:0',
        messageId: 'msg_1',
        turnId: 'turn_1',
        streaming: true,
        content: 'Hello world',
      }),
    ]);
    ct.flushPending();

    // message_complete arrives with no prior live.assistant_complete patch
    ct.applyEvent({
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'Hello world',
      seq: 42,
      queued_count: 0,
      messages: [],
      completed_at: '2026-01-01T00:00:01Z',
      context_usage: null,
      token_usage: null,
      attachments: [],
      chat_mode: undefined,
      chat_mode_source: undefined,
      partial: false,
      finish_reason: null,
      assistant_phase_index: 0,
      runtime: null,
    });

    // Fallback must have set streaming:false in place
    expect(ct.size).toBe(1);
    const items = ct.toArray();
    if (items[0]!.kind === 'message') {
      expect(items[0]!.streaming).toBe(false);
    }
  });

  it('message_complete does not create a second assistant item', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      makeProjectedMessage({
        id: 'message:msg_1:phase:0',
        messageId: 'msg_1',
        turnId: 'turn_1',
        streaming: true,
        content: 'Answer',
      }),
    ]);
    ct.flushPending();

    ct.applyEvent({
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'Answer',
      seq: 1,
      queued_count: 0,
      messages: [],
      completed_at: '2026-01-01T00:00:01Z',
      context_usage: null,
      token_usage: null,
      attachments: [],
      chat_mode: undefined,
      chat_mode_source: undefined,
      partial: false,
      finish_reason: null,
      assistant_phase_index: 0,
      runtime: null,
    });

    // Must still be exactly 1 item — no duplicate
    const assistants = ct.toArray().filter(
      (i) => i.kind === 'message' && i.role === 'assistant'
    );
    expect(assistants).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// _finalizeStreamingForTurn: all streaming items finalized on message_complete
// ---------------------------------------------------------------------------

describe('ChatTimeline _finalizeStreamingForTurn (via message_complete)', () => {
  function makeMessageComplete(overrides: Record<string, unknown> = {}) {
    return {
      type: 'message_complete' as const,
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'Done',
      seq: 42,
      queued_count: 0,
      messages: [],
      completed_at: '2026-01-01T00:00:01Z',
      context_usage: null,
      token_usage: null,
      attachments: [],
      chat_mode: undefined,
      chat_mode_source: undefined,
      partial: false,
      finish_reason: null,
      assistant_phase_index: 0,
      runtime: null,
      ...overrides,
    };
  }

  it('finalizes all streaming assistant phases for the turn', () => {
    // Multi-phase turn: phase 0 and phase 1 both streaming.
    // The completion patch only carries phase 1 (the final phase).
    // Phase 0 must also be finalized by the safety net.
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      { id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
        content: 'Phase 0', streaming: true, messageId: 'msg_1', turnId: 'turn_1',
        assistantPhaseIndex: 0 },
      { id: 'message:msg_1:phase:1', kind: 'message', role: 'assistant',
        content: 'Phase 1', streaming: true, messageId: 'msg_1', turnId: 'turn_1',
        assistantPhaseIndex: 1 },
    ]);
    ct.flushPending();

    ct.applyEvent(makeMessageComplete({ assistant_phase_index: 1 }));

    const assistants = ct.toArray().filter(
      (i): i is import('$lib/chat').MessageTimelineItem =>
        i.kind === 'message' && i.role === 'assistant',
    );
    expect(assistants).toHaveLength(2);
    // Both phases must be finalized — no hanging spinner
    expect(assistants.every((a) => a.streaming === false)).toBe(true);
  });

  it('finalizes streaming thinking blocks on message_complete', () => {
    // Thinking segment is still streaming when message_complete arrives.
    // No separate "thinking complete" event exists — the safety net must finalize it.
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      {
        id: 'thinking:msg_1:phase:0:blk_1',
        kind: 'thinking',
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        streaming: true,
        activeTitle: 'Thinking',
        blocks: [
          { block_id: 'blk_1', title: 'Thinking', content: 'Step 1', html: '',
            source: 'summary', complete: false },
        ],
        timestamp: null,
      },
    ]);
    ct.flushPending();

    ct.applyEvent(makeMessageComplete());

    const thinking = ct.toArray().filter((i) => i.kind === 'thinking');
    expect(thinking).toHaveLength(1);
    const thinkItem = thinking[0] as import('$lib/chat').ThinkingTimelineItem;
    // Thinking must be finalized — no hanging spinner
    expect(thinkItem.streaming).toBe(false);
    expect(thinkItem.activeTitle).toBeNull();
    // Blocks must be marked complete
    expect(thinkItem.blocks.every((b) => b.complete)).toBe(true);
  });

  it('thinking item id is stable as blocks accumulate (no orphan)', () => {
    // Simulates the runtime snapshot growing: first one block, then two.
    // The id must stay the same so the store merges rather than orphaning.
    const ct = new ChatTimeline();

    // First snapshot: one block
    ct.enqueuePatch([{
      id: 'thinking:msg_1:phase:0:blk_1',
      kind: 'thinking', messageId: 'msg_1', turnId: 'turn_1',
      assistantPhaseIndex: 0, streaming: true, activeTitle: 'Thinking',
      blocks: [{ block_id: 'blk_1', title: 'Thinking', content: 'Step 1',
        html: '', source: 'summary', complete: false }],
      timestamp: null,
    }]);
    ct.flushPending();
    expect(ct.size).toBe(1);

    // Second snapshot: two blocks — same id (first block id unchanged)
    ct.enqueuePatch([{
      id: 'thinking:msg_1:phase:0:blk_1',
      kind: 'thinking', messageId: 'msg_1', turnId: 'turn_1',
      assistantPhaseIndex: 0, streaming: true, activeTitle: 'Thinking',
      blocks: [
        { block_id: 'blk_1', title: 'Thinking', content: 'Step 1', html: '',
          source: 'summary', complete: true },
        { block_id: 'blk_2', title: 'Thinking', content: 'Step 2', html: '',
          source: 'summary', complete: false },
      ],
      timestamp: null,
    }]);
    ct.flushPending();

    // Must still be exactly 1 thinking item — no orphan, no duplicate
    expect(ct.size).toBe(1);
    const thinkItem = ct.toArray()[0] as import('$lib/chat').ThinkingTimelineItem;
    expect(thinkItem.kind).toBe('thinking');
    // Both blocks must be present after merge
    expect(thinkItem.blocks).toHaveLength(2);
  });

  it('does not finalize thinking items from a different turn', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'thinking:msg_other:phase:0:blk_x',
      kind: 'thinking', messageId: 'msg_other', turnId: 'turn_other',
      assistantPhaseIndex: 0, streaming: true, activeTitle: 'Thinking',
      blocks: [{ block_id: 'blk_x', title: 'Thinking', content: 'Other',
        html: '', source: 'summary', complete: false }],
      timestamp: null,
    }]);
    ct.flushPending();

    // message_complete for a different turn
    ct.applyEvent(makeMessageComplete({ message_id: 'msg_1', turn_id: 'turn_1' }));

    const thinkItem = ct.toArray()[0] as import('$lib/chat').ThinkingTimelineItem;
    // Must remain streaming — different turn
    expect(thinkItem.streaming).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Symptom 1: message disappears after refresh
//
// replaceAll must NOT evict unconfirmed live items that are transiently absent
// from a refresh projection (refresh racing event persistence). Persisted,
// terminal items absent from the snapshot are still evicted normally.
// ---------------------------------------------------------------------------

describe('ChatTimeline.replaceAll — refresh does not drop unconfirmed live items', () => {
  const PERSISTED_KEY = '0000:000000000000005:000000:02:000000000';
  const SENTINEL_KEY = '9999:999999999999999:000000:02:000000000';

  function streamingAssistant(overrides: Record<string, unknown> = {}) {
    return {
      id: 'msg_live',
      kind: 'message',
      role: 'assistant',
      content: 'Streaming answer',
      seq: 0,
      timestamp: '2026-01-01T00:00:00Z',
      messageId: 'msg_live',
      turnId: 'turn_live',
      assistantPhaseIndex: 0,
      streaming: true,
      orderKey: SENTINEL_KEY,
      sessionId: 'sess_1',
      ...overrides,
    };
  }

  function persistedUserMessage(overrides: Record<string, unknown> = {}) {
    return {
      id: 'user_1',
      kind: 'message',
      role: 'user',
      content: 'Question',
      seq: 4,
      timestamp: '2026-01-01T00:00:00Z',
      orderKey: '0000:000000000000004:000000:00:000000000',
      sessionId: 'sess_1',
      ...overrides,
    };
  }

  it('preserves a streaming assistant message absent from the refresh projection', () => {
    const ct = new ChatTimeline();
    // Live state: user message (persisted) + streaming assistant (not yet persisted)
    ct.replaceAll([persistedUserMessage(), streamingAssistant()]);
    expect(ct.toArray().map((i) => i.id)).toContain('msg_live');

    // A refresh fires before the assistant event is durably queryable: the
    // projection contains only the user message.
    ct.replaceAll([persistedUserMessage()]);

    const ids = ct.toArray().map((i) => i.id);
    expect(ids).toContain('user_1');
    // BUG (pre-fix): msg_live evicted. FIXED: preserved as unconfirmed live.
    expect(ids).toContain('msg_live');
  });

  it('preserves a just-finalized assistant carrying a sentinel orderKey', () => {
    const ct = new ChatTimeline();
    // Finalized (streaming:false) but still sentinel-keyed (not yet persisted)
    ct.replaceAll([
      persistedUserMessage(),
      streamingAssistant({ streaming: false, content: 'Final answer' }),
    ]);
    ct.replaceAll([persistedUserMessage()]); // refresh missing the assistant
    expect(ct.toArray().map((i) => i.id)).toContain('msg_live');
  });

  it('preserves a non-terminal tool_call absent from the refresh projection', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([
      persistedUserMessage(),
      {
        id: 'tool:call_live',
        kind: 'tool_call',
        callId: 'call_live',
        toolName: 'bash',
        status: 'running',
        timestamp: '2026-01-01T00:00:01Z',
        turnId: 'turn_live',
        sessionId: 'sess_1',
        orderKey: SENTINEL_KEY,
      },
    ]);
    ct.replaceAll([persistedUserMessage()]);
    expect(ct.toArray().map((i) => i.id)).toContain('tool:call_live');
  });

  it('still evicts a persisted, terminal item absent from the refresh (real removal)', () => {
    const ct = new ChatTimeline();
    ct.replaceAll([
      persistedUserMessage(),
      // A fully persisted, completed assistant message (real seq, not streaming).
      streamingAssistant({
        id: 'msg_old',
        messageId: 'msg_old',
        streaming: false,
        orderKey: PERSISTED_KEY,
      }),
    ]);
    // Server legitimately removed msg_old (e.g. compaction). Refresh omits it.
    ct.replaceAll([persistedUserMessage()]);
    const ids = ct.toArray().map((i) => i.id);
    expect(ids).toContain('user_1');
    expect(ids).not.toContain('msg_old');
  });

  it('REGRESSION A: evicts optimistic local-user: item when canonical echo is in the snapshot', () => {
    // Regression: the sentinel-orderKey preserve branch in _unconfirmedLiveIds
    // had no kind/role guard. An optimistic user message minted while the
    // assistant is streaming gets mintTailOrderKey → sentinel seq → was preserved
    // across a refresh even though the snapshot already contained the canonical
    // server user message → duplicate (one correct, one on the tail).
    const ct = new ChatTimeline();
    const TAIL_KEY = '9998:999999999999999:000001:00:000000001'; // sentinel tail key

    // State: optimistic user (local-user:, tail sentinel key) + streaming assistant
    ct.replaceAll([
      {
        id: 'local-user:cmsg_1',
        kind: 'message',
        role: 'user',
        content: 'Hello',
        seq: null,
        timestamp: '2026-01-01T00:00:00Z',
        optimistic: true,
        deliveryStatus: 'sending',
        clientMessageId: 'cmsg_1',
        orderKey: TAIL_KEY,
        sessionId: 'sess_1',
      },
      streamingAssistant(),
    ]);
    expect(ct.toArray().map((i) => i.id)).toContain('local-user:cmsg_1');

    // Refresh: snapshot contains the canonical server user message (different id)
    // but NOT the local-user: item. The optimistic item must be EVICTED.
    ct.replaceAll([
      {
        id: 'user_server_1',
        kind: 'message',
        role: 'user',
        content: 'Hello',
        seq: 1,
        timestamp: '2026-01-01T00:00:00Z',
        clientMessageId: 'cmsg_1',
        orderKey: '0000:000000000000001:000000:00:000000000',
        sessionId: 'sess_1',
      },
      streamingAssistant(),
    ]);

    const ids = ct.toArray().map((i) => i.id);
    expect(ids).toContain('user_server_1');
    // BUG (pre-fix): local-user:cmsg_1 was preserved → duplicate on tail.
    // FIXED: user messages are excluded from _unconfirmedLiveIds → evicted.
    expect(ids).not.toContain('local-user:cmsg_1');
    expect(ids).toHaveLength(2); // user_server_1 + streaming assistant only
  });
});

// ---------------------------------------------------------------------------
// Symptom 3: hanging spinner on clean turn end (finalize gaps)
// ---------------------------------------------------------------------------

describe('ChatTimeline.message_complete — finalizes all turn streaming (gap closure)', () => {
  function makeComplete(overrides: Record<string, unknown> = {}) {
    return {
      type: 'message_complete' as const,
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'Done',
      seq: 42,
      queued_count: 0,
      messages: [],
      completed_at: '2026-01-01T00:00:01Z',
      ...overrides,
    } as unknown as import('$lib/types/api').CognisWebSocketEvent;
  }

  it('finalizes a tool_call with NO turnId at clean turn end', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      {
        id: 'tool:call_x',
        kind: 'tool_call',
        callId: 'call_x',
        toolName: 'memory_search',
        status: 'started',
        timestamp: null,
        // intentionally NO turnId — the gap that left it hanging pre-fix
        sessionId: 'sess_1',
        orderKey: '9999:999999999999999:000000:03:000000000',
      },
    ]);
    ct.flushPending();

    ct.applyEvent(makeComplete({ message_id: 'msg_1', turn_id: 'turn_1' }));

    const tool = ct.toArray()[0] as import('$lib/chat').ToolCallTimelineItem;
    expect(tool.kind).toBe('tool_call');
    // FIXED: an unattributed in-flight tool is finalized at turn end.
    expect(tool.status).toBe('completed');
  });

  it('finalizes streaming items when message_complete carries neither id', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      {
        id: 'msg_live',
        kind: 'message',
        role: 'assistant',
        content: 'Answer',
        seq: 0,
        timestamp: null,
        messageId: 'msg_live',
        turnId: 'turn_live',
        assistantPhaseIndex: 0,
        streaming: true,
        orderKey: '9999:999999999999999:000000:02:000000000',
        sessionId: 'sess_1',
      },
    ]);
    ct.flushPending();

    // message_complete with NO message_id and NO turn_id (the id-less gap).
    ct.applyEvent(makeComplete({ message_id: null, turn_id: null }));

    const msg = ct.toArray()[0] as import('$lib/chat').MessageTimelineItem;
    expect(msg.streaming).toBe(false);
  });

  it('still does NOT finalize a tool_call belonging to a DIFFERENT turn', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      {
        id: 'tool:call_other',
        kind: 'tool_call',
        callId: 'call_other',
        toolName: 'bash',
        status: 'running',
        timestamp: null,
        turnId: 'turn_other',
        sessionId: 'sess_1',
        orderKey: '9999:999999999999999:000000:03:000000000',
      },
    ]);
    ct.flushPending();

    ct.applyEvent(makeComplete({ message_id: 'msg_1', turn_id: 'turn_1' }));

    const tool = ct.toArray()[0] as import('$lib/chat').ToolCallTimelineItem;
    // Different turn — must stay running.
    expect(tool.status).toBe('running');
  });
});

// ---------------------------------------------------------------------------
// INV-PHASE-ORDER: within a turn, items must be ordered by (phase, kind_rank).
//
// Bug 1: completion item with real seq (small) jumped above sentinel-seq
//        earlier-phase thinking/tool siblings.
// Bug 2: phase-1 thinking appeared after a finalized phase-0 assistant because
//        on_thinking was called without canonical_items.
//
// The fix: build_assistant_completion_item uses sentinel-band orderKey;
//          on_thinking/on_token pass canonical_items to project_runtime_timeline_items.
// ---------------------------------------------------------------------------

describe('INV-PHASE-ORDER: phase ordering within a turn', () => {
  // Sentinel-seq orderKey (live runtime items before persistence)
  const SENTINEL = (phase: number, kindRank: number, local = 0) =>
    `9998:999999999999999:${String(phase).padStart(6, '0')}:${String(kindRank).padStart(2, '0')}:${String(local).padStart(9, '0')}`;
  // Real-seq orderKey (persisted / completion item)
  const REAL = (seq: number, phase: number, kindRank: number) =>
    `9998:${String(seq).padStart(15, '0')}:${String(phase).padStart(6, '0')}:${String(kindRank).padStart(2, '0')}:000000000`;

  it('Bug 1: completion item with real seq must NOT sort above sentinel-seq earlier-phase siblings', () => {
    const ct = new ChatTimeline();
    // Phase 0: thinking (sentinel) + tool (sentinel)
    // Phase 1: assistant completion (real seq 42) — BUG: sorts before phase-0 siblings
    ct.replaceAll([
      {
        id: 'thinking:msg_1:phase:0:blk_1',
        kind: 'thinking',
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        streaming: false,
        blocks: [{ block_id: 'blk_1', title: 'T', content: 'c', html: '', source: 'content', complete: true }],
        timestamp: null,
        orderKey: SENTINEL(0, 1),
      },
      {
        id: 'tool:call_1',
        kind: 'tool_call',
        callId: 'call_1',
        toolName: 'search',
        status: 'completed',
        timestamp: null,
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        sessionId: 'sess_1',
        orderKey: SENTINEL(0, 3),
      },
      {
        id: 'message:msg_1:phase:1',
        kind: 'message',
        role: 'assistant',
        content: 'Final answer',
        seq: 0,
        timestamp: null,
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 1,
        streaming: false,
        // FIXED: sentinel-band key — same band as siblings
        orderKey: SENTINEL(1, 2),
        sessionId: 'sess_1',
      },
    ]);

    const items = ct.toArray();
    const ids = items.map((i) => i.id);
    // Correct order: thinking(p0) → tool(p0) → assistant(p1)
    expect(ids.indexOf('thinking:msg_1:phase:0:blk_1')).toBeLessThan(ids.indexOf('tool:call_1'));
    expect(ids.indexOf('tool:call_1')).toBeLessThan(ids.indexOf('message:msg_1:phase:1'));
  });

  it('Bug 1 regression: real-seq completion item WOULD sort wrong (demonstrates the bug)', () => {
    const ct = new ChatTimeline();
    // Same scenario but with the OLD buggy real-seq orderKey for the completion
    ct.replaceAll([
      {
        id: 'thinking:msg_1:phase:0:blk_1',
        kind: 'thinking',
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        streaming: false,
        blocks: [{ block_id: 'blk_1', title: 'T', content: 'c', html: '', source: 'content', complete: true }],
        timestamp: null,
        orderKey: SENTINEL(0, 1),
      },
      {
        id: 'tool:call_1',
        kind: 'tool_call',
        callId: 'call_1',
        toolName: 'search',
        status: 'completed',
        timestamp: null,
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        sessionId: 'sess_1',
        orderKey: SENTINEL(0, 3),
      },
      {
        id: 'message:msg_1:phase:1',
        kind: 'message',
        role: 'assistant',
        content: 'Final answer',
        seq: 0,
        timestamp: null,
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 1,
        streaming: false,
        // OLD buggy key: real seq 42 sorts BEFORE sentinel-seq siblings
        orderKey: REAL(42, 1, 2),
        sessionId: 'sess_1',
      },
    ]);

    const items = ct.toArray();
    const ids = items.map((i) => i.id);
    // With the buggy key, assistant sorts FIRST (real seq 42 < sentinel 999...999)
    // This test documents the bug — it SHOULD fail INV-PHASE-ORDER
    const assistantIdx = ids.indexOf('message:msg_1:phase:1');
    const thinkingIdx = ids.indexOf('thinking:msg_1:phase:0:blk_1');
    // The bug: assistant (phase 1) renders before thinking (phase 0)
    expect(assistantIdx).toBeLessThan(thinkingIdx);
  });

  it('Bug 2: phase-1 thinking must sort after phase-0 assistant when both are sentinel', () => {
    const ct = new ChatTimeline();
    // Phase 0: assistant (sentinel, finalized)
    // Phase 1: thinking (sentinel) — BUG: with real-seq phase-0 assistant, thinking sorts after
    ct.replaceAll([
      {
        id: 'message:msg_1:phase:0',
        kind: 'message',
        role: 'assistant',
        content: 'Phase 0 answer',
        seq: 0,
        timestamp: null,
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        streaming: false,
        // FIXED: sentinel key — same band as phase-1 thinking
        orderKey: SENTINEL(0, 2),
        sessionId: 'sess_1',
      },
      {
        id: 'thinking:msg_1:phase:1:blk_2',
        kind: 'thinking',
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 1,
        streaming: true,
        blocks: [{ block_id: 'blk_2', title: 'T', content: 'c', html: '', source: 'content', complete: false }],
        timestamp: null,
        orderKey: SENTINEL(1, 1),
      },
    ]);

    const items = ct.toArray();
    const ids = items.map((i) => i.id);
    // Phase 0 assistant must come before phase 1 thinking
    expect(ids.indexOf('message:msg_1:phase:0')).toBeLessThan(
      ids.indexOf('thinking:msg_1:phase:1:blk_2'),
    );
  });

  it('INV-PHASE-ORDER invariant catches the bug on a snapshot', () => {
    // Simulate the buggy state: assistant (phase 1, real seq) before thinking (phase 0, sentinel)
    const items = [
      {
        id: 'message:msg_1:phase:1',
        kind: 'message',
        role: 'assistant',
        content: 'Final',
        seq: 0,
        timestamp: null,
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 1,
        streaming: false,
        orderKey: REAL(42, 1, 2),
      },
      {
        id: 'thinking:msg_1:phase:0:blk_1',
        kind: 'thinking',
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        streaming: false,
        blocks: [],
        timestamp: null,
        orderKey: SENTINEL(0, 1),
      },
    ];
    const violations = checkPhaseOrder(items as unknown as import('$lib/chat').TimelineItem[], 0);
    expect(violations.length).toBeGreaterThan(0);
    expect(violations[0]!.invariant).toBe('INV-PHASE-ORDER');
  });
});

// ---------------------------------------------------------------------------
// Thinking id stability: no duplicate when first block completes and is popped
// ---------------------------------------------------------------------------

describe('ChatTimeline thinking id stability across block completion', () => {
  // Simulate the session_cache first_block_id anchor in the projected items.
  // The backend now emits the stable anchor; the client store is id-keyed so
  // a stable id means no orphan and no duplicate.

  function thinkingPatch(id: string, blockIds: string[], streaming: boolean) {
    return {
      id,
      kind: 'thinking' as const,
      messageId: 'msg_1',
      turnId: 'turn_1',
      assistantPhaseIndex: 0,
      streaming,
      activeTitle: streaming ? 'Thinking' : null,
      blocks: blockIds.map((bid) => ({
        block_id: bid,
        title: 'Thinking',
        content: `content of ${bid}`,
        html: '',
        source: 'content' as const,
        complete: !streaming,
      })),
      timestamp: null,
      orderKey: '9998:999999999999999:000000:01:000000000',
    };
  }

  it('no duplicate when first block completes and id stays stable (anchor fix)', () => {
    const ct = new ChatTimeline();
    const STABLE_ID = 'thinking:msg_1:phase:0:blk_first';

    // Phase 1: blk_first streaming — id = thinking:...:blk_first
    ct.enqueuePatch([thinkingPatch(STABLE_ID, ['blk_first'], true)]);
    ct.flushPending();
    expect(ct.size).toBe(1);
    expect(ct.toArray()[0]!.id).toBe(STABLE_ID);

    // Phase 2: blk_first completed (still in patch), blk_second added
    // id still = thinking:...:blk_first (anchor stable)
    ct.enqueuePatch([thinkingPatch(STABLE_ID, ['blk_first', 'blk_second'], true)]);
    ct.flushPending();
    expect(ct.size).toBe(1); // still ONE item — no duplicate
    expect(ct.toArray()[0]!.id).toBe(STABLE_ID);

    // Phase 3: blk_first popped from session_cache; only blk_second in snapshot.
    // WITHOUT the anchor fix, the backend would emit id thinking:...:blk_second
    // → two items in the store (orphan + duplicate). WITH the fix, id stays stable.
    ct.enqueuePatch([thinkingPatch(STABLE_ID, ['blk_second'], true)]);
    ct.flushPending();
    expect(ct.size).toBe(1); // still ONE item — anchor fix working
    expect(ct.toArray()[0]!.id).toBe(STABLE_ID);
  });

  it('duplicate would occur without the anchor fix (demonstrates the bug)', () => {
    const ct = new ChatTimeline();
    const ID_BEFORE_POP = 'thinking:msg_1:phase:0:blk_first';
    const ID_AFTER_POP = 'thinking:msg_1:phase:0:blk_second'; // what the buggy backend emitted

    // Before pop: id = blk_first
    ct.enqueuePatch([thinkingPatch(ID_BEFORE_POP, ['blk_first'], true)]);
    ct.flushPending();
    expect(ct.size).toBe(1);

    // After pop (buggy backend): id shifts to blk_second → DUPLICATE
    ct.enqueuePatch([thinkingPatch(ID_AFTER_POP, ['blk_second'], true)]);
    ct.flushPending();
    // With the buggy id, the store now has TWO items (the bug)
    expect(ct.size).toBe(2);
    const ids = ct.toArray().map((i) => i.id);
    expect(ids).toContain(ID_BEFORE_POP); // orphaned (stuck streaming)
    expect(ids).toContain(ID_AFTER_POP);  // duplicate
  });

  it('no stuck spinner after message_complete when id was stable throughout', () => {
    const ct = new ChatTimeline();
    const STABLE_ID = 'thinking:msg_1:phase:0:blk_first';

    ct.enqueuePatch([thinkingPatch(STABLE_ID, ['blk_first', 'blk_second'], true)]);
    ct.flushPending();

    // message_complete finalizes the turn
    ct.applyEvent({
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'Done',
      seq: 10,
      queued_count: 0,
      messages: [],
      completed_at: '2026-01-01T00:00:01Z',
    } as unknown as import('$lib/types/api').CognisWebSocketEvent);

    const items = ct.toArray();
    expect(items).toHaveLength(1);
    const thinking = items[0] as import('$lib/chat').ThinkingTimelineItem;
    expect(thinking.streaming).toBe(false); // no stuck spinner
  });
});
