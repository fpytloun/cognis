import type { ChatV2ClientState } from './sync-engine';
import type {
  AuthChallengeTimelineItem,
  MessageTimelineItem,
  QuestionSetTimelineItem,
  TodoStateTimelineItem,
  TimelineItem,
  ToolCallTimelineItem,
} from './types';
import { visibleTimelineItems } from './sync-engine';

export function selectVisibleTimeline(state: ChatV2ClientState): TimelineItem[] {
  return visibleTimelineItems(state);
}

export const TIMELINE_KIND_RENDER_POLICY = {
  message: true,
  thinking: true,
  tool_call: true,
  delegation: true,
  managed_conversation: true,
  task: true,
  question_set: true,
  auth_challenge: true,
  credential_request: true,
  todo_state: false,
  artifact: true,
  assistant_deliverable: true,
  file_diff: true,
  notice: true,
  compaction: true,
  error: true,
} as const satisfies Record<TimelineItem['kind'], boolean>;

export function isRenderableTimelineItem(item: TimelineItem): boolean {
  return TIMELINE_KIND_RENDER_POLICY[item.kind];
}

export function selectRenderableTimeline(items: TimelineItem[]): TimelineItem[] {
  return items.filter(isRenderableTimelineItem);
}

function normalizedToolName(tool: ToolCallTimelineItem): string {
  return tool.tool_name.toLocaleLowerCase().replace(/_/g, '');
}

function hasDeferredAuthChallenge(value: unknown): boolean {
  if (typeof value === 'string') return value.startsWith('$auth_challenge:');
  if (Array.isArray(value)) return value.some(hasDeferredAuthChallenge);
  if (!value || typeof value !== 'object') return false;
  const record = value as Record<string, unknown>;
  if (typeof record.value_ref === 'string' && record.value_ref.startsWith('$auth_challenge:')) return true;
  if (record.auth_challenge && typeof record.auth_challenge === 'object') return true;
  return Object.values(record).some(hasDeferredAuthChallenge);
}

export function isAuthChallengeToolCall(tool: ToolCallTimelineItem): boolean {
  const name = normalizedToolName(tool);
  if (name === 'requestauthchallenge') return true;
  return (name === 'browserfill' || name === 'browsereval') && hasDeferredAuthChallenge(tool.arguments);
}

export function isPendingInputToolCall(tool: ToolCallTimelineItem): boolean {
  const name = normalizedToolName(tool);
  return name === 'steprequestquestions'
    || name === 'requestuserinput'
    || isAuthChallengeToolCall(tool);
}

export function selectPendingInputToolCall(items: TimelineItem[]): ToolCallTimelineItem | null {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind !== 'tool_call' || !isPendingInputToolCall(item)) continue;
    if (!['pending', 'running', 'waiting'].includes(item.status ?? '')) continue;
    if (item.result_preview != null || item.progress_complete === true) continue;
    return item;
  }
  return null;
}

export type PendingInputTimelineItem = QuestionSetTimelineItem | AuthChallengeTimelineItem;

export function selectPendingInputItem(items: TimelineItem[]): PendingInputTimelineItem | null {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === 'question_set' && item.status === 'waiting') return item;
    if (item.kind === 'auth_challenge' && item.status === 'waiting') return item;
  }
  return null;
}

export function selectSearchableMessages(items: TimelineItem[]): MessageTimelineItem[] {
  return items.filter((item): item is MessageTimelineItem => item.kind === 'message');
}

export function selectLatestTodoState(
  items: TimelineItem[],
  resetOnUserMessage = false,
): TodoStateTimelineItem['todos'] {
  let lowerBound = 0;
  if (resetOnUserMessage) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item?.kind === 'message' && item.role === 'user') {
        lowerBound = index;
        break;
      }
    }
  }
  for (let index = items.length - 1; index >= lowerBound; index -= 1) {
    const item = items[index];
    if (item?.kind === 'todo_state') return item.todos;
  }
  return [];
}

export function selectHasActiveTurn(state: ChatV2ClientState): boolean {
  return state.runtime?.has_active_turn === true;
}

export function selectActiveTurnId(state: ChatV2ClientState): string | null {
  return state.runtime?.active_turn?.turn_id ?? null;
}

export function selectNeedsRecovery(state: ChatV2ClientState): boolean {
  return state.syncStatus === 'gapped';
}

export function selectQueuedCount(state: ChatV2ClientState): number {
  return state.queue?.queued_count ?? 0;
}
