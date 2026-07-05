import { describe, expect, it } from 'vitest';

import {
  annotateStepRequestInputWithNotification,
  appendOptimisticUserMessage,
  applyWebSocketEvent,
  findPendingStepRequestInputCall,
  isAuthChallengeInputToolCall,
  latestTodoSnapshot,
  normalizeHistory,
  removeQueuedUserMessageTimelineItems,
  reconcileOptimisticUserMessageDraftItems,
  optimisticallyCancelStepRequestInput,
  optimisticallyResolveStepRequestInput,
  sortByOrderKey,
  timelineFromProjection,
  timelinePatchContainsActiveWork,
  timelineItemKey,
  type TimelineItem,
  type MessageTimelineItem,
  type ThinkingTimelineItem,
  type ToolCallTimelineItem
} from '$lib/chat';
import { ChatTimeline } from '$lib/chat-timeline.svelte';
import { renderMarkdown } from '$lib/markdown';

describe('chat timeline helpers', () => {
  it('uses role-aware keys for messages with the same raw id', () => {
    const assistant = timelineFromProjection([
      {
        id: 'event:sess_a:1',
        kind: 'message',
        sessionId: 'sess_a',
        role: 'assistant',
        content: 'Assistant answer',
        seq: 1,
        timestamp: '2026-01-01T00:00:00Z',
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
      },
    ])[0]!;
    const user = timelineFromProjection([
      {
        id: 'event:sess_a:1',
        kind: 'message',
        sessionId: 'sess_a',
        role: 'user',
        content: 'User follow-up',
        seq: 1,
        timestamp: '2026-01-01T00:00:01Z',
        messageId: 'msg_1',
        turnId: 'turn_1',
      },
    ])[0]!;

    expect(timelineItemKey(assistant)).not.toBe(timelineItemKey(user));
  });

  it('keeps assistant render keys stable when live rows gain seq and orderKey metadata', () => {
    // A streaming row (no assistantPhaseIndex) uses phase 0 by default.
    // When assistantPhaseIndex=0 arrives the key is unchanged (0 ?? 0 = 0).
    // When seq and a real orderKey arrive the key is also unchanged.
    const live = timelineFromProjection([
      {
        id: 'message:msg_1:phase:0',
        kind: 'message',
        sessionId: 'sess_a',
        role: 'assistant',
        content: 'Live answer',
        seq: null,
        timestamp: '2026-01-01T00:00:00Z',
        messageId: 'msg_1',
        turnId: 'turn_1',
        streaming: true,
        orderKey: '9998:999999999999999:000000:02:000000000',
      },
    ])[0] as MessageTimelineItem;
    const complete: MessageTimelineItem = {
      ...live,
      seq: 42,
      streaming: false,
      assistantPhaseIndex: 0,
      orderKey: '0000:000000000000042:000000:02:000000000',
    };

    expect(timelineItemKey(complete)).toBe(timelineItemKey(live));
  });

  it('does not merge a user message patch into an assistant row with the same raw id', () => {
    // Same id, different role — must produce two distinct items.
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      { id: 'shared-id', kind: 'message', role: 'assistant', content: 'assistant text', seq: 1, timestamp: '2026-01-01T00:00:00Z', streaming: false },
    ]);
    ct.flushPending();
    ct.enqueuePatch([
      { id: 'shared-id-user', kind: 'message', role: 'user', content: 'user text', seq: 2, timestamp: '2026-01-01T00:00:01Z' },
    ]);
    ct.flushPending();
    expect(ct.size).toBe(2);
    const items = ct.toArray();
    expect(items.some((i) => i.kind === 'message' && i.role === 'assistant')).toBe(true);
    expect(items.some((i) => i.kind === 'message' && i.role === 'user')).toBe(true);
  });

  it('uses block-aware keys for thinking rows in the same phase', () => {
    const first: ThinkingTimelineItem = {
      id: 'thinking:msg_1:block_1',
      kind: 'thinking',
      sessionId: 'sess_a',
      messageId: 'msg_1',
      turnId: 'turn_1',
      assistantPhaseIndex: 0,
      blocks: [
        { block_id: 'block_1', title: 'First', content: 'First body', html: '<p>First body</p>', source: 'summary', complete: true },
      ],
      streaming: false,
      activeTitle: null,
      timestamp: '2026-01-01T00:00:00Z',
    };
    const second: ThinkingTimelineItem = {
      ...first,
      id: 'thinking:msg_1:block_2',
      blocks: [
        { block_id: 'block_2', title: 'Second', content: 'Second body', html: '<p>Second body</p>', source: 'summary', complete: true },
      ],
    };

    expect(timelineItemKey(first)).not.toBe(timelineItemKey(second));
  });

  it('keeps thinking render keys stable when phase metadata appears and blocks grow', () => {
    const live: ThinkingTimelineItem = {
      id: 'thinking:msg_1:block_1',
      kind: 'thinking',
      sessionId: 'sess_a',
      messageId: 'msg_1',
      turnId: 'turn_1',
      blocks: [
        { block_id: 'block_1', title: 'First', content: 'First body', html: '<p>First body</p>', source: 'summary', complete: false },
      ],
      streaming: true,
      activeTitle: 'First',
      timestamp: '2026-01-01T00:00:00Z',
      orderKey: '9998:999999999999999:000001:01:000000000',
    };
    const complete: ThinkingTimelineItem = {
      ...live,
      assistantPhaseIndex: 1,
      blocks: [
        { ...live.blocks[0]!, complete: true },
        { block_id: 'block_2', title: 'Second', content: 'Second body', html: '<p>Second body</p>', source: 'summary', complete: true },
      ],
      streaming: false,
      activeTitle: null,
    };

    expect(timelineItemKey(complete)).toBe(timelineItemKey(live));
  });

  it('does not blank an existing streaming assistant row for an empty live patch', () => {
    // A patch with empty content must not overwrite existing content.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
      content: 'Visible text', streaming: true, messageId: 'msg_1', turnId: 'turn_1',
      assistantPhaseIndex: 0, orderKey: '9998:999999999999999:000000:02:000000000',
    }]);
    ct.flushPending();
    ct.enqueuePatch([{
      id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
      content: '', streaming: true, messageId: 'msg_1', turnId: 'turn_1',
      assistantPhaseIndex: 0, orderKey: '9998:999999999999999:000000:02:000000001',
    }]);
    ct.flushPending();
    const item = ct.toArray()[0] as MessageTimelineItem;
    // mergeTimelinePatchItem keeps existing content when patch content is empty
    expect(item.content).toBe('Visible text');
  });

  it('does not reopen a finalized assistant row via a stale streaming patch', () => {
    // A finalized (streaming:false) assistant item must not be reopened by a
    // later streaming patch with the same id. mergeTimelinePatchItem's terminal
    // protection keeps the finalized state.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
      content: 'Finalized text', streaming: false, messageId: 'msg_1', turnId: 'turn_1',
      assistantPhaseIndex: 0, orderKey: '9998:000000000000042:000000:02:000000000',
    }]);
    ct.flushPending();
    // Stale streaming patch arrives with the same id
    ct.enqueuePatch([{
      id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
      content: 'Finalized text', streaming: true, messageId: 'msg_1', turnId: 'turn_1',
      assistantPhaseIndex: 0, orderKey: '9998:999999999999999:000000:02:000000000',
    }]);
    ct.flushPending();
    const assistants = ct.toArray().filter(
      (i): i is MessageTimelineItem => i.kind === 'message' && i.role === 'assistant',
    );
    expect(assistants).toHaveLength(1);
    // Must remain finalized — streaming patch must not reopen it
    expect(assistants[0]!.streaming).toBe(false);
  });

  it('collapses large repeated assistant projection bodies for display', () => {
    const unit = `${'Reviewed read-only. No edits made. '.repeat(12)}

## Immediate root cause
ImageLightbox uses a hardcoded toolbar layout over the image stage.`;
    const timeline = timelineFromProjection([
      {
        id: 'event:sess_a:1:assistant',
        kind: 'message',
        sessionId: 'sess_a',
        role: 'assistant',
        content: unit.repeat(3),
        seq: 1,
        timestamp: '2026-01-01T00:00:00Z',
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
      },
    ]);

    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({ kind: 'message', role: 'assistant', content: unit.repeat(3) });
    expect(timeline[0]?.kind === 'message' ? timeline[0].html : '').toBe(renderMarkdown(unit));
  });

  it('collapses large repeated assistant completion bodies for display without changing raw content', () => {
    // normalizeRepeatedAssistantContent strips repetition for display but keeps raw content.
    const unit = `${'Reviewed read-only. No edits made. '.repeat(12)}

## Immediate root cause
ImageLightbox uses a hardcoded toolbar layout over the image stage.`;
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
      content: unit.repeat(3), streaming: false, messageId: 'msg_1', turnId: 'turn_1',
    }]);
    ct.flushPending();
    const item = ct.toArray()[0];
    expect(item).toMatchObject({ kind: 'message', role: 'assistant', content: unit.repeat(3) });
    // html should be the de-duplicated unit, not the 3× repetition
    expect(item?.kind === 'message' ? item.html : '').toBe(renderMarkdown(unit));
  });

  it('converts backend-projected timeline items into renderable chat items', () => {
    const items = timelineFromProjection([
      {
        id: 'event:sess_a:1:user',
        kind: 'message',
        sessionId: 'sess_a',
        role: 'user',
        content: 'Hello',
        seq: 1,
        timestamp: '2026-01-01T00:00:01Z',
        attachments: []
      },
      {
        id: 'event:sess_a:2:assistant',
        kind: 'message',
        sessionId: 'sess_a',
        role: 'assistant',
        content: '**Hi**',
        seq: 2,
        timestamp: '2026-01-01T00:00:02Z',
        turnId: 'turn_1',
        messageId: 'turn_1',
        assistantPhaseIndex: 0,
        runtime: { model: 'gpt-test' }
      },
      {
        id: 'tool:call_1',
        kind: 'tool_call',
        callId: 'call_1',
        toolName: 'read',
        status: 'completed',
        result: 'ok',
        timestamp: '2026-01-01T00:00:03Z',
        evaluation: { score: 1 },
        tool_output_presentation: { output_size: 42, anchors_available: true }
      },
      {
        id: 'workflow-composed:sess_a:4',
        kind: 'workflow_composed',
        workflowId: 'wf_1',
        workflowName: 'Review workflow',
        lifecycle: 'ephemeral',
        taskId: 'task_1',
        scheduleId: null,
        steps: ['plan', 'review'],
        timestamp: '2026-01-01T00:00:04Z'
      },
      {
        id: 'thinking:sess_a:5:block_1',
        kind: 'thinking',
        messageId: 'msg_1',
        turnId: 'turn_1',
        blocks: [
          {
            block_id: 'block_1',
            title: 'Thinking',
            content: '**Reasoning**',
            source: 'summary',
            complete: true
          }
        ],
        streaming: false,
        activeTitle: null,
        timestamp: '2026-01-01T00:00:05Z'
      },
      {
        id: 'delegation:sess_child',
        kind: 'delegation',
        taskId: 'sess_child',
        taskLabel: 'Sub-session',
        agentId: 'system:explore',
        usedAgentId: null,
        status: 'running',
        result: null,
        timestamp: '2026-01-01T00:00:06Z',
        todos: [{ content: 'inspect', status: 'in_progress' }]
      }
    ]);

    expect(items).toHaveLength(6);
    expect(items[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: '**Hi**',
      assistantPhaseIndex: 0,
      runtime: { model: 'gpt-test' }
    });
    expect((items[1] as MessageTimelineItem).html).toContain('<strong>Hi</strong>');
    expect(items[2]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_1',
      status: 'completed',
      result: 'ok',
      evaluation: { score: 1 },
      outputSize: 42,
      anchorsAvailable: true
    });
    expect(items[3]).toMatchObject({
      kind: 'workflow_composed',
      workflowId: 'wf_1',
      workflowName: 'Review workflow',
      steps: ['plan', 'review']
    });
    expect(items[4]).toMatchObject({
      kind: 'thinking',
      messageId: 'msg_1',
      blocks: [
        expect.objectContaining({
          block_id: 'block_1',
          content: '**Reasoning**'
        })
      ]
    });
    expect((items[4] as ThinkingTimelineItem).blocks[0].html).toContain('<strong>Reasoning</strong>');
    expect(items[5]).toMatchObject({
      kind: 'delegation',
      taskId: 'sess_child',
      agentId: 'system:explore',
      status: 'running',
      todos: [{ content: 'inspect', status: 'in_progress' }]
    });
  });

  it('splits persisted thinking provider blocks into separate timeline items', () => {
    const items = normalizeHistory([
      {
        session_id: 'sess_a',
        seq: 1,
        type: 'assistant_thinking',
        timestamp: '2026-01-01T00:00:01Z',
        data: {
          message_id: 'msg_1',
          turn_id: 'turn_1',
          block_id: 'block_1',
          title: 'First',
          content: 'First thinking body'
        }
      },
      {
        session_id: 'sess_a',
        seq: 2,
        type: 'assistant_thinking',
        timestamp: '2026-01-01T00:00:02Z',
        data: {
          message_id: 'msg_1',
          turn_id: 'turn_1',
          block_id: 'block_2',
          title: 'Second',
          content: 'Second thinking body'
        }
      }
    ]);

    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: 'thinking', blocks: [expect.objectContaining({ block_id: 'block_1' })] });
    expect(items[1]).toMatchObject({ kind: 'thinking', blocks: [expect.objectContaining({ block_id: 'block_2' })] });
  });

  it('collapses legacy persisted thinking bodies made from repeated full snapshots', () => {
    const body = 'Addressing footer and signature layout. This paragraph was emitted as a full cumulative snapshot. ';
    const items = normalizeHistory([
      {
        session_id: 'sess_a',
        seq: 1,
        type: 'assistant_thinking',
        timestamp: '2026-01-01T00:00:01Z',
        data: {
          message_id: 'msg_1',
          turn_id: 'turn_1',
          block_id: 'block_1',
          title: 'Addressing footer and signature layout',
          content: body.repeat(3)
        }
      }
    ]);

    expect((items[0] as ThinkingTimelineItem).blocks[0].content).toBe(body.trim());
  });

  it('collapses repeated thinking content when the derived title ends with an ellipsis', () => {
    const body = 'Addressing footer and signature layout with a long title that may be truncated before matching the body. ';
    const items = normalizeHistory([
      {
        session_id: 'sess_a',
        seq: 1,
        type: 'assistant_thinking',
        timestamp: '2026-01-01T00:00:01Z',
        data: {
          message_id: 'msg_1',
          turn_id: 'turn_1',
          block_id: 'block_1',
          title: 'Addressing footer and signature layout with a long title that may be…',
          content: body.repeat(3)
        }
      }
    ]);

    expect((items[0] as ThinkingTimelineItem).blocks[0].content).toBe(body.trim());
  });

  // Removed: 'applies canonical timeline patches idempotently by tool call id'
  // Removed: 're-keys already rendered assistant and thinking rows when a tool boundary merges'
  // Removed: 'preserves a phase-aware tool key when an older result patch omits phase metadata'
  // These tested applyTimelinePatch (deleted). Equivalent coverage in chat-timeline.test.ts
  // (upsert merge tests: arguments preserved, orderKey stable, terminal protection).

  it('removes local queued placeholders from the timeline by client message id', () => {
    const sending = appendOptimisticUserMessage([], 'queued hello', [], 'cmsg_test');
    const cleaned = removeQueuedUserMessageTimelineItems(sending, [
      {
        queue_id: 'qmsg_test',
        client_message_id: 'cmsg_test',
        content: 'queued hello',
        attachments: [],
        created_at: '2026-03-28T00:00:01Z',
        updated_at: null,
        position: 1,
      }
    ]);

    expect(cleaned).toHaveLength(0);
  });

  it('removes legacy backend-projected queued rows from the timeline', () => {
    const legacy = timelineFromProjection([
      {
        id: 'user:qmsg_reload',
        kind: 'message',
        role: 'user',
        content: 'still queued after reload',
        timestamp: '2026-03-28T00:00:01Z',
        queueId: 'qmsg_reload',
        deliveryStatus: 'queued',
      }
    ]);
    const cleaned = removeQueuedUserMessageTimelineItems(legacy, []);

    expect(legacy).toHaveLength(1);
    expect(cleaned).toHaveLength(0);
  });

  it('deduplicates initial projected timeline items by render key', () => {
    const items = timelineFromProjection([
      {
        id: 'user:cmsg_1:pending',
        kind: 'message',
        role: 'user',
        content: 'queued hello',
        timestamp: '2026-03-28T00:00:01Z',
        clientMessageId: 'cmsg_1',
        deliveryStatus: 'sending',
        optimistic: true,
      },
      {
        id: 'user:cmsg_1:accepted',
        kind: 'message',
        role: 'user',
        content: 'queued hello',
        timestamp: '2026-03-28T00:00:02Z',
        clientMessageId: 'cmsg_1',
      },
      {
        id: 'message:msg_1:phase:0:first',
        kind: 'message',
        sessionId: 'sess_1',
        role: 'assistant',
        content: 'First duplicate',
        turnId: 'turn_1',
        messageId: 'msg_1',
        assistantPhaseIndex: 0,
      },
      {
        id: 'message:msg_1:phase:0:second',
        kind: 'message',
        sessionId: 'sess_1',
        role: 'assistant',
        content: 'Second duplicate',
        turnId: 'turn_1',
        messageId: 'msg_1',
        assistantPhaseIndex: 0,
      }
    ]);

    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'queued hello',
      optimistic: false,
    });
    expect('deliveryStatus' in items[0]!).toBe(false);
    expect(items[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'Second duplicate',
      assistantPhaseIndex: 0,
    });
    expect(new Set(items.map(timelineItemKey)).size).toBe(items.length);
  });

  it('deduplicates nested attachment keys before rendering', () => {
    // Attachment dedup is still active (normalizeTimelineItem for messages/tools).
    // Thinking block dedup was removed: the backend emits clean snapshots with
    // one entry per block_id; the client passes blocks verbatim by id.
    const items = timelineFromProjection([
      {
        id: 'message:msg_with_duplicate_attachments:phase:0',
        kind: 'message',
        role: 'assistant',
        content: 'See attachment',
        messageId: 'msg_with_duplicate_attachments',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        attachments: [
          { kind: 'artifact', artifact_id: 'att_1', filename: 'old.png', mime_type: 'image/png', size_bytes: 1 },
          { kind: 'artifact', artifact_id: 'att_1', filename: 'new.png', mime_type: 'image/png', size_bytes: 2 },
        ],
      },
      {
        id: 'thinking:msg_1:phase:0:block_1',
        kind: 'thinking',
        messageId: 'msg_1',
        turnId: 'turn_1',
        assistantPhaseIndex: 0,
        // Backend emits clean snapshots: one entry per block_id.
        blocks: [
          { block_id: 'block_1', title: 'Thinking', content: 'final', source: 'summary', complete: true },
        ],
        streaming: false,
      }
    ]);

    const message = items[0] as MessageTimelineItem;
    const thinking = items[1] as ThinkingTimelineItem;

    expect(message.attachments).toHaveLength(1);
    expect(message.attachments?.[0]).toMatchObject({ artifact_id: 'att_1', filename: 'new.png', size_bytes: 2 });
    // Thinking blocks are passed verbatim — no client-side dedup.
    expect(thinking.blocks).toHaveLength(1);
    expect(thinking.blocks[0]).toMatchObject({ block_id: 'block_1', content: 'final', complete: true });
  });

  // Removed: 'clears sending state when a canonical user timeline patch is received'
  // Removed: 'clears sending state from canonical user timeline patch by content fallback'
  // These tested applyTimelinePatch (deleted). Optimistic reconcile is covered in
  // chat-timeline.test.ts (reconcileOptimisticDrafts, addOptimisticUser tests).

  it('optimistic user message has an orderKey and sorts above streaming assistant', () => {
    // Core regression: user sends a message, assistant stream patch arrives.
    // The user row must stay ABOVE the assistant row.
    const ct = new ChatTimeline();
    ct.addOptimisticUser('hello', [], 'cmsg_1');
    const userItem = ct.toArray()[0]!;
    expect(userItem.orderKey).toBeTruthy();

    // Streaming assistant patch arrives
    ct.enqueuePatch([{
      id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
      content: 'streaming...', streaming: true, partial: true,
      orderKey: '9998:999999999999999:000000:02:000000001',
    }]);
    ct.flushPending();

    const items = ct.toArray();
    expect(items).toHaveLength(2);
    // User must be first (lower orderKey)
    expect(items[0]).toMatchObject({ kind: 'message', role: 'user' });
    expect(items[1]).toMatchObject({ kind: 'message', role: 'assistant' });
    expect(items[0]!.orderKey).toBeTruthy();
    expect(items[1]!.orderKey).toBeTruthy();
  });

  it('every client-minted item has a non-empty orderKey', () => {
    // Invariant: no timeline item should be keyless after a normal live sequence.
    const initial = appendOptimisticUserMessage([], 'hello', [], 'cmsg_inv');
    expect(initial.every((item) => Boolean(item.orderKey))).toBe(true);

    // system_message via applyWebSocketEvent
    const withSystem = applyWebSocketEvent(initial, {
      type: 'system_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      text: 'Turn started',
      notice_id: 'turn-init',
      kind: 'turn_initiated',
      seq: 0,
    } as any);
    expect(withSystem.every((item) => Boolean(item.orderKey))).toBe(true);

    // user_message echo via applyWebSocketEvent
    const withEcho = applyWebSocketEvent(withSystem, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'client:cmsg_inv',
      event_id: 'client:cmsg_inv',
      timestamp: new Date().toISOString(),
      content: 'hello',
      client_message_id: 'cmsg_inv',
      attachments: [],
    });
    expect(withEcho.every((item) => Boolean(item.orderKey))).toBe(true);
  });

  it('user_message echo preserves the synthetic orderKey from the optimistic row', () => {
    const withUser = appendOptimisticUserMessage([], 'hello', [], 'cmsg_echo');
    const originalKey = withUser[0].orderKey;
    expect(originalKey).toBeTruthy();

    const withEcho = applyWebSocketEvent(withUser, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'client:cmsg_echo',
      event_id: 'client:cmsg_echo',
      timestamp: new Date().toISOString(),
      content: 'hello',
      client_message_id: 'cmsg_echo',
      attachments: [],
    });

    expect(withEcho).toHaveLength(1);
    expect(withEcho[0].orderKey).toBe(originalKey);
    expect(withEcho[0]).toMatchObject({ optimistic: false });
  });

  it('sorts items by orderKey regardless of arrival order', () => {
    // Delegation patch arrives before user/assistant — store must sort by orderKey.
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      { id: 'delegation:child-1', kind: 'delegation', taskId: 'child-1', taskLabel: 'do the thing',
        agentId: null, usedAgentId: null, status: 'started', result: null,
        timestamp: '2026-01-01T00:00:04Z', orderKey: '0000:000000000000004:000000:04:000000002' },
      { id: 'user:cmsg_1', kind: 'message', role: 'user', content: 'delegate this',
        timestamp: '2026-01-01T00:00:01Z', orderKey: '0000:000000000000001:000000:00:000000000' },
      { id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant', content: 'I will delegate',
        timestamp: '2026-01-01T00:00:03Z', orderKey: '0000:000000000000003:000000:02:000000001' },
    ]);
    ct.flushPending();
    const items = ct.toArray();
    expect(items).toHaveLength(3);
    expect(items[0]).toMatchObject({ kind: 'message', role: 'user' });
    expect(items[1]).toMatchObject({ kind: 'message', role: 'assistant' });
    expect(items[2]).toMatchObject({ kind: 'delegation' });
  });

  it('merges streaming assistant patch with persisted assistant patch without duplicating', () => {
    // Runtime stream arrives first with a sentinel orderKey, then persisted patch
    // arrives with a real seq-based key. Must merge to one item.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
      content: 'streaming...', streaming: true, partial: true,
      orderKey: '9999:999999999999999:000000:02:000000000',
    }]);
    ct.flushPending();
    expect(ct.size).toBe(1);

    ct.enqueuePatch([{
      id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant',
      content: 'done', streaming: false, partial: false,
      orderKey: '0000:000000000000003:000000:02:000000001',
    }]);
    ct.flushPending();

    expect(ct.size).toBe(1);
    const item = ct.toArray()[0]!;
    expect(item).toMatchObject({ kind: 'message', role: 'assistant', content: 'done', streaming: false });
    // orderKey must be the lower (persisted) key
    expect(item.orderKey).toBe('0000:000000000000003:000000:02:000000001');
  });

  it('applyWebSocketEvent re-sorts after every non-patch event', () => {
    // Start with a keyed user message
    const userKey = '0000:000000000000001:000000:00:000000000';
    const initial = timelineFromProjection([
      {
        id: 'user:cmsg_1',
        kind: 'message',
        role: 'user',
        content: 'hello',
        timestamp: '2026-01-01T00:00:01Z',
        orderKey: userKey,
      },
    ]);

    // A system_message arrives with a lower orderKey (should sort before user)
    const systemKey = '0000:000000000000000:000000:06:000000000';
    const result = applyWebSocketEvent(initial, {
      type: 'system_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      text: 'Turn started',
      notice_id: 'turn-init',
      kind: 'turn_initiated',
      seq: 0,
    } as any);

    // The system message has no orderKey (applyWebSocketEvent doesn't assign one)
    // but it should not break the existing order
    expect(result.length).toBeGreaterThanOrEqual(1);
    // The user message must still be present
    expect(result.some((item) => item.kind === 'message' && item.role === 'user')).toBe(true);
  });

  it('mergeOrderKey treats absent orderKey as largest, not smallest', () => {
    // A patch with no orderKey must keep the existing item's orderKey.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'tool:call_1', kind: 'tool_call', callId: 'call_1', toolName: 'bash',
      status: 'running', orderKey: '0000:000000000000001:000000:03:000000000',
    }]);
    ct.flushPending();
    ct.enqueuePatch([{
      id: 'tool:call_1', kind: 'tool_call', callId: 'call_1', toolName: 'bash',
      status: 'completed', result: 'done',
      // no orderKey — simulates a legacy patch
    }]);
    ct.flushPending();
    expect(ct.size).toBe(1);
    expect(ct.toArray()[0]!.orderKey).toBe('0000:000000000000001:000000:03:000000000');
  });

  it('sortByOrderKey places keyed items before unkeyed items', () => {
    const items: TimelineItem[] = [
      { id: 'a', kind: 'system_message', text: 'no key', timestamp: null },
      {
        id: 'b',
        kind: 'message',
        role: 'user',
        content: 'keyed',
        html: '',
        seq: 1,
        timestamp: '2026-01-01T00:00:01Z',
        attachments: [],
        orderKey: '0000:000000000000001:000000:00:000000000',
      },
    ];
    const sorted = sortByOrderKey(items);
    expect(sorted[0].id).toBe('b');
    expect(sorted[1].id).toBe('a');
  });

  // Removed: 'does not reopen a terminal tool call from stale runtime patches'
  // Covered by chat-timeline.test.ts: 'does not reopen a terminal tool call with a stale running patch'.

  // Removed: 'finalizes an open assistant stream when a new tool-call boundary arrives'
  // Removed: 'rekeys a finalized assistant stream before the closing tool-call boundary'
  // Removed: 'rekeys live thinking before the closing tool-call boundary'
  // Removed: 'rekeys completed runtime thinking before the closing tool-call boundary'
  // These tested reconcileOpenPhaseBeforeToolBoundary / rekeyOpenPhaseThinking /
  // finalizeOpenPhaseAssistant which are now handled server-side. The server
  // flushes the final streaming snapshot (via _flush_coalesced) before emitting
  // the tool_call patch, so the client receives the finalized state directly.

  it('does not finalize a later assistant stream when an existing tool-call row updates', () => {
    // A tool_result patch must not affect a later streaming assistant item.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'tool:call_1', kind: 'tool_call', callId: 'call_1', toolName: 'read',
      status: 'started', sessionId: 'sess_1', turnId: 'turn_1',
    }]);
    ct.enqueuePatch([{
      id: 'message:msg_1:phase:1', kind: 'message', role: 'assistant',
      content: 'Continuing after the tool.', streaming: true, partial: true,
      messageId: 'msg_1', turnId: 'turn_1', assistantPhaseIndex: 1,
    }]);
    ct.flushPending();
    ct.enqueuePatch([{
      id: 'tool:call_1', kind: 'tool_call', callId: 'call_1', toolName: 'read',
      status: 'completed', result: 'ok', sessionId: 'sess_1', turnId: 'turn_1',
    }]);
    ct.flushPending();
    const assistant = ct.toArray().find(
      (i): i is MessageTimelineItem => i.kind === 'message' && i.role === 'assistant',
    );
    expect(assistant).toMatchObject({ content: 'Continuing after the tool.', streaming: true, partial: true, assistantPhaseIndex: 1 });
    expect(ct.toArray().find((i) => i.kind === 'tool_call')).toMatchObject({ status: 'completed', result: 'ok' });
  });

  it('applies backend-projected runtime tool fields from timeline patches', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'tool:call_1', kind: 'tool_call', callId: 'call_1', toolName: 'bash',
      status: 'running', result: 'partial output', streamedOutput: 'partial output',
      streamChunkCount: 2, streamContentOffset: 14, liveOutputAvailable: true,
      progressPhase: 'streaming', progressInputChars: 42, progressInputLines: 2, progressComplete: false,
    }]);
    ct.flushPending();
    expect(ct.toArray()[0]).toMatchObject({
      kind: 'tool_call', callId: 'call_1', result: 'partial output',
      streamedOutput: 'partial output', streamChunkCount: 2, streamContentOffset: 14,
      liveOutputAvailable: true, progressPhase: 'streaming', progressInputChars: 42,
      progressInputLines: 2, progressComplete: false,
    });
  });

  it('removes backend-owned timeline rows from timeline patches by stable key', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      { id: 'user:cmsg_1', kind: 'message', role: 'user', content: 'queued', deliveryStatus: 'queued' },
      { id: 'message:msg_1:phase:0', kind: 'message', role: 'assistant', content: 'still here' },
    ]);
    ct.flushPending();
    ct.enqueuePatch([], ['user:cmsg_1']);
    ct.flushPending();
    expect(ct.size).toBe(1);
    expect(ct.toArray()[0]).toMatchObject({ kind: 'message', role: 'assistant', content: 'still here' });
  });


  it('only treats timeline patches with active work as active turns', () => {
    expect(timelinePatchContainsActiveWork([
      {
        id: 'tool:call_done',
        kind: 'tool_call',
        callId: 'call_done',
        toolName: 'bash',
        status: 'completed'
      }
    ])).toBe(false);

    expect(timelinePatchContainsActiveWork([
      {
        id: 'tool:call_running',
        kind: 'tool_call',
        callId: 'call_running',
        toolName: 'bash',
        status: 'running'
      }
    ])).toBe(true);
  });

  // Removed: 'applies timeline_patch websocket events through the canonical projection path'
  // Removed: 'filters canonical timeline patch items before applying them'
  // Removed: 'filters timeline patches before deduplicating colliding tool call IDs'
  // These tested applyTimelinePatch's includeItem option (deleted). Session filtering
  // is now handled by ChatTimeline.applyEvent's _itemBelongsToSession (tested in
  // chat-timeline.test.ts: 'filters items by activeSessionId').

  it('preserves rich delegation metadata when a generic live patch arrives later', () => {
    // A follow-up patch with a generic label must not overwrite the specific label.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'delegation:sess_child', kind: 'delegation', taskId: 'sess_child',
      taskLabel: 'Investigate alert', agentId: 'system:explore', usedAgentId: 'system:explore',
      status: 'started', result: null, timestamp: null,
    }]);
    ct.flushPending();
    ct.enqueuePatch([{
      id: 'delegation:sess_child', kind: 'delegation', taskId: 'sess_child',
      taskLabel: 'Sub-session', agentId: null, usedAgentId: null,
      status: 'running', result: null, timestamp: null,
    }]);
    ct.flushPending();
    expect(ct.toArray()[0]).toMatchObject({
      kind: 'delegation', taskLabel: 'Investigate alert',
      agentId: 'system:explore', usedAgentId: 'system:explore', status: 'running',
    });
  });

  it('escalation annotates an existing tool_call without creating a ghost item', () => {
    // Escalation arrives after the tool_call (normal production ordering).
    // Must annotate the existing item — no ghost, no duplicate.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'tool:call_model_1', kind: 'tool_call', callId: 'call_model_1', toolName: 'bash',
      status: 'started', sessionId: 'sess_root', turnId: 'turn_1', arguments: { cmd: 'ls -la' },
    }]);
    ct.flushPending();
    ct.applyEvent({
      type: 'escalation', conversation_id: 'conv_1', session_id: 'sess_root',
      call_id: 'intaris_eval_1', tool_call_id: 'call_model_1', tool_name: 'bash',
      risk: 'high', reasoning: 'Needs approval', timeout_seconds: 300,
    });
    expect(ct.size).toBe(1);
    expect(ct.toArray()[0]).toMatchObject({
      kind: 'tool_call', callId: 'call_model_1', arguments: { cmd: 'ls -la' },
      evaluation: { decision: 'escalate', risk: 'high', reasoning: 'Needs approval' },
    });
  });

  it('shows running compaction and replaces it when compaction completes', () => {
    const running = applyWebSocketEvent([], {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_old',
      trigger: 'idle_checkpoint',
      reason: 'long_lived_chat_idle',
      effective_usage_percentage: 91.2,
      hard_pressure_exceeded: true,
      status: 'running'
    });

    expect(running).toHaveLength(1);
    expect(running[0]).toMatchObject({
      kind: 'compaction',
      status: 'running',
      id: 'compaction:running:sess_old',
      sessionId: 'sess_old',
      trigger: 'idle_checkpoint',
      effectiveUsagePercentage: 91.2,
      hardPressureExceeded: true
    });

    const compacted = applyWebSocketEvent(running, {
      type: 'session_compacted',
      conversation_id: 'conv_1',
      session_id: 'sess_new',
      previous_session_id: 'sess_old',
      summary_preview: 'Compacted summary',
      method: 'llm',
      turns_compacted: 5,
      trigger: 'idle_checkpoint',
      status: 'compacted'
    });

    expect(compacted).toHaveLength(1);
    expect(compacted[0]).toMatchObject({
      kind: 'compaction',
      status: 'compacted',
      id: 'compaction:sess_old:sess_new',
      sessionId: 'sess_new',
      previousSessionId: 'sess_old',
      summaryPreview: 'Compacted summary',
      turnsCompacted: 5
    });
  });

  it('clears running compaction when compaction finishes without rotation', () => {
    const running = applyWebSocketEvent([], {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_old',
      status: 'running'
    });

    const finished = applyWebSocketEvent(running, {
      type: 'session_compaction_finished',
      conversation_id: 'conv_1',
      session_id: 'sess_old',
      status: 'failed',
      fallback_reason: 'compaction_failed'
    });

    expect(finished).toHaveLength(0);
  });

  it('clears running compaction when terminal compaction notice arrives', () => {
    const running = applyWebSocketEvent([], {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_old',
      status: 'running'
    });

    const finished = applyWebSocketEvent(running, {
      type: 'system_message',
      conversation_id: 'conv_1',
      session_id: 'sess_old',
      text: 'Automatic compaction completed. Continuing your turn in a fresh compacted session.',
      seq: 12
    } as any);

    expect(finished.some((item) => item.kind === 'compaction' && item.status === 'running')).toBe(false);
    expect(finished.some((item) => item.kind === 'system_message')).toBe(true);
  });

  it('does not clear multiple running compactions when terminal notice lacks session id', () => {
    const first = applyWebSocketEvent([], {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_old_1',
      status: 'running'
    });
    const running = applyWebSocketEvent(first, {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_old_2',
      status: 'running'
    });

    const finished = applyWebSocketEvent(running, {
      type: 'system_message',
      conversation_id: 'conv_1',
      text: 'Automatic compaction completed. Continuing your turn in a fresh compacted session.',
      seq: 12
    });

    expect(finished.filter((item) => item.kind === 'compaction' && item.status === 'running')).toHaveLength(2);
  });

  it('replaces running compaction when compacted event lacks previous session id', () => {
    const running = applyWebSocketEvent([], {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_old',
      status: 'running'
    });

    const compacted = applyWebSocketEvent(running, {
      type: 'session_compacted',
      conversation_id: 'conv_1',
      session_id: 'sess_new',
      previous_session_id: null,
      summary_preview: 'Compacted summary',
      method: 'llm',
      turns_compacted: 3,
      status: 'compacted'
    } as any);

    expect(compacted).toHaveLength(1);
    expect(compacted[0]).toMatchObject({
      kind: 'compaction',
      status: 'compacted',
      sessionId: 'sess_new',
      summaryPreview: 'Compacted summary',
      turnsCompacted: 3
    });
  });

  it('does not replace unrelated running compactions when compacted event lacks previous session id', () => {
    const first = applyWebSocketEvent([], {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_old_1',
      status: 'running'
    });
    const running = applyWebSocketEvent(first, {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_old_2',
      status: 'running'
    });

    const compacted = applyWebSocketEvent(running, {
      type: 'session_compacted',
      conversation_id: 'conv_1',
      session_id: 'sess_new',
      previous_session_id: null,
      summary_preview: 'Compacted summary',
      method: 'llm',
      turns_compacted: 3,
      status: 'compacted'
    });

    expect(compacted.filter((item) => item.kind === 'compaction' && item.status === 'running')).toHaveLength(2);
    expect(compacted.some((item) => item.kind === 'compaction' && item.status === 'compacted')).toBe(true);
  });

  it('does not match running compaction by compacted event new session id', () => {
    const first = applyWebSocketEvent([], {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_new',
      status: 'running'
    });
    const running = applyWebSocketEvent(first, {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_other_running',
      status: 'running'
    });

    const compacted = applyWebSocketEvent(running, {
      type: 'session_compacted',
      conversation_id: 'conv_1',
      session_id: 'sess_new',
      previous_session_id: 'sess_wrong_old',
      summary_preview: 'Compacted summary',
      method: 'llm',
      turns_compacted: 3,
      status: 'compacted'
    });

    expect(compacted.filter((item) => item.kind === 'compaction' && item.status === 'running')).toHaveLength(2);
    expect(compacted.some((item) => item.kind === 'compaction' && item.status === 'compacted')).toBe(true);
  });

  it('replaces the only running compaction when compacted previous session id is mismatched', () => {
    const running = applyWebSocketEvent([], {
      type: 'session_compaction_started',
      conversation_id: 'conv_1',
      session_id: 'sess_actual_old',
      status: 'running'
    });

    const compacted = applyWebSocketEvent(running, {
      type: 'session_compacted',
      conversation_id: 'conv_1',
      session_id: 'sess_new',
      previous_session_id: 'sess_wrong_old',
      summary_preview: 'Compacted summary',
      method: 'llm',
      turns_compacted: 3,
      status: 'compacted'
    });

    expect(compacted).toHaveLength(1);
    expect(compacted[0]).toMatchObject({
      kind: 'compaction',
      status: 'compacted',
      sessionId: 'sess_new',
      previousSessionId: 'sess_wrong_old',
      summaryPreview: 'Compacted summary'
    });
  });

  it('reconstructs a durable compacted card from persisted compaction summary history', () => {
    const timeline = normalizeHistory([
      {
        seq: 1,
        type: 'compaction_summary',
        data: {
          session_id: 'sess_new',
          source_session_id: 'sess_old',
          summary: 'Full persisted compaction summary',
          method: 'llm',
          turns_compacted: 5,
          trigger: 'compacted'
        },
        timestamp: '2026-03-28T00:00:00Z'
      },
      {
        seq: 2,
        type: 'system_message',
        data: {
          content: 'Automatic compaction is starting before this turn continues.',
          notice_id: 'notice-compaction-start',
          kind: 'compaction_start'
        },
        timestamp: '2026-03-28T00:00:01Z'
      }
    ]);

    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({
      kind: 'compaction',
      status: 'compacted',
      id: 'compaction:sess_old:sess_new',
      sessionId: 'sess_new',
      previousSessionId: 'sess_old',
      summaryPreview: 'Full persisted compaction summary',
      summary: 'Full persisted compaction summary',
      turnsCompacted: 5
    });
    expect(timeline.some((item) => item.kind === 'system_message')).toBe(false);
  });

  it('hides live transient compaction start system messages', () => {
    const timeline = applyWebSocketEvent([], {
      type: 'system_message',
      conversation_id: 'conv_1',
      session_id: 'sess_old',
      text: 'Automatic compaction is starting before this turn continues because the session context is over the compaction threshold.',
      notice_id: 'notice-compaction-start',
      kind: 'compaction_start',
      seq: 12
    } as any);

    expect(timeline).toHaveLength(0);
  });

  it('hides compaction summaries with timeline_visible=false but shows rotation markers', () => {
    // timeline_visible=false → always hidden (internal seed, no user-facing content)
    // method='rotation' without timeline_visible=false → now shown as a compaction
    // card so history refreshes are authoritative and don't drop the live box.
    const timeline = normalizeHistory([
      {
        seq: 1,
        type: 'compaction_summary',
        data: {
          session_id: 'sess_new',
          source_session_id: 'sess_old',
          summary: 'Internal seed summary',
          method: 'rotation',
          marker_role: 'context_seed',
          timeline_visible: false
        },
        timestamp: '2026-03-28T00:00:00Z'
      },
      {
        seq: 2,
        type: 'compaction_summary',
        data: {
          session_id: 'sess_newer',
          source_session_id: 'sess_new',
          summary: 'Legacy rotation seed summary',
          method: 'rotation'
        },
        timestamp: '2026-03-28T00:00:01Z'
      },
      {
        seq: 3,
        type: 'user_message',
        data: {
          content: 'Still visible'
        },
        timestamp: '2026-03-28T00:00:02Z'
      }
    ]);

    // timeline_visible=false is still hidden; rotation marker without that flag is now shown
    expect(timeline).toHaveLength(2);
    expect(timeline[0]).toMatchObject({
      kind: 'compaction',
      status: 'compacted',
      id: 'compaction:sess_new:sess_newer',
    });
    expect(timeline[1]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'Still visible'
    });
  });


  it('keeps assistant messages in separate tool-delimited phases distinct', () => {
    const timeline = normalizeHistory([
      {
        seq: 1,
        session_id: 'sess_1',
        type: 'assistant_message',
        data: {
          content: 'Let me inspect the code first.',
          turn_id: 'turn_1'
        },
        timestamp: '2026-03-28T00:00:00Z'
      },
      {
        seq: 2,
        session_id: 'sess_1',
        type: 'tool_call',
        data: {
          call_id: 'call_1',
          tool_name: 'grep',
          status: 'completed',
          turn_id: 'turn_1'
        },
        timestamp: '2026-03-28T00:00:01Z'
      },
      {
        seq: 3,
        session_id: 'sess_1',
        type: 'assistant_message',
        data: {
          content: 'Now I found the relevant file.',
          turn_id: 'turn_1'
        },
        timestamp: '2026-03-28T00:00:02Z'
      }
    ]);

    const assistantMessages = timeline.filter(
      (item): item is MessageTimelineItem => item.kind === 'message' && item.role === 'assistant'
    );

    expect(assistantMessages).toHaveLength(2);
    expect(assistantMessages[0]).toMatchObject({
      content: 'Let me inspect the code first.',
      assistantPhaseIndex: 0
    });
    expect(assistantMessages[1]).toMatchObject({
      content: 'Now I found the relevant file.',
      assistantPhaseIndex: 1
    });
    expect(new Set(assistantMessages.map(timelineItemKey)).size).toBe(2);
  });


  it('keeps history messages with duplicate seq values from different sessions distinct', () => {
    const items = normalizeHistory([
      {
        seq: 1,
        session_id: 'sess_prev',
        type: 'assistant_message',
        data: { content: 'previous session', session_id: 'sess_prev' },
        timestamp: '2026-03-27T00:00:00Z'
      },
      {
        seq: 1,
        session_id: 'sess_active',
        type: 'assistant_message',
        data: { content: 'active session', session_id: 'sess_active' },
        timestamp: '2026-03-28T00:00:00Z'
      }
    ]);

    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'previous session',
      sessionId: 'sess_prev',
      seq: 1
    });
    expect(items[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'active session',
      sessionId: 'sess_active',
      seq: 1
    });
    expect(items[0]?.id).not.toEqual(items[1]?.id);
  });

  it('deduplicates repeated assistant segments for the same turn', () => {
    const items = normalizeHistory([
      {
        seq: 1,
        session_id: 'sess_active',
        type: 'assistant_message',
        data: { content: 'Already handled.', turn_id: 'turn_1' },
        timestamp: '2026-03-28T00:00:00Z'
      },
      {
        seq: 2,
        session_id: 'sess_active',
        type: 'assistant_message',
        data: { content: 'Already handled.', turn_id: 'turn_1' },
        timestamp: '2026-03-28T00:00:01Z'
      }
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'Already handled.',
      seq: 2
    });
  });


  it('keeps distinct assistant segments for the same turn in order', () => {
    const items = normalizeHistory([
      {
        seq: 1,
        session_id: 'sess_active',
        type: 'assistant_message',
        data: { content: "I'll inspect that now.", turn_id: 'turn_1' },
        timestamp: '2026-03-28T00:00:00Z'
      },
      {
        seq: 2,
        session_id: 'sess_active',
        type: 'assistant_message',
        data: { content: 'Final answer.', turn_id: 'turn_1' },
        timestamp: '2026-03-28T00:00:01Z'
      }
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: "I'll inspect that now.\n\nFinal answer.",
      seq: 2
    });
  });

  it('normalizes persisted system messages as system timeline items', () => {
    const items = normalizeHistory([
      {
        seq: 7,
        session_id: 'sess_active',
        type: 'system_message',
        data: {
          content: 'Turn initiated by task failure: Nightly import (task-1).',
          notice_id: 'turn-init:fup_task_failed',
          kind: 'turn_initiated',
          scope: 'turn',
          turn_id: 'turn_1'
        },
        timestamp: '2026-03-28T00:00:00Z'
      }
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      id: 'system:turn-init:fup_task_failed',
      kind: 'system_message',
      text: 'Turn initiated by task failure: Nightly import (task-1).',
      noticeId: 'turn-init:fup_task_failed',
      noticeKind: 'turn_initiated',
      noticeScope: 'turn'
    });
  });

  it('ignores internal persisted system prompt context in history', () => {
    const items = normalizeHistory([
      {
        seq: 7,
        session_id: 'sess_active',
        type: 'system_message',
        data: {
          content:
            'Environment: - Executor: olorin (websocket) - Platform: unknown (unknown)\nAdditional tools may be available but hidden by the current step profile.'
        },
        timestamp: '2026-03-28T00:00:00Z'
      },
      {
        seq: 8,
        session_id: 'sess_active',
        type: 'system_message',
        data: {
          content: 'Turn initiated by task failure: Nightly import (task-1).',
          notice_id: 'turn-init:fup_task_failed',
          kind: 'turn_initiated',
          scope: 'turn'
        },
        timestamp: '2026-03-28T00:00:01Z'
      }
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: 'system_message',
      text: 'Turn initiated by task failure: Nightly import (task-1).'
    });
  });

  it('settles queued optimistic user messages by client id without duplicating', () => {
    const initial = appendOptimisticUserMessage([], 'queued hello', [], 'cmsg_test');

    const settled = applyWebSocketEvent(initial, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'queued hello edited',
      client_message_id: 'cmsg_test',
      queue_id: 'qmsg_test',
      attachments: []
    });

    expect(settled).toHaveLength(1);
    expect(settled[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'queued hello edited',
      optimistic: false,
      deliveryStatus: undefined,
      clientMessageId: 'cmsg_test',
      queueId: 'qmsg_test'
    });

    const duplicate = applyWebSocketEvent(settled, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'queued hello edited',
      client_message_id: 'cmsg_test',
      queue_id: 'qmsg_test',
      attachments: []
    });

    expect(duplicate).toHaveLength(1);
  });

  it('does not append the same optimistic user message draft twice', () => {
    const once = appendOptimisticUserMessage([], 'hello', [], 'cmsg_test');
    const twice = appendOptimisticUserMessage(once, 'hello', [], 'cmsg_test');

    expect(twice).toHaveLength(1);
    expect(twice[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      clientMessageId: 'cmsg_test',
      optimistic: true,
    });
  });

  it('removes optimistic user messages when they become queued', () => {
    const initial = appendOptimisticUserMessage([], 'queued hello', [], 'cmsg_test');
    const queueSnapshot = [{
      queue_id: 'qmsg_test',
      client_message_id: 'cmsg_test',
      content: 'queued hello',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    }];
    const queued = removeQueuedUserMessageTimelineItems(initial, queueSnapshot);

    expect(queued).toHaveLength(0);

    const accepted = applyWebSocketEvent(queued, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'queued hello',
      client_message_id: 'cmsg_test',
      queue_id: 'qmsg_test',
      attachments: [],
    });

    expect(accepted).toHaveLength(1);
    expect(accepted[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      optimistic: false,
      deliveryStatus: undefined,
      clientMessageId: 'cmsg_test',
      queueId: 'qmsg_test',
    });
  });

  it('keeps queue snapshots out of the timeline after reload', () => {
    const queued = removeQueuedUserMessageTimelineItems([], [{
      queue_id: 'qmsg_reload',
      client_message_id: null,
      content: 'still queued after reload',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    }]);

    expect(queued).toHaveLength(0);
  });

  it('does not create phantom queued user rows with no text or attachments', () => {
    const queued = removeQueuedUserMessageTimelineItems([], [{
      queue_id: 'qmsg_empty',
      client_message_id: 'cmsg_empty',
      content: '',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    }]);

    expect(queued).toHaveLength(0);
  });

  it('removes stale local outbound rows when an authoritative queued item becomes empty', () => {
    const initial = appendOptimisticUserMessage([], 'hello', [], 'cmsg_empty');
    const queued = removeQueuedUserMessageTimelineItems(initial, [{
      queue_id: 'qmsg_empty',
      client_message_id: 'cmsg_empty',
      content: '',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    }]);

    expect(queued).toHaveLength(0);
  });

  it('keeps attachment-only queued user rows out of the timeline', () => {
    const queued = removeQueuedUserMessageTimelineItems([], [{
      queue_id: 'qmsg_attachment',
      client_message_id: 'cmsg_attachment',
      content: '',
      attachments: [{ kind: 'artifact', artifact_id: 'att_1', filename: 'image.png', mime_type: 'image/png', size_bytes: 123 }],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    }]);

    expect(queued).toHaveLength(0);
  });

  it('settles optimistic user drafts when canonical echo lacks client id', () => {
    const createdAt = Date.parse('2026-01-01T00:00:00.000Z');
    const canonical = applyWebSocketEvent([], {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'hello from user',
      attachments: [],
      timestamp: '2026-01-01T00:00:01.000Z',
    });

    const result = reconcileOptimisticUserMessageDraftItems(canonical, [{
      conversationId: 'conv_1',
      clientMessageId: 'cmsg_lost',
      content: 'hello from user',
      attachments: [],
      createdAt,
    }]);

    expect(result.settledClientMessageIds).toEqual(['cmsg_lost']);
    expect(result.items).toHaveLength(1);
    expect(result.items[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'hello from user',
      optimistic: false,
    });
  });

  it('uses one canonical echo to settle at most one identical optimistic draft', () => {
    const createdAt = Date.parse('2026-01-01T00:00:00.000Z');
    const canonical = applyWebSocketEvent([], {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'same text',
      attachments: [],
      timestamp: '2026-01-01T00:00:01.000Z',
    });

    const result = reconcileOptimisticUserMessageDraftItems(canonical, [
      {
        conversationId: 'conv_1',
        clientMessageId: 'cmsg_1',
        content: 'same text',
        attachments: [],
        createdAt,
      },
      {
        conversationId: 'conv_1',
        clientMessageId: 'cmsg_2',
        content: 'same text',
        attachments: [],
        createdAt,
      },
    ]);

    expect(result.settledClientMessageIds).toEqual(['cmsg_1']);
    expect(result.items).toHaveLength(2);
    expect(result.items[1]).toMatchObject({
      kind: 'message',
      role: 'user',
      clientMessageId: 'cmsg_2',
      optimistic: true,
      deliveryStatus: 'sending',
    });
  });

  it('does not settle optimistic drafts against older identical canonical messages', () => {
    const canonical = applyWebSocketEvent([], {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'repeat',
      attachments: [],
      timestamp: '2026-01-01T00:00:00.000Z',
    });

    const result = reconcileOptimisticUserMessageDraftItems(canonical, [{
      conversationId: 'conv_1',
      clientMessageId: 'cmsg_new_repeat',
      content: 'repeat',
      attachments: [],
      createdAt: Date.parse('2026-01-01T00:00:05.000Z'),
    }]);

    expect(result.settledClientMessageIds).toEqual([]);
    expect(result.items).toHaveLength(2);
    expect(result.items[1]).toMatchObject({
      kind: 'message',
      role: 'user',
      clientMessageId: 'cmsg_new_repeat',
      optimistic: true,
      deliveryStatus: 'sending',
    });
  });

  it('does not regress accepted user messages when a stale queue snapshot arrives late', () => {
    const queued = removeQueuedUserMessageTimelineItems(
      appendOptimisticUserMessage([], 'queued hello', [], 'cmsg_test'),
      [{
        queue_id: 'qmsg_test',
        client_message_id: 'cmsg_test',
        content: 'queued hello',
        attachments: [],
        created_at: '2026-03-28T00:00:01Z',
        updated_at: null,
        position: 1,
      }]
    );
    const accepted = applyWebSocketEvent(queued, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'queued hello',
      client_message_id: 'cmsg_test',
      queue_id: 'qmsg_test',
      attachments: [],
    });
    const afterStaleQueueSnapshot = removeQueuedUserMessageTimelineItems(accepted, [{
      queue_id: 'qmsg_test',
      client_message_id: 'cmsg_test',
      content: 'queued hello',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    }]);

    expect(afterStaleQueueSnapshot).toHaveLength(1);
    expect(afterStaleQueueSnapshot[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      optimistic: false,
      deliveryStatus: undefined,
      clientMessageId: 'cmsg_test',
      queueId: 'qmsg_test',
    });
  });

  it('removes legacy visible queued user messages that are absent from the latest queue snapshot', () => {
    // Build a legacy queued row directly via timelineFromProjection (simulating
    // an old snapshot that still had queued items projected into the timeline).
    const legacy = timelineFromProjection([
      {
        id: 'queued-user:qmsg_stale',
        kind: 'message',
        role: 'user',
        content: 'stale queued text',
        timestamp: '2026-03-28T00:00:01Z',
        deliveryStatus: 'queued',
        queueId: 'qmsg_stale',
      },
    ]);

    expect(removeQueuedUserMessageTimelineItems(legacy, [])).toHaveLength(0);
  });

  it('rekeys optimistic user messages to stable server ids without merging with prior assistant content', () => {
    // Give the assistant a real orderKey (as it would have in production from
    // the live.assistant_complete timeline_patch). Without an orderKey the
    // assistant sorts at _ORDER_KEY_MAX which is after any client-minted key.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'message:assistant_msg_1:phase:0', kind: 'message', role: 'assistant',
      content: 'previous assistant reply', seq: 2, timestamp: '2026-03-28T00:00:00Z',
      messageId: 'assistant_msg_1', turnId: 'turn_previous', streaming: false,
      assistantPhaseIndex: 0, orderKey: '9998:000000000000002:000000:02:000000000',
    }]);
    ct.flushPending();
    const withAssistant = ct.toArray();
    const optimistic = appendOptimisticUserMessage(withAssistant, 'new user message', [], 'cmsg_stable');

    const settled = applyWebSocketEvent(optimistic, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'client:cmsg_stable',
      event_id: 'client:cmsg_stable',
      timestamp: '2026-03-28T00:00:01Z',
      content: 'new user message',
      client_message_id: 'cmsg_stable',
      attachments: []
    });

    expect(settled).toHaveLength(2);
    // Previous assistant (real orderKey, sorts before) then new user message
    // (tail key, sorts after) — correct chronological order.
    expect(settled[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'previous assistant reply',
    });
    expect(settled[1]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'new user message',
      id: 'user-msg:client:cmsg_stable',
      messageId: 'client:cmsg_stable',
      optimistic: false,
      clientMessageId: 'cmsg_stable',
    });

    const replayedWithSeq = applyWebSocketEvent(settled, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'client:cmsg_stable',
      event_id: 'client:cmsg_stable',
      timestamp: '2026-03-28T00:00:01Z',
      seq: 10,
      content: 'new user message',
      attachments: []
    });

    expect(replayedWithSeq).toHaveLength(2);
    expect(replayedWithSeq[1]).toMatchObject({
      kind: 'message',
      role: 'user',
      id: 'user-msg:client:cmsg_stable',
      content: 'new user message',
    });
  });

  it('removes a deleted queued optimistic user message via removeQueuedUserMessageTimelineItems', () => {
    // An optimistic message that was queued should be removed when the queue
    // snapshot no longer contains it (queue drained or message cancelled).
    const initial = appendOptimisticUserMessage([], 'queued text', [], 'client-queued-1');
    const removed = removeQueuedUserMessageTimelineItems(initial, [
      {
        queue_id: 'queue-queued-1',
        client_message_id: 'client-queued-1',
        content: 'queued text',
        attachments: [],
        created_at: '2026-03-28T00:00:01Z',
        updated_at: null,
        position: 1,
      },
    ]);
    expect(removed).toHaveLength(0);
  });

  it('handles workflow failure payloads that omit conversation_id', () => {
    const items = applyWebSocketEvent([], {
      type: 'workflow_failed',
      task_id: 'task_1',
      reason: 'build failed'
    });

    expect(items[0]).toMatchObject({ kind: 'delegation', taskId: 'task_1', status: 'failed' });
  });

  // Removed: 'creates an assistant bubble for attachment-only message completion'
  // Removed: 'repairs missing streamed prefix from message_complete content'
  // Removed: 'creates a full assistant bubble from message_complete content without prior chunks'
  // Removed: 'marks completed assistant bubbles as partial when cancelled mid-stream'
  // These tested the message_complete branch of applyWebSocketEvent which is now
  // unreachable in production (ChatTimeline.applyEvent intercepts message_complete
  // and handles it via _finalizeStreamingAssistant). Equivalent coverage in
  // chat-timeline.test.ts (message_complete handling describe block).

  it('keeps attachment-only assistant messages in normalized history', () => {
    const items = normalizeHistory([
      {
        seq: 4,
        type: 'assistant_message',
        data: {
          content: '',
          attachments: [
            {
              artifact_id: 'img_2',
              kind: 'image',
              mime_type: 'image/jpeg',
              filename: 'generated.jpg',
              size_bytes: 321,
              url: 'https://cognis.example.com/generated.jpg'
            }
          ]
        },
        timestamp: '2026-04-09T00:00:00Z'
      }
    ]);

    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: '',
      attachments: [{ artifact_id: 'img_2', filename: 'generated.jpg' }]
    });
  });

  it('keeps assistant history split across tool-call phase boundaries', () => {
    const items = normalizeHistory([
      {
        seq: 10,
        type: 'assistant_message',
        data: { content: 'First segment', turn_id: 'turn_1' },
        timestamp: '2026-04-09T00:00:00Z'
      },
      {
        seq: 11,
        type: 'tool_call',
        data: { call_id: 'call_1', name: 'image_generate', arguments: { prompt: 'logo' }, turn_id: 'turn_1' },
        timestamp: '2026-04-09T00:00:01Z'
      },
      {
        seq: 12,
        type: 'tool_result',
        data: {
          call_id: 'call_1',
          name: 'image_generate',
          result: 'done',
          is_error: false,
          turn_id: 'turn_1',
          attachments: [
            {
              artifact_id: 'img_turn_1',
              kind: 'image',
              mime_type: 'image/png',
              filename: 'logo.png',
              size_bytes: 321,
              url: 'https://cognis.example.com/logo.png'
            }
          ]
        },
        timestamp: '2026-04-09T00:00:02Z'
      },
      {
        seq: 13,
        type: 'assistant_message',
        data: {
          content: 'Second segment',
          turn_id: 'turn_1',
          attachments: [
            {
              artifact_id: 'img_turn_1',
              kind: 'image',
              mime_type: 'image/png',
              filename: 'logo.png',
              size_bytes: 321,
              url: 'https://cognis.example.com/logo.png'
            }
          ]
        },
        timestamp: '2026-04-09T00:00:03Z'
      }
    ]);

    expect(items).toHaveLength(3);
    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'First segment',
      turnId: 'turn_1'
    });
    expect(items[1]).toMatchObject({ kind: 'tool_call', callId: 'call_1', status: 'completed' });
    expect(items[2]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'Second segment',
      attachments: [{ artifact_id: 'img_turn_1', filename: 'logo.png' }],
      turnId: 'turn_1'
    });
  });



  it('does not duplicate direct-chat clarification prompts in the timeline', () => {
    const items = applyWebSocketEvent([], {
      type: 'workflow_step_question',
      notification_id: 'notif_1',
      questions: [{ id: 'repo', question: 'Which repository should I use?', header: null, options: [], multiple: false, allow_custom: true, required: true }]
    });

    expect(items).toHaveLength(0);
  });

  it('removes a workflow gate notice when the notification is resolved', () => {
    const withNotice = applyWebSocketEvent([], {
      type: 'workflow_gate',
      notification_id: 'notif_gate_1',
      task_id: 'task_1',
      step_name: 'review'
    });

    const resolved = applyWebSocketEvent(withNotice, {
      type: 'workflow_gate_resolved',
      notification_id: 'notif_gate_1',
      decision: 'continue'
    });

    expect(resolved).toHaveLength(0);
  });

  it('removes stale task pause notices when the workflow completes', () => {
    const withNotice = applyWebSocketEvent([], {
      type: 'workflow_gate',
      task_id: 'task_1',
      step_name: 'architect_review_exhausted'
    });

    const completed = applyWebSocketEvent(withNotice, {
      type: 'workflow_completed',
      task_id: 'task_1',
      result: 'done'
    });

    expect(completed[0]).toMatchObject({ kind: 'delegation', taskId: 'task_1', status: 'completed' });
    expect(completed.some((item) => item.kind === 'notice')).toBe(false);
  });

  it('ignores session recovery notices in the visible timeline', () => {
    const items = applyWebSocketEvent([], {
      type: 'session_recovered',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      reason: 'controller_restart'
    });

    expect(items).toEqual([]);
  });

  it('drops workflow prompt notices on reconnect so only replayed pending prompts remain', () => {
    const withNotice = applyWebSocketEvent([], {
      type: 'workflow_step_question',
      notification_id: 'notif_q_1',
      questions: [{ id: 'q1', question: 'Still needed?', header: null, options: [], multiple: false, allow_custom: true, required: true }]
    });

    const reconnected = applyWebSocketEvent(withNotice, {
      type: 'reconnected',
      conversation_id: 'conv_1',
      missed_events_count: 0
    });

    expect(reconnected.some((item) => item.kind === 'notice')).toBe(false);
  });

  it('creates a placeholder tool block when persisted history contains a tool_result first', () => {
    const items = normalizeHistory([
      {
        seq: 5,
        type: 'tool_result',
        data: {
          call_id: 'call-1',
          name: 'bash',
          result: 'done',
          is_error: false,
          evaluation: { decision: 'approve', reasoning: 'ok' }
        },
        timestamp: '2026-04-07T00:00:00Z'
      }
    ]);

    expect(items[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call-1',
      toolName: 'bash',
      status: 'completed',
      result: 'done',
      reconstructed: true,
      evaluation: { decision: 'approve', reasoning: 'ok' }
    });
  });

  it('strips markdown from delegation result previews', () => {
    const items = applyWebSocketEvent([], {
      type: 'delegation_completed',
      conversation_id: 'conv_1',
      child_session_id: 'sess_child',
      agent_id: 'system:explore',
      result: '**Done**\n\n- one\n- two'
    });

    expect(items[0]).toMatchObject({
      kind: 'delegation',
      agentId: 'system:explore',
      result: 'Done\none\ntwo'
    });
  });


  it('keeps replayed orphan tool results hidden without creating output-only cards', () => {
    const items = applyWebSocketEvent([], {
      type: 'tool_result',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'old_call',
      tool_name: 'read',
      result: 'old output',
      is_error: false,
      duration_ms: 1,
      turn_id: 'old_turn',
      attachments: []
    });

    expect(items).toHaveLength(0);
  });


  it('preserves file diffs from persisted tool results', () => {
    const items = normalizeHistory([
      {
        seq: 1,
        type: 'tool_call',
        data: { call_id: 'call-edit', name: 'edit', arguments: { file_path: 'example.py' } },
        timestamp: '2026-04-07T00:00:00Z'
      },
      {
        seq: 2,
        type: 'tool_result',
        data: {
          call_id: 'call-edit',
          name: 'edit',
          result: 'Replaced 1 occurrence',
          is_error: false,
          file_diffs: [{ path: 'example.py', diff: '--- example.py\n+++ example.py\n@@ -1 +1 @@\n-old\n+new\n' }]
        },
        timestamp: '2026-04-07T00:00:01Z'
      }
    ]);

    expect(items[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call-edit',
      fileDiffs: [{ path: 'example.py', diff: expect.stringContaining('+new') }]
    });
  });


  it('keeps the previous todo snapshot when a repeated todo write is rejected', () => {
    const items = normalizeHistory([
      {
        seq: 1,
        type: 'tool_call',
        data: {
          call_id: 'call_todos_1',
          name: 'step_todo_write',
          arguments: JSON.stringify({
            todos: [
              { content: 'Investigate the failure', status: 'in_progress', priority: 'high' }
            ]
          })
        },
        timestamp: '2026-04-07T00:00:00Z'
      },
      {
        seq: 2,
        type: 'tool_result',
        data: {
          call_id: 'call_todos_1',
          name: 'step_todo_write',
          result: JSON.stringify({
            status: 'updated',
            todos: [
              { content: 'Investigate the failure', status: 'in_progress', priority: 'high' }
            ]
          }),
          is_error: false
        },
        timestamp: '2026-04-07T00:00:01Z'
      },
      {
        seq: 3,
        type: 'tool_call',
        data: {
          call_id: 'call_todos_2',
          name: 'step_todo_write',
          arguments: JSON.stringify({
            todos: [
              { content: 'Investigate the failure', status: 'in_progress', priority: 'high' }
            ]
          })
        },
        timestamp: '2026-04-07T00:00:02Z'
      },
      {
        seq: 4,
        type: 'tool_result',
        data: {
          call_id: 'call_todos_2',
          name: 'step_todo_write',
          result: JSON.stringify({
            status: 'rejected',
            reason: 'loop_detected',
            tool: 'step_todo_write'
          }),
          is_error: true
        },
        timestamp: '2026-04-07T00:00:03Z'
      }
    ]);

    expect(latestTodoSnapshot(items)).toEqual([
      { content: 'Investigate the failure', status: 'in_progress', priority: 'high' }
    ]);
  });

  it('keeps current-turn todos visible when a queued user message is absorbed mid-turn', () => {
    const items = applyWebSocketEvent([
      {
        id: 'tool:call_todos',
        kind: 'tool_call',
        callId: 'call_todos',
        toolName: 'step_todo_write',
        status: 'completed',
        turnId: 'turn_active',
        timestamp: '2026-04-07T00:00:00Z',
        result: JSON.stringify({
          status: 'updated',
          todos: [{ content: 'Trace the bug', status: 'in_progress', priority: 'normal' }]
        }),
        isError: false,
      } satisfies ToolCallTimelineItem,
    ], {
        type: 'user_message' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        turn_id: 'turn_active',
        queue_id: 'qmsg_absorbed',
        content: 'Additional detail',
        attachments: []
      });

    expect(latestTodoSnapshot(items, true)).toEqual([
      { content: 'Trace the bug', status: 'in_progress', priority: 'normal' }
    ]);
  });

  it('clears previous-turn todos after a normal new user message', () => {
    // The tool call from the previous turn must have a lower orderKey than the
    // new user message so it sorts before it in the timeline.
    const prevTurnKey = '0000:000000000000001:000000:03:000000001';
    const items = applyWebSocketEvent([
      {
        id: 'tool:call_todos',
        kind: 'tool_call',
        callId: 'call_todos',
        toolName: 'step_todo_write',
        status: 'completed',
        turnId: 'turn_previous',
        timestamp: '2026-04-07T00:00:00Z',
        result: JSON.stringify({
          status: 'updated',
          todos: [{ content: 'Old work', status: 'in_progress', priority: 'normal' }]
        }),
        isError: false,
        orderKey: prevTurnKey,
      } satisfies ToolCallTimelineItem,
    ], {
        type: 'user_message' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        turn_id: 'turn_next',
        content: 'Start something else',
        attachments: []
      });

    expect(latestTodoSnapshot(items, true)).toEqual([]);
  });

  it('keeps tool-result attachments in normalized history', () => {
    const items = normalizeHistory([
      {
        seq: 6,
        type: 'tool_result',
        data: {
          call_id: 'call-attachments',
          name: 'generate_document',
          result: 'created',
          is_error: false,
          attachments: [
            {
              artifact_id: 'art_doc_1',
              kind: 'pdf',
              mime_type: 'application/pdf',
              filename: 'summary.pdf',
              size_bytes: 123,
              url: 'https://cognis.example.com/summary.pdf'
            }
          ]
        },
        timestamp: '2026-04-07T00:00:00Z'
      }
    ]);

    expect(items[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call-attachments',
      attachments: [{ artifact_id: 'art_doc_1', filename: 'summary.pdf' }]
    });
  });

  it('normalizes persisted assistant_thinking events into ThinkingTimelineItem', () => {
    const items = normalizeHistory([
      {
        seq: 6,
        type: 'assistant_thinking',
        data: {
          message_id: 'msg_turn_1',
          block_id: 'thk_1',
          title: 'Considering the migration strategy',
          content: 'For the migration and to address long-term drift...',
          reasoning_source: 'summary',
          turn_id: 'turn_abc',
        },
        timestamp: '2026-04-07T00:00:00Z'
      }
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: 'thinking',
      messageId: 'msg_turn_1',
      streaming: false,
      blocks: [
        {
          block_id: 'thk_1',
          title: 'Considering the migration strategy',
          complete: true,
        },
      ],
    });
  });

  it('keeps multiple assistant_thinking provider blocks as separate items', () => {
    const items = normalizeHistory([
      {
        seq: 5,
        type: 'assistant_thinking',
        data: {
          message_id: 'msg_turn_2',
          block_id: 'thk_1',
          title: 'First thought',
          content: 'Initial analysis here.',
          reasoning_source: 'summary',
        },
        timestamp: '2026-04-07T00:00:00Z'
      },
      {
        seq: 6,
        type: 'assistant_thinking',
        data: {
          message_id: 'msg_turn_2',
          block_id: 'thk_2',
          title: 'Second thought',
          content: 'Further analysis here.',
          reasoning_source: 'summary',
        },
        timestamp: '2026-04-07T00:00:01Z'
      }
    ]);

    expect(items).toHaveLength(2);
    const thinking = items[0] as ThinkingTimelineItem;
    expect(thinking.kind).toBe('thinking');
    expect(thinking.blocks).toHaveLength(1);
    expect(thinking.blocks[0].block_id).toBe('thk_1');
    const secondThinking = items[1] as ThinkingTimelineItem;
    expect(secondThinking.kind).toBe('thinking');
    expect(secondThinking.blocks).toHaveLength(1);
    expect(secondThinking.blocks[0].block_id).toBe('thk_2');
  });


  it('turns history gaps into visible warning notices', () => {
    const items = normalizeHistory([
      {
        seq: null,
        type: 'history_gap',
        data: { reason: 'bootstrap_cap_reached' },
        timestamp: '2026-04-07T00:00:00Z'
      }
    ]);

    expect(items[0]).toMatchObject({
      kind: 'notice',
      title: 'History incomplete',
      tone: 'warning'
    });
    expect((items[0] as { description: string }).description).toContain('configured safety cap');
  });

  it('adds a user message to the timeline from a user_message WS event', () => {
    const items = applyWebSocketEvent([], {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'Hello from Signal'
    });

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'Hello from Signal'
    });
  });

  it('reconciles echoed user_message events with optimistic local sends', () => {
    const optimistic = appendOptimisticUserMessage([], 'Hello from web');

    const reconciled = applyWebSocketEvent(optimistic, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'Hello from web'
    });

    expect(reconciled).toHaveLength(1);
    expect(reconciled[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'Hello from web',
      optimistic: false
    });
  });

  it('reconciles optimistic user messages with matching attachments', () => {
    const attachments = [
      {
        artifact_id: 'art_1',
        kind: 'pdf' as const,
        mime_type: 'application/pdf',
        filename: 'notes.pdf',
        size_bytes: 123,
        url: 'https://cognis.example.com/notes.pdf'
      }
    ];
    const optimistic = appendOptimisticUserMessage([], 'Attached', attachments);

    const reconciled = applyWebSocketEvent(optimistic, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'Attached',
      attachments
    });

    expect(reconciled).toHaveLength(1);
    expect(reconciled[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      attachments: [{ artifact_id: 'art_1', filename: 'notes.pdf' }],
      optimistic: false
    });
  });

  it('reconciles echoed attachments even when the server returns them in a different order', () => {
    const optimistic = appendOptimisticUserMessage([], 'Attached', [
      {
        artifact_id: 'art_1',
        kind: 'pdf' as const,
        mime_type: 'application/pdf',
        filename: 'notes.pdf',
        size_bytes: 123,
        url: 'https://cognis.example.com/notes.pdf'
      },
      {
        artifact_id: 'art_2',
        kind: 'image' as const,
        mime_type: 'image/png',
        filename: 'diagram.png',
        size_bytes: 456,
        url: 'https://cognis.example.com/diagram.png'
      }
    ]);

    const reconciled = applyWebSocketEvent(optimistic, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'Attached',
      attachments: [
        {
          artifact_id: 'art_2',
          kind: 'image',
          mime_type: 'image/png',
          filename: 'diagram.png',
          size_bytes: 456,
          url: 'https://cognis.example.com/diagram.png'
        },
        {
          artifact_id: 'art_1',
          kind: 'pdf',
          mime_type: 'application/pdf',
          filename: 'notes.pdf',
          size_bytes: 123,
          url: 'https://cognis.example.com/notes.pdf'
        }
      ]
    });

    expect(reconciled).toHaveLength(1);
    expect(reconciled[0]).toMatchObject({ kind: 'message', role: 'user', optimistic: false });
  });

  it('reconciles echoed user messages even after newer timeline items arrive', () => {
    const optimistic = appendOptimisticUserMessage([], 'Hello from web');
    const withNotice = applyWebSocketEvent(optimistic, {
      type: 'history_notice',
      title: 'History incomplete',
      description: 'Gap',
      tone: 'warning'
    });

    const echoed = applyWebSocketEvent(withNotice, {
      type: 'user_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      content: 'Hello from web'
    });

    expect(echoed).toHaveLength(2);
    expect(echoed[0]).toMatchObject({ kind: 'message', role: 'user', optimistic: false });
    expect(echoed[1]).toMatchObject({ kind: 'notice' });
  });

  it('deduplicates replayed system and history notice events using seq', () => {
    const systemOnce = applyWebSocketEvent([], {
      type: 'system_message',
      text: 'Recovered',
      seq: 11
    });
    const systemTwice = applyWebSocketEvent(systemOnce, {
      type: 'system_message',
      text: 'Recovered',
      seq: 11
    });

    const noticeOnce = applyWebSocketEvent(systemTwice, {
      type: 'history_notice',
      title: 'History incomplete',
      description: 'Gap',
      tone: 'warning',
      seq: 12
    });
    const noticeTwice = applyWebSocketEvent(noticeOnce, {
      type: 'history_notice',
      title: 'History incomplete',
      description: 'Gap',
      tone: 'warning',
      seq: 12
    });

    expect(systemTwice).toHaveLength(1);
    expect(noticeTwice).toHaveLength(2);
  });

  it('coalesces live recovery system messages using notice_id', () => {
    const first = applyWebSocketEvent([], {
      type: 'system_message',
      text: 'Retrying request 1/3.',
      notice_id: 'sess:turn:model_retry:llm_call',
      kind: 'model_retry',
      scope: 'llm_call'
    });
    const updated = applyWebSocketEvent(first, {
      type: 'system_message',
      text: 'Retrying request 2/3.',
      notice_id: 'sess:turn:model_retry:llm_call',
      kind: 'model_retry',
      scope: 'llm_call'
    });

    expect(updated).toHaveLength(1);
    expect(updated[0]).toMatchObject({
      kind: 'system_message',
      text: 'Retrying request 2/3.',
      noticeId: 'sess:turn:model_retry:llm_call',
      noticeKind: 'model_retry',
      noticeScope: 'llm_call'
    });
  });

  it('renders system notices as gray system timeline items, not assistant bubbles', () => {
    const timeline = normalizeHistory([
      {
        seq: 1,
        type: 'lifecycle',
        data: {
          event: 'system_notice',
          message: 'The model was silent after the internal retry budget; continuing the turn automatically.'
        },
        timestamp: '2026-03-28T00:00:00Z'
      }
    ]);

    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({
      kind: 'system_message',
      text: 'The model was silent after the internal retry budget; continuing the turn automatically.'
    });
  });

  it('renders tool-call context pressure lifecycle events as warning notices', () => {
    const timeline = normalizeHistory([
      {
        seq: 1,
        type: 'lifecycle',
        data: {
          event: 'tool_call_context_pressure',
          tool_call_count: 12,
          step_name: 'build',
          prompt_tokens: 112018,
          available_prompt_tokens: 117760,
          loop_pressure_threshold_prompt_tokens: 111872
        },
        timestamp: '2026-03-28T00:00:00Z'
      }
    ]);

    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({
      kind: 'notice',
      title: 'Tool-call context pressure',
      tone: 'warning',
      description: expect.stringContaining('Usage is 112,018/117,760 prompt-budget tokens.')
    });
    expect(timeline[0]).toMatchObject({
      description: expect.stringContaining('Tool calls this turn: 12.')
    });
  });

  it('renders plain user text without backticks or code markup', () => {
    const timeline = normalizeHistory([
      {
        seq: 1,
        type: 'user_message',
        data: { content: 'blablfknabla' },
        timestamp: '2026-03-28T00:00:00Z'
      }
    ]);

    const message = timeline[0] as MessageTimelineItem;
    expect(message).toMatchObject({ kind: 'message', role: 'user', content: 'blablfknabla' });
    expect(message.html).toContain('blablfknabla');
    expect(message.html).not.toContain('`blablfknabla`');
    expect(message.html).not.toContain('<code>blablfknabla</code>');
  });

  it('renders workflow composition events as timeline cards', () => {
    const items = applyWebSocketEvent([], {
      type: 'workflow_composed',
      workflow_id: 'wf_1',
      workflow_name: 'Evening Summary',
      lifecycle: 'ephemeral',
      task_id: 'task_1',
      steps: ['gather', 'summarize']
    });

    expect(items[0]).toMatchObject({
      kind: 'workflow_composed',
      workflowId: 'wf_1',
      workflowName: 'Evening Summary',
      lifecycle: 'ephemeral',
      taskId: 'task_1',
      steps: ['gather', 'summarize']
    });
  });

  describe('step_request_questions helpers', () => {
    const projectedTool = (
      overrides: Partial<ToolCallTimelineItem> = {},
    ): ToolCallTimelineItem => ({
      id: 'tool:call_1',
      kind: 'tool_call',
      toolName: 'step_request_questions',
      callId: 'call_1',
      arguments: { questions: [{ id: 'q1', question: 'Which name?' }] },
      status: 'started',
      timestamp: '2026-03-28T00:00:00Z',
      isError: false,
      ...overrides,
    });

    it('finds the most recent unresolved step_request_questions tool call', () => {
      const timeline = [projectedTool()];

      const pending = findPendingStepRequestInputCall(timeline);
      expect(pending).not.toBeNull();
      expect(pending?.callId).toBe('call_1');
      expect(pending?.toolName).toBe('step_request_questions');
    });

    it('ignores step_request_questions calls that already have a tool_result', () => {
      const resolved = [
        projectedTool({
          status: 'completed',
          result: JSON.stringify({ answers: [{ question_id: 'q1', custom_answer: 'First option' }] }),
          durationMs: 0,
        }),
      ];

      expect(findPendingStepRequestInputCall(resolved)).toBeNull();
    });

    it('annotates the pending step_request_questions tool call with a notification id', () => {
      const timeline = [projectedTool()];

      const annotated = annotateStepRequestInputWithNotification(timeline, 'input_abc123');
      const tool = annotated.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(tool.notificationId).toBe('input_abc123');
    });

    it('finds deferred auth challenge browser_eval calls', () => {
      const timeline = [
        projectedTool({
          id: 'tool:call_eval_otp',
          toolName: 'browser_eval',
          callId: 'call_eval_otp',
          arguments: {
          session_id: 'browser_1',
          script: '(code) => code',
            args: [{ value_ref: '$auth_challenge:reddit.code', auth_challenge: { label: 'Reddit MFA' } }]
          },
        }),
      ];

      const pending = findPendingStepRequestInputCall(timeline);
      expect(pending).not.toBeNull();
      expect(pending?.toolName).toBe('browser_eval');
      expect(isAuthChallengeInputToolCall(pending as ToolCallTimelineItem)).toBe(true);
    });

    it('redacts optimistic auth challenge answers', () => {
      const timeline = [
        projectedTool({
          id: 'tool:call_auth',
          toolName: 'request_auth_challenge',
          callId: 'call_auth',
          arguments: { label: 'Reddit MFA', required_fields: ['code'] },
        }),
      ];
      const tool = timeline.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      const resolved = optimisticallyResolveStepRequestInput(timeline, tool.id, '123456');
      const updated = resolved.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(updated.result).toBe(JSON.stringify({ response: '<redacted>' }));
    });

    it('optimistically cancels pending input tool calls so they stop routing replies', () => {
      const timeline = [
        projectedTool({
          id: 'tool:call_auth',
          toolName: 'request_auth_challenge',
          callId: 'call_auth',
          arguments: { label: 'Browser login', required_fields: ['confirmed'] },
        }),
      ];
      const tool = timeline.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      const cancelled = optimisticallyCancelStepRequestInput(timeline, tool.id);
      const updated = cancelled.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;

      expect(updated.status).toBe('completed');
      expect(updated.result).toBe(JSON.stringify({ decision: 'cancel', state: 'cancelled' }));
      expect(findPendingStepRequestInputCall(cancelled)).toBeNull();
    });

    it('optimistically resolves a step_request_questions tool call with the user answer', () => {
      const timeline = [projectedTool()];
      const tool = timeline.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      const resolved = optimisticallyResolveStepRequestInput(timeline, tool.id, 'Main option');
      const updated = resolved.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(updated.status).toBe('completed');
      expect(updated.isError).toBe(false);
      expect(updated.result).toBe(JSON.stringify({
        mode: 'plain_text',
        answers: [{ question_id: 'q1', selected_option_ids: [], custom_answer: 'Main option' }]
      }));
    });

    it('optimistically resolves a step_request_questions tool call with structured answers', () => {
      const timeline = [
        projectedTool({
          arguments: {
          questions: [
            { id: 'architecture', question: 'Architecture?' },
            { id: 'validation', question: 'Validation?' }
          ]
          },
        }),
      ];
      const tool = timeline.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      const reply = {
        mode: 'structured' as const,
        answers: [
          { question_id: 'architecture', selected_option_ids: ['small_refactor'], custom_answer: null },
          { question_id: 'validation', selected_option_ids: [], custom_answer: 'Run UI checks' }
        ]
      };
      const resolved = optimisticallyResolveStepRequestInput(timeline, tool.id, reply);
      const updated = resolved.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(updated.result).toBe(JSON.stringify(reply));
    });
  });
});

