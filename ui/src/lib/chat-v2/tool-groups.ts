import type {
  MessageTimelineItem,
  ThinkingTimelineItem,
  TimelineItem,
  ToolCallTimelineItem,
  TurnCycleState
} from '$lib/chat-v2/types';
import type { UserPreferences } from '$lib/types/api';

export type ToolGroupKind =
  | 'explore'
  | 'command'
  | 'edit'
  | 'delegate'
  | 'web'
  | 'browser'
  | 'memory'
  | 'knowledgebase'
  | 'mixed';

export type ActivityGroupIcon =
  | 'search'
  | 'terminal'
  | 'edit'
  | 'delegate'
  | 'globe'
  | 'browser'
  | 'database'
  | 'wrench'
  | 'brain';

export interface ToolGroupSummary {
  kind: ToolGroupKind;
  label: string;
  icon: ActivityGroupIcon;
  accentClass: string;
  toolCount: number;
  detailLabel: string;
  editStats?: {
    fileCount: number;
    additions: number;
    deletions: number;
  };
  durationMs: number | null;
  startedAt: string | null;
  failedCount: number;
  status: 'running' | 'failed' | 'complete';
}

export interface TimelineItemRow {
  kind: 'item';
  item: TimelineItem;
}

export interface ToolGroupRow {
  kind: 'tool_group';
  id: string;
  items: ToolCallTimelineItem[];
  summary: ToolGroupSummary;
  defaultExpanded: boolean;
}

export interface ThinkingGroupSummary {
  label: string;
  icon: ActivityGroupIcon;
  accentClass: string;
  thoughtCount: number;
  detailLabel: string;
  durationMs: number | null;
  status: 'running' | 'failed' | 'complete';
}

export interface ThinkingGroupRow {
  kind: 'thinking_group';
  id: string;
  items: ThinkingTimelineItem[];
  summary: ThinkingGroupSummary;
  defaultExpanded: boolean;
}

export type ActivitySegmentEntry =
  | { kind: 'tool_group'; group: ToolGroupRow }
  | { kind: 'assistant'; item: MessageTimelineItem };

export interface ActivitySegmentRow {
  kind: 'activity_segment';
  id: string;
  entries: ActivitySegmentEntry[];
  toolGroups: ToolGroupRow[];
  summary: ToolGroupSummary;
  assistantPreview: string | null;
  defaultExpanded: boolean;
}

export type TimelineRow = TimelineItemRow | ToolGroupRow | ThinkingGroupRow | ActivitySegmentRow;

type FileActivityRole = 'none' | 'file_read' | 'file_edit';
type ActivityRunKey = ToolGroupKind | 'file_work';
type CycleStateLookup = Map<string, TurnCycleState>;

const INTERNAL_TOOL_NAMES = new Set([
  'search_tools',
  'skill_load',
  'skill_asset_materialize',
  'todo_write',
  'step_todo_write',
  'read_tool_output',
  'search_tool_output',
  'list_tool_output_anchors',
  'read_tool_output_anchor'
]);

const UNGROUPED_TOOL_NAMES = new Set([
  'request_auth_challenge',
  'request_credential',
  'request_user_input',
  'step_request_questions'
]);

const EXPLORATION_TOOLS = new Set([
  'read',
  'list_directory',
  'grep',
  'glob',
  'lsp',
  'search_tools',
  'artifact_read',
  'artifact_get_metadata',
  'artifact_get_url',
  'artifact_list_recent',
  'artifact_search',
  'read_tool_output',
  'search_tool_output',
  'list_tool_output_anchors',
  'read_tool_output_anchor',
  'read_task_deliverable',
  'get_project',
  'list_projects',
  'get_subsession',
  'list_subsessions',
  'get_task',
  'list_tasks',
  'get_task_output',
  'get_task_step_output',
  'get_task_step_logs',
  'list_task_step_runs',
  'get_workflow',
  'list_workflows',
  'list_conversations',
  'read_conversation_messages',
  'search_conversations',
  'summarize_conversation',
  'agent_conversation_get',
  'agent_conversation_list',
  'agent_conversation_wait',
  'list_credentials',
  'skill_export',
  'skill_get',
  'skill_list',
  'skill_load',
  'skill_versions',
  'office_read',
  'office_get',
  'office_query',
  'office_validate',
  'office_render'
]);

