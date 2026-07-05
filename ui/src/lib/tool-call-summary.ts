export interface ToolCallSummaryInput {
  toolName: string;
  status?: string;
  arguments?: Record<string, unknown>;
  result?: string | null;
  isError?: boolean;
}

interface TodoStatusCounts {
  activeCount: number;
  pendingCount: number;
  completedCount: number;
  cancelledCount: number;
}

export interface StructuredToolEntry {
  key: string;
  value: unknown;
}

export interface StepTodoItemPresentation {
  content: string;
  status: string;
}

function normalizedToolName(toolName: string): string {
  return toolName.toLowerCase().replace(/_/g, '');
}

function cleanToolResult(raw: string | null | undefined): string {
  if (raw == null) return '';
  return raw
    .replace(/^<tool_result[^>]*>\n?/, '')
    .replace(/\n?<\/tool_result>\s*$/, '');
}

function parsedToolResult(raw: string | null | undefined): Record<string, unknown> | null {
  const cleaned = cleanToolResult(raw);
  if (!cleaned) return null;
  try {
    const parsed = JSON.parse(cleaned);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function quotedStringField(raw: string, field: string): string {
  const escaped = field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = raw.match(new RegExp(`"${escaped}"\\s*:\\s*"([^"]+)"`));
  return match?.[1] ?? '';
}

function formatStatusCounts(counts: Array<[string, number]>): string {
  return counts
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${count} ${label}`)
    .join(', ');
}

function todosFrom(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((todo): todo is Record<string, unknown> => (
    todo !== null && typeof todo === 'object' && !Array.isArray(todo)
  ));
}

function stringField(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function objectKeys(value: unknown): string[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
  return Object.keys(value as Record<string, unknown>).filter(Boolean);
}

function objectEntries(value: unknown): StructuredToolEntry[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>)
    .filter(([key]) => Boolean(key))
    .map(([key, entryValue]) => ({ key, value: entryValue }));
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => stringField(item)).filter(Boolean);
}

function normalizedDeliverableFormat(value: unknown): 'markdown' | 'plain' | 'html' {
  return value === 'plain' || value === 'html' ? value : 'markdown';
}

function successfulToolResult(item: ToolCallSummaryInput): boolean {
  return item.status === 'completed' && item.isError !== true;
}

function todoStatusCounts(todos: Array<Record<string, unknown>>): TodoStatusCounts {
  return todos.reduce<TodoStatusCounts>((acc, todo) => {
    const status = String(todo.status ?? '').toLowerCase();
    if (status === 'in_progress' || status === 'active' || status === 'running') {
      acc.activeCount += 1;
    } else if (status === 'pending' || status === 'todo' || status === 'open') {
      acc.pendingCount += 1;
    } else if (status === 'completed' || status === 'complete' || status === 'done') {
      acc.completedCount += 1;
    } else if (status === 'cancelled' || status === 'canceled') {
      acc.cancelledCount += 1;
    }
    return acc;
  }, {
    activeCount: 0,
    pendingCount: 0,
    completedCount: 0,
    cancelledCount: 0,
  });
}

function todoSummary(todos: Array<Record<string, unknown>>): string {
  const counts = todoStatusCounts(todos);
  return formatStatusCounts([
    ['active', counts.activeCount],
    ['pending', counts.pendingCount],
    ['completed', counts.completedCount],
    ['cancelled', counts.cancelledCount],
  ]);
}

function todoItems(todos: Array<Record<string, unknown>>): StepTodoItemPresentation[] {
  return todos
    .map((todo) => ({
      content: stringField(todo.content),
      status: stringField(todo.status) || 'pending',
    }))
    .filter((todo) => todo.content);
}

export interface WriteDeliverablePresentation {
  kind: 'write_deliverable';
  title: string;
  content: string;
  format: 'markdown' | 'plain' | 'html';
  deliverableId: string;
  version: number | null;
  length: number | null;
  outputKeys: string[];
}

export interface StepCompletePresentation {
  kind: 'step_complete';
  summary: string;
  outcomeStatus: string;
  outcomeReason: string;
  claims: string[];
  outputs: StructuredToolEntry[];
  metadata: StructuredToolEntry[];
  outputKeys: string[];
  metadataKeys: string[];
  notificationMode: string;
  notificationReason: string;
}

export interface StepTodoWritePresentation {
  kind: 'step_todo_write';
  status: string;
  count: number;
  todos: StepTodoItemPresentation[];
  statusSummary: string;
  guidance: string;
  unchanged: boolean;
  nonTerminalCount: number | null;
}

export type ToolOutputHelperKind =
  | 'read_tool_output'
  | 'search_tool_output'
  | 'list_tool_output_anchors'
  | 'read_tool_output_anchor';

export interface ToolOutputHelperPresentation {
  kind: 'tool_output_helper';
  helperKind: ToolOutputHelperKind;
  title: string;
  summary: string;
  sourceCallId: string;
  queryEntries: StructuredToolEntry[];
  receivedSummary: string;
  receivedDetails: StructuredToolEntry[];
  continuationHint: string;
}

export type WorkflowToolPresentation = WriteDeliverablePresentation | StepCompletePresentation | StepTodoWritePresentation;

const toolOutputHelperTitles: Record<ToolOutputHelperKind, string> = {
  read_tool_output: 'Read stored tool output',
  search_tool_output: 'Search stored tool output',
  list_tool_output_anchors: 'List stored output anchors',
  read_tool_output_anchor: 'Read stored output anchor',
};

const normalizedToolOutputHelpers: Record<string, ToolOutputHelperKind> = {
  readtooloutput: 'read_tool_output',
  searchtooloutput: 'search_tool_output',
  listtooloutputanchors: 'list_tool_output_anchors',
  readtooloutputanchor: 'read_tool_output_anchor',
};

export function delegationToolCallDisplayTitle(argumentsValue: Record<string, unknown> | null | undefined): string {
  if (!argumentsValue) return '';
  return stringField(argumentsValue.title)
    || stringField(argumentsValue.task_title)
    || stringField(argumentsValue.task);
}

export function workflowToolPresentation(item: ToolCallSummaryInput): WorkflowToolPresentation | null {
  if (!successfulToolResult(item)) return null;

  const name = normalizedToolName(item.toolName);
  const args = item.arguments ?? {};
  const parsed = parsedToolResult(item.result);

  if (name === 'writedeliverable') {
    const content = typeof args.content === 'string' ? args.content : '';
    if (!content.trim()) return null;
    return {
      kind: 'write_deliverable',
      title: stringField(args.title) || 'Deliverable',
      content,
      format: normalizedDeliverableFormat(args.format),
      deliverableId: stringField(parsed?.deliverable_id),
      version: typeof parsed?.version === 'number' ? parsed.version : null,
      length: typeof parsed?.length === 'number' ? parsed.length : content.length,
      outputKeys: objectKeys(args.outputs),
    };
  }

  if (name === 'stepcomplete') {
    const outcome = args.outcome && typeof args.outcome === 'object' && !Array.isArray(args.outcome)
      ? args.outcome as Record<string, unknown>
      : {};
    const notification = args.notification && typeof args.notification === 'object' && !Array.isArray(args.notification)
      ? args.notification as Record<string, unknown>
      : {};
    const outputs = objectEntries(args.outputs);
    const metadata = objectEntries(args.metadata);
    return {
      kind: 'step_complete',
      summary: stringField(args.summary) || 'Step completed.',
      outcomeStatus: stringField(outcome.status) || 'success',
      outcomeReason: stringField(outcome.reason),
      claims: stringArray(args.claims),
      outputs,
      metadata,
      outputKeys: outputs.map((entry) => entry.key),
      metadataKeys: metadata.map((entry) => entry.key),
      notificationMode: stringField(notification.mode),
      notificationReason: stringField(notification.reason),
    };
  }

  if (name === 'steptodowrite') {
    const resultTodos = todosFrom(parsed?.todos);
    const todos = resultTodos.length > 0 ? resultTodos : todosFrom(args.todos);
    if (todos.length === 0) return null;
    const explicitSummary = stringField(parsed?.status_summary);
    const guidance = stringField(parsed?.guidance);
    return {
      kind: 'step_todo_write',
      status: stringField(parsed?.status) || 'updated',
      count: typeof parsed?.count === 'number' ? parsed.count : todos.length,
      todos: todoItems(todos),
      statusSummary: explicitSummary || todoSummary(todos),
      guidance,
      unchanged: parsed?.unchanged === true,
      nonTerminalCount: typeof parsed?.non_terminal_count === 'number' ? parsed.non_terminal_count : null,
    };
  }

  return null;
}

function numberField(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function addQueryEntry(entries: StructuredToolEntry[], key: string, value: unknown): void {
  if (value == null || value === '') return;
  entries.push({ key, value });
}

function toolOutputHelperSummary(kind: ToolOutputHelperKind, args: Record<string, unknown>): string {
  const sourceCallId = stringField(args.call_id);
  if (kind === 'search_tool_output') {
    const pattern = stringField(args.pattern);
    return pattern ? `Search ${sourceCallId} for “${pattern}”` : `Search ${sourceCallId}`;
  }
  if (kind === 'read_tool_output_anchor') {
    const anchor = stringField(args.anchor);
    return anchor ? `Read anchor ${anchor} from ${sourceCallId}` : `Read an anchor from ${sourceCallId}`;
  }
  if (kind === 'list_tool_output_anchors') {
    return `List anchors for ${sourceCallId}`;
  }
  const offset = numberField(args.offset);
  const limit = numberField(args.limit);
  if (offset !== null || limit !== null) {
    const parts = [
      offset !== null ? `from line ${offset}` : '',
      limit !== null ? `${limit} lines` : '',
    ].filter(Boolean);
    return `Read ${sourceCallId} ${parts.join(', ')}`.trim();
  }
  return `Read ${sourceCallId}`;
}

function toolOutputQueryEntries(kind: ToolOutputHelperKind, args: Record<string, unknown>): StructuredToolEntry[] {
  const entries: StructuredToolEntry[] = [];
  addQueryEntry(entries, 'source call', stringField(args.call_id));
  if (kind === 'search_tool_output') {
    addQueryEntry(entries, 'pattern', stringField(args.pattern));
    addQueryEntry(entries, 'context lines', numberField(args.context_lines));
  }
  if (kind === 'read_tool_output_anchor') {
    addQueryEntry(entries, 'anchor', stringField(args.anchor));
    addQueryEntry(entries, 'before lines', numberField(args.before_lines));
    addQueryEntry(entries, 'after lines', numberField(args.after_lines));
  }
  if (kind === 'read_tool_output') {
    addQueryEntry(entries, 'offset', numberField(args.offset));
    addQueryEntry(entries, 'limit', numberField(args.limit));
  }
  return entries;
}

function lineCount(value: string): number {
  if (!value) return 0;
  return value.split('\n').length;
}

function firstOutputLine(value: string): string {
  return value.split('\n').map((line) => line.trim()).find(Boolean) ?? '';
}

function receivedSummary(item: ToolCallSummaryInput): {
  summary: string;
  details: StructuredToolEntry[];
  continuationHint: string;
} {
  const cleaned = cleanToolResult(item.result);
  if (item.status === 'started' && !cleaned.trim()) {
    return { summary: 'Waiting for stored tool output.', details: [], continuationHint: '' };
  }
  if (item.isError) {
    const error = firstOutputLine(cleaned);
    return {
      summary: error ? `Tool output query failed: ${error}` : 'Tool output query failed.',
      details: [],
      continuationHint: '',
    };
  }
  if (!cleaned.trim()) {
    return { summary: 'No output was returned.', details: [], continuationHint: '' };
  }

  const details: StructuredToolEntry[] = [];
  const showing = cleaned.match(/Showing lines\s+([\d,]+)[–-]([\d,]+)\s+of\s+([\d,]+)/i);
  const total = cleaned.match(/\(Total:\s*([\d,]+)\s+lines?\)/i);
  const nextOffset = cleaned.match(/Use offset=([\d,]+)/i);
  const count = lineCount(cleaned);

  if (showing) {
    details.push({ key: 'page', value: `lines ${showing[1]}–${showing[2]} of ${showing[3]}` });
  }
  if (total) {
    details.push({ key: 'returned lines', value: total[1] });
  } else {
    details.push({ key: 'received lines', value: count });
  }
  if (nextOffset) {
    details.push({ key: 'next offset', value: nextOffset[1] });
  }

  const summary = showing
    ? `Received lines ${showing[1]}–${showing[2]} of ${showing[3]}.`
    : total
      ? `Received ${total[1]} output lines.`
      : `Received ${count} output line${count === 1 ? '' : 's'}.`;

  return {
    summary,
    details,
    continuationHint: nextOffset ? `Use offset=${nextOffset[1]} to continue.` : '',
  };
}

export function toolOutputHelperPresentation(item: ToolCallSummaryInput): ToolOutputHelperPresentation | null {
  const helperKind = normalizedToolOutputHelpers[normalizedToolName(item.toolName)];
  if (!helperKind) return null;
  const args = item.arguments ?? {};
  const sourceCallId = stringField(args.call_id);
  if (!sourceCallId) return null;
  const received = receivedSummary(item);
  return {
    kind: 'tool_output_helper',
    helperKind,
    title: toolOutputHelperTitles[helperKind],
    summary: toolOutputHelperSummary(helperKind, args),
    sourceCallId,
    queryEntries: toolOutputQueryEntries(helperKind, args),
    receivedSummary: received.summary,
    receivedDetails: received.details,
    continuationHint: received.continuationHint,
  };
}

export function skillLoadDisplayName(item: ToolCallSummaryInput): string {
  if (normalizedToolName(item.toolName) !== 'skillload') return '';

  const parsed = parsedToolResult(item.result);
  const parsedName = parsed?.name;
  if (typeof parsedName === 'string' && parsedName.trim()) return parsedName.trim();

  const rawName = quotedStringField(cleanToolResult(item.result), 'name');
  if (rawName.trim()) return rawName.trim();

  const argSkill = item.arguments?.skill;
  if (typeof argSkill === 'string' && argSkill.trim()) return argSkill.trim();
  const argSkillId = item.arguments?.skill_id;
  if (typeof argSkillId === 'string' && argSkillId.trim()) return argSkillId.trim();
  return '';
}

export function stepTodoWriteStatusSummary(item: ToolCallSummaryInput): string {
  if (normalizedToolName(item.toolName) !== 'steptodowrite') return '';
  const parsed = parsedToolResult(item.result);

  const explicitSummary = parsed?.status_summary;
  if (typeof explicitSummary === 'string' && explicitSummary.trim()) {
    return explicitSummary.trim();
  }

  const resultTodos = todosFrom(parsed?.todos);
  const todos = resultTodos.length > 0 ? resultTodos : todosFrom(item.arguments?.todos);
  if (todos.length === 0) return '';

  return todoSummary(todos);
}