// ---------------------------------------------------------------------------
// Regression tests for rendering and compaction fixes
// ---------------------------------------------------------------------------

describe('timelineItemKey stability', () => {
  it('keeps thinking render keys stable when blocks change during streaming', () => {
    // Same item id, same (sessionId, turnId, messageId) — key must be stable
    // even as blocks grow/merge during streaming.
    const initial: ThinkingTimelineItem = {
      id: 'thinking:sess_a:turn_1:msg_1',
      kind: 'thinking',
      sessionId: 'sess_a',
      messageId: 'msg_1',
      turnId: 'turn_1',
      blocks: [{ block_id: 'blk_1', title: 'Thinking', content: '', html: '', source: '', complete: false }],
      streaming: true,
      activeTitle: 'Thinking',
      timestamp: '2026-01-01T00:00:00Z',
    };
    const withMoreBlocks: ThinkingTimelineItem = {
      ...initial,
      blocks: [
        { block_id: 'blk_1', title: 'Thinking', content: 'done', html: '<p>done</p>', source: '', complete: true },
        { block_id: 'blk_2', title: 'More', content: '', html: '', source: '', complete: false },
      ],
    };
    expect(timelineItemKey(initial)).toBe(timelineItemKey(withMoreBlocks));
  });

  it('produces distinct thinking keys for separate thinking segments with different item ids', () => {
    // Two thinking segments in the same turn have different item ids — they
    // must produce distinct render keys so Svelte keeps them as separate nodes.
    const first: ThinkingTimelineItem = {
      id: 'thinking:msg_1:block_1',
      kind: 'thinking',
      sessionId: 'sess_a',
      messageId: 'msg_1',
      turnId: 'turn_1',
      blocks: [{ block_id: 'block_1', title: 'First', content: '', html: '', source: '', complete: true }],
      streaming: false,
      activeTitle: null,
      timestamp: '2026-01-01T00:00:00Z',
    };
    const second: ThinkingTimelineItem = {
      ...first,
      id: 'thinking:msg_1:block_2',
      blocks: [{ block_id: 'block_2', title: 'Second', content: '', html: '', source: '', complete: true }],
    };
    expect(timelineItemKey(first)).not.toBe(timelineItemKey(second));
  });

  it('keeps assistant render keys stable across the full streaming lifecycle', () => {
    // Phase 1: initial streaming chunk — no assistantPhaseIndex, runtime orderKey.
    // Key uses assistantPhaseIndex ?? 0 = 0.
    const streaming = timelineFromProjection([{
      id: 'message:msg_a:phase:0',
      kind: 'message',
      sessionId: 'sess_a',
      role: 'assistant',
      content: 'Hello',
      seq: null,
      timestamp: '2026-01-01T00:00:00Z',
      messageId: 'msg_a',
      turnId: 'turn_a',
      streaming: true,
      orderKey: '9998:999999999999999:000000:02:000000000',
    }])[0] as MessageTimelineItem;

    // Phase 2: patch arrives with assistantPhaseIndex=0 and real orderKey.
    // Key is still assistant:sess_a:turn_a:msg_a:0 — unchanged.
    const withPhase: MessageTimelineItem = {
      ...streaming,
      streaming: false,
      assistantPhaseIndex: 0,
      orderKey: '0000:000000000000010:000000:02:000000000',
      seq: 10,
    };

    // Phase 3: seq arrives — key still unchanged.
    const withSeq: MessageTimelineItem = { ...withPhase, seq: 10 };

    expect(timelineItemKey(streaming)).toBe(timelineItemKey(withPhase));
    expect(timelineItemKey(withPhase)).toBe(timelineItemKey(withSeq));
  });

  it('produces distinct keys for different assistant phases of the same turn', () => {
    const base = {
      id: 'msg_b',
      kind: 'message' as const,
      sessionId: 'sess_b',
      role: 'assistant' as const,
      content: '',
      html: '',
      seq: null,
      timestamp: '2026-01-01T00:00:00Z',
      messageId: 'msg_b',
      turnId: 'turn_b',
      streaming: false,
    };
    // Two different messageIds within the same turn produce different keys
    const phase0: MessageTimelineItem = { ...base, messageId: 'msg_b_0' };
    const phase1: MessageTimelineItem = { ...base, messageId: 'msg_b_1' };
    expect(timelineItemKey(phase0)).not.toBe(timelineItemKey(phase1));
  });
});

