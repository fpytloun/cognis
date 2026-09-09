/**
 * Pure partitioning/classification logic for the Control Center dashboard
 * ("/"). Kept side-effect free so the composition and ordering rules can be
 * unit tested without mounting Svelte components or hitting the network.
 */

import type {
  Agent,
  AgentDirectChat,
  Conversation,
  DashboardIssuesResponse,
  Schedule,
  Task,
  TaskBoard,
  TaskBoardItem
} from '$lib/types/api';

export const DEFAULT_TASK_LANE_LIMIT = 5;
export const DEFAULT_SCHEDULE_LANE_LIMIT = 5;
export const DEFAULT_ACTIVE_CONVERSATION_LIMIT = 5;
export const DEFAULT_RECENT_CONVERSATION_LIMIT = 8;

const ACTIVE_MANAGED_TURN_STATES = new Set(['running', 'queued', 'waiting']);
const NON_TOPIC_WEB_KINDS = new Set([
  'agent_direct',
  'main',
  'channel',
  'external',
  'task_control',
  'agent_work',
  'managed_agent_conversation'
]);

export interface DashboardTaskLanes {
  running: TaskBoardItem[];
  waiting: TaskBoardItem[];
  recent: TaskBoardItem[];
}

export interface DashboardConversationLanes {
  active: Conversation[];
  waiting: Conversation[];
  recent: Conversation[];
}

const WAITING_NOTIFICATION_LABELS: Record<string, string> = {
  escalation: 'Approval required',
  gate: 'Review required',
  step_question: 'Question waiting',
  credential_request: 'Credentials required',
  auth_challenge: 'Authentication required'
};

const TASK_ATTENTION_LABELS: Record<string, string> = {
  escalation: 'Approval required',
  gate: 'Review required',
  step_input: 'Input required',
  step_question: 'Question waiting',
  credential_request: 'Credentials required',
  auth_challenge: 'Authentication required',
  recovery: 'Recovery decision required'
};

function recencyKey(value: string | null | undefined): string {
  return value ?? '';
}

function taskActivityValue(item: Pick<TaskBoardItem, 'updated_at' | 'completed_at' | 'started_at' | 'created_at'>): string {
  return recencyKey(item.updated_at ?? item.completed_at ?? item.started_at ?? item.created_at);
}

/**
 * Split a task board into the three bounded lanes the dashboard shows:
 * `running` (from the canonical `running` column, highest priority and most
 * recently active first), `waiting` (all paused tasks), and `recent` (from
 * the canonical `done` column, most recently finished first). Running and
 * recent are capped at `limit`; waiting contains every loaded paused task.
 */
export function partitionTaskLanes(board: TaskBoard | null | undefined, limit = DEFAULT_TASK_LANE_LIMIT): DashboardTaskLanes {
  const runningItems = board?.columns?.running?.items ?? [];
  const pausedItems = board?.columns?.paused?.items ?? [];
  const doneItems = board?.columns?.done?.items ?? [];
  const nonPausedColumnItems = Object.entries(board?.columns ?? {})
    .filter(([column]) => column !== 'paused')
    .flatMap(([, value]) => value.items);
  const paused = [...nonPausedColumnItems, ...pausedItems]
    .filter(isDashboardTaskWaiting);
  const pausedById = new Map(paused.map((item) => [item.task_id, item]));
  const pausedIds = new Set(pausedById.keys());

  const running = [...runningItems]
    .filter((item) => item.status !== 'paused')
    .sort((left, right) => {
      if (left.priority !== right.priority) return right.priority - left.priority;
      return taskActivityValue(right).localeCompare(taskActivityValue(left));
    })
    .slice(0, limit);

  const recent = doneItems
    .filter((item) => !pausedIds.has(item.task_id) && item.status !== 'paused')
    .sort((left, right) => taskActivityValue(right).localeCompare(taskActivityValue(left)))
    .slice(0, limit);

  const waiting = [...pausedById.values()]
    .sort((left, right) => taskActivityValue(right).localeCompare(taskActivityValue(left)));

  return { running, waiting, recent };
}

export function isDashboardTaskWaiting(task: Pick<TaskBoardItem, 'status'>): boolean {
  return task.status === 'paused';
}

export function dashboardTaskRequiresAction(task: Pick<TaskBoardItem, 'attention_type'>): boolean {
  return Boolean(task.attention_type?.trim());
}

export function dashboardTaskWaitingReason(
  task: Pick<TaskBoardItem, 'attention_type' | 'result_summary' | 'progress_summary'>
): string {
  const attentionType = task.attention_type?.trim() ?? '';
  if (attentionType) return TASK_ATTENTION_LABELS[attentionType] ?? 'Action required';
  return task.result_summary?.trim()
    || task.progress_summary?.current_step_name?.trim()
    || 'Paused';
}

/**
 * Enabled schedules with a resolvable next-fire time, soonest first.
 * Schedules without a `next_fire_at` (disabled, expired, one-shot already
 * fired) are omitted entirely rather than sorted to the end.
 */
export function upcomingSchedules(schedules: Schedule[] | null | undefined, limit = DEFAULT_SCHEDULE_LANE_LIMIT): Schedule[] {
  return (schedules ?? [])
    .filter((schedule) => schedule.enabled && Boolean(schedule.next_fire_at))
    .sort((left, right) => (left.next_fire_at as string).localeCompare(right.next_fire_at as string))
    .slice(0, limit);
}

/**
 * A conversation counts as "active now" when it has an in-flight direct
 * turn, or its managed-agent turn state indicates in-flight work. This
 * mirrors `managedConversationTurnState` in `chat-page.ts` without importing
 * the whole chat-page module (which pulls in unrelated UI-only helpers).
 */