const FILE_READ_TOOLS = new Set([
  'read',
  'list_directory',
  'grep',
  'glob',
  'lsp'
]);

const WEB_TOOLS = new Set([
  'web_search',
  'web_fetch',
  'web_crawl',
  'web_map',
  'web_research'
]);

const EDIT_TOOLS = new Set([
  'apply_patch',
  'write',
  'edit',
  'multiedit',
  'office_create',
  'office_patch'
]);

const DELEGATION_TOOLS = new Set([
  'delegate',
  'cancel_subsession',
  'create_task',
  'update_task',
  'cancel_task',
  'retry_task',
  'respond_task_input',
  'resolve_task_pause',
  'compose_and_run_workflow',
  'agent_conversation_create',
  'agent_conversation_send',
  'agent_conversation_fork',
  'agent_conversation_retry',
  'agent_conversation_interrupt',
  'agent_conversation_close'
]);

function normalizedToolName(item: ToolCallTimelineItem): string {
  return item.tool_name.trim();
}

export function isInternalToolCall(item: ToolCallTimelineItem): boolean {
  return INTERNAL_TOOL_NAMES.has(normalizedToolName(item));
}

export function isUngroupedToolCall(item: ToolCallTimelineItem): boolean {
  return UNGROUPED_TOOL_NAMES.has(normalizedToolName(item));
}

function classifyTool(item: ToolCallTimelineItem): ToolGroupKind {
  const name = normalizedToolName(item);
  const fileRole = fileActivityRole(item);
  if (name === 'bash') return 'command';
  if (fileRole === 'file_edit') return 'edit';
  if (WEB_TOOLS.has(name)) return 'web';
  if (name.startsWith('browser_')) return 'browser';
  if (name.startsWith('memory_')) return 'memory';
  if (name.startsWith('knowledgebase_')) return 'knowledgebase';
  if (EXPLORATION_TOOLS.has(name)) return 'explore';
  if (DELEGATION_TOOLS.has(name) || name.startsWith('agent_conversation_')) return 'delegate';
  return 'mixed';
}

function fileActivityRole(item: ToolCallTimelineItem): FileActivityRole {
  const name = normalizedToolName(item);
  if (EDIT_TOOLS.has(name) || (item.file_diffs?.length ?? 0) > 0) return 'file_edit';
  if (FILE_READ_TOOLS.has(name)) return 'file_read';
  return 'none';
}

function groupFileActivityRole(group: ToolGroupRow): FileActivityRole {
  let hasRead = false;
  let hasEdit = false;
  for (const item of group.items) {
    const role = fileActivityRole(item);
    if (role === 'none') return 'none';
    if (role === 'file_edit') hasEdit = true;
    if (role === 'file_read') hasRead = true;
  }
  if (hasEdit) return 'file_edit';
  return hasRead ? 'file_read' : 'none';
}

function groupRunKey(group: ToolGroupRow): ActivityRunKey {
  return groupFileActivityRole(group) === 'none' ? group.summary.kind : 'file_work';
}

function defaultDetailLabel(toolCount: number): string {
  return `${toolCount} ${toolCount === 1 ? 'tool' : 'tools'}`;
}

function countDiffLines(diff: string): { additions: number; deletions: number } {
  let additions = 0;
  let deletions = 0;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+++') || line.startsWith('---')) continue;
    if (line.startsWith('+')) additions += 1;
    if (line.startsWith('-')) deletions += 1;
  }
  return { additions, deletions };
}