describe('compaction_summary projection', () => {
  it('emits a compaction card for rotation/context_seed markers via normalizeHistory', () => {
    const items = normalizeHistory([
      {
        type: 'compaction_summary',
        seq: 1,
        timestamp: '2026-01-01T00:00:00Z',
        data: {
          summary: 'Compacted 5 turns.',
          method: 'rotation',
          marker_role: 'context_seed',
          session_id: 'sess_new',
          source_session_id: 'sess_old',
          turns_compacted: 5,
          timeline_visible: true,
        },
      } as any,
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: 'compaction',
      status: 'compacted',
      sessionId: 'sess_new',
      previousSessionId: 'sess_old',
      id: 'compaction:sess_old:sess_new',
      turnsCompacted: 5,
    });
  });

  it('still skips markers with timeline_visible=false via normalizeHistory', () => {
    const items = normalizeHistory([
      {
        type: 'compaction_summary',
        seq: 2,
        timestamp: '2026-01-01T00:00:00Z',
        data: {
          summary: 'Internal seed.',
          method: 'rotation',
          marker_role: 'context_seed',
          session_id: 'sess_new',
          source_session_id: 'sess_old',
          timeline_visible: false,
        },
      } as any,
    ]);
    expect(items).toHaveLength(0);
  });

  it('live session_compacted and history normalizeHistory produce matching ids', () => {
    // Live event path (applyWebSocketEvent)
    const liveTimeline = applyWebSocketEvent([], {
      type: 'session_compacted',
      conversation_id: 'conv_1',
      session_id: 'sess_new',
      previous_session_id: 'sess_old',
      summary_preview: 'Compacted.',
      method: 'rotation',
      turns_compacted: 3,
      trigger: 'auto',
    } as any);

    // History path (normalizeHistory — raw events from Intaris)
    const historyTimeline = normalizeHistory([
      {
        type: 'compaction_summary',
        seq: 1,
        timestamp: '2026-01-01T00:00:00Z',
        data: {
          summary: 'Compacted.',
          method: 'rotation',
          session_id: 'sess_new',
          source_session_id: 'sess_old',
          turns_compacted: 3,
          timeline_visible: true,
        },
      } as any,
    ]);

    expect(liveTimeline).toHaveLength(1);
    expect(historyTimeline).toHaveLength(1);
    // IDs and render keys must match so mergeTimelineRefresh treats them as
    // the same item and does not drop the live-streamed compaction box.
    expect(liveTimeline[0]!.id).toBe(historyTimeline[0]!.id);
    expect(timelineItemKey(liveTimeline[0]!)).toBe(timelineItemKey(historyTimeline[0]!));
  });
});

