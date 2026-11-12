import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { ChatV2ApiClient } from '$lib/chat-v2/api';
import type { Deliverable, StepRun } from '$lib/types/api';
import type {
  ChatRealtimeFrame,
  ChatSnapshot,
  QueueMessage,
  TimelineBackfillResponse,
  TimelineItem,
  TimelineScope,
} from '$lib/chat-v2/types';
import ScopedChatV2Timeline from './ScopedChatV2Timeline.svelte';

function message(index: number, content = `message ${index}`): TimelineItem {
  return {
    id: `message:${index}`,
    kind: 'message',
    sort_key: `0000:${String(index).padStart(15, '0')}:000000:02:000000000`,
    source_refs: [],
    stable: true,
    role: 'assistant',
    content,
    message_id: `msg-${index}`,
    attachments: [],
    partial: false,
  } as TimelineItem;
}

function snapshot(
  scope: TimelineScope,
  items: TimelineItem[],
  beforeCursor: string | null,
): ChatSnapshot {
  return {
    schema_version: 2,
    projection_version: 'test',
    scope,
    conversation: {
      conversation_id: scope.conversation_id ?? 'conversation',
      agent_id: 'agent',
      status: 'active',
    },
    timeline: {
      items,
      has_more_before: beforeCursor !== null,
      before_cursor: beforeCursor,
    },
    state: {
      state_version: 1,
      snapshot_generated_at: '',
      capabilities: [],
      active_turn: {},
      pending: {},
      active_session: {},
    },
    queue: { messages: [], queued_count: 0 },
    runtime: {
      runtime_epoch: 'epoch',
      runtime_revision: 0,
      generated_at: '',
      has_active_turn: false,
      active_turn: null,
      volatile_items: [],
      cycle_states: [],
    },
    cursor: `cursor:${scope.key}`,
    server_time: '',
  } as unknown as ChatSnapshot;
}

function backfill(
  scope: TimelineScope,
  items: TimelineItem[],
  beforeCursor: string | null,
): TimelineBackfillResponse {
  return {
    schema_version: 2,
    projection_version: 'test',
    conversation_id: scope.conversation_id ?? 'conversation',
    scope,
    items,
    has_more_before: beforeCursor !== null,
    before_cursor: beforeCursor,
    server_time: '',
  };
}

const scope: TimelineScope = {
  key: 'session:child',
  kind: 'session',
  session_id: 'child',
  conversation_id: 'conversation',
};

function realtime() {
  let listener: ((frame: any) => void) | null = null;
  return {
    subscribe: vi.fn((next: (frame: any) => void) => {
      listener = next;
      return () => { listener = null; };
    }),
    acquireChatV2: vi.fn(),
    updateChatV2Cursor: vi.fn(),
    releaseChatV2: vi.fn(),
    emit(frame: any) {
      listener?.(frame);
    },
  };
}

function timelineOptions(before: string) {
  return expect.objectContaining({ before, signal: expect.any(AbortSignal) });
}

async function readyViewport(): Promise<HTMLElement> {
  const viewport = await screen.findByTestId('scoped-timeline-viewport');
  // Row presence no longer implies that initial positioning has revealed it.
  await waitFor(() => {
    expect(viewport.querySelector('[data-timeline-row-key]')).not.toBeNull();
    expect(viewport).not.toHaveClass('invisible');
  });
  return viewport;
}

class ResizeObserverMock {
  static instances: ResizeObserverMock[] = [];
  callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    ResizeObserverMock.instances.push(this);
  }

  observe() {}
  disconnect() {}

  emit(): void {
    this.callback([], this as unknown as ResizeObserver);
  }
}