function summarizeEditStats(items: ToolCallTimelineItem[]): ToolGroupSummary['editStats'] | null {
  const paths = new Set<string>();
  let additions = 0;
  let deletions = 0;

  for (const item of items) {
    for (const fileDiff of item.file_diffs ?? []) {
      if (fileDiff.path) paths.add(fileDiff.path);
      const diffStats = countDiffLines(fileDiff.diff ?? '');
      additions += diffStats.additions;
      deletions += diffStats.deletions;
    }
  }

  if (paths.size === 0) {
    return null;
  }

  return {
    fileCount: paths.size,
    additions,
    deletions
  };
}

function formatEditDetail(stats: ToolGroupSummary['editStats'] | null, fallbackToolCount: number): string {
  if (!stats) {
    return defaultDetailLabel(fallbackToolCount);
  }
  const fileLabel = `${stats.fileCount} ${stats.fileCount === 1 ? 'file' : 'files'}`;
  if (stats.additions === 0 && stats.deletions === 0) {
    return fileLabel;
  }
  return `${fileLabel} (+${stats.additions}/-${stats.deletions})`;
}

function appendFailureDetail(label: string, failedCount: number): string {
  if (failedCount <= 0) return label;
  return `${label} (${failedCount} failed)`;
}

function shouldShowFailureDetail(kind: ToolGroupKind): boolean {
  return kind === 'command' || kind === 'mixed';
}

function earliestTimestamp(items: ToolCallTimelineItem[]): string | null {
  let earliest: { value: string; time: number } | null = null;
  for (const item of items) {
    if (!item.created_at) continue;
    const time = new Date(item.created_at).getTime();
    if (Number.isNaN(time)) continue;
    if (!earliest || time < earliest.time) {
      earliest = { value: item.created_at, time };
    }
  }
  return earliest?.value ?? null;
}

function summarizeToolGroup(items: ToolCallTimelineItem[]): ToolGroupSummary {
  const fileRoles = items.map(fileActivityRole);
  const hasFileEdit = fileRoles.includes('file_edit');
  const hasOnlyFileReads = fileRoles.length > 0 && fileRoles.every((role) => role === 'file_read');
  const kind = hasFileEdit
    ? 'edit'
    : hasOnlyFileReads
      ? 'explore'
      : classifyTool(items[0]);
  const durationMs = items.some((item) => typeof item.duration_ms === 'number')
    ? items.reduce((total, item) => total + (item.duration_ms ?? 0), 0)
    : null;
  const failedCount = items.filter((item) => item.is_error || item.status === 'failed').length;
  const failed = failedCount > 0;
  const running = !failed && items.some((item) => item.status === 'pending' || item.status === 'running' || item.status === 'waiting');
  const editStats = kind === 'edit' ? summarizeEditStats(items) : null;
  const detailLabel = kind === 'edit'
    ? formatEditDetail(editStats, items.length)
    : defaultDetailLabel(items.length);
  const common = {
    kind,
    toolCount: items.length,
    detailLabel: shouldShowFailureDetail(kind)
      ? appendFailureDetail(detailLabel, failedCount)
      : detailLabel,
    editStats: editStats ?? undefined,
    durationMs,
    startedAt: earliestTimestamp(items),
    failedCount,
    status: failed ? 'failed' as const : running ? 'running' as const : 'complete' as const
  };
  switch (kind) {
    case 'explore':
      return { ...common, label: 'Exploring…', icon: 'search', accentClass: 'border-sky-400/30 text-sky-200' };
    case 'command':
      return { ...common, label: 'Running commands…', icon: 'terminal', accentClass: 'border-violet-400/30 text-violet-200' };
    case 'edit':
      return { ...common, label: 'Editing files…', icon: 'edit', accentClass: 'border-emerald-400/30 text-emerald-200' };
    case 'delegate':
      return { ...common, label: 'Delegating work…', icon: 'delegate', accentClass: 'border-amber-400/30 text-amber-200' };
    case 'web':
      return { ...common, label: 'Searching web…', icon: 'globe', accentClass: 'border-cyan-400/30 text-cyan-200' };
    case 'browser':
      return { ...common, label: 'Using browser…', icon: 'browser', accentClass: 'border-indigo-400/30 text-indigo-200' };
    case 'memory':
      return { ...common, label: 'Accessing memory…', icon: 'brain', accentClass: 'border-fuchsia-400/30 text-fuchsia-200' };
    case 'knowledgebase':
      return { ...common, label: 'Querying knowledgebase…', icon: 'database', accentClass: 'border-teal-400/30 text-teal-200' };
    default:
      return { ...common, label: 'Using tools…', icon: 'wrench', accentClass: 'border-slate-500/40 text-slate-200' };
  }
}

