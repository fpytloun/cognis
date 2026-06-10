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

export type WorkflowToolPresentation = WriteDeliverablePresentation | StepCompletePresentation | StepTodoWritePresentation;

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
