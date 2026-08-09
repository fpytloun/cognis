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
  | 'image'
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
  | 'image'
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
type ActivityRunKey = ToolGroupKind;
type CycleStateLookup = Map<string, TurnCycleState>;
type DiffLineCounts = { additions: number; deletions: number };

const INTERNAL_TOOL_NAMES = new Set([
  'search_tools',
  'describe_tool',
  'validate_tool_call',
  'skill_load',
  'skill_asset_materialize',
  'todo_write',
  'step_todo_write',
  'todo_list',
  'step_todo_list',
  'switch_agent_profile',
  'switch_executor',
  'request_user_input',
  'step_request_questions',
  'request_auth_challenge',
  'request_credential',
  'read_tool_output',
  'search_tool_output',
  'list_tool_output_anchors',
  'read_tool_output_anchor',
  'attach_artifact'
]);

const UNGROUPED_TOOL_NAMES = new Set([
  'request_auth_challenge',
  'request_credential',
  'request_user_input',
  'step_request_questions'
]);

const diffLineCountCache = new Map<string, DiffLineCounts>();
const toolGroupSummaryCache = new Map<string, ToolGroupSummary>();
const activitySegmentSummaryCache = new Map<string, ToolGroupSummary>();
const timestampCache = new WeakMap<ToolCallTimelineItem, { value: string | null | undefined; time: number | null }>();

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