function summarizeThinkingGroup(items: ThinkingTimelineItem[]): ThinkingGroupSummary {
  const blocks = items.flatMap((item) => item.blocks ?? []);
  const durationMs = blocks.some((block) => typeof block.duration_ms === 'number')
    ? blocks.reduce((total, block) => total + (block.duration_ms ?? 0), 0)
    : null;
  const failed = items.some((item) => item.status === 'failed')
    || blocks.some((block) => block.status === 'failed');
  const running = !failed && (
    items.some((item) => item.status === 'pending' || item.status === 'running' || item.status === 'waiting')
    || blocks.some((block) => block.status === 'running')
  );
  const thoughtCount = blocks.length || items.length;
  return {
    label: 'Thinking…',
    icon: 'brain',
    accentClass: 'border-cyan-400/30 text-cyan-300/80',
    thoughtCount,
    detailLabel: `${thoughtCount} ${thoughtCount === 1 ? 'thought' : 'thoughts'}`,
    durationMs,
    status: failed ? 'failed' : running ? 'running' : 'complete'
  };
}

function canJoinToolGroup(previous: ToolCallTimelineItem, next: ToolCallTimelineItem): boolean {
  const previousKind = classifyTool(previous);
  const nextKind = classifyTool(next);
  const previousFileRole = fileActivityRole(previous);
  const nextFileRole = fileActivityRole(next);
  const compatibleKind = previousKind === nextKind
    || (previousFileRole !== 'none' && nextFileRole !== 'none');
  const previousHasCycle = typeof previous.turn_cycle_index === 'number';
  const nextHasCycle = typeof next.turn_cycle_index === 'number';
  const compatibleCycle = previousHasCycle && nextHasCycle
    ? previous.turn_cycle_index === next.turn_cycle_index
    : !previousHasCycle && !nextHasCycle;
  // assistant_phase_index is deliberately NOT a join key: phases are assigned
  // PER TOOL CALL (each call bumps the turn's phase counter), so two adjacent
  // tools in one cycle carry different phases by design — the phase orders
  // items, turn_cycle_index groups them. Joining on live-mutable predicates
  // (isLiveToolCall) made grouping differ between streaming and reload: one
  // merged group live, split groups after refresh. The join keys below are
  // identical for the live overlay item and its canonical confirmation.
  return previous.turn_id === next.turn_id
    && compatibleCycle
    && compatibleKind;
}

function canJoinThinkingGroup(previous: ThinkingTimelineItem, next: ThinkingTimelineItem): boolean {
  return previous.turn_id === next.turn_id
    && previous.assistant_phase_index === next.assistant_phase_index;
}

// Group row ids must stay STABLE across the live -> settled transition.
// They are the keyed-each render keys: an id change remounts the whole group
// block, which resets its expand/collapse state and changes its height —
// shifting all content below it and jumping the user's scroll position.
// assistant_phase_index is deliberately NOT part of the id: it is typically
// null while the turn streams and becomes a concrete number when the turn
// settles, which would change every live group's id exactly at turn end.
// The first member's item id alone is unique (an item belongs to exactly one
// group); turn_id is kept for debuggability.
function toolGroupId(items: ToolCallTimelineItem[]): string {
  const first = items[0];
  return [
    'tool-group',
    first?.turn_id ?? 'no-turn',
    first?.id ?? 'unknown'
  ].join(':');
}