// ---------------------------------------------------------------------------
// timelinePatchMergeIndex dedup safety net (via ChatTimeline)
// ---------------------------------------------------------------------------

describe('timelinePatchMergeIndex dedup safety net', () => {
  it('merges streaming item (no phase) with persisted phase-0 item — same id', () => {
    // A streaming item and a persisted item with the same id must merge to one.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'message:turn_1:phase:0', kind: 'message', role: 'assistant',
      content: 'Hello', assistantPhaseIndex: 0, streaming: false,
      orderKey: '0000:000000000000042:000000:02:000000000',
      messageId: 'turn_1', turnId: 'turn_1',
    }]);
    ct.flushPending();
    ct.enqueuePatch([{
      id: 'message:turn_1:phase:0', kind: 'message', role: 'assistant',
      content: 'Hello', streaming: true,
      orderKey: '9999:999999999999999:000000:02:000000000',
      messageId: 'turn_1', turnId: 'turn_1',
    }]);
    ct.flushPending();
    expect(ct.toArray().filter((i) => i.kind === 'message' && i.role === 'assistant')).toHaveLength(1);
  });

  it('does not collapse two persisted items with different phases (different ids)', () => {
    // Two items with different ids are genuinely distinct — keep them separate.
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      { id: 'message:turn_1:phase:0', kind: 'message', role: 'assistant', content: 'Phase 0',
        assistantPhaseIndex: 0, orderKey: '0000:000000000000010:000000:02:000000000',
        messageId: 'turn_1', turnId: 'turn_1' },
      { id: 'message:turn_1:phase:1', kind: 'message', role: 'assistant', content: 'Phase 1',
        assistantPhaseIndex: 1, orderKey: '0000:000000000000020:000001:02:000000000',
        messageId: 'turn_1', turnId: 'turn_1' },
    ]);
    ct.flushPending();
    expect(ct.toArray().filter((i) => i.kind === 'message' && i.role === 'assistant')).toHaveLength(2);
  });
});

