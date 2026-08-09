import type {
  TimelineItem,
  ToolCallTimelineItem,
  TurnCycleState,
} from '$lib/chat-v2/types';
import type { BackgroundWorkItem, BackgroundWorkTodo } from '$lib/types/api';

const inactiveBackgroundStatuses = new Set([
  'complete',
  'completed',
  'error',
  'failed',
  'cancelled',
  'interrupted',
  'idle',
]);

export function backgroundWorkItemIsRunning(item: BackgroundWorkItem): boolean {
  if (item.kind === 'managed_conversation') {
    return item.status === 'running' || item.status === 'queued';
  }
  return !inactiveBackgroundStatuses.has(item.status);
}

function backgroundWorkActivityMs(item: BackgroundWorkItem): number | null {
  for (const value of [item.updated_at, item.started_at]) {
    if (!value) continue;
    const timestamp = Date.parse(value);
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return null;
}

export function sortBackgroundWorkByActivity(
  items: readonly BackgroundWorkItem[],
): BackgroundWorkItem[] {
  return items
    .map((item, index) => ({
      item,
      index,
      activityMs: backgroundWorkActivityMs(item),
    }))
    .sort((left, right) => {
      if (left.activityMs === null && right.activityMs === null) {
        return left.index - right.index;
      }
      if (left.activityMs === null) return 1;
      if (right.activityMs === null) return -1;
      return right.activityMs - left.activityMs || left.index - right.index;
    })
    .map(({ item }) => item);
}

function managedConversationId(item: ToolCallTimelineItem): string {
  const liveConversation = item.managed_conversation?.conversation;
  if (liveConversation && typeof liveConversation === 'object') {
    const value = (liveConversation as Record<string, unknown>).conversation_id;
    if (typeof value === 'string') return value;
  }
  const argumentId = item.arguments?.conversation_id;
  if (typeof argumentId === 'string') return argumentId;
  try {
    const parsed = JSON.parse(item.result_preview ?? '') as Record<string, unknown>;
    const conversation = parsed.conversation;
    if (conversation && typeof conversation === 'object') {
      const value = (conversation as Record<string, unknown>).conversation_id;
      if (typeof value === 'string') return value;
    }
  } catch {
    // A bounded result preview may not contain complete JSON.
  }
  return '';
}

export function overlayManagedConversationStatus(
  items: readonly TimelineItem[],
  work: readonly BackgroundWorkItem[],
): TimelineItem[] {
  const byTarget = new Map(
    work
      .filter((item) => item.kind === 'managed_conversation' && item.target_conversation_id)
      .map((item) => [item.target_conversation_id as string, item]),
    );
  if (byTarget.size === 0) return [...items];
  return items.map((item) => {
    if (item.kind !== 'tool_call' || !item.tool_name.startsWith('agent_conversation_')) {
      return item;
    }
    const background = byTarget.get(managedConversationId(item));
    if (!background) return item;
    const current = item.managed_conversation ?? {};
    const rawConversation = current.conversation;
    const conversation = rawConversation && typeof rawConversation === 'object'
      ? rawConversation as Record<string, unknown>
      : {};
    return {
      ...item,
      managed_conversation: {
        ...current,
        status: background.status,
        conversation: {
          ...conversation,
          conversation_id: background.target_conversation_id,
          turn_state: background.status,
          active_turn_id: backgroundWorkItemIsRunning(background)
            ? conversation.active_turn_id
            : null,
        },
      },
    };
  });
}

function normalizeTodos(todos: Array<Record<string, unknown>> | null | undefined): BackgroundWorkTodo[] {
  return (todos ?? []).flatMap((todo) => {
    const content = typeof todo.content === 'string' ? todo.content.trim() : '';
    if (!content) return [];
    return [{
      content,
      status: typeof todo.status === 'string' ? todo.status : 'pending',
      priority: typeof todo.priority === 'string' ? todo.priority : 'normal',
    }];
  });
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function foldedDelegation(
  item: ToolCallTimelineItem,
  controllerConversationId: string,
): BackgroundWorkItem | null {
  const payload = item.delegation;
  if (!payload) return null;
  const sessionId = stringValue(payload.child_session_id) || stringValue(payload.childSessionId);
  if (!sessionId) return null;
  return {
    kind: 'delegated_session',
    work_id: sessionId,
    controller_conversation_id: controllerConversationId,
    session_id: sessionId,
    title: stringValue(payload.title) || stringValue(payload.task_title) || 'Delegated work',
    agent_id: (
      stringValue(payload.used_agent_id)
      || stringValue(payload.agent_id)
      || 'delegate'
    ),
    agent_profile_id: (
      stringValue(payload.used_agent_profile_id)
      || stringValue(payload.agent_profile_id)
      || null
    ),
    status: stringValue(payload.status) || stringValue(item.status) || 'running',
    started_at: stringValue(payload.started_at) || item.created_at,
    updated_at: stringValue(payload.completed_at) || item.updated_at,
    todos: normalizeTodos(
      Array.isArray(payload.todos)
        ? payload.todos.filter((todo): todo is Record<string, unknown> => (
            todo !== null && typeof todo === 'object' && !Array.isArray(todo)
          ))
        : [],
    ),
  };
}

export function currentCycleDelegations(
  items: readonly TimelineItem[],
  cycleStates: readonly TurnCycleState[],
  activeTurnId: string | null | undefined,
  controllerConversationId: string,
): BackgroundWorkItem[] {
  if (!activeTurnId) return [];
  const currentCycle = cycleStates
    .filter((state) => state.turn_id === activeTurnId)
    .reduce<number | null>(
      (latest, state) => latest === null || state.turn_cycle_index > latest
        ? state.turn_cycle_index
        : latest,
      null,
    );
  if (currentCycle === null) return [];
  const currentItems = items.filter((item) => (
    (item.kind === 'delegation' || item.kind === 'tool_call')
    && item.turn_id === activeTurnId
    && item.turn_cycle_index === currentCycle
  ));
  const bySessionId = new Map<string, BackgroundWorkItem>();
  for (const item of currentItems) {
    if (item.kind !== 'delegation') continue;
    bySessionId.set(item.child_session_id, {
      kind: 'delegated_session',
      work_id: item.child_session_id,
      controller_conversation_id: controllerConversationId,
      session_id: item.child_session_id,
      title: item.title || 'Delegated work',
      agent_id: item.used_agent_id || item.agent_id || 'delegate',
      status: item.status,
      started_at: item.created_at,
      updated_at: item.updated_at,
      todos: normalizeTodos(item.todos),
    });
  }
  for (const item of currentItems) {
    if (item.kind !== 'tool_call') continue;
    const folded = foldedDelegation(item, controllerConversationId);
    if (folded) bySessionId.set(folded.work_id, folded);
  }
  return [...bySessionId.values()];
}

export function mergeCurrentCycleDelegations(
  projected: readonly BackgroundWorkItem[],
  cycleDelegations: readonly BackgroundWorkItem[],
): BackgroundWorkItem[] {
  const merged = new Map(projected.map((item) => [item.work_id, item]));
  for (const item of cycleDelegations) {
    merged.set(item.work_id, item);
  }
  return [...merged.values()];
}