function thinkingGroupId(items: ThinkingTimelineItem[]): string {
  const first = items[0];
  return [
    'thinking-group',
    first?.turn_id ?? 'no-turn',
    first?.id ?? 'unknown'
  ].join(':');
}

function activitySegmentId(entries: ActivitySegmentEntry[]): string {
  const firstEntry = entries[0];
  const firstGroup = firstEntry?.kind === 'tool_group'
    ? firstEntry.group
    : entries.find((entry) => entry.kind === 'tool_group')?.group;
  const firstAssistant = firstEntry?.kind === 'assistant'
    ? firstEntry.item
    : entries.find((entry) => entry.kind === 'assistant')?.item;
  return [
    'activity-segment',
    firstAssistant?.turn_id ?? firstGroup?.items[0]?.turn_id ?? 'no-turn',
    firstAssistant?.id ?? firstGroup?.items[0]?.id ?? 'unknown',
    firstGroup?.id ?? 'no-tools'
  ].join(':');
}

function canStartActivitySegment(row: TimelineRow): row is TimelineItemRow & { item: MessageTimelineItem } {
  return row.kind === 'item'
    && row.item.kind === 'message'
    && row.item.role === 'assistant'
    && !!row.item.turn_id;
}

function cycleStateKey(turnId: string, turnCycleIndex: number): string {
  return `${turnId}:${turnCycleIndex}`;
}

function buildCycleStateLookup(cycleStates: readonly TurnCycleState[]): CycleStateLookup {
  const lookup = new Map<string, TurnCycleState>();
  for (const state of cycleStates) {
    lookup.set(cycleStateKey(state.turn_id, state.turn_cycle_index), state);
  }
  return lookup;
}

function itemCycleState(
  cycleStates: CycleStateLookup,
  item: MessageTimelineItem | ToolCallTimelineItem
): TurnCycleState | undefined {
  if (!item.turn_id || typeof item.turn_cycle_index !== 'number') {
    return undefined;
  }
  return cycleStates.get(cycleStateKey(item.turn_id, item.turn_cycle_index));
}

function activityRunHasBackendToolCycle(
  entries: ActivitySegmentEntry[],
  cycleStates: CycleStateLookup
): boolean {
  if (cycleStates.size === 0) {
    return false;
  }
  return entries.some((entry) => {
    if (entry.kind === 'assistant') {
      return itemCycleState(cycleStates, entry.item)?.has_tool_activity === true;
    }
    return entry.group.items.some((item) => itemCycleState(cycleStates, item)?.has_tool_activity === true);
  });
}

function toolGroupMatchesAssistantCycle(group: ToolGroupRow, assistant: MessageTimelineItem): boolean {
  if (group.items.length === 0) {
    return false;
  }

  if (!group.items.every((item) => item.turn_id === assistant.turn_id)) {
    return false;
  }

  if (typeof assistant.turn_cycle_index !== 'number') {
    return false;
  }

  return group.items.every((item) => item.turn_cycle_index === assistant.turn_cycle_index);
}

function toolGroupCycleIndex(group: ToolGroupRow): number | null | undefined {
  let cycleIndex: number | null | undefined;
  for (const item of group.items) {
    const itemCycleIndex = typeof item.turn_cycle_index === 'number'
      ? item.turn_cycle_index
      : null;
    if (cycleIndex === undefined) {
      cycleIndex = itemCycleIndex;
      continue;
    }
    if (cycleIndex !== itemCycleIndex) {
      return undefined;
    }
  }
  return cycleIndex;
}

function inferMissingAssistantCycle(group: ToolGroupRow, assistant: MessageTimelineItem): number | null | undefined {
  if (typeof assistant.turn_cycle_index === 'number') {
    return undefined;
  }
  if (group.items.length === 0) {
    return undefined;
  }
  if (!group.items.every((item) => item.turn_id === assistant.turn_id)) {
    return undefined;
  }
  return toolGroupCycleIndex(group);
}

