import type { Readable, Unsubscriber } from 'svelte/store';

import type {
  ChatRealtimeFrame,
  ChatSnapshot,
  RuntimeOverlaySnapshot,
  TimelineScope,
  WorkstreamRef,
} from '$lib/chat-v2/types';
import type { ContextUsage } from '$lib/types/api';

export type FocusedDiagnosticsFreshness =
  | 'idle'
  | 'current'
  | 'updating'
  | 'reconnecting'
  | 'unavailable';

export interface FocusedSessionDiagnosticsState {
  sessionId: string | null;
  contextUsage: ContextUsage | null;
  freshness: FocusedDiagnosticsFreshness;
}

interface ConnectionState {
  status: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'stalled';
}

interface DiagnosticsSocket {
  subscribe(listener: (event: ChatRealtimeFrame | { type: string }) => void): Unsubscriber;
  acquireChatV2(scope: TimelineScope, cursor: string): void;
  updateChatV2Cursor(scope: TimelineScope, cursor: string): void;
  releaseChatV2(scopeKey: string): void;
  state: Readable<ConnectionState>;
}

type SnapshotLoader = (
  scope: TimelineScope,
  options: { signal: AbortSignal },
) => Promise<ChatSnapshot>;

function isChatRealtimeFrame(
  event: ChatRealtimeFrame | { type: string },
): event is ChatRealtimeFrame {
  return event.type === 'chat_v2_frame' && 'ops' in event;
}

const EMPTY_STATE: FocusedSessionDiagnosticsState = {
  sessionId: null,
  contextUsage: null,
  freshness: 'unavailable',
};

function isOngoing(node: WorkstreamRef | null): boolean {
  return node?.execution_state === 'queued'
    || node?.execution_state === 'running'
    || node?.execution_state === 'waiting'
    || node?.execution_state === 'recovering';
}

function contextMatchesExecution(
  usage: ContextUsage | null,
  node: WorkstreamRef | null,
): boolean {
  if (!usage || !node) return false;
  if (node.active_turn_id && usage.turn_id !== node.active_turn_id) return false;
  if (
    node.runtime_selection_revision != null
    && usage.runtime_selection_revision !== node.runtime_selection_revision
  ) return false;
  if (node.model && usage.model !== node.model) return false;
  if (node.reasoning_effort && usage.reasoning_effort !== node.reasoning_effort) return false;
  if (node.agent_profile_id && usage.agent_profile_id !== node.agent_profile_id) return false;
  return true;
}

function executionSignature(node: WorkstreamRef | null): string {
  return JSON.stringify([
    node?.session_id ?? null,
    node?.execution_state ?? null,
    node?.active_turn_id ?? null,
    node?.execution_turn_id ?? null,
    node?.runtime_selection_revision ?? null,
    node?.agent_profile_id ?? null,
    node?.model ?? null,
    node?.reasoning_effort ?? null,
  ]);
}

export class FocusedSessionDiagnosticsController {
  private scope: TimelineScope | null = null;
  private execution: WorkstreamRef | null = null;
  private executionKey = executionSignature(null);
  private visible = false;
  private subscribed = false;
  private generation = 0;
  private abortController: AbortController | null = null;
  private running = false;
  private dirty = false;
  private disposed = false;
  private socketConnected = false;
  private runtimeGeneratedAt = 0;
  private runtimeEpoch: string | null = null;
  private runtimeRevision = -1;
  private requestedExecutionRefreshFor: string | null = null;
  private retained = new Map<string, ContextUsage>();
  private stateValue: FocusedSessionDiagnosticsState = EMPTY_STATE;
  private readonly unsubscribeEvents: Unsubscriber;
  private readonly unsubscribeState: Unsubscriber;

  constructor(
    private readonly socket: DiagnosticsSocket,
    private readonly loadSnapshot: SnapshotLoader,
    private readonly onChange: (state: FocusedSessionDiagnosticsState) => void,
    private readonly onExecutionRefresh: () => boolean | Promise<boolean> = () => true,
  ) {
    this.unsubscribeEvents = socket.subscribe((event) => this.handleEvent(event));
    this.unsubscribeState = socket.state.subscribe((state) => {
      const connected = state.status === 'connected';
      const reconnected = connected && !this.socketConnected;
      this.socketConnected = connected;
      if (!this.visible || !this.scope) return;
      if (!connected) {
        this.publish(this.contextUsage(), 'reconnecting');
      } else if (reconnected) {
        this.invalidate();
      }
    });
  }

  select(scope: TimelineScope | null, execution: WorkstreamRef | null, visible: boolean): void {
    const changed = scope?.key !== this.scope?.key;
    const nextExecutionKey = executionSignature(execution);
    const executionChanged = nextExecutionKey !== this.executionKey;
    this.visible = visible;
    this.execution = execution;
    this.executionKey = nextExecutionKey;
    if (changed || !visible) {
      this.release();
      this.scope = visible ? scope : null;
      this.generation += 1;
      this.runtimeGeneratedAt = 0;
      this.runtimeEpoch = null;
      this.runtimeRevision = -1;
      this.requestedExecutionRefreshFor = null;
    } else {
      this.scope = scope;
    }
    if (!visible || !scope) {
      this.publish(null, 'unavailable');
      return;
    }
    const retained = this.retained.get(scope.session_id ?? '') ?? null;
    this.publish(retained, this.freshness(retained));
    if (changed || executionChanged || !this.subscribed) this.invalidate();
  }

