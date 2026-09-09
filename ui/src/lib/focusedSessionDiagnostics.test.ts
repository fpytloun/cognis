import { writable } from 'svelte/store';
import { describe, expect, it, vi } from 'vitest';

import {
  FocusedSessionDiagnosticsController,
  type FocusedSessionDiagnosticsState,
} from './focusedSessionDiagnostics';
import { reconcileRenderedOverviewSources } from './chat-page';
import {
  sessionTimelineScope,
  type ChatRealtimeFrame,
  type ChatSnapshot,
  type RuntimeOverlaySnapshot,
  type WorkstreamRef,
} from './chat-v2/types';

function workstream(overrides: Partial<WorkstreamRef> = {}): WorkstreamRef {
  return {
    key: 'session:session-1',
    kind: 'main',
    root_key: 'session:session-1',
    edge_kind: 'root',
    ordinal: 0,
    session_id: 'session-1',
    event_store_session_id: 'session-1',
    title: 'Focused session',
    agent_id: 'riker',
    agent_profile_id: 'smart',
    status: 'active',
    current: true,
    superseded: false,
    model: 'gpt-6-astra',
    reasoning_effort: 'low',
    execution_state: 'running',
    active_turn_id: 'turn-2',
    execution_turn_id: 'turn-2',
    runtime_selection_revision: 2,
    ...overrides,
  };
}

function runtime(
  revision: number,
  options: {
    turnId?: string;
    model?: string;
    profile?: string;
    effort?: string;
    generatedAt?: string;
    promptTokens?: number;
  } = {},
): RuntimeOverlaySnapshot {
  return {
    runtime_epoch: 'epoch-1',
    runtime_revision: revision,
    generated_at: options.generatedAt ?? `2026-09-08T20:12:0${revision}Z`,
    has_active_turn: true,
    active_turn: {
      turn_id: options.turnId ?? 'turn-2',
      session_id: 'session-1',
      status: 'running',
    },
    volatile_items: [],
    context_usage: {
      runtime_metadata_revision: revision,
      turn_id: options.turnId ?? 'turn-2',
      runtime_selection_revision: 2,
      measured_at: options.generatedAt ?? `2026-09-08T20:12:0${revision}Z`,
      measurement_source: 'projected_prompt',
      prompt_tokens: options.promptTokens ?? revision * 100,
      max_context_tokens: 500_000,
      percentage: 1,
      model: options.model ?? 'gpt-6-astra',
      reasoning_effort: options.effort ?? 'low',
      agent_profile_id: options.profile ?? 'smart',
    },
  };
}

function snapshot(scope: ReturnType<typeof sessionTimelineScope>, value: RuntimeOverlaySnapshot): ChatSnapshot {
  return {
    schema_version: 2,
    projection_version: 'test',
    scope,
    conversation: { conversation_id: 'conversation-1', agent_id: 'riker', status: 'active' },
    timeline: { items: [], has_more_before: false },
    state: {
      state_version: 1,
      snapshot_generated_at: value.generated_at,
      capabilities: [],
      active_turn: {},
      pending: {},
      active_session: {},
    },
    queue: { messages: [], queued_count: 0 },
    runtime: value,
    cursor: `cursor-${value.runtime_revision}`,
    server_time: value.generated_at,
  };
}

function harness(
  loader = vi.fn(),
  refreshExecution: () => boolean | Promise<boolean> = vi.fn(() => true),
) {
  const connection = writable<{
    status: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'stalled';
  }>({ status: 'connected' });
  let listener: ((event: ChatRealtimeFrame | { type: string }) => void) | null = null;
  const socket = {
    state: connection,
    subscribe: vi.fn((next: typeof listener) => {
      listener = next;
      return () => {
        listener = null;
      };
    }),
    acquireChatV2: vi.fn(),
    updateChatV2Cursor: vi.fn(),
    releaseChatV2: vi.fn(),
  };
  const states: FocusedSessionDiagnosticsState[] = [];
  const controller = new FocusedSessionDiagnosticsController(
    socket,
    loader,
    (state) => states.push(state),
    refreshExecution,
  );
  return {
    connection,
    controller,
    emit: (event: ChatRealtimeFrame | { type: string }) => listener?.(event),
    socket,
    states,
    refreshExecution,
  };
}