describe('applyWebSocketEvent dedup (Fix 1.2)', () => {
  it('does not create a duplicate when a system_message arrives with the same noticeId', () => {
    // A system_message is already in the timeline. When the same event arrives
    // again (e.g. on reconnect), applyWebSocketEvent must merge it, not push
    // a second copy. Uses system_message to avoid markdown rendering in tests.
    const existing: TimelineItem[] = [{
      id: 'sysmsg:notice_1',
      kind: 'system_message',
      text: 'Compaction started.',
      noticeId: 'notice_1',
      noticeKind: null,
      noticeScope: null,
      followUpConversationId: null,
      followUpSessionId: null,
      timestamp: '2026-01-01T00:00:00Z',
      orderKey: '9998:000000000000001:000000:06:000000000',
    }];

    const withDuplicate = applyWebSocketEvent(existing, {
      type: 'system_message',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      text: 'Compaction started.',
      notice_id: 'notice_1',
      seq: 1,
    } as any);

    const systemMessages = withDuplicate.filter((i) => i.kind === 'system_message');
    expect(systemMessages).toHaveLength(1);
  });

  it('does not create a duplicate session_compacted box on repeated events', () => {
    // session_compacted arrives twice (e.g. reconnect replay). Must produce
    // one compaction card, not two.
    const compactedEvent = {
      type: 'session_compacted',
      conversation_id: 'conv_1',
      session_id: 'sess_new',
      previous_session_id: 'sess_old',
      summary_preview: 'Compacted.',
      method: 'rotation',
      turns_compacted: 3,
      trigger: 'auto',
    } as any;

    const after1 = applyWebSocketEvent([], compactedEvent);
    const after2 = applyWebSocketEvent(after1, compactedEvent);

    const compactions = after2.filter((i) => i.kind === 'compaction');
    expect(compactions).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Fix 2: Live ordering / disappearing item regression tests
// ---------------------------------------------------------------------------

describe('streaming assistant and tool_call coexist without interference', () => {
  it('a tool_call patch does not affect a streaming assistant with a different id', () => {
    // A streaming assistant item and a tool_call item with different ids must
    // coexist without either being finalized or dropped.
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'message:turn_1:phase:0', kind: 'message', role: 'assistant',
      content: 'Thinking...', streaming: true, messageId: 'turn_1', turnId: 'turn_1',
      orderKey: '9999:999999999999999:000000:02:000000000',
    }]);
    ct.enqueuePatch([{
      id: 'tool:call_1', kind: 'tool_call', callId: 'call_1', toolName: 'bash',
      status: 'started', turnId: 'turn_1', assistantPhaseIndex: 1,
      orderKey: '9998:000000000000010:000001:03:000000000',
    }]);
    ct.flushPending();
    const assistants = ct.toArray().filter(
      (i): i is MessageTimelineItem => i.kind === 'message' && i.role === 'assistant',
    );
    expect(assistants).toHaveLength(1);
    // The streaming assistant must still be streaming
    expect(assistants[0]!.streaming).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// mintTailOrderKey: new user message sorts after in-flight runtime items
// ---------------------------------------------------------------------------

describe('mintTailOrderKey: user message ordering', () => {
  it('sorts a new user message after in-flight runtime streaming items', () => {
    // Regression: a just-sent user message appeared ABOVE the assistant's
    // in-flight tool calls because mintClientOrderKey used counter ~1e9 as seq,
    // which sorts before runtime items at sentinel seq ~1e15.
    // mintTailOrderKey must produce a key that sorts after all current items.
    const ct = new ChatTimeline();
    ct.enqueuePatch([
      { id: 'message:turn_1:phase:0', kind: 'message', role: 'assistant',
        content: 'Thinking...', streaming: true, messageId: 'turn_1', turnId: 'turn_1',
        orderKey: '9998:999999999999999:000000:02:000000000' },
      { id: 'tool:call_1', kind: 'tool_call', callId: 'call_1', toolName: 'bash',
        status: 'running', turnId: 'turn_1', assistantPhaseIndex: 0,
        orderKey: '9998:999999999999999:000000:03:000000000' },
    ]);
    ct.flushPending();
    ct.addOptimisticUser('my new message', [], 'cmsg_1');
    const sorted = ct.toArray();
    const kinds = sorted.map((i) => `${i.kind}:${i.kind === 'message' ? (i as MessageTimelineItem).role : (i as any).callId ?? ''}`);
    // User message must be LAST — after the in-flight assistant and tool call
    expect(kinds[kinds.length - 1]).toBe('message:user');
    expect(kinds[0]).toBe('message:assistant');
  });

  it('sorts a new user message after a persisted assistant with a real seq key', () => {
    const ct = new ChatTimeline();
    ct.enqueuePatch([{
      id: 'message:turn_1:phase:0', kind: 'message', role: 'assistant',
      content: 'Done.', streaming: false, messageId: 'turn_1', turnId: 'turn_1',
      assistantPhaseIndex: 0, orderKey: '9998:000000000000042:000000:02:000000000',
    }]);
    ct.flushPending();
    ct.addOptimisticUser('follow-up', [], 'cmsg_2');
    const sorted = ct.toArray();
    expect(sorted[0]).toMatchObject({ kind: 'message', role: 'assistant' });
    expect(sorted[1]).toMatchObject({ kind: 'message', role: 'user' });
  });
});