function toolGroupMatchesAssistantCycleOrInferred(
  group: ToolGroupRow,
  assistant: MessageTimelineItem,
  inferredCycleIndex: number | null | undefined
): boolean {
  if (toolGroupMatchesAssistantCycle(group, assistant)) {
    return true;
  }
  if (inferredCycleIndex === undefined || typeof assistant.turn_cycle_index === 'number') {
    return false;
  }
  if (!group.items.every((item) => item.turn_id === assistant.turn_id)) {
    return false;
  }
  return toolGroupCycleIndex(group) === inferredCycleIndex;
}

function segmentToolGroups(entries: ActivitySegmentEntry[]): ToolGroupRow[] {
  return entries
    .filter((entry): entry is { kind: 'tool_group'; group: ToolGroupRow } => entry.kind === 'tool_group')
    .map((entry) => entry.group);
}

function groupTurnId(group: ToolGroupRow): string | null | undefined {
  return group.items[0]?.turn_id;
}

function groupMatchesRunTurn(group: ToolGroupRow, turnId: string | null | undefined): boolean {
  return group.items.every((item) => item.turn_id === turnId);
}

function summarizeActivitySegment(toolGroups: ToolGroupRow[]): ToolGroupSummary {
  return summarizeToolGroup(toolGroups.flatMap((group) => group.items));
}

function assistantPreviewText(entry: ActivitySegmentEntry): string | null {
  if (entry.kind !== 'assistant') return null;
  const content = entry.item.content.trim();
  return content || null;
}

function activitySegmentAssistantPreview(entries: ActivitySegmentEntry[], status: ToolGroupSummary['status']): string | null {
  if (status === 'running') {
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const preview = assistantPreviewText(entries[index]);
      if (preview) return preview;
    }
    return null;
  }
  for (const entry of entries) {
    const preview = assistantPreviewText(entry);
    if (preview) return preview;
  }
  return null;
}

function activityRunHasAssistant(entries: ActivitySegmentEntry[]): boolean {
  return entries.some((entry) => entry.kind === 'assistant');
}

function activityRunHasActiveAssistant(entries: ActivitySegmentEntry[]): boolean {
  return entries.some((entry) =>
    entry.kind === 'assistant'
    && (entry.item.partial || entry.item.status === 'running')
  );
}

function activityRunShouldRender(entries: ActivitySegmentEntry[], runKey: ActivityRunKey): boolean {
  if (activityRunHasAssistant(entries)) return true;
  if (runKey !== 'file_work' && runKey !== 'command') return false;
  return segmentToolGroups(entries).length > 1;
}

function activityRunHasMatchingToolGroup(entries: ActivitySegmentEntry[], assistant: MessageTimelineItem): boolean {
  return entries.some((entry) =>
    entry.kind === 'tool_group'
    && toolGroupMatchesAssistantCycle(entry.group, assistant)
  );
}

function appendMatchingToolGroupsForAssistant(
  rows: TimelineRow[],
  index: number,
  runKey: ActivityRunKey,
  assistant: MessageTimelineItem,
  inferredCycleIndex: number | null | undefined,
  entries: ActivitySegmentEntry[]
): number {
  let nextIndex = index;
  while (nextIndex < rows.length) {
    const candidate = rows[nextIndex];
    if (
      candidate.kind !== 'tool_group'
      || groupRunKey(candidate) !== runKey
      || !toolGroupMatchesAssistantCycleOrInferred(candidate, assistant, inferredCycleIndex)
    ) {
      break;
    }
    entries.push({ kind: 'tool_group', group: candidate });
    nextIndex += 1;
  }
  return nextIndex;
}