describe('ScopedChatV2Timeline history backfill', () => {
  it('marks an existing timeline document stale when step metadata changes without reloading', async () => {
    const document: Deliverable = {
      deliverable_id: 'live-task-document', step_run_id: 'live-task-run',
      version: 1, attempt_number: 1, lineage_id: 'live-task-document', revision: 1,
      format: 'markdown', title: 'Live result', content: 'The first task output.',
      target: 'none', outputs: {}, status: 'approved', evaluator_feedback: null,
      created_at: null, updated_at: null,
      task_freshness: { state: 'current', replacement_status: null, label: 'Current output' },
    };
    const stepRun: StepRun = {
      step_run_id: 'live-task-run', step_name: 'publish', task_id: 'live-task',
      status: 'running', attempt: 1, attempt_number: 1, agent_id: 'live-agent',
      step_type: 'run', superseded_by_step_run_id: null, workspace_root: null,
      working_directory: null, conversation_id: null, session_id: null,
      intaris_session_id: null, deliverable_id: document.deliverable_id,
      require_deliverable: true, evaluation: null, runtime_info: null, todos: [],
      started_at: null, completed_at: null, updated_at: null, deliverables: [document],
      output: null, deliverable_freshness: {
        [document.deliverable_id]: { state: 'current', replacement_status: null, label: 'Current output' },
      },
    };
    const taskScope: TimelineScope = {
      kind: 'task_step', key: 'task_step:live-task-run', step_run_id: 'live-task-run',
      task_id: 'live-task', conversation_id: 'live-task-conversation',
    };
    const item: TimelineItem = {
      id: 'document:live-task-document', kind: 'assistant_deliverable',
      sort_key: '0000:000000000000001:000000:02:000000000',
      source_refs: [], stable: true, deliverable_id: document.deliverable_id,
      format: document.format, deliverable: document,
    };
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(taskScope, [item], null)),
      sync: vi.fn(), timeline: vi.fn(),
    };
    const props = { scope: taskScope, api, realtime: realtime(), stepRun };
    const view = render(ScopedChatV2Timeline, props);
    expect(await screen.findByText('The first task output.')).toBeVisible();

    await view.rerender({ ...props,
      deliverableFreshness: { [document.deliverable_id]: {
        state: 'stale', replacement_status: 'running',
        label: 'Stale — revision 1, new revision in progress',
      } },
    });
    for (const label of await screen.findAllByText('Stale — revision 1, new revision in progress')) {
      expect(label).toBeVisible();
    }
    expect(screen.queryByText('The first task output.')).toBeNull();
    expect(screen.getAllByTestId('rich-deliverable')).toHaveLength(1);
    await fireEvent.click(await screen.findByRole('button', { name: 'Expand document' }));
    expect(await screen.findByText('The first task output.')).toBeVisible();
    await view.rerender({ ...props,
      deliverableFreshness: { [document.deliverable_id]: {
        state: 'stale', replacement_status: 'paused',
        label: 'Stale — revision 1, revision paused',
      } },
    });
    for (const label of await screen.findAllByText('Stale — revision 1, revision paused')) {
      expect(label).toBeVisible();
    }
    expect(screen.getByText('The first task output.')).toBeVisible();
    view.unmount();
  });

  it('fills a reduced editable history window and stops automatic retries after a page error', async () => {
    const editableScope: TimelineScope = {
      key: 'conversation:reduced-history',
      kind: 'conversation',
      conversation_id: 'reduced-history',
    };
    const payload = snapshot(editableScope, [message(3)], 'before-3');
    payload.timeline.history_window_reduced = true;
    const api = {
      snapshot: vi.fn().mockResolvedValue(payload),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockResolvedValueOnce(backfill(editableScope, [message(2)], 'before-2'))
        .mockRejectedValueOnce(new Error('Document storage unavailable'))
        .mockResolvedValueOnce(backfill(editableScope, [message(1)], null)),
    };
    render(ScopedChatV2Timeline, {
      scope: editableScope, api, realtime: realtime(), hasEditableComposer: true,
    });
    await screen.findByText('Document storage unavailable');
    expect(api.timeline).toHaveBeenCalledTimes(2);
    expect(api.timeline).toHaveBeenNthCalledWith(1, editableScope, timelineOptions('before-3'));
    expect(api.timeline).toHaveBeenNthCalledWith(2, editableScope, timelineOptions('before-2'));
    ResizeObserverMock.instances.forEach((observer) => observer.emit());
    await waitFor(() => expect(api.timeline).toHaveBeenCalledTimes(2));
    await fireEvent.click(screen.getByRole('button', { name: /^Try again$/ }));
    await screen.findByText('message 1');
    expect(api.timeline).toHaveBeenNthCalledWith(3, editableScope, timelineOptions('before-2'));
  });

  it('can move live feedback outside the transcript without changing runtime or editability', async () => {
    const payload = snapshot(scope, [message(1)], null);
    payload.runtime.has_active_turn = true;
    const api = { snapshot: vi.fn().mockResolvedValue(payload), sync: vi.fn(), timeline: vi.fn() };
    const onRuntimeActiveChange = vi.fn();
    const props = { scope, api, realtime: realtime(), onRuntimeActiveChange, showLiveIndicator: false };
    const view = render(ScopedChatV2Timeline, props);
    await screen.findByText('message 1');
    await waitFor(() => expect(onRuntimeActiveChange).toHaveBeenLastCalledWith(true));
    expect(screen.queryByTestId('timeline-running-indicator')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    await view.rerender({ ...props, showLiveIndicator: true });
    expect(await screen.findByTestId('timeline-running-indicator')).toBeInTheDocument();
    expect(api.snapshot).toHaveBeenCalledOnce();
  });

  it('advances empty initial history without an animation frame and stops at a repeated cursor', async () => {
    const originalRaf = globalThis.requestAnimationFrame;
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [], 'empty-before-1')),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockResolvedValueOnce(backfill(scope, [], 'empty-before-2'))
        .mockResolvedValueOnce(backfill(scope, [], 'empty-before-2')),
    };
    try {
      render(ScopedChatV2Timeline, { scope, api, realtime: realtime(), autoBackfill: false });
      await screen.findByText('Earlier history could not be advanced.');
      expect(api.timeline).toHaveBeenCalledTimes(2);
      expect(screen.queryByText('Loading timeline…')).toBeNull();
      expect(screen.queryByText('Loading earlier timeline history.')).toBeNull();
    } finally {
      vi.stubGlobal('requestAnimationFrame', originalRaf);
    }
  });

  it('keeps typed critical event-store failures actionable while the WebSocket is degraded', async () => {
    const { ChatV2ApiError } = await import('$lib/chat-v2/api');
    window.dispatchEvent(new Event('offline'));
    const api = {
      snapshot: vi.fn().mockRejectedValue(new ChatV2ApiError('Critical fixture event store failure', {
        code: 'event_store_unavailable', status: 503, details: { recoverable: true },
      })),
      sync: vi.fn(), timeline: vi.fn(),
    };
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    try {
      expect(await screen.findByRole('alert')).toHaveTextContent('Critical fixture event store failure');
      expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled();
      expect(screen.queryByText('Loading timeline…')).toBeNull();
      expect(api.snapshot).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      window.dispatchEvent(new Event('online'));
    }
  });

  it('renders queued messages and hides Following for editable composers', async () => {
    const scope: TimelineScope = {
      key: 'conversation:conversation-queue',
      kind: 'conversation',
      conversation_id: 'conversation-queue'
    };
    const queuedSnapshot = snapshot(scope, [message(1)], null);
    queuedSnapshot.queue = {
      messages: [{ queue_id: 'queue-1', content: 'queued follow-up', attachments: [], position: 0 }],
      queued_count: 1
    };
    render(ScopedChatV2Timeline, {
      scope,
      api: {
        snapshot: vi.fn().mockResolvedValue(queuedSnapshot),
        sync: vi.fn(),
        timeline: vi.fn()
      },
      realtime: realtime(),
      hasEditableComposer: true
    });
    expect(await screen.findByTestId('scoped-queued-message-queue-1')).toHaveTextContent('queued follow-up');
    expect(screen.queryByText('Following')).not.toBeInTheDocument();
  });

  it('promotes a queued message at the authoritative user-message boundary', async () => {
    const scope: TimelineScope = {
      key: 'conversation:conversation-queue-boundary',
      kind: 'conversation',
      conversation_id: 'conversation-queue-boundary'
    };
    const queuedSnapshot = snapshot(scope, [], null);
    queuedSnapshot.queue = {
      messages: [{
        queue_id: 'queue-boundary',
        client_message_id: 'client-boundary',
        content: 'queued plan',
        attachments: [],
        position: 0,
      }],
      queued_count: 1
    };
    const channel = realtime();
    render(ScopedChatV2Timeline, {
      scope,
      api: {
        snapshot: vi.fn().mockResolvedValue(queuedSnapshot),
        sync: vi.fn().mockResolvedValue({
          ...queuedSnapshot,
          cursor_before: queuedSnapshot.cursor,
          cursor_after: queuedSnapshot.cursor,
          ops: [],
          reset_required: false,
          reset_reason: null,
          has_more: false,
        }),
        timeline: vi.fn()
      },
      realtime: channel,
      hasEditableComposer: true
    });
    expect(await screen.findByTestId('scoped-queued-message-queue-boundary')).toBeInTheDocument();

    channel.emit({
      type: 'turn_started',
      conversation_id: scope.conversation_id,
      message_id: 'turn-different-from-client',
      turn_id: 'turn-different-from-client',
      chat_mode: 'plan',
    });
    expect(screen.getByTestId('scoped-queued-message-queue-boundary')).toBeInTheDocument();

    channel.emit({
      type: 'user_message',
      conversation_id: scope.conversation_id,
      message_id: 'canonical-message',
      turn_id: 'turn-different-from-client',
      client_message_id: 'client-boundary',
      content: 'queued plan',
      attachments: [],
      chat_mode: 'plan',
      timestamp: '2026-08-30T00:00:00Z',
    });

    await waitFor(() => {
      expect(screen.queryByTestId('scoped-queued-message-queue-boundary')).toBeNull();
    });
    expect(await screen.findByText('queued plan')).toBeInTheDocument();
  });

  it('does not automatically backfill editable history and signals one successful load', async () => {
    const editableScope: TimelineScope = {
      key: 'conversation:editable',
      kind: 'conversation',
      conversation_id: 'editable',
    };
    const onInitialLoaded = vi.fn();
    const api = {
      snapshot: vi.fn().mockResolvedValue(
        snapshot(editableScope, Array.from({ length: 200 }, (_, index) => message(index + 1)), 'before-200'),
      ),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    const view = render(ScopedChatV2Timeline, {
      scope: editableScope,
      api,
      realtime: realtime(),
      hasEditableComposer: true,
      onInitialLoaded,
    });

    await screen.findByText('message 200');
    await waitFor(() => expect(onInitialLoaded).toHaveBeenCalledOnce());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.timeline).not.toHaveBeenCalled();
    expect(screen.queryByText(/Load older/i)).not.toBeInTheDocument();

    await view.rerender({
      scope: editableScope,
      api,
      realtime: realtime(),
      hasEditableComposer: true,
      onInitialLoaded,
    });
    expect(onInitialLoaded).toHaveBeenCalledOnce();
  }, 20_000);

  it('does not deliver a queued initial-load callback after transcript disposal', async () => {
    const disposalScope: TimelineScope = {
      key: 'conversation:queued-disposal',
      kind: 'conversation',
      conversation_id: 'queued-disposal',
    };
    const onInitialLoaded = vi.fn();
    const live = realtime();
    let dispose: (() => void) | undefined;
    let disposal = Promise.resolve();
    let disposed = false;
    const fetch = vi.fn<typeof globalThis.fetch>();
    const api = new ChatV2ApiClient({ fetch });
    const loadedSnapshot = snapshot(disposalScope, [message(1)], null);
    loadedSnapshot.queue = {
      messages: [{ queue_id: 'queued-disposal', content: 'Queued fixture message', attachments: [], position: 0 }],
      queued_count: 1,
    };
    vi.spyOn(api, 'snapshot').mockResolvedValue(loadedSnapshot);
    const view = render(ScopedChatV2Timeline, {
      scope: disposalScope,
      api,
      realtime: live,
      onInitialLoaded,
      onQueueChange: (messages: QueueMessage[]) => {
        if (!disposed && messages.some(message => message.queue_id === 'queued-disposal')) {
          disposed = true;
          dispose?.();
        }
      },
    });
    dispose = () => { disposal = Promise.resolve(view.unmount()); };
    await waitFor(() => expect(disposed).toBe(true));
    await disposal;
    await tick();
    expect(view.container.childElementCount).toBe(0);
    expect(onInitialLoaded).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('does not signal initial load after a snapshot error', async () => {
    const onInitialLoaded = vi.fn();
    const snapshotApi = vi.fn()
      .mockRejectedValueOnce(new Error('snapshot failed'))
      .mockResolvedValueOnce(snapshot(scope, [message(1)], null));
    render(ScopedChatV2Timeline, {
      scope,
      api: {
        snapshot: snapshotApi,
        sync: vi.fn(),
        timeline: vi.fn(),
      },
      realtime: realtime(),
      onInitialLoaded,
    });

    await screen.findByText('snapshot failed');
    expect(onInitialLoaded).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await screen.findByText('message 1');
    await waitFor(() => expect(snapshotApi).toHaveBeenCalledTimes(2));
  });

  it('hides ambient refresh in compact mode but keeps error-local retry', async () => {
    const snapshotApi = vi.fn().mockRejectedValue(new Error('compact snapshot failed'));
    render(ScopedChatV2Timeline, {
      scope,
      compact: true,
      api: {
        snapshot: snapshotApi,
        sync: vi.fn(),
        timeline: vi.fn(),
      },
      realtime: realtime(),
    });

    await screen.findByText('compact snapshot failed');
    expect(screen.queryByRole('button', { name: 'Refresh timeline' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('scoped-timeline-status-row')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(snapshotApi).toHaveBeenCalledTimes(2));
  });

  beforeAll(() => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    Object.defineProperty(HTMLDivElement.prototype, 'clientHeight', {
      configurable: true,
      get() {
        return this.dataset.testid === 'scoped-timeline-viewport' ? 300 : 0;
      },
    });
    Object.defineProperty(HTMLDivElement.prototype, 'scrollHeight', {
      configurable: true,
      get() {
        if (this.dataset.testid !== 'scoped-timeline-viewport') return 0;
        return this.querySelectorAll('[data-timeline-row-key]').length * 100;
      },
    });
  });

  it('loads on wheel-up at the top without a prior click', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-1')),
      sync: vi.fn(),
      timeline: vi.fn().mockResolvedValue(backfill(scope, [message(0)], null)),
    };
    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    viewport.scrollTop = 0;

    await fireEvent.wheel(viewport, { deltaY: -20 });

    await waitFor(() => expect(api.timeline).toHaveBeenCalledWith(scope, timelineOptions('before-1')));
    expect(screen.getByText('message 0')).toBeTruthy();
  });

  it('preserves the visual anchor after trusted top-edge backfill on an editable view', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-1')),
      sync: vi.fn(),
      timeline: vi.fn().mockResolvedValue(backfill(scope, [message(0)], null)),
    };
    render(ScopedChatV2Timeline, {
      scope,
      api,
      realtime: realtime(),
      hasEditableComposer: true,
    });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    expect(api.timeline).not.toHaveBeenCalled();
    viewport.scrollTop = 0;

    await fireEvent.wheel(viewport, { deltaY: -20 });
    await fireEvent.wheel(viewport, { deltaY: -20 });

    await screen.findByText('message 0');
    await waitFor(() => expect(viewport.scrollTop).toBe(100));
  });

  it('loads one page for each distinct editable top-edge encounter', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-1')),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockResolvedValueOnce(backfill(scope, [message(0)], 'before-0'))
        .mockResolvedValueOnce(backfill(scope, [message(-1)], null)),
    };
    render(ScopedChatV2Timeline, {
      scope,
      api,
      realtime: realtime(),
      hasEditableComposer: true,
    });
    const viewport = await readyViewport();
    await screen.findByText('message 4');

    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await screen.findByText('message 0');
    expect(api.timeline).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.queryByText('Loading earlier timeline history.')).toBeNull());

    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await screen.findByText('message -1');
    expect(api.timeline).toHaveBeenCalledTimes(2);
    expect(api.timeline.mock.calls.map((call) => call[1]?.before)).toEqual(['before-1', 'before-0']);
  });

  it('deduplicates concurrent near-top events for the same cursor', async () => {
    let resolvePage: ((page: TimelineBackfillResponse) => void) | undefined;
    const pendingPage = new Promise<TimelineBackfillResponse>((resolve) => {
      resolvePage = resolve;
    });
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-1')),
      sync: vi.fn(),
      timeline: vi.fn().mockReturnValue(pendingPage),
    };
    render(ScopedChatV2Timeline, { scope, api, realtime: realtime(), hasEditableComposer: true });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    viewport.scrollTop = 0;
    await fireEvent.scroll(viewport);
    await fireEvent.scroll(viewport);
    await fireEvent.keyDown(viewport, { key: 'PageUp' });
    expect(api.timeline).toHaveBeenCalledOnce();
    resolvePage?.(backfill(scope, [message(0)], null));
    await screen.findByText('message 0');
  });

  it('follows delayed row resize until a trusted upward scroll disengages it', async () => {
    ResizeObserverMock.instances = [];
    const channel = realtime();
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, Array.from({ length: 8 }, (_, index) => message(index + 1)), null)),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    const view = render(ScopedChatV2Timeline, {
      scope,
      api,
      realtime: channel,
      hasEditableComposer: true,
    });
    const viewport = await readyViewport();
    await screen.findByText('message 8');
    expect(viewport.scrollTop).toBe(viewport.scrollHeight);

    channel.emit({
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'test',
      conversation_id: 'conversation',
      scope,
      cursor_before: 'cursor:session:child',
      cursor_after: 'cursor:resize-follow',
      ops: [{ op: 'upsert_item', item: message(9, 'delayed resize row') }],
      runtime: null,
      server_time: '',
    } as ChatRealtimeFrame);
    await screen.findByText('delayed resize row');
    await fireEvent.pointerDown(viewport);
    await fireEvent.wheel(viewport, { deltaY: 100 });
    await fireEvent.keyDown(viewport, { key: 'ArrowDown' });
    await fireEvent.scroll(viewport);
    expect(screen.queryByRole('button', { name: 'Resume live follow' })).toBeNull();
    ResizeObserverMock.instances.forEach((observer) => observer.emit());
    expect(viewport.scrollTop).toBe(viewport.scrollHeight);

    viewport.scrollTop = 200;
    await fireEvent.wheel(viewport, { deltaY: -100 });
    await fireEvent.scroll(viewport);
    const pausedTop = viewport.scrollTop;
    expect(screen.getByRole('button', { name: 'Resume live follow' })).toBeVisible();
    const row = screen.getByText('delayed resize row');
    await view.rerender({ scope: { ...scope }, api, realtime: channel, hasEditableComposer: true });
    expect(screen.getByTestId('scoped-timeline-viewport')).toBe(viewport);
    expect(screen.getByText('delayed resize row')).toBe(row);
    expect(screen.getByRole('button', { name: 'Resume live follow' })).toBeVisible();
    ResizeObserverMock.instances.forEach((observer) => observer.emit());
    expect(viewport.scrollTop).toBe(pausedTop);
    expect(api.snapshot).toHaveBeenCalledOnce();
  });

  it('loads when a scrollbar scroll reaches the top', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-1')),
      sync: vi.fn(),
      timeline: vi.fn().mockResolvedValue(backfill(scope, [message(0)], null)),
    };
    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    viewport.scrollTop = 0;

    await fireEvent.wheel(viewport, { deltaY: -20 });

    await waitFor(() => expect(api.timeline).toHaveBeenCalledWith(scope, timelineOptions('before-1')));
    expect(screen.getByText('message 0')).toBeTruthy();
  });

  it('resumes follow for a submitted message but not restored outbox messages', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, Array.from({ length: 8 }, (_, index) => message(index + 1)), null)),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    const { component } = render(ScopedChatV2Timeline, {
      scope, api, realtime: realtime(), hasEditableComposer: true,
    });
    const viewport = await readyViewport();
    await screen.findByText('message 8');
    viewport.scrollTop = 200;
    await fireEvent.wheel(viewport, { deltaY: -100 });
    await fireEvent.scroll(viewport);
    expect(screen.getByRole('button', { name: 'Resume live follow' })).toBeVisible();
    component.stageOptimisticMessage({ scopeKey: scope.key, content: 'Restored draft', clientMessageId: 'restored-draft' });
    await screen.findByText('Restored draft');
    expect(screen.getByRole('button', { name: 'Resume live follow' })).toBeVisible();
    component.stageOptimisticMessage({ scopeKey: scope.key, content: 'New submission', clientMessageId: 'submitted-draft', follow: true });
    await screen.findByText('New submission');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Resume live follow' })).not.toBeInTheDocument());
    expect(viewport.scrollTop).toBe(viewport.scrollHeight);
  });

  it.each([
    ['touch', async (viewport: HTMLElement) => {
      await fireEvent.touchStart(viewport, { touches: [{ clientY: 100 }] });
      await fireEvent.touchMove(viewport, { touches: [{ clientY: 120 }] });
    }],
    ['keyboard', async (viewport: HTMLElement) => {
      await fireEvent.keyDown(viewport, { key: 'PageUp' });
    }],
  ])('loads on the %s upward interaction path', async (_label, interact) => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-1')),
      sync: vi.fn(),
      timeline: vi.fn().mockResolvedValue(backfill(scope, [message(0)], null)),
    };
    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    viewport.scrollTop = 0;

    await interact(viewport);

    await waitFor(() => expect(api.timeline).toHaveBeenCalledOnce());
  });

  it('loads the bounded initial history even after the viewport is scrollable', async () => {
    const pages = [
      backfill(scope, [message(2)], 'before-2'),
      backfill(scope, [message(1)], 'before-1'),
      backfill(scope, [message(0)], null),
    ];
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [message(3)], 'before-3')),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockResolvedValueOnce(pages[0])
        .mockResolvedValueOnce(pages[1])
        .mockResolvedValueOnce(pages[2]),
    };

    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });

    await waitFor(() => expect(api.timeline).toHaveBeenCalledTimes(3));
    expect(api.timeline.mock.calls.map((call) => call[1]?.before)).toEqual([
      'before-3',
      'before-2',
      'before-1',
    ]);
    expect(screen.getByText('message 0')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Load older/ })).toBeNull();
  });

  it('loads older history when the initial visible timeline is empty', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [], 'before-empty')),
      sync: vi.fn(),
      timeline: vi.fn().mockResolvedValue(backfill(scope, [message(0, 'first visible row')], null)),
    };

    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });

    await screen.findByText('first visible row');
    expect(api.timeline).toHaveBeenCalledWith(scope, timelineOptions('before-empty'));
  });

  it('continues through empty and duplicate pages while the cursor advances', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [message(3)], 'before-3')),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockResolvedValueOnce(backfill(scope, [], 'before-2'))
        .mockResolvedValueOnce(backfill(scope, [message(3)], 'before-1'))
        .mockResolvedValueOnce(backfill(scope, [message(2, 'visible older row')], null)),
    };

    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });

    await screen.findByText('visible older row');
    expect(api.timeline.mock.calls.map((call) => call[1]?.before)).toEqual([
      'before-3',
      'before-2',
      'before-1',
    ]);
    expect(screen.getAllByText('message 3')).toHaveLength(1);
  });

  it('bounds automatic empty-page loading and continues after a user action', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [], 'before-9')),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockResolvedValueOnce(backfill(scope, [], 'before-8'))
        .mockResolvedValueOnce(backfill(scope, [], 'before-7'))
        .mockResolvedValueOnce(backfill(scope, [], 'before-6'))
        .mockResolvedValueOnce(backfill(scope, [], 'before-5'))
        .mockResolvedValueOnce(backfill(scope, [], 'before-4'))
        .mockResolvedValueOnce(backfill(scope, [], 'before-3'))
        .mockResolvedValueOnce(backfill(scope, [], 'before-2'))
        .mockResolvedValueOnce(backfill(scope, [], 'before-1'))
        .mockResolvedValueOnce(backfill(scope, [message(0, 'continued row')], null)),
    };

    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });

    await waitFor(() => expect(api.timeline).toHaveBeenCalledTimes(8));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.timeline).toHaveBeenCalledTimes(8);
    expect(screen.queryByText('No events recorded yet.')).toBeNull();
    const viewport = screen.getByTestId('scoped-timeline-viewport');
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await screen.findByText('continued row');
    expect(api.timeline).toHaveBeenCalledTimes(9);
  });

  it('stops on a repeated cursor without issuing a duplicate request', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [], 'repeated')),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockResolvedValueOnce(backfill(scope, [], 'repeated'))
        .mockResolvedValueOnce(backfill(scope, [message(0, 'manual retry row')], null)),
    };

    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });

    await waitFor(() => expect(api.timeline).toHaveBeenCalledOnce());
    const viewport = screen.getByTestId('scoped-timeline-viewport');
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('manual retry row')).not.toBeInTheDocument();
    expect(api.timeline).toHaveBeenCalledOnce();
  });

  it('retries the same cursor after a transient backfill failure', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-retry')),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockRejectedValueOnce(new Error('temporary history failure'))
        .mockResolvedValueOnce(backfill(scope, [message(0, 'retried older row')], null)),
    };
    render(ScopedChatV2Timeline, {
      scope,
      api,
      realtime: realtime(),
      hasEditableComposer: true,
    });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await screen.findByText('temporary history failure');

    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await screen.findByText('retried older row');
    expect(api.timeline).toHaveBeenCalledTimes(2);
    expect(api.timeline.mock.calls.map((call) => call[1]?.before)).toEqual([
      'before-retry',
      'before-retry',
    ]);
  });

  it('ignores a stale backfill and lets the new scope load after it settles', async () => {
    let resolveOldPage: ((page: TimelineBackfillResponse) => void) | undefined;
    const oldPage = new Promise<TimelineBackfillResponse>((resolve) => {
      resolveOldPage = resolve;
    });
    const nextScope: TimelineScope = {
      key: 'session:next',
      kind: 'session',
      session_id: 'next',
      conversation_id: 'conversation',
    };
    const api = {
      snapshot: vi.fn(async (requestedScope: TimelineScope) => (
        requestedScope.key === scope.key
          ? snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-old')
          : snapshot(nextScope, [1, 2, 3, 4].map((item) => message(item + 10, `next ${item}`)), 'before-next')
      )),
      sync: vi.fn(),
      timeline: vi.fn((requestedScope: TimelineScope) => (
        requestedScope.key === scope.key
          ? oldPage
          : Promise.resolve(backfill(nextScope, [message(10, 'next older row')], null))
      )),
    };
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await waitFor(() => expect(api.timeline).toHaveBeenCalledOnce());

    await view.rerender({ scope: nextScope, api, realtime: realtime() });
    resolveOldPage?.(backfill(scope, [message(0, 'stale history')], null));

    await screen.findByText('next 4');
    await readyViewport();
    expect(screen.queryByText('stale history')).toBeNull();
    expect(viewport.closest('[data-scope-key]')).toHaveAttribute('data-scope-key', nextScope.key);
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await screen.findByText('next older row');
    expect(api.timeline).toHaveBeenCalledWith(nextScope, timelineOptions('before-next'));
  });

  it('does not restore the old anchor when the scope changes after backfill apply', async () => {
    let resolveOldPage: ((page: TimelineBackfillResponse) => void) | undefined;
    const oldPage = new Promise<TimelineBackfillResponse>((resolve) => {
      resolveOldPage = resolve;
    });
    let resolveNextSnapshot: ((value: ChatSnapshot) => void) | undefined;
    const nextSnapshot = new Promise<ChatSnapshot>((resolve) => {
      resolveNextSnapshot = resolve;
    });
    const nextScope: TimelineScope = {
      key: 'session:post-tick',
      kind: 'session',
      session_id: 'post-tick',
      conversation_id: 'conversation',
    };
    const api = {
      snapshot: vi.fn((requestedScope: TimelineScope, _options?: { signal?: AbortSignal }) => (
        requestedScope.key === scope.key
          ? Promise.resolve(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-old'))
          : nextSnapshot
      )),
      sync: vi.fn(),
      timeline: vi.fn().mockReturnValue(oldPage),
    };
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    let scrollTop = 0;
    const scrollWrites: number[] = [];
    Object.defineProperty(viewport, 'scrollTop', {
      configurable: true,
      get: () => scrollTop,
      set: (value: number) => {
        scrollTop = value;
        scrollWrites.push(value);
      },
    });
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await waitFor(() => expect(api.timeline).toHaveBeenCalledOnce());

    resolveOldPage?.(backfill(scope, [message(0, 'applied old row')], null));
    // Resume loadOlder through applyBackfill and stop it at its awaited tick.
    await Promise.resolve();
    scrollWrites.length = 0;
    await view.rerender({ scope: nextScope, api, realtime: realtime() });
    await Promise.resolve();

    expect(scrollWrites).toEqual([]);
    expect(screen.queryByText('applied old row')).toBeNull();
    resolveNextSnapshot?.(snapshot(nextScope, [message(10, 'post-tick scope')], null));
    await screen.findByText('post-tick scope');
  });

  it('resets a pending backfill on refresh and permits new older loading', async () => {
    let resolveStalePage: ((page: TimelineBackfillResponse) => void) | undefined;
    const stalePage = new Promise<TimelineBackfillResponse>((resolve) => {
      resolveStalePage = resolve;
    });
    let resolveRefresh: ((value: ChatSnapshot) => void) | undefined;
    const refreshedSnapshot = new Promise<ChatSnapshot>((resolve) => {
      resolveRefresh = resolve;
    });
    const api = {
      snapshot: vi.fn()
        .mockResolvedValueOnce(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-old'))
        .mockReturnValueOnce(refreshedSnapshot),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockReturnValueOnce(stalePage)
        .mockResolvedValueOnce(backfill(scope, [message(10, 'refreshed older row')], null)),
    };
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: realtime(), autoBackfill: false });
    await screen.findByText('message 4');
    const viewport = await readyViewport();
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await waitFor(() => expect(api.timeline).toHaveBeenCalledOnce());

    const refresh = view.component.refresh();
    resolveRefresh?.(snapshot(scope, [11, 12, 13, 14].map((item) => message(item)), 'before-refresh'));
    await screen.findByText('message 14');
    await refresh;
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await screen.findByText('refreshed older row');

    resolveStalePage?.(backfill(scope, [message(0, 'stale older row')], null));
    await Promise.resolve();
    expect(screen.queryByText('stale older row')).toBeNull();
    expect(api.timeline).toHaveBeenNthCalledWith(2, scope, timelineOptions('before-refresh'));
  });

  it('blocks all older-loading interactions until refresh settles', async () => {
    let resolveInitialPage: ((page: TimelineBackfillResponse) => void) | undefined;
    const initialPage = new Promise<TimelineBackfillResponse>((resolve) => {
      resolveInitialPage = resolve;
    });
    let resolveRefresh: ((value: ChatSnapshot) => void) | undefined;
    const refreshedSnapshot = new Promise<ChatSnapshot>((resolve) => {
      resolveRefresh = resolve;
    });
    const api = {
      snapshot: vi.fn()
        .mockResolvedValueOnce(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-old'))
        .mockReturnValueOnce(refreshedSnapshot),
      sync: vi.fn(),
      timeline: vi.fn()
        .mockReturnValueOnce(initialPage)
        .mockResolvedValueOnce(backfill(scope, [message(10, 'post-refresh older row')], null)),
    };
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: realtime(), autoBackfill: false });
    await screen.findByText('message 4');
    const viewport = await readyViewport();
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await waitFor(() => expect(api.timeline).toHaveBeenCalledOnce());

    const refresh = view.component.refresh();
    await waitFor(() => expect(api.snapshot).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/Load older/i)).not.toBeInTheDocument();
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await fireEvent.touchStart(viewport, { touches: [{ clientY: 100 }] });
    await fireEvent.touchMove(viewport, { touches: [{ clientY: 120 }] });
    await fireEvent.keyDown(viewport, { key: 'PageUp' });
    expect(api.timeline).toHaveBeenCalledOnce();

    resolveRefresh?.(snapshot(scope, [11, 12, 13, 14].map((item) => message(item)), 'before-refresh'));
    await screen.findByText('message 14');
    await refresh;
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await screen.findByText('post-refresh older row');
    resolveInitialPage?.(backfill(scope, [message(0, 'stale older row')], null));
    await Promise.resolve();
    expect(screen.queryByText('stale older row')).toBeNull();
    expect(api.timeline).toHaveBeenCalledTimes(2);
    expect(api.timeline).toHaveBeenNthCalledWith(2, scope, timelineOptions('before-refresh'));
  });

  it('preserves a realtime row that arrives while a backfill is pending', async () => {
    let resolvePage: ((page: TimelineBackfillResponse) => void) | undefined;
    const page = new Promise<TimelineBackfillResponse>((resolve) => {
      resolvePage = resolve;
    });
    const channel = realtime();
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [1, 2, 3, 4].map((item) => message(item)), 'before-1')),
      sync: vi.fn(),
      timeline: vi.fn().mockReturnValue(page),
    };
    render(ScopedChatV2Timeline, { scope, api, realtime: channel });
    const viewport = await readyViewport();
    await screen.findByText('message 4');
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -20 });
    await waitFor(() => expect(api.timeline).toHaveBeenCalledOnce());

    channel.emit({
      type: 'chat_v2_frame',
      schema_version: 2,
      projection_version: 'test',
      conversation_id: 'conversation',
      scope,
      cursor_before: 'cursor:session:child',
      cursor_after: 'cursor:live',
      ops: [{ op: 'upsert_item', item: message(5, 'realtime row') }],
      runtime: null,
      server_time: '',
    } as ChatRealtimeFrame);
    resolvePage?.(backfill(scope, [message(0, 'older row')], null));

    await screen.findByText('older row');
    expect(screen.getByText('realtime row')).toBeTruthy();
  });

  it('aborts a pending snapshot on unmount and ignores its late result', async () => {
    let resolveSnapshot: ((value: ChatSnapshot) => void) | undefined;
    const pendingSnapshot = new Promise<ChatSnapshot>((resolve) => {
      resolveSnapshot = resolve;
    });
    const api = {
      snapshot: vi.fn().mockReturnValue(pendingSnapshot),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    await waitFor(() => expect(api.snapshot).toHaveBeenCalledOnce());
    const signal = api.snapshot.mock.calls[0]?.[1]?.signal as AbortSignal;

    view.unmount();
    expect(signal.aborted).toBe(true);
    resolveSnapshot?.(snapshot(scope, [message(1, 'late snapshot row')], null));
    await Promise.resolve();
    expect(screen.queryByText('late snapshot row')).toBeNull();
  });

  it('never acquires realtime after unmount when the cached view resolves late', async () => {
    const { TimelineController } = await import('$lib/chat-v2/timeline-controller.svelte');
    let resolveCache: (() => void) | undefined;
    const spy = vi.spyOn(TimelineController.prototype, 'hydrate').mockReturnValue(
      new Promise((resolve) => { resolveCache = resolve; }),
    );
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [message(1)], null)),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    const channel = realtime();
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: channel });
    await waitFor(() => expect(spy).toHaveBeenCalled());

    view.unmount();
    resolveCache?.();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(api.snapshot).not.toHaveBeenCalled();
    expect(channel.acquireChatV2).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it('opens from a cached view with a cursor sync instead of a snapshot', async () => {
    const { TimelineController } = await import('$lib/chat-v2/timeline-controller.svelte');
    const { ChatV2Store } = await import('$lib/chat-v2/store.svelte');
    const seeded = new ChatV2Store();
    seeded.replaceFromSnapshotIfUnchanged(snapshot(scope, [message(1, 'cached row')], null), seeded.refreshWatermark());
    const spy = vi.spyOn(TimelineController.prototype, 'hydrate').mockImplementation(async function (this: InstanceType<typeof TimelineController>) {
      this.store.restoreState(seeded.serializeState());
    });
    const api = {
      snapshot: vi.fn(),
      sync: vi.fn().mockResolvedValue({
        schema_version: 2,
        projection_version: 'test',
        cursor_before: `cursor:${scope.key}`,
        cursor_after: `cursor:${scope.key}`,
        has_more: false,
        ops: [],
      }),
      timeline: vi.fn(),
    };
    const channel = realtime();
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: channel });

    expect(await screen.findByText('cached row')).toBeTruthy();
    await waitFor(() => expect(api.sync).toHaveBeenCalled());
    expect(channel.acquireChatV2).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(channel.releaseChatV2).toHaveBeenCalledWith(scope.key);
    spy.mockRestore();
  });

  it.each([
    scope,
    { kind: 'conversation', key: 'conversation:managed-history', conversation_id: 'managed-history' } as TimelineScope,
  ].flatMap(scope => [false, true].map(offline => ({ scope, offline }))))(
    'reveals $scope.kind cached history during sync and initializes once (offline=$offline)', async ({ scope, offline }) => {
    const onInitialLoaded = vi.fn();
    const { TimelineController } = await import('$lib/chat-v2/timeline-controller.svelte');
    const { ChatV2Store } = await import('$lib/chat-v2/store.svelte');
    const seeded = new ChatV2Store();
    seeded.replaceFromSnapshotIfUnchanged(snapshot(scope, [message(1, 'quiet cached row')], null), seeded.refreshWatermark());
    const spy = vi.spyOn(TimelineController.prototype, 'hydrate').mockImplementation(async function (this: InstanceType<typeof TimelineController>) {
      this.store.restoreState(seeded.serializeState());
    });
    let finish!: () => void;
    const gate = new Promise<void>((resolve) => { finish = resolve; });
    const api = { snapshot: vi.fn(), timeline: vi.fn(), sync: vi.fn<ChatV2ApiClient['sync']>(async () => {
      await gate;
      if (offline) throw new TypeError('Network disconnected');
      return { schema_version: 2, projection_version: 'test', scope, cursor_before: `cursor:${scope.key}`,
        cursor_after: `cursor:${scope.key}:quiet`, conversation_id: scope.conversation_id!, reset_required: false,
        server_time: '', has_more: false, ops: [] };
    }) };
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: realtime(), compact: true, onInitialLoaded });
    try {
      await waitFor(() => expect(api.sync).toHaveBeenCalled());
      expect(document.querySelector(`[data-cursor="cursor:${scope.key}"][data-freshness="reconciling"]`)).toBeTruthy();
      expect(screen.queryByTestId('scoped-timeline-status-row')).toBeNull();
      expect(screen.queryByText('Refreshing…')).toBeNull();
      await waitFor(() => expect(screen.getByTestId('scoped-timeline-viewport')).not.toHaveClass('invisible'));
      expect(screen.getByText('quiet cached row')).toBeVisible();
      expect(onInitialLoaded).not.toHaveBeenCalled();
      const viewport = screen.getByTestId('scoped-timeline-viewport');
      viewport.scrollTop = 50;
      await fireEvent.wheel(viewport, { deltaY: -80 });
      finish();
      await waitFor(() => expect(onInitialLoaded).toHaveBeenCalledTimes(1));
      expect(onInitialLoaded).toHaveBeenCalledWith();
      await screen.findByText('quiet cached row');
      if (!offline) await waitFor(() => expect(document.querySelector(`[data-cursor="cursor:${scope.key}:quiet"]`)).toBeTruthy());
      expect(viewport.scrollTop).toBe(50);
      expect(screen.queryByTestId('scoped-timeline-status-row')).toBeNull();
    } finally { finish(); view.unmount(); spy.mockRestore(); }
  });

  it('shows a recoverable snapshot error and succeeds on retry', async () => {
    const api = {
      snapshot: vi.fn()
        .mockRejectedValueOnce(new Error('Request timed out after 30 seconds.'))
        .mockResolvedValueOnce(snapshot(scope, [message(1, 'retry row')], null)),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });

    await screen.findByText('Request timed out after 30 seconds.');
    const retry = screen.getByRole('button', { name: 'Try again' });
    expect(retry).toBeEnabled();
    await fireEvent.click(retry);
    await screen.findByText('retry row');
    expect(screen.queryByText('Request timed out after 30 seconds.')).toBeNull();
  });

  it('aborts the old snapshot when the scope changes without flashing an error', async () => {
    const nextScope: TimelineScope = {
      key: 'session:next-abort',
      kind: 'session',
      session_id: 'next-abort',
      conversation_id: 'conversation',
    };
    const api = {
      snapshot: vi.fn((requestedScope: TimelineScope, _options?: { signal?: AbortSignal }) => (
        requestedScope.key === scope.key
          ? new Promise<ChatSnapshot>(() => {})
          : Promise.resolve(snapshot(nextScope, [message(2, 'new scope row')], null))
      )),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    const view = render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    await waitFor(() => expect(api.snapshot).toHaveBeenCalledOnce());
    const oldSignal = api.snapshot.mock.calls[0]?.[1]?.signal as AbortSignal;

    await view.rerender({ scope: nextScope, api, realtime: realtime() });
    expect(oldSignal.aborted).toBe(true);
    await screen.findByText('new scope row');
    expect(screen.queryByText(/aborted/i)).toBeNull();
  });

  it('uses a bounded initial snapshot timeout well under the old 30s value', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [message(1)], null)),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    await waitFor(() => expect(api.snapshot).toHaveBeenCalledOnce());
    const timeoutMs = api.snapshot.mock.calls[0]?.[1]?.timeoutMs as number;
    expect(timeoutMs).toBeGreaterThanOrEqual(12_000);
    expect(timeoutMs).toBeLessThanOrEqual(15_000);
  });

  it('yields before applying every snapshot and discards it if the scope changes during the yield', async () => {
    const items = Array.from({ length: 24 }, (_, i) => message(i + 1));
    const pendingSnapshot = snapshot(scope, items, null);
    const nextScope: TimelineScope = {
      key: 'session:after-yield',
      kind: 'session',
      session_id: 'after-yield',
      conversation_id: 'conversation',
    };
    const api = {
      snapshot: vi.fn((requestedScope: TimelineScope) => (
        requestedScope.key === scope.key
          ? Promise.resolve(pendingSnapshot)
          : new Promise<ChatSnapshot>(() => {})
      )),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    const pendingYield: { release: (() => void) | null } = { release: null };
    vi.stubGlobal('scheduler', {
      yield: () => new Promise<void>((resolve) => {
        pendingYield.release = resolve;
      }),
    });
    try {
      const onInitialLoaded = vi.fn();
      const view = render(ScopedChatV2Timeline, {
        scope,
        api,
        realtime: realtime(),
        onInitialLoaded,
      });
      await waitFor(() => expect(api.snapshot).toHaveBeenCalledOnce());
      await waitFor(() => expect(pendingYield.release).not.toBeNull());
      // The snapshot resolved, but the yield has not fired yet: projection
      // must not have run, so the loading state (and therefore
      // Back/Close responsiveness) is preserved instead of blocking on it.
      expect(screen.getByText('Loading timeline…')).toBeTruthy();
      expect(screen.queryByText('message 1')).toBeNull();
      expect(onInitialLoaded).not.toHaveBeenCalled();

      // Navigate away (scope change) while the yield is still pending.
      await view.rerender({ scope: nextScope, api, realtime: realtime() });
      pendingYield.release?.();
      await Promise.resolve();
      await Promise.resolve();

      expect(screen.queryByText('message 1')).toBeNull();
    } finally {
      vi.stubGlobal('scheduler', { yield: () => Promise.resolve() });
    }
  });

  it('applies a small snapshot after the paint yield completes', async () => {
    const items = [message(1, 'small snapshot row')];
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, items, null)),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
    await screen.findByText('small snapshot row');
  });

  it('does not depend on requestAnimationFrame in standalone Safari', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [message(1, 'Safari task fallback row')], null)),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    vi.stubGlobal('scheduler', undefined);
    vi.stubGlobal('requestAnimationFrame', () => 1);
    try {
      render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
      expect(await screen.findByText('Safari task fallback row')).toBeTruthy();
      expect(screen.queryByText('Loading timeline…')).toBeNull();
    } finally {
      vi.stubGlobal('scheduler', { yield: () => Promise.resolve() });
      vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
        callback(0);
        return 1;
      });
    }
  });

  it('bounds a stalled scheduler yield with an independent watchdog', async () => {
    const api = {
      snapshot: vi.fn().mockResolvedValue(snapshot(scope, [message(1, 'yield watchdog row')], null)),
      sync: vi.fn(),
      timeline: vi.fn(),
    };
    vi.stubGlobal('scheduler', { yield: () => new Promise<void>(() => {}) });
    try {
      render(ScopedChatV2Timeline, { scope, api, realtime: realtime() });
      expect(await screen.findByText('yield watchdog row')).toBeTruthy();
      expect(screen.queryByText('Loading timeline…')).toBeNull();
    } finally {
      vi.stubGlobal('scheduler', { yield: () => Promise.resolve() });
    }
  });
});
