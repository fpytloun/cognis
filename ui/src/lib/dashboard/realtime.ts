export type DashboardRealtimeLoad = 'conversations' | 'tasks' | 'schedules' | 'issues';
export type DashboardModalDetailInvalidation =
  | { kind: 'conversation'; id: string }
  | { kind: 'task'; id: string }
  | { kind: 'schedule'; id: string };

export interface DashboardRealtimeBatch {
  loads: ReadonlySet<DashboardRealtimeLoad>;
  modalDetails: readonly DashboardModalDetailInvalidation[];
  conversationPatches: readonly DashboardConversationPatch[];
}

export type OpenDashboardEntity =
  | { kind: 'conversation' | 'task'; id: string }
  | { kind: 'schedule'; id: string };

export function dashboardBatchRefreshesModal(
  batch: DashboardRealtimeBatch,
  entity: OpenDashboardEntity | null
): boolean {
  if (!entity) return false;
  return batch.modalDetails.some((detail) => (
    detail.kind === entity.kind && detail.id === entity.id
  ));
}

export type DashboardRealtimeEvent = {
  type: string;
  payload?: unknown;
  conversation_id?: unknown;
  task_id?: unknown;
  schedule_id?: unknown;
  title?: unknown;
  status?: unknown;
  agent_id?: unknown;
  agent_profile_id?: unknown;
  active_session_id?: unknown;
  project_id?: unknown;
  context?: unknown;
  context_type?: unknown;
  context_data?: unknown;
  has_unread?: unknown;
  has_active_turn?: unknown;
  pending_notification_types?: unknown;
  reason?: unknown;
  conversation?: unknown;
};

export type DashboardConversationPatch = {
  conversation_id: string;
  title?: string;
  status?: string;
  agent_id?: string;
  agent_profile_id?: string;
  active_session_id?: string;
  has_unread?: boolean;
  has_active_turn?: boolean;
  pending_notification_types?: string[];
};

export interface DashboardRealtimeClient {
  subscribe(listener: (event: DashboardRealtimeEvent) => void): () => void;
}

const CONVERSATION_EVENTS = new Set([
  'sidebar_conversation_upsert', 'sidebar_conversation_removed', 'conversation_updated',
  'message_complete', 'turn_started', 'turn_settled', 'queued', 'queued_messages_updated',
  'workflow_step_question', 'auth_challenge', 'credential_request', 'escalation',
  'conversation_runtime_snapshot', 'reconnected'
]);
const TASK_EVENTS = new Set([
  'task_paused', 'workflow_composed', 'workflow_step_started', 'workflow_step_completed',
  'workflow_gate', 'workflow_question', 'workflow_gate_resolved', 'workflow_question_resolved',
  'workflow_completed', 'workflow_failed', 'workflow_cancelled'
]);

export function dashboardRealtimeLoads(
  type: string,
  reason?: unknown,
): DashboardRealtimeLoad[] {
  if (type === 'scope_invalidated') {
    if (reason === 'sidebar_changed' || reason === 'chat_scope_changed') return ['conversations'];
    if (reason === 'notification_state_changed') return ['conversations', 'tasks', 'schedules', 'issues'];
    if (reason === 'task_progress_changed') return ['tasks', 'schedules', 'issues'];
    if (reason === 'schedule_action_changed') return ['schedules', 'issues'];
    return [];
  }
  if (type === 'conversation_state_delta') return ['conversations', 'tasks', 'schedules', 'issues'];
  if (type === 'schedule_action_changed') return ['schedules', 'issues'];
  if (CONVERSATION_EVENTS.has(type)) return ['conversations'];
  if (TASK_EVENTS.has(type)) return ['tasks', 'schedules', 'issues'];
  return [];
}

const CONVERSATION_METADATA_FIELDS = new Set([
  'title', 'status', 'agent_id', 'agent_profile_id', 'active_session_id',
  'project_id', 'context', 'context_type', 'context_data'
]);

function stringField(event: DashboardRealtimeEvent, field: string): string | null {
  const value = (event as unknown as Record<string, unknown>)[field];
  return typeof value === 'string' && value ? value : null;
}

export function dashboardModalDetailInvalidation(
  event: DashboardRealtimeEvent
): DashboardModalDetailInvalidation | null {
  return dashboardModalDetailInvalidations(event)[0] ?? null;
}