describe('FocusedSessionDiagnosticsController', () => {
  it('changes Sol/high last-turn telemetry to updating until Astra/low assembles the active turn', async () => {
    const scope = sessionTimelineScope('session-1', 'conversation-1');
    const old = runtime(1, {
      turnId: 'turn-1',
      model: 'gpt-5.6-sol',
      effort: 'high',
      promptTokens: 309_750,
    });
    const loader = vi.fn(async () => snapshot(scope, old));
    const { controller, emit, refreshExecution, states } = harness(loader);

    controller.select(scope, workstream({
      model: 'gpt-5.6-sol',
      reasoning_effort: 'high',
      execution_turn_id: 'turn-1',
    }), true);
    await vi.waitFor(() => expect(loader).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(states.at(-1)?.freshness).toBe('updating'));
    expect(states.at(-1)?.contextUsage?.model).toBe('gpt-5.6-sol');

    emit({
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'test',
      scope,
      conversation_id: 'conversation-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-2',
      ops: [],
      runtime: runtime(2),
      server_time: '2026-09-08T20:12:02Z',
    });

    expect(states.at(-1)?.freshness).toBe('updating');
    expect(states.at(-1)?.contextUsage?.model).toBe('gpt-6-astra');
    expect(refreshExecution).toHaveBeenCalledOnce();
    controller.updateExecution(workstream());
    expect(states.at(-1)?.freshness).toBe('current');
    emit({
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'test',
      scope,
      conversation_id: 'conversation-1',
      cursor_before: 'cursor-2',
      cursor_after: 'cursor-3',
      ops: [],
      runtime: runtime(3),
      server_time: '2026-09-08T20:12:03Z',
    });
    expect(refreshExecution).toHaveBeenCalledOnce();
    controller.dispose();
  });

  it('retains the last measurement while idle without treating age as a fault', async () => {
    const scope = sessionTimelineScope('session-1', 'conversation-1');
    const loader = vi.fn(async () => snapshot(scope, runtime(1)));
    const { controller, states } = harness(loader);
    controller.select(scope, workstream({ execution_state: 'idle', active_turn_id: null }), true);
    await vi.waitFor(() => expect(states.at(-1)?.freshness).toBe('idle'));
    expect(states.at(-1)?.contextUsage?.prompt_tokens).toBe(100);
    controller.dispose();
  });

  it('rejects out-of-order runtime frames for the selected child', async () => {
    const scope = sessionTimelineScope('session-1', 'conversation-1');
    const loader = vi.fn(async () => snapshot(scope, runtime(3, { promptTokens: 300 })));
    const { controller, emit, states } = harness(loader);
    controller.select(scope, workstream(), true);
    await vi.waitFor(() => expect(states.at(-1)?.contextUsage?.prompt_tokens).toBe(300));
    emit({
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'test',
      scope,
      conversation_id: 'conversation-1',
      cursor_before: 'cursor-2',
      cursor_after: 'cursor-1',
      ops: [],
      runtime: runtime(2, { promptTokens: 200 }),
      server_time: '2026-09-08T20:12:02Z',
    });
    expect(states.at(-1)?.contextUsage?.prompt_tokens).toBe(300);
    controller.dispose();
  });

  it('releases hidden scopes and reconciles once after reconnect', async () => {
    const scope = sessionTimelineScope('session-1', 'conversation-1');
    const loader = vi.fn(async () => snapshot(scope, runtime(loader.mock.calls.length)));
    const { connection, controller, socket } = harness(loader);
    controller.select(scope, workstream(), true);
    await vi.waitFor(() => expect(socket.acquireChatV2).toHaveBeenCalledOnce());
    controller.select(scope, workstream(), false);
    expect(socket.releaseChatV2).toHaveBeenCalledWith(scope.key);

    controller.select(scope, workstream(), true);
    await vi.waitFor(() => expect(socket.acquireChatV2).toHaveBeenCalledTimes(2));
    connection.set({ status: 'reconnecting' });
    connection.set({ status: 'connected' });
    await vi.waitFor(() => expect(loader).toHaveBeenCalledTimes(3));
    expect(socket.updateChatV2Cursor).toHaveBeenCalledTimes(1);
    controller.dispose();
  });

  it('keeps one trailing reconciliation and ignores the disposed response', async () => {
    const scope = sessionTimelineScope('session-1', 'conversation-1');
    let resolveFirst!: (value: ChatSnapshot) => void;
    const first = new Promise<ChatSnapshot>((resolve) => {
      resolveFirst = resolve;
    });
    const loader = vi.fn()
      .mockReturnValueOnce(first)
      .mockResolvedValue(snapshot(scope, runtime(2)));
    const { controller, socket } = harness(loader);
    controller.select(scope, workstream(), true);
    controller.invalidate();
    resolveFirst(snapshot(scope, runtime(1)));
    await vi.waitFor(() => expect(loader).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(socket.acquireChatV2).toHaveBeenCalledOnce());
    controller.dispose();
    expect(socket.releaseChatV2).toHaveBeenCalledOnce();
  });

  it('allows the same telemetry identity to retry after execution reconciliation fails', async () => {
    const scope = sessionTimelineScope('session-1', 'conversation-1');
    const currentRuntime = runtime(2);
    const loader = vi.fn(async () => snapshot(scope, currentRuntime));
    const refreshExecution = vi.fn<() => Promise<boolean>>()
      .mockRejectedValueOnce(new Error('temporary overview failure'))
      .mockResolvedValueOnce(true);
    const { controller, emit } = harness(loader, refreshExecution);
    controller.select(scope, workstream({ execution_turn_id: 'turn-1' }), true);
    await vi.waitFor(() => expect(refreshExecution).toHaveBeenCalledOnce());

    emit({
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'test',
      scope,
      conversation_id: 'conversation-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-2',
      ops: [],
      runtime: currentRuntime,
      server_time: currentRuntime.generated_at,
    });
    await vi.waitFor(() => expect(refreshExecution).toHaveBeenCalledTimes(2));
    controller.dispose();
  });

  it('retries child telemetry until the rendered root Workstream applies', async () => {
    const scope = sessionTimelineScope('session-1', 'conversation-1');
    const currentRuntime = runtime(2);
    const loader = vi.fn(async () => snapshot(scope, currentRuntime));
    let rootAttempts = 0;
    let renderedWorkstream = workstream({ execution_turn_id: 'turn-1' });
    let controller!: FocusedSessionDiagnosticsController;
    const refreshExecution = vi.fn(async () => {
      const applied = await reconcileRenderedOverviewSources({
        focusedScopeKey: scope.key,
        rootScopeKey: 'conversation:conversation-1',
        currentFocusedScopeKey: () => scope.key,
        loadRoot: async () => {
          rootAttempts += 1;
          if (rootAttempts === 1) return false;
          renderedWorkstream = workstream();
          controller.updateExecution(renderedWorkstream);
          return true;
        },
        loadFocused: async () => true,
      });
      return applied;
    });
    const harnessValue = harness(loader, refreshExecution);
    controller = harnessValue.controller;
    controller.select(scope, renderedWorkstream, true);
    await vi.waitFor(() => expect(refreshExecution).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(rootAttempts).toBe(1));
    expect(renderedWorkstream.execution_turn_id).toBe('turn-1');

    harnessValue.emit({
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'test',
      scope,
      conversation_id: 'conversation-1',
      cursor_before: 'cursor-1',
      cursor_after: 'cursor-2',
      ops: [],
      runtime: currentRuntime,
      server_time: currentRuntime.generated_at,
    });

    await vi.waitFor(() => expect(refreshExecution).toHaveBeenCalledTimes(2));
    expect(renderedWorkstream.execution_turn_id).toBe('turn-2');
    expect(harnessValue.states.at(-1)?.freshness).toBe('current');
    controller.dispose();
  });
});