const IMAGE_TOOLS = new Set([
  'image_generate',
  'image_edit'
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
  'retry_subsession',
  'follow_up_subsession',
  'fork_subsession',
  'fork',
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
  'agent_conversation_get',
  'agent_conversation_list',
  'agent_conversation_wait',
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
  if (IMAGE_TOOLS.has(name)) return 'image';
  if (name.startsWith('memory_')) return 'memory';
  if (name.startsWith('knowledgebase_')) return 'knowledgebase';
  if (EXPLORATION_TOOLS.has(name)) return 'explore';
  if (DELEGATION_TOOLS.has(name) || name.startsWith('agent_conversation_')) return 'delegate';
  return 'mixed';
}

function fileActivityRole(item: ToolCallTimelineItem): FileActivityRole {
  // NAME-BASED ONLY. Grouping/classification must depend solely on immutable
  // signals so streaming and reload produce identical groups. file_diffs is
  // live-mutable (it arrives on the tool RESULT, absent while the call is still
  // running) and can appear on non-edit tools such as `bash`; keying file-work
  // classification on it would let a shell command masquerade as a file edit
  // after reload and absorb following reads into an editing run. file_diffs
  // still drives the edit STATS/label in summarizeEditStats, which does not
  // affect identity or run continuation.
  const name = normalizedToolName(item);
  if (EDIT_TOOLS.has(name)) return 'file_edit';
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

// A group is "file work" when EVERY member is a file read or file edit
// (groupFileActivityRole returns 'none' the moment a non-file tool is present,
// e.g. artifact_read / web / bash). File-edit groups have role 'file_edit';
// file-read-only groups have role 'file_read'.
function groupIsFileEdit(group: ToolGroupRow): boolean {
  return groupFileActivityRole(group) === 'file_edit';
}

function groupIsFileRead(group: ToolGroupRow): boolean {
  return groupFileActivityRole(group) === 'file_read';
}

// The run key tracks whether the CURRENT run has entered edit mode. Once a run
// contains a file edit, subsequent file reads fold into it ("reads during
// editing"); before any edit, file reads form their own "Exploring…" run.
type NormalizedRunKey = ActivityRunKey | 'file_work_read' | 'file_work_edit';

function initialRunKey(group: ToolGroupRow): NormalizedRunKey {
  if (groupIsFileEdit(group)) return 'file_work_edit';
  if (groupIsFileRead(group)) return 'file_work_read';
  return group.summary.kind;
}

// Directional continuation: given the current run key and the next candidate
// group, decide whether the group extends the run and what the run key becomes.
// The forward-escalation rule (Option A — never fold backward):
//   file reads  -> file reads : continue as read run
//   file reads  -> file edit  : BREAK (the edit starts a new "Editing…" run;
//                               pre-edit reads stay their own Exploring segment)
//   file edit   -> file edit  : continue as edit run
//   file edit   -> file reads : continue as edit run (reads-during-editing)
//   anything else             : continue only when the plain kind matches
// Returns the run key to adopt if the group continues, or null to break.
function continueRunKey(runKey: NormalizedRunKey, group: ToolGroupRow): NormalizedRunKey | null {
  const isEdit = groupIsFileEdit(group);
  const isRead = groupIsFileRead(group);
  if (runKey === 'file_work_edit') {
    return isEdit || isRead ? 'file_work_edit' : null;
  }
  if (runKey === 'file_work_read') {
    if (isRead) return 'file_work_read';
    // Reads followed by an edit: do NOT extend — the edit begins a fresh run.
    return null;
  }
  return group.summary.kind === runKey ? runKey : null;
}

function defaultDetailLabel(toolCount: number): string {
  return `${toolCount} ${toolCount === 1 ? 'tool' : 'tools'}`;
}

function countDiffLines(diff: string): DiffLineCounts {
  let additions = 0;
  let deletions = 0;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+++') || line.startsWith('---')) continue;
    if (line.startsWith('+')) additions += 1;
    if (line.startsWith('-')) deletions += 1;
  }
  return { additions, deletions };
}

function diffLineCountCacheKey(item: ToolCallTimelineItem): string | null {
  return item.updated_at ? `${item.id}:${item.updated_at}` : null;
}

function countItemDiffLines(item: ToolCallTimelineItem): DiffLineCounts {
  const cacheKey = diffLineCountCacheKey(item);
  if (cacheKey) {
    const cached = diffLineCountCache.get(cacheKey);
    if (cached) return cached;
  }

  let additions = 0;
  let deletions = 0;
  for (const fileDiff of item.file_diffs ?? []) {
    const diffStats = countDiffLines(fileDiff.diff ?? '');
    additions += diffStats.additions;
    deletions += diffStats.deletions;
  }

  const counts = { additions, deletions };
  if (cacheKey) {
    diffLineCountCache.set(cacheKey, counts);
  }
  return counts;
}

function summarizeEditStats(items: ToolCallTimelineItem[]): ToolGroupSummary['editStats'] | null {
  const paths = new Set<string>();
  let additions = 0;
  let deletions = 0;

  for (const item of items) {
    for (const fileDiff of item.file_diffs ?? []) {
      if (fileDiff.path) paths.add(fileDiff.path);
    }
    const diffStats = countItemDiffLines(item);
    additions += diffStats.additions;
    deletions += diffStats.deletions;
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
  return kind === 'command' || kind === 'mixed' || kind === 'web';
}

function earliestTimestamp(items: ToolCallTimelineItem[]): string | null {
  let earliest: { value: string; time: number } | null = null;
  for (const item of items) {
    if (!item.created_at) continue;
    const time = itemTimestampMs(item);
    if (time === null) continue;
    if (!earliest || time < earliest.time) {
      earliest = { value: item.created_at, time };
    }
  }
  return earliest?.value ?? null;
}

function itemTimestampMs(item: ToolCallTimelineItem): number | null {
  const cached = timestampCache.get(item);
  if (cached && cached.value === item.created_at) return cached.time;
  if (!item.created_at) {
    timestampCache.set(item, { value: item.created_at, time: null });
    return null;
  }
  const time = new Date(item.created_at).getTime();
  const normalized = Number.isNaN(time) ? null : time;
  timestampCache.set(item, { value: item.created_at, time: normalized });
  return normalized;
}

function summaryCacheKey(items: ToolCallTimelineItem[]): string | null {
  const parts: string[] = [];
  for (const item of items) {
    if (!item.updated_at) return null;
    parts.push(item.id, item.status ?? '', item.updated_at);
  }
  return parts.join('\u001f');
}

function summarizeToolGroup(items: ToolCallTimelineItem[]): ToolGroupSummary {
  const cacheKey = summaryCacheKey(items);
  if (cacheKey) {
    const cached = toolGroupSummaryCache.get(cacheKey);
    if (cached) return cached;
  }

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
  let summary: ToolGroupSummary;
  switch (kind) {
    case 'explore':
      summary = { ...common, label: 'Exploring…', icon: 'search', accentClass: 'border-sky-400/30 text-sky-200' };
      break;
    case 'command':
      summary = { ...common, label: 'Running commands…', icon: 'terminal', accentClass: 'border-violet-400/30 text-violet-200' };
      break;
    case 'edit':
      summary = { ...common, label: 'Editing files…', icon: 'edit', accentClass: 'border-emerald-400/30 text-emerald-200' };
      break;
    case 'delegate':
      summary = { ...common, label: 'Delegating work…', icon: 'delegate', accentClass: 'border-amber-400/30 text-amber-200' };
      break;
    case 'web':
      summary = { ...common, label: 'Searching web…', icon: 'globe', accentClass: 'border-cyan-400/30 text-cyan-200' };
      break;
    case 'browser':
      summary = { ...common, label: 'Using browser…', icon: 'browser', accentClass: 'border-indigo-400/30 text-indigo-200' };
      break;
    case 'image':
      summary = { ...common, label: 'Generating images…', icon: 'image', accentClass: 'border-pink-400/30 text-pink-200' };
      break;
    case 'memory':
      summary = { ...common, label: 'Accessing memory…', icon: 'brain', accentClass: 'border-fuchsia-400/30 text-fuchsia-200' };
      break;
    case 'knowledgebase':
      summary = { ...common, label: 'Querying knowledgebase…', icon: 'database', accentClass: 'border-teal-400/30 text-teal-200' };
      break;
    default:
      summary = { ...common, label: 'Using tools…', icon: 'wrench', accentClass: 'border-slate-500/40 text-slate-200' };
  }

  if (cacheKey) {
    toolGroupSummaryCache.set(cacheKey, summary);
  }
  return summary;
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
  const compatibleCycle = typeof previous.turn_cycle_index === 'number'
    && previous.turn_cycle_index === next.turn_cycle_index;
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

// The stable within-cycle disambiguator for a segment: the first tool call_id
// in the run, falling back to the first assistant message id. Tool call_ids are
// content identities that never change once emitted — unlike the run's first
// ROW (which flips when an assistant folds at the front) or its classification
// (which flips when a tool result arrives with file_diffs). Deriving the key
// from this instead of a positional ordinal keeps it stable even when an
// earlier same-cycle segment appears or disappears between frames.
function segmentStableDisambiguator(entries: ActivitySegmentEntry[]): string | null {
  for (const entry of entries) {
    if (entry.kind !== 'tool_group') continue;
    for (const item of entry.group.items) {
      if (item.call_id) return `t:${item.call_id}`;
      if (item.id) return `t:${item.id}`;
    }
  }
  for (const entry of entries) {
    if (entry.kind === 'assistant' && entry.item.id) return `a:${entry.item.id}`;
  }
  return null;
}

function activitySegmentId(entries: ActivitySegmentEntry[], runKey: NormalizedRunKey): string {
  const firstItemId = entries.find((entry) => {
    if (entry.kind === 'assistant') return !!entry.item.id;
    return entry.group.items.some((item) => !!item.id);
  });
  const itemId = firstItemId?.kind === 'assistant'
    ? firstItemId.item.id
    : firstItemId?.group.items.find((item) => !!item.id)?.id;
  const firstTurnId = entries.find((entry) => {
    if (entry.kind === 'assistant') return !!entry.item.turn_id;
    return entry.group.items.some((item) => !!item.turn_id);
  });
  const turnId = firstTurnId?.kind === 'assistant'
    ? firstTurnId.item.turn_id
    : firstTurnId?.group.items.find((item) => !!item.turn_id)?.turn_id;
  const firstCycleIndex = entries.find((entry) => {
    if (entry.kind === 'assistant') return typeof entry.item.turn_cycle_index === 'number';
    return entry.group.items.some((item) => typeof item.turn_cycle_index === 'number');
  });
  const cycleIndex = firstCycleIndex?.kind === 'assistant'
    ? firstCycleIndex.item.turn_cycle_index
    : firstCycleIndex?.group.items.find((item) => typeof item.turn_cycle_index === 'number')?.turn_cycle_index;
  // When the segment has a stable (turn, cycle) identity — which every stamped
  // turn now does — key on `{turn}:{cycle}:{stableDisambiguator}`. The
  // disambiguator is the run's first tool call_id (or first assistant id),
  // which is a fixed content identity. This id is immutable across every
  // transition that used to remount the block and reset expansion:
  //   - an assistant message folding at the FRONT of a run (first-row flip),
  //   - a tool result arriving with file_diffs that reclassifies the run
  //     (command/explore -> edit, runKey flip),
  //   - an earlier same-cycle segment appearing/disappearing between frames
  //     (a positional ordinal would shift; a content id does not).
  // It also guarantees uniqueness: two segments cannot contain the same first
  // tool call. The first-item id + runKey are retained only for the legacy
  // `no-cycle` fallback, where no stable grouping key exists.
  if (typeof cycleIndex === 'number' && turnId) {
    const disambiguator = segmentStableDisambiguator(entries) ?? `row:${itemId ?? 'unknown'}`;
    return `activity-segment:${turnId}:${cycleIndex}:${disambiguator}`;
  }
  return [
    'activity-segment',
    turnId ?? 'no-turn',
    'no-cycle',
    itemId ?? 'unknown',
    runKey
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

function assistantIsLive(item: MessageTimelineItem): boolean {
  return item.partial || item.status === 'running' || item.status === 'pending';
}

function assistantCycleHasBackendToolActivity(
  cycleStates: CycleStateLookup,
  item: MessageTimelineItem
): boolean {
  if (!item.turn_id) {
    return false;
  }
  if (typeof item.turn_cycle_index !== 'number') {
    return false;
  }
  return cycleStates.get(cycleStateKey(item.turn_id, item.turn_cycle_index))?.has_tool_activity === true;
}

function canFoldAssistantIntoActivity(
  cycleStates: CycleStateLookup,
  item: MessageTimelineItem
): boolean {
  return !assistantIsLive(item) || assistantCycleHasBackendToolActivity(cycleStates, item);
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

function segmentToolGroups(entries: ActivitySegmentEntry[]): ToolGroupRow[] {
  return entries
    .filter((entry): entry is { kind: 'tool_group'; group: ToolGroupRow } => entry.kind === 'tool_group')
    .map((entry) => entry.group);
}

function groupTurnId(group: ToolGroupRow): string | null | undefined {
  return group.items[0]?.turn_id;
}

function groupMatchesRunTurn(
  group: ToolGroupRow,
  turnId: string | null | undefined
): boolean {
  return group.items.every((item) => item.turn_id === turnId);
}

function mergeAdjacentToolGroupsAcrossCycles(rows: TimelineRow[]): TimelineRow[] {
  const merged: TimelineRow[] = [];

  for (const row of rows) {
    const previous = merged[merged.length - 1];
    if (
      previous?.kind === 'tool_group'
      && row.kind === 'tool_group'
      && groupMatchesRunTurn(row, groupTurnId(previous))
      && continueRunKey(initialRunKey(previous), row) !== null
    ) {
      const items = [...previous.items, ...row.items];
      merged[merged.length - 1] = {
        ...previous,
        items,
        summary: summarizeToolGroup(items),
        defaultExpanded: previous.defaultExpanded || row.defaultExpanded
      };
      continue;
    }
    merged.push(row);
  }

  return merged;
}

function summarizeActivitySegment(toolGroups: ToolGroupRow[]): ToolGroupSummary {
  const items = toolGroups.flatMap((group) => group.items);
  const cacheKey = summaryCacheKey(items);
  if (cacheKey) {
    const cached = activitySegmentSummaryCache.get(cacheKey);
    if (cached) return cached;
  }
  const summary = summarizeToolGroup(items);
  if (cacheKey) {
    activitySegmentSummaryCache.set(cacheKey, summary);
  }
  return summary;
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

function activityRunShouldRender(entries: ActivitySegmentEntry[], runKey: NormalizedRunKey): boolean {
  void runKey;
  if (activityRunHasAssistant(entries)) return true;
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
  runKey: NormalizedRunKey,
  assistant: MessageTimelineItem,
  entries: ActivitySegmentEntry[]
): { nextIndex: number; runKey: NormalizedRunKey } {
  let nextIndex = index;
  let currentRunKey = runKey;
  while (nextIndex < rows.length) {
    const candidate = rows[nextIndex];
    if (candidate.kind !== 'tool_group' || !toolGroupMatchesAssistantCycle(candidate, assistant)) {
      break;
    }
    const advanced = continueRunKey(currentRunKey, candidate);
    if (advanced === null) {
      break;
    }
    entries.push({ kind: 'tool_group', group: candidate });
    currentRunKey = advanced;
    nextIndex += 1;
  }
  return { nextIndex, runKey: currentRunKey };
}

function collectActivityRun(
  rows: TimelineRow[],
  index: number,
  cycleStates: CycleStateLookup
): { entries: ActivitySegmentEntry[]; runKey: NormalizedRunKey; nextIndex: number } | null {
  const row = rows[index];
  const entries: ActivitySegmentEntry[] = [];
  let runKey: NormalizedRunKey;
  let runTurnId: string | null | undefined;
  let nextIndex: number;

  if (row.kind === 'tool_group') {
    runKey = initialRunKey(row);
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
    if (!toolGroupMatchesAssistantCycle(nextRow, row.item)) {
      return null;
    }
    if (!canFoldAssistantIntoActivity(cycleStates, row.item)) {
      return null;
    }
    runKey = initialRunKey(nextRow);
    runTurnId = row.item.turn_id;
    entries.push({ kind: 'assistant', item: row.item });
    const appended = appendMatchingToolGroupsForAssistant(rows, index + 1, runKey, row.item, entries);
    nextIndex = appended.nextIndex;
    runKey = appended.runKey;
  } else {
    return null;
  }

  while (nextIndex < rows.length) {
    const candidate = rows[nextIndex];
    if (candidate.kind === 'tool_group') {
      const advanced = continueRunKey(runKey, candidate);
      if (advanced === null || !groupMatchesRunTurn(candidate, runTurnId)) {
        break;
      }
      entries.push({ kind: 'tool_group', group: candidate });
      runKey = advanced;
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
    if (
      nextToolGroup
      && nextToolGroup.kind === 'tool_group'
      && continueRunKey(runKey, nextToolGroup) !== null
      && candidate.item.turn_id === runTurnId
      && toolGroupMatchesAssistantCycle(nextToolGroup, candidate.item)
    ) {
      if (!canFoldAssistantIntoActivity(cycleStates, candidate.item)) {
        break;
      }
      entries.push({ kind: 'assistant', item: candidate.item });
      const appended = appendMatchingToolGroupsForAssistant(rows, nextIndex + 1, runKey, candidate.item, entries);
      nextIndex = appended.nextIndex;
      runKey = appended.runKey;
      continue;
    }

    if (activityRunHasMatchingToolGroup(entries, candidate.item)) {
      // Trailing fold: an assistant with NO following tool group, matching a
      // tool group already in the run. This is the turn's final answer folding
      // into the activity it produced. A LIVE (partial/running) assistant must
      // NEVER fold here: while streaming it is the visible answer, and folding
      // it would make the just-streamed message vanish into the collapsed
      // segment (only to re-appear when a later frame corrects its cycle). The
      // backend cycle can transiently collide during streaming (phase-vs-cycle
      // skew, completion-frame clobber); the mid-fold branch above already
      // handles a live assistant that has a genuine following same-cycle group.
      // A trailing live assistant stays standalone until it settles.
      if (assistantIsLive(candidate.item)) {
        break;
      }
      if (!canFoldAssistantIntoActivity(cycleStates, candidate.item)) {
        break;
      }
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
  const renderedToolCallIds = new Set(
    visibleItems
      .filter((item): item is ToolCallTimelineItem => item.kind === 'tool_call')
      .map((item) => item.call_id)
  );
  const interactionFilteredItems = preferences.chat.show_internal_tool_calls
    ? visibleItems.filter(
        (item) => item.kind !== 'user_interaction'
          || !item.origin_call_id
          || !renderedToolCallIds.has(item.origin_call_id)
      )
    : visibleItems;
  if (!preferences.chat.group_tool_calls) {
    return interactionFilteredItems.map((item) => ({ kind: 'item', item }));
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

  for (const item of interactionFilteredItems) {
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
  if (preferences.chat.keep_assistant_messages_separate) {
    // Assistant folding is optional, but adjacent cross-cycle tool batching is
    // not. The raw-row pass splits every cycle before this point, even when no
    // visible assistant item separates compatible tool activity.
    return mergeAdjacentToolGroupsAcrossCycles(rows);
  }
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
      id: activitySegmentId(run.entries, run.runKey),
      entries: run.entries,
      toolGroups,
      summary,
      assistantPreview: activitySegmentAssistantPreview(run.entries, summary.status),
      // Decouple folding from collapsing. A segment stays EXPANDED while its
      // assistant text is still live (streaming), so the just-streamed content
      // remains visible through the fold transition instead of being hidden
      // behind the one-line preview the instant the first tool call confirms
      // backend activity. It collapses on its own once the assistant message
      // completes (the next cycle begins or the turn settles) — the tidy-up
      // follows the agent's progress rather than yanking text mid-stream.
      // Failed tool groups still force-expand so errors stay visible.
      defaultExpanded: toolGroups.some((group) => group.defaultExpanded)
        || activityRunHasActiveAssistant(run.entries)
    });
    index = run.nextIndex;
  }

  return folded;
}