export function dashboardModalDetailInvalidations(
  event: DashboardRealtimeEvent
): DashboardModalDetailInvalidation[] {
  if (event.type === 'sidebar_conversation_upsert') {
    const id = stringField(event, 'conversation_id');
    return id ? [{ kind: 'conversation', id }] : [];
  }
  if (
    event.type === 'conversation_updated'
    && [...CONVERSATION_METADATA_FIELDS].some((field) => field in event)
  ) {
    const id = stringField(event, 'conversation_id');
    return id ? [{ kind: 'conversation', id }] : [];
  }
  if (TASK_EVENTS.has(event.type)) {
    const id = stringField(event, 'task_id');
    return id ? [{ kind: 'task', id }] : [];
  }
  if (event.type === 'schedule_action_changed') {
    const id = stringField(event, 'schedule_id');
    return id ? [{ kind: 'schedule', id }] : [];
  }
  if (event.type === 'scope_invalidated') {
    const conversationId = stringField(event, 'conversation_id');
    const taskId = stringField(event, 'task_id');
    const scheduleId = stringField(event, 'schedule_id');
    const reason = String(event.reason ?? '');
    const details: DashboardModalDetailInvalidation[] = [];
    if (
      conversationId
      && [
        'sidebar_changed',
        'chat_scope_changed',
        'notification_state_changed',
        'task_progress_changed',
        'schedule_action_changed',
      ].includes(reason)
    ) details.push({ kind: 'conversation', id: conversationId });
    if (
      taskId
      && ['notification_state_changed', 'task_progress_changed', 'schedule_action_changed'].includes(
        reason,
      )
    ) details.push({ kind: 'task', id: taskId });
    if (scheduleId && ['notification_state_changed', 'schedule_action_changed'].includes(reason)) {
      details.push({ kind: 'schedule', id: scheduleId });
    }
    return details;
  }
  return [];
}

export function dashboardConversationPatch(
  event: DashboardRealtimeEvent,
): DashboardConversationPatch | null {
  if (!['conversation_updated', 'sidebar_conversation_upsert'].includes(event.type)) return null;
  const conversationId = stringField(event, 'conversation_id');
  if (!conversationId) return null;
  const patch: DashboardConversationPatch = { conversation_id: conversationId };
  const nested = (
    event.type === 'sidebar_conversation_upsert'
    && event.conversation !== null
    && typeof event.conversation === 'object'
  )
    ? event.conversation as Record<string, unknown>
    : {};
  const source = { ...nested, ...event } as Record<string, unknown>;
  for (const field of [
    'title', 'status', 'agent_id', 'agent_profile_id', 'active_session_id',
  ] as const) {
    if (typeof source[field] === 'string') patch[field] = source[field] as string;
  }
  for (const field of ['has_unread', 'has_active_turn'] as const) {
    if (typeof source[field] === 'boolean') patch[field] = source[field] as boolean;
  }
  if (
    Array.isArray(source.pending_notification_types)
    && source.pending_notification_types.every((item) => typeof item === 'string')
  ) {
    patch.pending_notification_types = [...source.pending_notification_types] as string[];
  }
  return Object.keys(patch).length > 1 ? patch : null;
}

export function subscribeDashboardRealtime(
  client: DashboardRealtimeClient,
  flush: (batch: DashboardRealtimeBatch) => void
): () => void {
  const pending = new Set<DashboardRealtimeLoad>();
  const pendingModalDetails = new Map<string, DashboardModalDetailInvalidation>();
  const pendingConversationPatches = new Map<string, DashboardConversationPatch>();
  let queued = false;
  let active = true;
  let frame: number | null = null;
  const unsubscribe = client.subscribe((event) => {
    if (event.type === 'chat_v2_frame') return;
    dashboardRealtimeLoads(event.type, event.reason).forEach((load) => pending.add(load));
    for (const detail of dashboardModalDetailInvalidations(event)) {
      pendingModalDetails.set(`${detail.kind}:${detail.id}`, detail);
    }
    const patch = dashboardConversationPatch(event);
    if (patch) {
      pendingConversationPatches.set(patch.conversation_id, {
        ...pendingConversationPatches.get(patch.conversation_id),
        ...patch,
      });
    }
    if (
      (pending.size === 0 && pendingModalDetails.size === 0 && pendingConversationPatches.size === 0)
      || queued
    ) return;
    queued = true;
    frame = requestAnimationFrame(() => {
      if (!active) return;
      queued = false;
      const loads = new Set(pending);
      const modalDetails = [...pendingModalDetails.values()];
      const conversationPatches = [...pendingConversationPatches.values()];
      pending.clear();
      pendingModalDetails.clear();
      pendingConversationPatches.clear();
      flush({ loads, modalDetails, conversationPatches });
    });
  });
  return () => {
    active = false;
    if (frame !== null) cancelAnimationFrame(frame);
    pending.clear();
    pendingModalDetails.clear();
    pendingConversationPatches.clear();
    unsubscribe();
  };
}
