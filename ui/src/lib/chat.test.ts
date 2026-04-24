import { describe, expect, it } from 'vitest';

import {
  annotateStepRequestInputWithNotification,
  appendOptimisticUserMessage,
  applyWebSocketEvent,
  findPendingStepRequestInputCall,
  normalizeHistory,
  optimisticallyResolveStepRequestInput,
  type ThinkingTimelineItem,
  type ToolCallTimelineItem
} from '$lib/chat';

describe('chat timeline helpers', () => {
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

  it('groups turn-scoped assistant history around tool calls', () => {
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

    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: 'tool_call', callId: 'call_1', status: 'completed' });
    expect(items[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'First segment\n\nSecond segment',
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
        content: '\n\nSecond segment',
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

    expect(live).toHaveLength(2);
    expect(live[0]).toMatchObject({ kind: 'tool_call', callId: 'call_turn_2', status: 'completed' });
    expect(live[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'First segment\n\nSecond segment',
      attachments: [{ artifact_id: 'img_turn_2', filename: 'logo.png' }],
      turnId: 'turn_2'
    });
    expect(history[0]).toMatchObject({ kind: 'tool_call', callId: 'call_turn_2', status: 'completed' });
    expect(history[1]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      content: 'First segment\n\nSecond segment',
      attachments: [{ artifact_id: 'img_turn_2', filename: 'logo.png' }],
      turnId: 'turn_2'
    });
  });

  it('does not duplicate direct-chat clarification prompts in the timeline', () => {
    const items = applyWebSocketEvent([], {
      type: 'workflow_step_question',
      notification_id: 'notif_1',
      question: 'Which repository should I use?'
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

  it('renders session recovery as a low-emphasis system message', () => {
    const items = applyWebSocketEvent([], {
      type: 'session_recovered',
      conversation_id: 'conv_1',
      session_id: 'sess_1',
      reason: 'controller_restart'
    });

    expect(items[0]).toMatchObject({
      id: 'session-recovered:sess_1',
      kind: 'system_message',
      text: 'The controller recovered this conversation after a restart.'
    });
  });

  it('drops workflow prompt notices on reconnect so only replayed pending prompts remain', () => {
    const withNotice = applyWebSocketEvent([], {
      type: 'workflow_step_question',
      notification_id: 'notif_q_1',
      question: 'Still needed?'
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

  it('keeps the assistant draft trailing behind live tool calls until completion', () => {
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

    expect(continued[0]).toMatchObject({ kind: 'tool_call', callId: 'call_live_1', status: 'completed' });
    expect(continued[1]).toMatchObject({ kind: 'tool_call', callId: 'call_live_2', status: 'started' });
    expect(continued[2]).toMatchObject({
      kind: 'message',
      role: 'assistant',
      streaming: true,
      content: 'Working on it and done',
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

  describe('step_request_input helpers', () => {
    it('finds the most recent unresolved step_request_input tool call', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_input',
        status: 'started',
        arguments: { question: 'Which name?' }
      });

      const pending = findPendingStepRequestInputCall(timeline);
      expect(pending).not.toBeNull();
      expect(pending?.callId).toBe('call_1');
      expect(pending?.toolName).toBe('step_request_input');
    });

    it('ignores step_request_input calls that already have a tool_result', () => {
      const started = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_input',
        status: 'started',
        arguments: { question: 'Which name?' }
      });
      const resolved = applyWebSocketEvent(started, {
        type: 'tool_result',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_input',
        result: JSON.stringify({ response: 'First option' }),
        is_error: false,
        duration_ms: 0
      });

      expect(findPendingStepRequestInputCall(resolved)).toBeNull();
    });

    it('annotates the pending step_request_input tool call with a notification id', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_input',
        status: 'started',
        arguments: { question: 'Which name?' }
      });

      const annotated = annotateStepRequestInputWithNotification(timeline, 'input_abc123');
      const tool = annotated.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(tool.notificationId).toBe('input_abc123');
    });

    it('optimistically resolves a step_request_input tool call with the user answer', () => {
      const timeline = applyWebSocketEvent([], {
        type: 'tool_call',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_input',
        status: 'started',
        arguments: { question: 'Which name?' }
      });
      const tool = timeline.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      const resolved = optimisticallyResolveStepRequestInput(timeline, tool.id, 'Main option');
      const updated = resolved.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(updated.status).toBe('completed');
      expect(updated.isError).toBe(false);
      expect(updated.result).toBe(JSON.stringify({ response: 'Main option' }));
      // A subsequent authoritative tool_result from the backend must still
      // take precedence so evaluation metadata / duration are populated.
      const authoritative = applyWebSocketEvent(resolved, {
        type: 'tool_result',
        conversation_id: 'conv_1',
        session_id: 'sess_1',
        call_id: 'call_1',
        tool_name: 'step_request_input',
        result: JSON.stringify({ response: 'Main option' }),
        is_error: false,
        duration_ms: 1200
      });
      const final = authoritative.find((item) => item.kind === 'tool_call') as ToolCallTimelineItem;
      expect(final.status).toBe('completed');
      expect(final.durationMs).toBe(1200);
    });
  });
});
