export interface ToolCallSummaryInput {
  toolName: string;
  status?: string;
  arguments?: Record<string, unknown>;
  result?: string | null;
}

interface TodoStatusCounts {
  activeCount: number;
  pendingCount: number;
  completedCount: number;
  cancelledCount: number;
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

  const counts = todos.reduce<TodoStatusCounts>((acc, todo) => {
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

  return formatStatusCounts([
    ['active', counts.activeCount],
    ['pending', counts.pendingCount],
    ['completed', counts.completedCount],
    ['cancelled', counts.cancelledCount],
  ]);
}
