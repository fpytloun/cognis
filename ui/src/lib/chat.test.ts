import { describe, expect, it } from 'vitest';

import {
  annotateStepRequestInputWithNotification,
  applyActiveStreamSnapshots,
  applyActiveThinkingSnapshots,
  applyActiveToolOutputSnapshots,
  applyRuntimeSnapshotOverlay,
  applyRuntimeTimelineEvent,
  appendOptimisticUserMessage,
  applyTimelinePatch,
  applyWebSocketEvent,
  clearActiveRuntimeTimelineItems,
  clearRuntimeOverlayForCanonicalEvent,
  createRuntimeTimelineOverlay,
  findPendingStepRequestInputCall,
  hydrateMessageCompleteFromRuntimeOverlay,
  projectDisplayTimeline,
  promoteRuntimeOverlayForCanonicalEvent,
  latestTodoSnapshot,
  normalizeHistory,
  pruneStaleQueuedUserMessages,
  removeQueuedOptimisticUserMessage,
  optimisticallyResolveStepRequestInput,
  timelineFromProjection,
  timelinePatchContainsActiveWork,
  timelineItemKey,
  upsertQueuedUserMessage,
  type TimelineItem,
  type MessageTimelineItem,
  type ThinkingTimelineItem,
  type ToolCallTimelineItem
} from '$lib/chat';