function collectActivityRun(
  rows: TimelineRow[],
  index: number,
  cycleStates: CycleStateLookup
): { entries: ActivitySegmentEntry[]; runKey: ActivityRunKey; nextIndex: number } | null {
  const row = rows[index];
  const entries: ActivitySegmentEntry[] = [];
  let runKey: ActivityRunKey;
  let runTurnId: string | null | undefined;
  let nextIndex: number;

  if (row.kind === 'tool_group') {
    runKey = groupRunKey(row);
    runTurnId = groupTurnId(row);
    entries.push({ kind: 'tool_group', group: row });
    nextIndex = index + 1;
  } else if (canStartActivitySegment(row)) {
    const nextRow = rows[index + 1];
    if (
      !nextRow
      || nextRow.kind !== 'tool_group'
    ) {
      return null;
    }
    const inferredCycleIndex = inferMissingAssistantCycle(nextRow, row.item);
    if (!toolGroupMatchesAssistantCycleOrInferred(nextRow, row.item, inferredCycleIndex)) {
      return null;
    }
    runKey = groupRunKey(nextRow);
    runTurnId = row.item.turn_id;
    entries.push({ kind: 'assistant', item: row.item });
    nextIndex = appendMatchingToolGroupsForAssistant(rows, index + 1, runKey, row.item, inferredCycleIndex, entries);
  } else {
    return null;
  }

  while (nextIndex < rows.length) {
    const candidate = rows[nextIndex];
    if (candidate.kind === 'tool_group') {
      if (
        groupRunKey(candidate) !== runKey
        || !groupMatchesRunTurn(candidate, runTurnId)
      ) {
        break;
      }
      entries.push({ kind: 'tool_group', group: candidate });
      nextIndex += 1;
      continue;
    }

    if (!canStartActivitySegment(candidate)) {
      break;
    }
    if (candidate.item.turn_id !== runTurnId) {
      break;
    }

    const nextToolGroup = rows[nextIndex + 1];
    const inferredCycleIndex = nextToolGroup?.kind === 'tool_group'
      ? inferMissingAssistantCycle(nextToolGroup, candidate.item)
      : undefined;
    if (
      nextToolGroup
      && nextToolGroup.kind === 'tool_group'
      && groupRunKey(nextToolGroup) === runKey
      && toolGroupMatchesAssistantCycleOrInferred(nextToolGroup, candidate.item, inferredCycleIndex)
    ) {
      entries.push({ kind: 'assistant', item: candidate.item });
      nextIndex = appendMatchingToolGroupsForAssistant(rows, nextIndex + 1, runKey, candidate.item, inferredCycleIndex, entries);
      continue;
    }

    if (activityRunHasMatchingToolGroup(entries, candidate.item)) {
      entries.push({ kind: 'assistant', item: candidate.item });
      nextIndex += 1;
      continue;
    }

    break;
  }

  if (!activityRunShouldRender(entries, runKey)) {
    return null;
  }

  return { entries, runKey, nextIndex };
}

function visibleItem(item: TimelineItem, preferences: UserPreferences): boolean {
  if (item.kind === 'thinking') {
    return preferences.chat.show_thinking_blocks;
  }
  if (item.kind === 'tool_call' && !preferences.chat.show_internal_tool_calls) {
    if (isInternalToolCall(item) && (item.is_error || item.status === 'failed')) {
      return true;
    }
    return !isInternalToolCall(item);
  }
  return true;
}

function dedupeVisibleToolCalls(items: TimelineItem[]): TimelineItem[] {
  const toolIndexesById = new Map<string, number>();
  const result: TimelineItem[] = [];
  // Child sessions already represented by a delegate/fork tool card (the
  // delegation payload folded onto the tool). A standalone delegation card
  // for the same child — delivered by an earlier sync window before the
  // fold could happen — is a duplicate representation and must be hidden.
  const foldedChildSessionIds = new Set<string>();
  for (const item of items) {
    if (item.kind === 'tool_call' && item.delegation) {
      const child = item.delegation.child_session_id;
      if (typeof child === 'string' && child) {
        foldedChildSessionIds.add(child);
      }
    }
  }

  for (const item of items) {
    if (item.kind === 'delegation' && foldedChildSessionIds.has(item.child_session_id)) {
      continue;
    }
    if (item.kind !== 'tool_call') {
      result.push(item);
      continue;
    }
    const dedupeKey = item.call_id || item.id;
    const existingIndex = toolIndexesById.get(dedupeKey);
    if (existingIndex !== undefined) {
      result[existingIndex] = item;
      continue;
    }
    toolIndexesById.set(dedupeKey, result.length);
    result.push(item);
  }

  return result;
}