export function isDashboardConversationActive(conversation: {
  has_active_turn: boolean;
  managed_agent?: { turn_state?: string | null } | null;
}): boolean {
  if (conversation.has_active_turn) return true;
  const state = conversation.managed_agent?.turn_state ?? null;
  return state !== null && ACTIVE_MANAGED_TURN_STATES.has(state);
}

export function dashboardConversationWaitingReason(conversation: Pick<Conversation, 'pending_notification_types'>): string | null {
  for (const type of conversation.pending_notification_types ?? []) {
    const label = WAITING_NOTIFICATION_LABELS[type];
    if (label) return label;
  }
  return null;
}

export function isDashboardConversationWaiting(conversation: Pick<Conversation, 'pending_notification_types'>): boolean {
  return dashboardConversationWaitingReason(conversation) !== null;
}

/** Ordinary web topic conversations only; mirrors backend orchestration surface classification. */
export function isDashboardTopicConversation(conversation: Conversation): boolean {
  const context = conversation.context;
  if ((context?.type ?? '').trim().toLowerCase() !== 'web') return false;
  if (conversation.managed_agent) return false;
  if (conversation.root_controller_conversation_id) return false;
  if (conversation.conversation_state?.task) return false;
  const kind = String(context.platform_data?.kind ?? '').trim().toLowerCase();
  if (NON_TOPIC_WEB_KINDS.has(kind)) return false;
  const ref = context.ref ?? '';
  if (ref.startsWith('web:agent_direct:')) return false;
  if (ref.startsWith('web:user:') && ref.endsWith(':default')) return false;
  return true;
}

function conversationActivityValue(conversation: Pick<Conversation, 'last_message_at'>): string {
  return recencyKey(conversation.last_message_at);
}

/**
 * Partition conversations into disjoint "active now" and "recent idle"
 * lanes. Active status is computed over the *full* input set before either
 * lane is capped, so a conversation that is active never leaks into the
 * recent lane merely because the active lane display limit was reached.
 */
export function partitionConversationLanes(
  conversations: Conversation[] | null | undefined,
  limits: { active?: number; recent?: number } = {}
): DashboardConversationLanes {
  const activeLimit = limits.active ?? DEFAULT_ACTIVE_CONVERSATION_LIMIT;
  const recentLimit = limits.recent ?? DEFAULT_RECENT_CONVERSATION_LIMIT;

  const sorted = (conversations ?? []).filter(isDashboardTopicConversation).sort((left, right) =>
    conversationActivityValue(right).localeCompare(conversationActivityValue(left))
  );

  const waitingAll = sorted.filter(isDashboardConversationWaiting);
  const waitingIds = new Set(waitingAll.map((conversation) => conversation.conversation_id));
  const activeAll = sorted.filter(
    (conversation) => !waitingIds.has(conversation.conversation_id) && isDashboardConversationActive(conversation)
  );
  const activeIds = new Set(activeAll.map((conversation) => conversation.conversation_id));
  const recentAll = sorted.filter(
    (conversation) =>
      !waitingIds.has(conversation.conversation_id) &&
      !activeIds.has(conversation.conversation_id)
  );

  return {
    active: activeAll.slice(0, activeLimit),
    waiting: waitingAll.slice(0, 5),
    recent: recentAll.slice(0, recentLimit)
  };
}

/** Primary agents eligible for the dashboard avatar strip, in stable display order. */
export function primaryAgentsForStrip(agents: Agent[] | null | undefined): Agent[] {
  return (agents ?? [])
    .filter((agent) => agent.agent_type === 'primary' && !agent.hidden && !agent.disabled)
    .sort((left, right) => (left.display_name ?? left.name).localeCompare(right.display_name ?? right.name));
}

/** Resolve an already-known agent-direct conversation for the given agent, if one exists. */
export function findAgentDirectChat(chats: AgentDirectChat[] | null | undefined, agentId: string): AgentDirectChat | null {
  return (chats ?? []).find((chat) => chat.agent.agent_id === agentId) ?? null;
}

export type DashboardHealthTone = 'healthy' | 'info' | 'warning' | 'critical';

export interface DashboardHealthSummary {
  tone: DashboardHealthTone;
  headline: string;
}

/** Compact one-line summary + severity tone driving the needs-attention strip vs. healthy line. */
export function summarizeDashboardIssues(response: DashboardIssuesResponse | null | undefined): DashboardHealthSummary {
  const summary = response?.summary;
  if (!summary || summary.total === 0) {
    return { tone: 'healthy', headline: 'All systems healthy' };
  }
  if (summary.critical > 0) {
    return {
      tone: 'critical',
      headline: `${summary.critical} critical issue${summary.critical === 1 ? '' : 's'} ${summary.critical === 1 ? 'needs' : 'need'} attention`
    };
  }
  if (summary.warning > 0) {
    return {
      tone: 'warning',
      headline: `${summary.warning} warning${summary.warning === 1 ? '' : 's'} to review`
    };
  }
  return { tone: 'info', headline: `${summary.info} item${summary.info === 1 ? '' : 's'} to review` };
}

/** True when a task belongs to the canonical `running` board classification. */
export function isRunningTask(task: Pick<Task, 'status'>): boolean {
  return task.status === 'running';
}

/**
 * Default agent to use for the dashboard's local "New chat" action: the
 * active primary agent if one exists, otherwise the first primary agent.
 */
export function defaultNewChatAgentId(agents: Agent[] | null | undefined): string | null {
  const primary = primaryAgentsForStrip(agents);
  if (primary.length === 0) return null;
  return primary.find((agent) => agent.status === 'active')?.agent_id ?? primary[0].agent_id;
}