describe('chat timeline helpers', () => {
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

  it('applies canonical timeline patches idempotently by tool call id', () => {
    const started = applyTimelinePatch([], [
      {
        id: 'tool:call_1',
        kind: 'tool_call',
        callId: 'call_1',
        toolName: 'bash',
        status: 'started',
        arguments: { command: 'true' }
      }
    ]);

    const completed = applyTimelinePatch(started, [
      {
        id: 'tool:call_1',
        kind: 'tool_call',
        callId: 'call_1',
        toolName: 'bash',
        status: 'completed',
        result: 'ok'
      }
    ]);

    expect(completed).toHaveLength(1);
    expect(completed[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_1',
      status: 'completed',
      arguments: { command: 'true' },
      result: 'ok'
    });
  });

  it('clears stale active runtime items when an authoritative snapshot is empty', () => {
    const items = timelineFromProjection([
      {
        id: 'message:sess_a:turn_1:assistant',
        kind: 'message',
        role: 'assistant',
        content: 'partial',
        partial: true,
        streaming: true
      },
      {
        id: 'message:sess_a:turn_1:assistant-progress',
        kind: 'message',
        role: 'assistant',
        content: 'progress',
        partial: false,
        streaming: true
      },
      {
        id: 'thinking:msg_1:block_1',
        kind: 'thinking',
        messageId: 'msg_1',
        blocks: [{ id: 'block_1', content: 'thinking', isComplete: false }],
        streaming: true
      },
      {
        id: 'tool:call_1',
        kind: 'tool_call',
        callId: 'call_1',
        toolName: 'bash',
        status: 'running'
      },
      {
        id: 'tool:call_2',
        kind: 'tool_call',
        callId: 'call_2',
        toolName: 'bash',
        status: 'completed',
        result: 'ok'
      }
    ]);

    expect(clearActiveRuntimeTimelineItems(items)).toHaveLength(2);
    expect(clearActiveRuntimeTimelineItems(items)[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'progress',
      streaming: false
    });
    expect(clearActiveRuntimeTimelineItems(items)[1]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_2',
      status: 'completed'
    });

    const withoutToolOutputs = clearActiveRuntimeTimelineItems(items, {
      streams: false,
      toolOutputs: true,
      thinking: false
    });
    expect(withoutToolOutputs).toHaveLength(4);
    expect(withoutToolOutputs.some((item) => item.kind === 'message' && item.role === 'assistant' && item.partial)).toBe(true);
    expect(withoutToolOutputs.some((item) => item.kind === 'thinking' && item.streaming)).toBe(true);
    expect(withoutToolOutputs.some((item) => item.kind === 'tool_call' && item.callId === 'call_1')).toBe(false);

    const retainedStreams = clearActiveRuntimeTimelineItems(items, {
      streams: true,
      toolOutputs: false,
      thinking: false,
      retainStreamedAssistantMessages: true
    });
    expect(retainedStreams.some((item) => item.kind === 'message' && item.role === 'assistant' && item.content === 'partial')).toBe(true);
    expect(retainedStreams.find((item) => item.kind === 'message' && item.role === 'assistant' && item.content === 'partial')).toMatchObject({
      streaming: false,
      partial: true
    });
  });

  it('retains active tool cards while refreshing runtime tool output snapshots', () => {
    const items = normalizeHistory([
      {
        seq: 1,
        type: 'tool_call',
        timestamp: '2024-01-01T00:00:00Z',
        data: {
          call_id: 'call_1',
          name: 'bash',
          status: 'started'
        }
      }
    ] as any);

    const retained = clearActiveRuntimeTimelineItems(items, {
      streams: false,
      toolOutputs: true,
      thinking: false,
      retainActiveToolCalls: true
    });

    expect(retained).toHaveLength(1);
    expect(retained[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_1',
      status: 'started'
    });

    const cleared = clearActiveRuntimeTimelineItems(items, {
      streams: false,
      toolOutputs: true,
      thinking: false,
      retainActiveToolCalls: false
    });
    expect(cleared).toHaveLength(0);
  });

  it('projects runtime thinking overlays without mutating canonical timeline', () => {
    const canonical = normalizeHistory([
      {
        seq: 1,
        type: 'user_message',
        timestamp: '2024-01-01T00:00:00Z',
        data: { content: 'check', message_id: 'user_1', turn_id: 'turn_1' }
      }
    ] as any);
    const overlay = applyRuntimeSnapshotOverlay(createRuntimeTimelineOverlay(), {
      active_streams: [],
      active_tool_outputs: [],
      active_thinking: [
        {
          session_id: 'sess_1',
          message_id: 'think_1',
          turn_id: 'turn_1',
          blocks: [
            {
              block_id: 'block_1',
              title: 'Thinking',
              content: 'Inspecting',
              source: 'summary',
              complete: false
            }
          ]
        }
      ]
    });

    const displayed = projectDisplayTimeline(canonical, overlay);
    expect(canonical).toHaveLength(1);
    expect(displayed).toHaveLength(2);
    expect(displayed[1]).toMatchObject({ kind: 'thinking', streaming: true });

    const clearedOverlay = applyRuntimeSnapshotOverlay(overlay, {
      active_streams: [],
      active_tool_outputs: [],
      active_thinking: [],
      has_active_turn: true
    });
    expect(projectDisplayTimeline(canonical, clearedOverlay)).toHaveLength(1);
    expect(canonical).toHaveLength(1);
  });

  it('clears runtime assistant overlay when canonical completion arrives', () => {
    let overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'live',
      index: 0,
      content_offset: 0
    } as any);
    const canonical: TimelineItem[] = [];
    expect(projectDisplayTimeline(canonical, overlay)).toHaveLength(1);

    const completion = {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'live',
      seq: 7,
      partial: false
    } as any;
    overlay = clearRuntimeOverlayForCanonicalEvent(overlay, completion);
    const completed = applyWebSocketEvent(canonical, completion);

    expect(overlay.items).toHaveLength(0);
    expect(projectDisplayTimeline(completed, overlay)).toHaveLength(1);
    expect(projectDisplayTimeline(completed, overlay)[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      streaming: false,
      seq: 7
    });
  });

  it('hydrates contentless assistant completion from runtime overlay before clearing it', () => {
    let overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'streamed answer',
      index: 0,
      content_offset: 0
    } as any);
    const completion = {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      seq: 8,
      partial: false
    } as any;

    const hydrated = hydrateMessageCompleteFromRuntimeOverlay(completion, overlay);
    overlay = clearRuntimeOverlayForCanonicalEvent(overlay, hydrated);
    const canonical = applyWebSocketEvent([], hydrated);

    expect(overlay.items).toHaveLength(0);
    expect(canonical).toHaveLength(1);
    expect(canonical[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'streamed answer',
      streaming: false,
      seq: 8
    });
  });

  it('does not leak runtime tool output buffers into canonical tool calls', () => {
    const overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'tool_result_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      delta: 'runtime output',
      stream: 'stdout',
      content_offset: 0,
      chunk_index: 0
    } as any);

    const canonical = applyWebSocketEvent([], {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      status: 'started',
      arguments: {}
    } as any);

    expect(canonical[0]).toMatchObject({ kind: 'tool_call', callId: 'call_1' });
    expect((canonical[0] as ToolCallTimelineItem).result).toBeUndefined();
    const displayed = projectDisplayTimeline(canonical, overlay);
    expect(displayed[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_1',
      result: 'runtime output',
      liveOutputAvailable: true
    });
  });

  it('promotes streamed assistant overlay before same-turn tools on contentless completion', () => {
    let overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'assistant before tool',
      index: 0,
      content_offset: 0
    } as any);
    let canonical = applyWebSocketEvent([], {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      status: 'started',
      turn_id: 'turn_1'
    } as any);
    const completion = hydrateMessageCompleteFromRuntimeOverlay({
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      seq: 9,
      partial: false
    } as any, overlay);

    const beforePromotion = canonical;
    canonical = promoteRuntimeOverlayForCanonicalEvent(canonical, overlay, completion);
    const promoted = canonical !== beforePromotion;
    overlay = clearRuntimeOverlayForCanonicalEvent(overlay, completion);
    if (!promoted) {
      canonical = applyWebSocketEvent(canonical, completion);
    }

    expect(overlay.items).toHaveLength(0);
    expect(canonical.map((item) => item.kind)).toEqual(['message', 'tool_call']);
    expect(canonical[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'assistant before tool',
      streaming: false,
      seq: 9
    });
  });

  it('projects streamed assistant overlay before same-turn canonical tool calls before completion', () => {
    const overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'assistant before tool',
      index: 0,
      content_offset: 0
    } as any);
    const canonical = applyWebSocketEvent([], {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      status: 'started',
      turn_id: 'turn_1'
    } as any);

    const displayed = projectDisplayTimeline(canonical, overlay);

    expect(displayed.map((item) => item.kind)).toEqual(['message', 'tool_call']);
    expect(displayed[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'assistant before tool',
      streaming: true
    });
    expect(displayed[1]).toMatchObject({ kind: 'tool_call', callId: 'call_1' });
  });

  it('promotes runtime tool output before truncated canonical tool result', () => {
    const overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'tool_result_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      delta: 'long runtime output',
      stream: 'stdout',
      content_offset: 0,
      chunk_index: 0
    } as any);
    let canonical = applyWebSocketEvent([], {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      status: 'started',
      turn_id: 'turn_1'
    } as any);
    const result = {
      type: 'tool_result',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      result: 'short',
      is_error: false,
      duration_ms: 10,
      turn_id: 'turn_1',
      transport_truncated: true
    } as any;

    canonical = promoteRuntimeOverlayForCanonicalEvent(canonical, overlay, result);
    const completed = applyWebSocketEvent(canonical, result);

    expect(completed[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_1',
      result: 'long runtime output',
      streamedOutput: 'long runtime output',
      status: 'completed'
    });
  });

  it('promotes runtime tool output before truncated terminal timeline patch', () => {
    let canonical = applyWebSocketEvent([], {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_patch',
      tool_name: 'bash',
      timestamp: '2026-01-01T00:00:00.000Z',
      status: 'started'
    } as any);
    let overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'tool_result_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_patch',
      delta: 'complete streamed output',
      chunk_index: 0,
      content_offset: 0,
      is_error: false
    } as any);
    const patchEvent = {
      type: 'timeline_patch',
      conversation_id: 'conv_1',
      items: [
        {
          id: 'tool:call_patch',
          kind: 'tool_call',
          callId: 'call_patch',
          toolName: 'bash',
          status: 'completed',
          result: 'short',
          transportTruncated: true,
          timestamp: '2026-01-01T00:00:01.000Z'
        }
      ]
    } as any;

    canonical = promoteRuntimeOverlayForCanonicalEvent(canonical, overlay, patchEvent);
    overlay = clearRuntimeOverlayForCanonicalEvent(overlay, patchEvent);
    canonical = applyTimelinePatch(canonical, patchEvent.items);

    expect(overlay.items).toHaveLength(0);
    expect(canonical).toHaveLength(1);
    expect(canonical[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_patch',
      status: 'completed',
      result: 'complete streamed output',
      streamedOutput: 'complete streamed output',
      liveOutputAvailable: true
    });
  });

  it('keeps later streamed assistant overlay after same-turn tool activity', () => {
    let canonical: TimelineItem[] = [];
    let overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_first',
      turn_id: 'turn_1',
      content: 'before tool',
      index: 0,
      content_offset: 0,
      timestamp: '2026-01-01T00:00:00.000Z'
    } as any);
    const firstComplete = hydrateMessageCompleteFromRuntimeOverlay({
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_first',
      turn_id: 'turn_1',
      seq: 1,
      partial: false,
      timestamp: '2026-01-01T00:00:00.100Z'
    } as any, overlay);
    canonical = promoteRuntimeOverlayForCanonicalEvent(canonical, overlay, firstComplete);
    overlay = clearRuntimeOverlayForCanonicalEvent(overlay, firstComplete);
    canonical = applyWebSocketEvent(canonical, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      status: 'started',
      turn_id: 'turn_1',
      timestamp: '2026-01-01T00:00:01.000Z'
    } as any);
    canonical = applyWebSocketEvent(canonical, {
      type: 'tool_result',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      result: 'ok',
      is_error: false,
      turn_id: 'turn_1',
      timestamp: '2026-01-01T00:00:02.000Z'
    } as any);
    overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_second',
      turn_id: 'turn_1',
      content: 'after tool',
      index: 0,
      content_offset: 0,
      timestamp: '2026-01-01T00:00:03.000Z'
    } as any);

    const displayed = projectDisplayTimeline(canonical, overlay);

    expect(displayed.map((item) => item.kind)).toEqual(['message', 'tool_call', 'message']);
    expect(displayed[0]).toMatchObject({ kind: 'message', content: 'before tool' });
    expect(displayed[1]).toMatchObject({ kind: 'tool_call', callId: 'call_1' });
    expect(displayed[2]).toMatchObject({ kind: 'message', content: 'after tool', streaming: true });
  });

  it('copies runtime metadata when promoting streamed message completion', () => {
    const overlay = applyRuntimeTimelineEvent(createRuntimeTimelineOverlay(), {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_runtime',
      turn_id: 'turn_1',
      content: 'streamed',
      index: 0,
      content_offset: 0
    } as any);
    const completion = hydrateMessageCompleteFromRuntimeOverlay({
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_runtime',
      turn_id: 'turn_1',
      seq: 2,
      partial: false,
      chat_mode: 'build',
      chat_mode_source: 'one_shot',
      runtime: {
        agent_id: 'laforge',
        model: 'gpt-5.5',
        provider_id: 'openai',
        reasoning_effort: 'high'
      }
    } as any, overlay);

    const canonical = promoteRuntimeOverlayForCanonicalEvent([], overlay, completion);

    expect(canonical[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'streamed',
      chatMode: 'build',
      chatModeSource: 'one_shot',
      runtime: {
        agent_id: 'laforge',
        model: 'gpt-5.5',
        provider_id: 'openai',
        reasoning_effort: 'high'
      }
    });
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

  it('applies timeline_patch websocket events through the canonical projection path', () => {
    const timeline = applyWebSocketEvent([], {
      type: 'timeline_patch',
      conversation_id: 'conv_1',
      source: 'live.tool_call',
      items: [
        {
          id: 'tool:call_1',
          kind: 'tool_call',
          callId: 'call_1',
          toolName: 'bash',
          status: 'started'
        }
      ]
    });

    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_1',
      status: 'started'
    });
  });

  it('filters canonical timeline patch items before applying them', () => {
    const timeline = applyTimelinePatch([], [
      {
        id: 'tool:child_call',
        kind: 'tool_call',
        sessionId: 'sess_child',
        callId: 'child_call',
        toolName: 'bash',
        status: 'started'
      },
      {
        id: 'tool:root_call',
        kind: 'tool_call',
        sessionId: 'sess_root',
        callId: 'root_call',
        toolName: 'read',
        status: 'started'
      }
    ], {
      includeItem: (item) => item.kind !== 'tool_call' || item.sessionId === 'sess_root'
    });

    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({ kind: 'tool_call', callId: 'root_call', sessionId: 'sess_root' });
  });

  it('preserves rich delegation metadata when a generic live patch arrives later', () => {
    const started = applyTimelinePatch([], [
      {
        id: 'delegation:sess_child',
        kind: 'delegation',
        taskId: 'sess_child',
        taskLabel: 'Investigate alert',
        agentId: 'system:explore',
        usedAgentId: 'system:explore',
        status: 'started'
      }
    ]);
    const patched = applyTimelinePatch(started, [
      {
        id: 'delegation:sess_child',
        kind: 'delegation',
        taskId: 'sess_child',
        taskLabel: 'Sub-session',
        agentId: null,
        usedAgentId: null,
        status: 'running'
      }
    ]);

    expect(patched[0]).toMatchObject({
      kind: 'delegation',
      taskLabel: 'Investigate alert',
      agentId: 'system:explore',
      usedAgentId: 'system:explore',
      status: 'running'
    });
  });

  it('backfills a visible tool call when escalation arrives before the tool event', () => {
    const timeline = applyWebSocketEvent([], {
      type: 'escalation',
      conversation_id: 'conv_1',
      session_id: 'sess_root',
      call_id: 'intaris_eval_1',
      tool_call_id: 'call_model_1',
      tool_name: 'bash',
      risk: 'high',
      reasoning: 'Needs approval',
      timeout_seconds: 300
    });

    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({
      kind: 'tool_call',
      id: 'tool:call_model_1',
      callId: 'call_model_1',
      toolName: 'bash',
      status: 'started',
      sessionId: 'sess_root',
      evaluation: { decision: 'escalate', risk: 'high', reasoning: 'Needs approval' }
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

    expect(timeline).toHaveLength(2);
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
    expect(timeline[1]).toMatchObject({
      kind: 'system_message',
      text: 'Automatic compaction is starting before this turn continues.'
    });
  });

  it('hides rotated compaction seed summaries from persisted history', () => {
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

    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'Still visible'
    });
  });

  it('normalizes history messages and updates streaming assistant content', () => {
    const initial = normalizeHistory([
      {
        seq: 1,
        type: 'user_message',
        data: { content: 'hello' },
        timestamp: '2026-03-28T00:00:00Z'
      }
    ]);

    const streamed = applyWebSocketEvent(initial, {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      content: 'Hi',
      index: 0
    });

    const completed = applyWebSocketEvent(streamed, {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      seq: 2,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
      attachments: [
        {
          artifact_id: 'art_1',
          kind: 'pdf',
          mime_type: 'application/pdf',
          filename: 'report.pdf',
          size_bytes: 123,
          url: 'https://cognis.example.com/report.pdf'
        }
      ]
    });

    expect(completed[0]).toMatchObject({ kind: 'message', role: 'user', content: 'hello' });
    expect(completed[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'Hi',
      seq: 2,
      attachments: [
        {
          artifact_id: 'art_1',
          filename: 'report.pdf'
        }
      ]
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

  it('finalizes the current assistant phase after a tool call without modifying the previous phase', () => {
    let timeline = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'First assistant phase.',
      index: 0,
      content_offset: 0
    });

    timeline = applyWebSocketEvent(timeline, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'grep',
      status: 'started',
      turn_id: 'turn_1',
      arguments: {}
    });

    timeline = applyWebSocketEvent(timeline, {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      content: 'Second assistant phase.',
      index: 0,
      content_offset: 0
    });

    timeline = applyWebSocketEvent(timeline, {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_1',
      turn_id: 'turn_1',
      seq: 3,
      content: 'Second assistant phase.',
      token_usage: null,
      context_usage: null,
      queued_count: 0,
      attachments: []
    });

    const assistantMessages = timeline.filter(
      (item): item is MessageTimelineItem => item.kind === 'message' && item.role === 'assistant'
    );

    expect(assistantMessages.map((item) => item.content)).toEqual([
      'First assistant phase.',
      'Second assistant phase.'
    ]);
    expect(assistantMessages.map((item) => item.assistantPhaseIndex)).toEqual([0, 1]);
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

  it('keeps streamed pre-tool commentary when completion payload contains only final suffix', () => {
    let items = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_1',
      turn_id: 'turn_1',
      content: "I'll inspect that now.",
      index: 0,
      content_offset: 0
    });

    items = applyWebSocketEvent(items, {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_1',
      turn_id: 'turn_1',
      content: '\n\nFinal answer.',
      index: 1,
      content_offset: "I'll inspect that now.".length
    });

    items = applyWebSocketEvent(items, {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_1',
      turn_id: 'turn_1',
      content: 'Final answer.',
      seq: 3,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
      attachments: []
    });

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: "I'll inspect that now.\n\nFinal answer.",
      streaming: false,
      seq: 3
    });
  });

  it('uses completion payload when streamed text only happens to end with the same suffix', () => {
    let items = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_1',
      turn_id: 'turn_1',
      content: 'Draft: Final answer.',
      index: 0,
      content_offset: 0
    });

    items = applyWebSocketEvent(items, {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_1',
      turn_id: 'turn_1',
      content: 'Final answer.',
      seq: 2,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
      attachments: []
    });

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'Final answer.',
      streaming: false,
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

  it('keeps optimistic user messages visible when they become queued', () => {
    const initial = appendOptimisticUserMessage([], 'queued hello', [], 'cmsg_test');
    const queued = upsertQueuedUserMessage(initial, {
      queue_id: 'qmsg_test',
      client_message_id: 'cmsg_test',
      content: 'queued hello',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    });

    expect(queued).toHaveLength(1);
    expect(queued[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      content: 'queued hello',
      optimistic: true,
      deliveryStatus: 'queued',
      clientMessageId: 'cmsg_test',
      queueId: 'qmsg_test',
    });

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

  it('creates a visible queued user message from queue snapshots after reload', () => {
    const queued = upsertQueuedUserMessage([], {
      queue_id: 'qmsg_reload',
      client_message_id: null,
      content: 'still queued after reload',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    });

    expect(queued).toHaveLength(1);
    expect(queued[0]).toMatchObject({
      kind: 'message',
      role: 'user',
      id: 'queued-user:qmsg_reload',
      content: 'still queued after reload',
      optimistic: true,
      deliveryStatus: 'queued',
      queueId: 'qmsg_reload',
    });
  });

  it('does not regress accepted user messages when a stale queue snapshot arrives late', () => {
    const queued = upsertQueuedUserMessage(
      appendOptimisticUserMessage([], 'queued hello', [], 'cmsg_test'),
      {
        queue_id: 'qmsg_test',
        client_message_id: 'cmsg_test',
        content: 'queued hello',
        attachments: [],
        created_at: '2026-03-28T00:00:01Z',
        updated_at: null,
        position: 1,
      }
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
    const afterStaleQueueSnapshot = upsertQueuedUserMessage(accepted, {
      queue_id: 'qmsg_test',
      client_message_id: 'cmsg_test',
      content: 'queued hello',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    });

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

  it('prunes visible queued user messages that are absent from the latest queue snapshot', () => {
    const queued = upsertQueuedUserMessage([], {
      queue_id: 'qmsg_stale',
      client_message_id: null,
      content: 'stale queued text',
      attachments: [],
      created_at: '2026-03-28T00:00:01Z',
      updated_at: null,
      position: 1,
    });

    expect(pruneStaleQueuedUserMessages(queued, new Set())).toHaveLength(0);
  });

  it('rekeys optimistic user messages to stable server ids without merging with prior assistant content', () => {
    const withAssistant = applyWebSocketEvent([], {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'assistant_msg_1',
      turn_id: 'turn_previous',
      content: 'previous assistant reply',
      seq: 2,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
    });
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
    expect(settled[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'previous assistant reply',
      id: expect.stringContaining('assistant_msg_1'),
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

  it('removes a deleted queued optimistic user message by stable id', () => {
    const timeline = upsertQueuedUserMessage(
      appendOptimisticUserMessage([], 'queued text', [], 'client-queued-1'),
      {
        queue_id: 'queue-queued-1',
        client_message_id: 'client-queued-1',
        content: 'queued text',
        attachments: [],
        created_at: '2026-03-28T00:00:01Z',
        updated_at: null,
        position: 1,
      }
    );
    const removed = removeQueuedOptimisticUserMessage(
      timeline,
      'queue-queued-1',
      'client-queued-1',
      'queued text',
      []
    );
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

  it('creates an assistant bubble for attachment-only message completion', () => {
    const items = applyWebSocketEvent([], {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_2',
      seq: 3,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
      attachments: [
        {
          artifact_id: 'img_1',
          kind: 'image',
          mime_type: 'image/jpeg',
          filename: 'banner.jpg',
          size_bytes: 456,
          url: 'https://cognis.example.com/banner.jpg'
        }
      ]
    });

    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: '',
      seq: 3,
      attachments: [{ artifact_id: 'img_1', filename: 'banner.jpg' }]
    });
  });

  it('repairs missing streamed prefix from message_complete content', () => {
    const streamed = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      content: 'tail only',
      index: 1
    });

    const completed = applyWebSocketEvent(streamed, {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      content: 'full message with missing prefix and tail only',
      seq: 4,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
    });

    expect(completed[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'full message with missing prefix and tail only',
      seq: 4,
      streaming: false,
    });
  });

  it('creates a full assistant bubble from message_complete content without prior chunks', () => {
    const items = applyWebSocketEvent([], {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_full',
      content: 'completed reply',
      seq: 5,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
    });

    expect(items[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'completed reply',
      seq: 5,
      streaming: false,
    });
  });

  it('marks completed assistant bubbles as partial when cancelled mid-stream', () => {
    const streamed = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_partial',
      content: 'partial reply',
      index: 0
    });

    const completed = applyWebSocketEvent(streamed, {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_partial',
      content: 'partial reply',
      seq: 6,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
      partial: true,
      finish_reason: 'user_cancelled'
    });

    expect(completed[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'partial reply',
      seq: 6,
      streaming: false,
      partial: true,
      finishReason: 'user_cancelled'
    });
  });

  it('clears retained live-only partial state on canonical non-partial completion', () => {
    const retained = clearActiveRuntimeTimelineItems(timelineFromProjection([
      {
        id: 'message:msg_complete:live',
        kind: 'message',
        role: 'assistant',
        messageId: 'msg_complete',
        turnId: 'turn_1',
        content: 'completed reply',
        partial: true,
        streaming: true
      }
    ]), {
      streams: true,
      toolOutputs: false,
      thinking: false,
      retainStreamedAssistantMessages: true
    });

    const completed = applyWebSocketEvent(retained, {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_complete',
      turn_id: 'turn_1',
      content: 'completed reply',
      seq: 7,
      token_usage: null,
      context_usage: null,
      queued_count: 0
    });

    expect(completed).toHaveLength(1);
    expect(completed[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'completed reply',
      streaming: false,
      partial: false
    });
  });

  it('does not let stale active stream snapshots reopen canonical completed assistant messages', () => {
    const completed = applyWebSocketEvent([], {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_complete',
      turn_id: 'turn_1',
      content: 'completed reply',
      seq: 8,
      token_usage: null,
      context_usage: null,
      queued_count: 0
    });

    const withStaleSnapshot = applyActiveStreamSnapshots(completed, [
      {
        conversation_id: 'conv_1',
        message_id: 'msg_complete',
        turn_id: 'turn_1',
        session_id: 'sess_1',
        content: 'completed',
        chunk_count: 1,
        content_offset: 9,
        updated_at: '2026-06-16T12:00:00Z'
      }
    ]);

    expect(withStaleSnapshot).toHaveLength(1);
    expect(withStaleSnapshot[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'completed reply',
      streaming: false,
      partial: false
    });
  });

  it('does not let stale active stream snapshots reopen canonical cancelled partial messages', () => {
    const completed = applyWebSocketEvent([], {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_cancelled',
      turn_id: 'turn_1',
      content: 'cancelled reply',
      seq: 9,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
      partial: true,
      finish_reason: 'user_cancelled'
    });

    const withStaleSnapshot = applyActiveStreamSnapshots(completed, [
      {
        conversation_id: 'conv_1',
        message_id: 'msg_cancelled',
        turn_id: 'turn_1',
        session_id: 'sess_1',
        content: 'cancelled',
        chunk_count: 1,
        content_offset: 9,
        updated_at: '2026-06-16T12:00:00Z'
      }
    ]);

    expect(withStaleSnapshot).toHaveLength(1);
    expect(withStaleSnapshot[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'cancelled reply',
      streaming: false,
      partial: true,
      finishReason: 'user_cancelled'
    });
  });

  it('keeps retained streamed assistant content and marks it active with stale active stream snapshots', () => {
    const retained = clearActiveRuntimeTimelineItems(timelineFromProjection([
      {
        id: 'message:msg_retained:live',
        kind: 'message',
        role: 'assistant',
        messageId: 'msg_retained',
        turnId: 'turn_1',
        content: 'longer retained reply',
        partial: true,
        streaming: true,
        streamChunkCount: 4
      }
    ]), {
      streams: true,
      toolOutputs: false,
      thinking: false,
      retainStreamedAssistantMessages: true
    });

    const withStaleSnapshot = applyActiveStreamSnapshots(retained, [
      {
        conversation_id: 'conv_1',
        message_id: 'msg_retained',
        turn_id: 'turn_1',
        session_id: 'sess_1',
        content: 'short',
        chunk_count: 2,
        content_offset: 5,
        updated_at: '2026-06-16T12:00:00Z'
      }
    ]);

    expect(withStaleSnapshot).toHaveLength(1);
    expect(withStaleSnapshot[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'longer retained reply',
      streaming: true,
      partial: true,
      streamChunkCount: 4
    });
  });

  it('refreshes retained non-partial live assistant streams from newer active stream snapshots', () => {
    const streamed = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      turn_id: 'turn_1',
      content: 'hello',
      index: 0,
      content_offset: 0
    });
    const retained = clearActiveRuntimeTimelineItems(streamed, {
      streams: true,
      toolOutputs: false,
      thinking: false,
      retainStreamedAssistantMessages: true
    });

    const refreshed = applyActiveStreamSnapshots(retained, [
      {
        conversation_id: 'conv_1',
        message_id: 'msg_live',
        turn_id: 'turn_1',
        session_id: 'sess_1',
        content: 'hello world',
        chunk_count: 2,
        content_offset: 11,
        updated_at: '2026-06-16T12:00:00Z'
      }
    ]);

    expect(refreshed).toHaveLength(1);
    expect(refreshed[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'hello world',
      streaming: true,
      streamChunkCount: 2
    });
  });

  it('does not shorten retained non-partial live assistant streams with stale active snapshots', () => {
    const streamed = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      turn_id: 'turn_1',
      content: 'hello world',
      index: 1,
      content_offset: 0
    });
    const retained = clearActiveRuntimeTimelineItems(streamed, {
      streams: true,
      toolOutputs: false,
      thinking: false,
      retainStreamedAssistantMessages: true
    });

    const refreshed = applyActiveStreamSnapshots(retained, [
      {
        conversation_id: 'conv_1',
        message_id: 'msg_live',
        turn_id: 'turn_1',
        session_id: 'sess_1',
        content: 'hello',
        chunk_count: 1,
        content_offset: 5,
        updated_at: '2026-06-16T12:00:00Z'
      }
    ]);

    expect(refreshed).toHaveLength(1);
    expect(refreshed[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'hello world',
      streaming: true,
      streamChunkCount: 2
    });
  });

  it('does not merge a later completed assistant message into a finalized bubble with the same turn id', () => {
    const first = applyWebSocketEvent([], {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_first',
      turn_id: 'turn_shared',
      content: 'first reply',
      seq: 2,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
    });

    const second = applyWebSocketEvent(first, {
      type: 'message_complete',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_second',
      turn_id: 'turn_shared',
      content: 'second reply',
      seq: 4,
      token_usage: null,
      context_usage: null,
      queued_count: 0,
    });

    expect(second).toHaveLength(2);
    expect(second[0]).toMatchObject({ kind: 'message', role: 'assistant', content: 'first reply' });
    expect(second[1]).toMatchObject({ kind: 'message', role: 'assistant', content: 'second reply' });
  });

  it('hydrates an active assistant stream snapshot after history reload', () => {
    const history = normalizeHistory([
      {
        seq: 1,
        type: 'user_message',
        data: { content: 'hello', turn_id: 'turn_live' },
        timestamp: '2026-04-20T00:00:00Z'
      }
    ]);

    const hydrated = applyActiveStreamSnapshots(history, [
      {
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_live',
        turn_id: 'turn_live',
        content: 'Already streamed',
        chunk_count: 2,
        content_offset: 16,
        updated_at: '2026-04-20T00:00:01Z'
      }
    ]);

    const continued = applyWebSocketEvent(hydrated, {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_live',
      turn_id: 'turn_live',
      content: ' and new',
      index: 2,
      content_offset: 16
    });

    expect(continued[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'Already streamed and new',
      streaming: true,
    });
  });

  it('ignores duplicate chunks by content offset and chunk index', () => {
    const first = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_dup',
      turn_id: 'turn_dup',
      content: 'Hello',
      index: 0,
      content_offset: 0
    });

    const duplicate = applyWebSocketEvent(first, {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_dup',
      turn_id: 'turn_dup',
      content: 'Hello',
      index: 0,
      content_offset: 0
    });

    expect(duplicate[0]).toMatchObject({ content: 'Hello' });
  });

  it('ignores stale stream snapshots that arrive after newer chunks', () => {
    const streamed = [
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_stale',
        turn_id: 'turn_stale',
        content: 'Hello',
        index: 0,
        content_offset: 0
      },
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_stale',
        turn_id: 'turn_stale',
        content: ' world',
        index: 1,
        content_offset: 5
      }
    ].reduce((items, event) => applyWebSocketEvent(items, event), [] as ReturnType<typeof normalizeHistory>);

    const afterSnapshot = applyWebSocketEvent(streamed, {
      type: 'assistant_stream_snapshot',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_stale',
      turn_id: 'turn_stale',
      content: 'Hello',
      chunk_count: 1,
      content_offset: 5,
      updated_at: '2026-04-20T00:00:01Z'
    });

    expect(afterSnapshot[0]).toMatchObject({ content: 'Hello world' });
  });

  it('buffers ahead-of-offset chunks until a snapshot fills the prefix', () => {
    const ahead = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_gap',
      turn_id: 'turn_gap',
      content: ' world',
      index: 1,
      content_offset: 5
    });

    expect(ahead).toHaveLength(0);

    const recovered = applyWebSocketEvent(ahead, {
      type: 'assistant_stream_snapshot',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_gap',
      turn_id: 'turn_gap',
      content: 'Hello',
      chunk_count: 1,
      content_offset: 5,
      updated_at: '2026-04-20T00:00:01Z'
    });

    expect(recovered[0]).toMatchObject({ content: 'Hello world', streaming: true });
  });

  it('buffers out-of-order chunks after a stream has started', () => {
    const first = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_out_of_order',
      turn_id: 'turn_out_of_order',
      content: 'Hello',
      index: 0,
      content_offset: 0
    });

    const ahead = applyWebSocketEvent(first, {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_out_of_order',
      turn_id: 'turn_out_of_order',
      content: ' again',
      index: 2,
      content_offset: 11
    });

    expect(ahead[0]).toMatchObject({ content: 'Hello' });

    const filled = applyWebSocketEvent(ahead, {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'turn_out_of_order',
      turn_id: 'turn_out_of_order',
      content: ' world',
      index: 1,
      content_offset: 5
    });

    expect(filled[0]).toMatchObject({ content: 'Hello world again', streaming: true });
  });

  it('finalizes a streamed assistant segment when a tool call starts', () => {
    const timeline = [
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_tool_boundary',
        turn_id: 'turn_tool_boundary',
        content: 'Checking',
        index: 0,
        content_offset: 0
      },
      {
        type: 'tool_call' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_boundary',
        tool_name: 'read_file',
        status: 'started',
        turn_id: 'turn_tool_boundary'
      }
    ].reduce((items, event) => applyWebSocketEvent(items, event), [] as ReturnType<typeof normalizeHistory>);

    expect(timeline[0]).toMatchObject({ kind: 'message', content: 'Checking', streaming: false });
    expect(timeline[1]).toMatchObject({ kind: 'tool_call', callId: 'call_boundary' });
  });

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

  it('keeps live websocket turns aligned with persisted turn history', () => {
    const live = [
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_2',
        turn_id: 'turn_2',
        content: 'First segment',
        index: 0,
      },
      {
        type: 'tool_call' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_turn_2',
        tool_name: 'image_generate',
        status: 'started',
        arguments: { prompt: 'logo' },
        turn_id: 'turn_2',
      },
      {
        type: 'tool_result' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_turn_2',
        tool_name: 'image_generate',
        result: 'done',
        is_error: false,
        duration_ms: 25,
        turn_id: 'turn_2',
        attachments: [
          {
            artifact_id: 'img_turn_2',
            kind: 'image' as const,
            mime_type: 'image/png',
            filename: 'logo.png',
            size_bytes: 321,
            url: 'https://cognis.example.com/logo.png'
          }
        ]
      },
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_2',
        turn_id: 'turn_2',
        content: 'Second segment',
        index: 1,
      },
      {
        type: 'message_complete' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_2',
        turn_id: 'turn_2',
        seq: 14,
        token_usage: null,
        context_usage: null,
        queued_count: 0,
        attachments: [
          {
            artifact_id: 'img_turn_2',
            kind: 'image' as const,
            mime_type: 'image/png',
            filename: 'logo.png',
            size_bytes: 321,
            url: 'https://cognis.example.com/logo.png'
          }
        ]
      }
    ].reduce((timeline, event) => applyWebSocketEvent(timeline, event), [] as ReturnType<typeof normalizeHistory>);

    const history = normalizeHistory([
      {
        seq: 10,
        type: 'assistant_message',
        data: { content: 'First segment', turn_id: 'turn_2' },
        timestamp: '2026-04-09T00:00:00Z'
      },
      {
        seq: 11,
        type: 'tool_call',
        data: { call_id: 'call_turn_2', name: 'image_generate', arguments: { prompt: 'logo' }, turn_id: 'turn_2' },
        timestamp: '2026-04-09T00:00:01Z'
      },
      {
        seq: 12,
        type: 'tool_result',
        data: {
          call_id: 'call_turn_2',
          name: 'image_generate',
          result: 'done',
          is_error: false,
          turn_id: 'turn_2',
          attachments: [
            {
              artifact_id: 'img_turn_2',
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
          turn_id: 'turn_2',
          attachments: [
            {
              artifact_id: 'img_turn_2',
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

    expect(live).toHaveLength(3);
    expect(live[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'First segment',
      turnId: 'turn_2'
    });
    expect(live[1]).toMatchObject({ kind: 'tool_call', callId: 'call_turn_2', status: 'completed' });
    expect(live[2]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'Second segment',
      attachments: [{ artifact_id: 'img_turn_2', filename: 'logo.png' }],
      turnId: 'turn_2'
    });
    expect(history[0]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'First segment',
      turnId: 'turn_2'
    });
    expect(history[1]).toMatchObject({ kind: 'tool_call', callId: 'call_turn_2', status: 'completed' });
    expect(history[2]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'Second segment',
      attachments: [{ artifact_id: 'img_turn_2', filename: 'logo.png' }],
      turnId: 'turn_2'
    });
  });

  it('merges tool progress into the final tool call card', () => {
    const withProgress = applyWebSocketEvent([], {
      type: 'tool_progress',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_patch',
      tool_name: 'apply_patch',
      progress: {
        phase: 'preparing_input',
        input_chars: 12000,
        input_lines: 400,
        complete: false,
      },
      turn_id: 'turn_1',
      timestamp: '2026-04-09T00:00:01Z',
    });

    expect(withProgress).toHaveLength(1);
    expect(withProgress[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_patch',
      toolName: 'apply_patch',
      status: 'started',
      progressPhase: 'preparing_input',
      progressInputChars: 12000,
      progressInputLines: 400,
    });

    const withToolCall = applyWebSocketEvent(withProgress, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_patch',
      tool_name: 'apply_patch',
      status: 'started',
      arguments: { patchText: '*** Begin Patch\n*** End Patch\n' },
      turn_id: 'turn_1',
      timestamp: '2026-04-09T00:00:02Z',
    });

    expect(withToolCall).toHaveLength(1);
    expect(withToolCall[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_patch',
      arguments: { patchText: '*** Begin Patch\n*** End Patch\n' },
      progressPhase: 'preparing_input',
      progressInputChars: 12000,
      progressInputLines: 400,
    });
  });

  it('keeps thinking above assistant within the same phase and splits on tool calls', () => {
    const timeline = [
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_phase',
        turn_id: 'turn_phase',
        content: 'Draft reply',
        index: 0,
      },
      {
        type: 'assistant_thinking_chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_phase',
        turn_id: 'turn_phase',
        block_id: 'thk_1',
        delta: 'Reasoning before tool',
        title: 'Exploring options',
        complete: false,
      },
      {
        type: 'assistant_thinking_block' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_phase',
        turn_id: 'turn_phase',
        block_id: 'thk_1',
        title: 'Exploring options',
        complete: true,
        content: 'Reasoning before tool',
      },
      {
        type: 'tool_call' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_phase_1',
        tool_name: 'bash',
        status: 'started',
        arguments: { command: 'pwd' },
        turn_id: 'turn_phase',
      },
      {
        type: 'assistant_thinking_chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_phase',
        turn_id: 'turn_phase',
        block_id: 'thk_2',
        delta: 'Reasoning after tool',
        title: 'Checking result',
        complete: false,
      },
      {
        type: 'assistant_thinking_block' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_phase',
        turn_id: 'turn_phase',
        block_id: 'thk_2',
        title: 'Checking result',
        complete: true,
        content: 'Reasoning after tool',
      },
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_phase',
        turn_id: 'turn_phase',
        content: 'Final reply',
        index: 1,
      },
    ].reduce((items, event) => applyWebSocketEvent(items, event), [] as ReturnType<typeof normalizeHistory>);

    expect(timeline[0]).toMatchObject({ kind: 'thinking' });
    expect(timeline[1]).toMatchObject({ kind: 'message', role: 'assistant', content: 'Draft reply' });
    expect(timeline[2]).toMatchObject({ kind: 'tool_call', callId: 'call_phase_1', status: 'started' });
    expect(timeline[3]).toMatchObject({ kind: 'thinking' });
    expect(timeline[4]).toMatchObject({ kind: 'message', role: 'assistant', content: 'Final reply' });
  });

  it('concatenates assistant chunks across thinking within the same phase', () => {
    const timeline = [
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_mix',
        turn_id: 'turn_mix',
        content: 'Part one',
        index: 0,
      },
      {
        type: 'assistant_thinking_block' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_mix',
        turn_id: 'turn_mix',
        block_id: 'thk_mix_1',
        title: 'Exploring',
        complete: true,
        content: 'Reasoning between message chunks',
      },
      {
        type: 'chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        message_id: 'turn_mix',
        turn_id: 'turn_mix',
        content: ' and part two',
        index: 1,
      },
    ].reduce((items, event) => applyWebSocketEvent(items, event), [] as ReturnType<typeof normalizeHistory>);

    expect(timeline).toHaveLength(2);
    expect(timeline[0]).toMatchObject({ kind: 'thinking' });
    expect(timeline[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      streaming: true,
      content: 'Part one and part two',
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

  it('accumulates live tool output chunks on the tool block', () => {
    const withTool = applyWebSocketEvent([], {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_live',
      tool_name: 'bash',
      status: 'started',
      arguments: { command: 'npm test' }
    });

    const withChunks = [
      {
        type: 'tool_result_chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_live',
        tool_name: 'bash',
        delta: 'line 1\n'
      },
      {
        type: 'tool_output_chunk' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_live',
        tool_name: 'bash',
        delta: 'line 2\n'
      }
    ].reduce((items, event) => applyWebSocketEvent(items, event), withTool);

    expect(withChunks[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_live',
      streamedOutput: 'line 1\nline 2\n',
      result: 'line 1\nline 2\n',
      sessionId: 'sess_1',
      liveOutputAvailable: true
    });
  });

  it('marks active tool output snapshots as live-pageable', () => {
    const items = applyActiveToolOutputSnapshots([], [
      {
        conversation_id: 'conv_1',
        call_id: 'call_live',
        session_id: 'sess_1',
        tool_name: 'bash',
        status: 'started',
        result: 'head\n...\ntail\n',
        chunk_count: 4,
        content_offset: 42,
        live_output_available: false
      }
    ]);

    expect(items[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_live',
      liveOutputAvailable: true,
      sessionId: 'sess_1'
    });
  });

  it('initializes chunk metadata for placeholder tool chunks', () => {
    const items = applyWebSocketEvent([], {
      type: 'tool_result_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_live',
      tool_name: 'bash',
      delta: '😀',
      chunk_index: 4,
      content_offset: 10
    });

    expect(items).toHaveLength(0);

    const withCall = applyWebSocketEvent(items, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_live',
      tool_name: 'bash',
      status: 'started',
      arguments: { command: 'npm test' }
    });

    expect(withCall[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_live',
      streamedOutput: '😀',
      streamChunkCount: 5,
      streamContentOffset: 12,
      sessionId: 'sess_1',
      liveOutputAvailable: true
    });
  });

  it('buffers live tool results until the matching tool call arrives', () => {
    const resultOnly = applyWebSocketEvent([], {
      type: 'tool_result',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_result_first',
      tool_name: 'read',
      result: 'late result',
      is_error: false,
      duration_ms: 42,
      turn_id: 'turn_1',
      attachments: []
    });

    expect(resultOnly).toHaveLength(0);

    const withCall = applyWebSocketEvent(resultOnly, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_result_first',
      tool_name: 'read',
      status: 'started',
      arguments: { file_path: '/tmp/file.txt' },
      turn_id: 'turn_1'
    });

    expect(withCall).toHaveLength(1);
    expect(withCall[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_result_first',
      toolName: 'read',
      arguments: { file_path: '/tmp/file.txt' },
      status: 'completed',
      result: 'late result',
      durationMs: 42
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

  it('does not attach buffered orphan tool results across turns with the same call id', () => {
    const orphan = applyWebSocketEvent([], {
      type: 'tool_result',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_reused',
      tool_name: 'read',
      result: 'old output',
      is_error: false,
      duration_ms: 1,
      turn_id: 'old_turn',
      attachments: []
    });

    const withLaterCall = applyWebSocketEvent(orphan, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_reused',
      tool_name: 'read',
      status: 'started',
      arguments: { file_path: '/tmp/new.txt' },
      turn_id: 'new_turn'
    });

    expect(withLaterCall).toHaveLength(1);
    expect(withLaterCall[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_reused',
      status: 'started',
      arguments: { file_path: '/tmp/new.txt' }
    });
    expect((withLaterCall[0] as ToolCallTimelineItem).result).toBeUndefined();
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

  it('preserves file diffs from live websocket tool results', () => {
    const started = applyWebSocketEvent([], {
      type: 'tool_call',
      call_id: 'call-live-edit',
      tool_name: 'write',
      status: 'started',
      arguments: { file_path: 'ui/src/lib/diff.ts' }
    });

    const completed = applyWebSocketEvent(started, {
      type: 'tool_result',
      call_id: 'call-live-edit',
      tool_name: 'write',
      result: 'Wrote file',
      is_error: false,
      duration_ms: 12,
      file_diffs: [{ path: 'ui/src/lib/diff.ts', diff: '--- ui/src/lib/diff.ts\n+++ ui/src/lib/diff.ts\n@@ -1 +1 @@\n-old\n+new\n' }]
    });

    expect(completed[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call-live-edit',
      fileDiffs: [{ path: 'ui/src/lib/diff.ts', diff: expect.stringContaining('+new') }]
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
    const items = [
      {
        type: 'tool_call' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_todos',
        tool_name: 'step_todo_write',
        status: 'started' as const,
        turn_id: 'turn_active',
        arguments: {
          todos: [
            { content: 'Trace the bug', status: 'in_progress', priority: 'normal' }
          ]
        }
      },
      {
        type: 'tool_result' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_todos',
        tool_name: 'step_todo_write',
        turn_id: 'turn_active',
        result: JSON.stringify({
          status: 'updated',
          todos: [
            { content: 'Trace the bug', status: 'in_progress', priority: 'normal' }
          ]
        }),
        is_error: false,
        duration_ms: 5
      },
      {
        type: 'user_message' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        turn_id: 'turn_active',
        queue_id: 'qmsg_absorbed',
        content: 'Additional detail',
        attachments: []
      }
    ].reduce((timeline, event) => applyWebSocketEvent(timeline, event), [] as ReturnType<typeof normalizeHistory>);

    expect(latestTodoSnapshot(items, true)).toEqual([
      { content: 'Trace the bug', status: 'in_progress', priority: 'normal' }
    ]);
  });

  it('clears previous-turn todos after a normal new user message', () => {
    const items = [
      {
        type: 'tool_call' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_todos',
        tool_name: 'step_todo_write',
        status: 'started' as const,
        turn_id: 'turn_previous',
        arguments: {
          todos: [
            { content: 'Old work', status: 'in_progress', priority: 'normal' }
          ]
        }
      },
      {
        type: 'tool_result' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_todos',
        tool_name: 'step_todo_write',
        turn_id: 'turn_previous',
        result: JSON.stringify({
          status: 'updated',
          todos: [
            { content: 'Old work', status: 'in_progress', priority: 'normal' }
          ]
        }),
        is_error: false,
        duration_ms: 5
      },
      {
        type: 'user_message' as const,
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        turn_id: 'turn_next',
        content: 'Start something else',
        attachments: []
      }
    ].reduce((timeline, event) => applyWebSocketEvent(timeline, event), [] as ReturnType<typeof normalizeHistory>);

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

  it('groups multiple assistant_thinking events for the same message into one item', () => {
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

    expect(items).toHaveLength(1);
    const thinking = items[0] as ThinkingTimelineItem;
    expect(thinking.kind).toBe('thinking');
    expect(thinking.blocks).toHaveLength(2);
    expect(thinking.blocks[0].block_id).toBe('thk_1');
    expect(thinking.blocks[1].block_id).toBe('thk_2');
  });

  it('accumulates live assistant_thinking_chunk events into a ThinkingTimelineItem', () => {
    const after1 = applyWebSocketEvent([], {
      type: 'assistant_thinking_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_live_1',
      delta: 'Considering',
      title: 'Considering the problem',
      complete: false,
    });

    expect(after1).toHaveLength(1);
    expect(after1[0]).toMatchObject({ kind: 'thinking', streaming: true });
    const thinking = after1[0] as ThinkingTimelineItem;
    expect(thinking.blocks[0].content).toBe('Considering');
    expect(thinking.activeTitle).toBe('Considering the problem');

    const after2 = applyWebSocketEvent(after1, {
      type: 'assistant_thinking_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_live_1',
      delta: ' further options',
      title: 'Considering the problem',
      complete: false,
    });

    const thinking2 = after2[0] as ThinkingTimelineItem;
    expect(thinking2.blocks[0].content).toBe('Considering further options');
  });

  it('applies active thinking snapshots to polled session timelines', () => {
    const items = applyActiveThinkingSnapshots([], [
      {
        session_id: 'sess_1',
        message_id: 'sr_1',
        turn_id: 'sr_1',
        updated_at: '2026-04-20T00:00:00Z',
        blocks: [
          {
            block_id: 'thk_1',
            title: 'Considering options',
            content: 'Considering options for the task',
            source: 'summary',
            complete: false,
          },
        ],
      },
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: 'thinking', streaming: true });
    const thinking = items[0] as ThinkingTimelineItem;
    expect(thinking.activeTitle).toBe('Considering options');
    expect(thinking.blocks[0].content).toBe('Considering options for the task');
  });

  it('finalizes a thinking block on assistant_thinking_block complete event', () => {
    const withChunk = applyWebSocketEvent([], {
      type: 'assistant_thinking_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_1',
      delta: 'Some analysis.',
      title: 'Some analysis',
      complete: false,
    });

    const withBlock = applyWebSocketEvent(withChunk, {
      type: 'assistant_thinking_block',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_1',
      title: 'Some analysis',
      complete: true,
    });

    const thinking = withBlock[0] as ThinkingTimelineItem;
    expect(thinking.streaming).toBe(false);
    expect(thinking.blocks[0].complete).toBe(true);
  });

  it('replaces streamed thinking with full completion content instead of duplicating it', () => {
    const withChunk = applyWebSocketEvent([], {
      type: 'assistant_thinking_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_1',
      delta: 'Some analysis.',
      title: 'Some analysis',
      complete: false,
      started_at: '2026-04-20T00:00:00Z',
    });

    const withBlock = applyWebSocketEvent(withChunk, {
      type: 'assistant_thinking_block',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_1',
      title: 'Some analysis',
      complete: true,
      content: 'Some analysis.',
      started_at: '2026-04-20T00:00:00Z',
      completed_at: '2026-04-20T00:00:02Z',
      duration_ms: 2000,
    });

    const thinking = withBlock[0] as ThinkingTimelineItem;
    expect(thinking.blocks).toHaveLength(1);
    expect(thinking.blocks[0].content).toBe('Some analysis.');
    expect(thinking.blocks[0].complete).toBe(true);
    expect(thinking.blocks[0].durationMs).toBe(2000);
    expect(thinking.streaming).toBe(false);
  });

  it('ignores duplicate full thinking block replay frames in the same segment', () => {
    const first = applyWebSocketEvent([], {
      type: 'assistant_thinking_block',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_1',
      title: 'Some analysis',
      complete: true,
      content: 'Some analysis.',
      started_at: '2026-04-20T00:00:00Z',
    });

    const duplicate = applyWebSocketEvent(first, {
      type: 'assistant_thinking_block',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_1_duplicate',
      title: 'Some analysis',
      complete: true,
      content: 'Some analysis.',
      started_at: '2026-04-20T00:00:00Z',
    });

    const thinking = duplicate[0] as ThinkingTimelineItem;
    expect(thinking.blocks).toHaveLength(1);
    expect(thinking.blocks[0].block_id).toBe('thk_1');
  });

  it('keeps distinct identical thinking blocks when timing differs', () => {
    const first = applyWebSocketEvent([], {
      type: 'assistant_thinking_block',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_1',
      title: 'Some analysis',
      complete: true,
      content: 'Some analysis.',
      started_at: '2026-04-20T00:00:00Z',
      completed_at: '2026-04-20T00:00:01Z',
    });

    const second = applyWebSocketEvent(first, {
      type: 'assistant_thinking_block',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_2',
      title: 'Some analysis',
      complete: true,
      content: 'Some analysis.',
      started_at: '2026-04-20T00:00:02Z',
      completed_at: '2026-04-20T00:00:03Z',
    });

    const thinking = second[0] as ThinkingTimelineItem;
    expect(thinking.blocks).toHaveLength(2);
    expect(thinking.blocks.map((block) => block.block_id)).toEqual(['thk_1', 'thk_2']);
  });

  it('interleaves thinking before tool calls in timeline order', () => {
    const withThinking = applyWebSocketEvent([], {
      type: 'assistant_thinking_chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      block_id: 'thk_1',
      delta: 'Deciding which tool to call.',
      title: 'Deciding which tool to call',
      complete: false,
    });

    const withTool = applyWebSocketEvent(withThinking, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_1',
      tool_name: 'bash',
      status: 'started',
      arguments: { command: 'ls' },
    });

    expect(withTool[0]).toMatchObject({ kind: 'thinking' });
    expect(withTool[1]).toMatchObject({ kind: 'tool_call' });
  });

  it('starts a new assistant phase after tool activity', () => {
    const streaming = applyWebSocketEvent([], {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      content: 'Working on it',
      index: 0
    });

    const withTool = applyWebSocketEvent(streaming, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_live_1',
      tool_name: 'bash',
      status: 'started',
      arguments: { command: 'pwd' }
    });

    const withResult = applyWebSocketEvent(withTool, {
      type: 'tool_result',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_live_1',
      tool_name: 'bash',
      result: 'done',
      is_error: false,
      duration_ms: 25,
    });

    const withSecondTool = applyWebSocketEvent(withResult, {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_live_2',
      tool_name: 'grep',
      status: 'started',
      arguments: { pattern: 'foo' }
    });

    const continued = applyWebSocketEvent(withSecondTool, {
      type: 'chunk',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      message_id: 'msg_live',
      content: ' and done',
      index: 1
    });

    expect(continued[0]).toMatchObject({ kind: 'message', role: 'assistant', content: 'Working on it' });
    expect(continued[1]).toMatchObject({ kind: 'tool_call', callId: 'call_live_1', status: 'completed' });
    expect(continued[2]).toMatchObject({ kind: 'tool_call', callId: 'call_live_2', status: 'started' });
    expect(continued[3]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      streaming: true,
      content: ' and done',
    });
  });

  it('stores live tool-result attachments on the tool block', () => {
    const withTool = applyWebSocketEvent([], {
      type: 'tool_call',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_live_artifact',
      tool_name: 'image_edit',
      status: 'started',
      arguments: { prompt: 'sharpen' }
    });

    const withResult = applyWebSocketEvent(withTool, {
      type: 'tool_result',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      call_id: 'call_live_artifact',
      tool_name: 'image_edit',
      result: 'done',
      is_error: false,
      duration_ms: 25,
      attachments: [
        {
          artifact_id: 'img_edited_1',
          kind: 'image',
          mime_type: 'image/png',
          filename: 'edited.png',
          size_bytes: 456,
          url: 'https://cognis.example.com/edited.png'
        }
      ]
    });

    expect(withResult[0]).toMatchObject({
      kind: 'tool_call',
      callId: 'call_live_artifact',
      attachments: [{ artifact_id: 'img_edited_1', filename: 'edited.png' }]
    });
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

  it('does not reconcile echoed user messages once newer timeline items exist', () => {
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

    expect(echoed).toHaveLength(3);
    expect(echoed[0]).toMatchObject({ kind: 'message', role: 'user', optimistic: true });
    expect(echoed[2]).toMatchObject({ kind: 'message', role: 'user', optimistic: false });
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
    it('finds the most recent unresolved step_request_questions tool call', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_questions',
        status: 'started',
        arguments: { questions: [{ id: 'q1', question: 'Which name?' }] }
      });

      const pending = findPendingStepRequestInputCall(timeline);
      expect(pending).not.toBeNull();
      expect(pending?.callId).toBe('call_1');
      expect(pending?.toolName).toBe('step_request_questions');
    });

    it('ignores step_request_questions calls that already have a tool_result', () => {
      const started = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_questions',
        status: 'started',
        arguments: { questions: [{ id: 'q1', question: 'Which name?' }] }
      });
      const resolved = applyWebSocketEvent(started, {
        type: 'tool_result',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_questions',
        result: JSON.stringify({ answers: [{ question_id: 'q1', custom_answer: 'First option' }] }),
        is_error: false,
        duration_ms: 0
      });

      expect(findPendingStepRequestInputCall(resolved)).toBeNull();
    });

    it('annotates the pending step_request_questions tool call with a notification id', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_questions',
        status: 'started',
        arguments: { questions: [{ id: 'q1', question: 'Which name?' }] }
      });

      const annotated = annotateStepRequestInputWithNotification(timeline, 'input_abc123');
      const tool = annotated.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(tool.notificationId).toBe('input_abc123');
    });

    it('finds deferred auth challenge browser_eval calls', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_eval_otp',
        tool_name: 'browser_eval',
        status: 'started',
        arguments: {
          session_id: 'browser_1',
          script: '(code) => code',
          args: [{ value_ref: '$auth_challenge:reddit.code', auth_challenge: { label: 'Reddit MFA' } }]
        }
      });

      const pending = findPendingStepRequestInputCall(timeline);
      expect(pending).not.toBeNull();
      expect(pending?.toolName).toBe('browser_eval');
    });

    it('redacts optimistic auth challenge answers', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_auth',
        tool_name: 'request_auth_challenge',
        status: 'started',
        arguments: { label: 'Reddit MFA', required_fields: ['code'] }
      });
      const tool = timeline.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      const resolved = optimisticallyResolveStepRequestInput(timeline, tool.id, '123456');
      const updated = resolved.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(updated.result).toBe(JSON.stringify({ response: '<redacted>' }));
    });

    it('optimistically resolves a step_request_questions tool call with the user answer', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_questions',
        status: 'started',
        arguments: { questions: [{ id: 'q1', question: 'Which name?' }] }
      });
      const tool = timeline.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      const resolved = optimisticallyResolveStepRequestInput(timeline, tool.id, 'Main option');
      const updated = resolved.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(updated.status).toBe('completed');
      expect(updated.isError).toBe(false);
      expect(updated.result).toBe(JSON.stringify({
        mode: 'plain_text',
        answers: [{ question_id: 'q1', selected_option_ids: [], custom_answer: 'Main option' }]
      }));
      // A subsequent authoritative tool_result from the backend must still
      // take precedence so evaluation metadata / duration are populated.
      const authoritative = applyWebSocketEvent(resolved, {
        type: 'tool_result',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_questions',
        result: JSON.stringify({ answers: [{ question_id: 'q1', custom_answer: 'Main option' }] }),
        is_error: false,
        duration_ms: 1200
      });
      const final = authoritative.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(final.status).toBe('completed');
      expect(final.durationMs).toBe(1200);
    });

    it('optimistically resolves a step_request_questions tool call with structured answers', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_questions',
        status: 'started',
        arguments: {
          questions: [
            { id: 'architecture', question: 'Architecture?' },
            { id: 'validation', question: 'Validation?' }
          ]
        }
      });
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