export function prepareTimelineRows(
  items: TimelineItem[],
  preferences: UserPreferences,
  cycleStates: readonly TurnCycleState[] = []
): TimelineRow[] {
  const visibleItems = dedupeVisibleToolCalls(items.filter((item) => visibleItem(item, preferences)));
  if (!preferences.chat.group_tool_calls) {
    return visibleItems.map((item) => ({ kind: 'item', item }));
  }

  const rows: TimelineRow[] = [];
  let pendingThinking: ThinkingTimelineItem[] = [];
  let pendingTools: ToolCallTimelineItem[] = [];

  function flushPendingTools(): void {
    if (pendingTools.length === 0) return;
    const summary = summarizeToolGroup(pendingTools);
    rows.push({
      kind: 'tool_group',
      id: toolGroupId(pendingTools),
      items: pendingTools,
      summary,
      defaultExpanded: false
    });
    pendingTools = [];
  }

  function appendToolCall(item: ToolCallTimelineItem): void {
    const previous = pendingTools[pendingTools.length - 1];
    if (previous && !canJoinToolGroup(previous, item)) {
      flushPendingTools();
    }
    pendingTools.push(item);
  }

  function flushPendingThinking(): void {
    if (pendingThinking.length === 0) return;
    const summary = summarizeThinkingGroup(pendingThinking);
    const shouldRenderGroup = pendingThinking.length > 1 || summary.thoughtCount > 1;
    if (!shouldRenderGroup) {
      rows.push({ kind: 'item', item: pendingThinking[0] });
    } else {
      rows.push({
        kind: 'thinking_group',
        id: thinkingGroupId(pendingThinking),
        items: pendingThinking,
        summary,
        defaultExpanded: summary.status === 'failed'
      });
    }
    pendingThinking = [];
  }

  for (const item of visibleItems) {
    if (item.kind === 'thinking') {
      flushPendingTools();
      const previous = pendingThinking[pendingThinking.length - 1];
      if (previous && !canJoinThinkingGroup(previous, item)) {
        flushPendingThinking();
      }
      pendingThinking.push(item);
      continue;
    }

    if (item.kind !== 'tool_call') {
      flushPendingTools();
      flushPendingThinking();
      rows.push({ kind: 'item', item });
      continue;
    }
    flushPendingThinking();
    if (isUngroupedToolCall(item)) {
      flushPendingTools();
      rows.push({ kind: 'item', item });
      continue;
    }
    appendToolCall(item);
  }
  flushPendingTools();
  flushPendingThinking();
  return foldActivitySegments(rows, buildCycleStateLookup(cycleStates));
}

function foldActivitySegments(rows: TimelineRow[], cycleStates: CycleStateLookup): TimelineRow[] {
  const folded: TimelineRow[] = [];
  let index = 0;

  while (index < rows.length) {
    const run = collectActivityRun(rows, index, cycleStates);
    if (!run) {
      folded.push(rows[index]);
      index += 1;
      continue;
    }

    const toolGroups = segmentToolGroups(run.entries);
    const summary = summarizeActivitySegment(toolGroups);

    folded.push({
      kind: 'activity_segment',
      id: activitySegmentId(run.entries),
      entries: run.entries,
      toolGroups,
      summary,
      assistantPreview: activitySegmentAssistantPreview(run.entries, summary.status),
      defaultExpanded: toolGroups.some((group) => group.defaultExpanded)
        || (
          activityRunHasActiveAssistant(run.entries)
          && !activityRunHasBackendToolCycle(run.entries, cycleStates)
        )
    });
    index = run.nextIndex;
  }

  return folded;
}