  updateExecution(execution: WorkstreamRef | null): void {
    this.execution = execution;
    this.executionKey = executionSignature(execution);
    const usage = this.contextUsage();
    this.publish(usage, this.freshness(usage));
    if (this.visible && this.scope && isOngoing(execution) && !contextMatchesExecution(usage, execution)) {
      this.invalidate();
    }
  }

  invalidate(): void {
    if (this.disposed || !this.visible || !this.scope) return;
    this.dirty = true;
    if (!this.running) void this.drain();
  }

  dispose(): void {
    this.disposed = true;
    this.release();
    this.unsubscribeEvents();
    this.unsubscribeState();
  }

  private async drain(): Promise<void> {
    this.running = true;
    try {
      while (this.dirty && !this.disposed && this.visible && this.scope) {
        this.dirty = false;
        const scope = this.scope;
        const generation = this.generation;
        this.abortController?.abort();
        const controller = new AbortController();
        this.abortController = controller;
        if (isOngoing(this.execution)) {
          this.publish(this.contextUsage(), this.socketConnected ? 'updating' : 'reconnecting');
        }
        try {
          const snapshot = await this.loadSnapshot(scope, { signal: controller.signal });
          if (
            controller.signal.aborted
            || generation !== this.generation
            || scope.key !== this.scope?.key
          ) continue;
          this.applyRuntime(snapshot.runtime);
          if (this.subscribed) {
            this.socket.updateChatV2Cursor(scope, snapshot.cursor);
          } else {
            this.socket.acquireChatV2(scope, snapshot.cursor);
            this.subscribed = true;
          }
        } catch {
          if (!controller.signal.aborted && generation === this.generation) {
            this.publish(
              this.contextUsage(),
              this.socketConnected ? 'unavailable' : 'reconnecting',
            );
          }
        }
      }
    } finally {
      this.running = false;
      if (this.dirty && !this.disposed) void this.drain();
    }
  }

  private handleEvent(event: ChatRealtimeFrame | { type: string }): void {
    if (
      !isChatRealtimeFrame(event)
      || !this.visible
      || !this.scope
      || event.scope?.key !== this.scope.key
    ) return;
    if (event.ops.some((operation) => operation.op === 'reset')) {
      this.invalidate();
      return;
    }
    if (event.runtime) this.applyRuntime(event.runtime);
  }

  private applyRuntime(runtime: RuntimeOverlaySnapshot): void {
    const generatedAt = Date.parse(runtime.generated_at) || 0;
    if (
      generatedAt < this.runtimeGeneratedAt
      || (
        runtime.runtime_epoch === this.runtimeEpoch
        && runtime.runtime_revision < this.runtimeRevision
      )
    ) return;
    this.runtimeGeneratedAt = generatedAt;
    this.runtimeEpoch = runtime.runtime_epoch;
    this.runtimeRevision = runtime.runtime_revision;
    const usage = runtime.context_usage ?? this.contextUsage();
    const sessionId = this.scope?.session_id ?? null;
    if (usage && sessionId) this.retained.set(sessionId, usage);
    const executionRefreshKey = usage
      ? `${usage.turn_id ?? ''}:${usage.runtime_selection_revision ?? ''}:${usage.runtime_metadata_revision ?? ''}`
      : null;
    if (
      usage
      && isOngoing(this.execution)
      && this.execution?.active_turn_id === usage.turn_id
      && executionRefreshKey !== this.requestedExecutionRefreshFor
      && (
        this.execution?.execution_turn_id !== this.execution?.active_turn_id
        || !contextMatchesExecution(usage, this.execution)
      )
    ) {
      this.requestedExecutionRefreshFor = executionRefreshKey;
      void Promise.resolve(this.onExecutionRefresh())
        .then((applied) => {
          if (!applied && this.requestedExecutionRefreshFor === executionRefreshKey) {
            this.requestedExecutionRefreshFor = null;
          }
        })
        .catch(() => {
          if (this.requestedExecutionRefreshFor === executionRefreshKey) {
            this.requestedExecutionRefreshFor = null;
          }
        });
    }
    this.publish(usage, this.freshness(usage));
  }

  private contextUsage(): ContextUsage | null {
    return this.stateValue.sessionId === (this.scope?.session_id ?? null)
      ? this.stateValue.contextUsage
      : this.retained.get(this.scope?.session_id ?? '') ?? null;
  }

  private freshness(usage: ContextUsage | null): FocusedDiagnosticsFreshness {
    if (!this.socketConnected) return 'reconnecting';
    if (!isOngoing(this.execution)) return usage ? 'idle' : 'unavailable';
    return contextMatchesExecution(usage, this.execution) ? 'current' : 'updating';
  }

  private publish(
    contextUsage: ContextUsage | null,
    freshness: FocusedDiagnosticsFreshness,
  ): void {
    this.stateValue = {
      sessionId: this.scope?.session_id ?? null,
      contextUsage,
      freshness,
    };
    this.onChange(this.stateValue);
  }

  private release(): void {
    this.abortController?.abort();
    this.abortController = null;
    this.dirty = false;
    if (this.subscribed && this.scope) this.socket.releaseChatV2(this.scope.key);
    this.subscribed = false;
  }
}
