import { isActiveToolStatus } from './timeline-render-model';

export interface ToolCallSummaryInput {
  toolName: string;
  status?: string;
  arguments?: Record<string, unknown>;
  result?: string | null;
  managedConversation?: Record<string, unknown>;
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
  priority: string;
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

function parsedMemoryToolResult(raw: string | null | undefined): Record<string, unknown> | null {
  const cleaned = cleanToolResult(raw);
  if (!cleaned) return null;
  try {
    const parsed = JSON.parse(cleaned);
    if (Array.isArray(parsed)) return { results: parsed };
    return parsed && typeof parsed === 'object'
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

function normalizedDeliverableFormat(value: unknown): 'markdown' | 'plain' | 'html' | 'rich' {
  return value === 'plain' || value === 'html' || value === 'rich' ? value : 'markdown';
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
      priority: stringField(todo.priority) || 'medium',
    }))
    .filter((todo) => todo.content);
}

export interface WriteDeliverablePresentation {
  kind: 'write_deliverable';
  title: string;
  format: 'markdown' | 'plain' | 'html' | 'rich';
  status: string;
  note: string;
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

export interface MemoryResultItemPresentation {
  title: string;
  body: string;
  accent: 'memory' | 'artifact' | 'category' | 'generic';
  meta: StructuredToolEntry[];
}

export interface MemoryToolPresentation {
  kind: 'memory_tool';
  variant: 'default' | 'saved';
  title: string;
  summary: string;
  requestLabel: string;
  requestText: string;
  requestDetails: StructuredToolEntry[];
  badges: string[];
  resultLabel: string;
  resultSummary: string;
  resultDetails: StructuredToolEntry[];
  resultItems: MemoryResultItemPresentation[];
  answer: string;
  text: string;
  error: string;
}

export interface ManagedConversationItemPresentation {
  conversationId: string;
  sessionId: string;
  agentId: string;
  title: string;
  status: string;
  turnState: string;
  conversationState: string;
  summary: string;
  error: string;
  controllerConversationId: string;
  followUpConversationId: string;
}

export interface ManagedConversationToolPresentation {
  kind: 'managed_conversation_tool';
  title: string;
  summary: string;
  requestLabel: string;
  requestText: string;
  requestDetails: StructuredToolEntry[];
  badges: string[];
  conversations: ManagedConversationItemPresentation[];
  primaryConversation: ManagedConversationItemPresentation | null;
  todos: StepTodoItemPresentation[];
  todoSummary: string;
  toolCallCount: number | null;
  lastTool: string;
  resultSummary: string;
  error: string;
  displayStatus: string;
}

export type NativeInspectionKind = 'read' | 'list_directory' | 'grep' | 'glob' | 'lsp';

export interface NativeReadLinePresentation {
  lineNumber: number;
  content: string;
}

export interface NativePathEntryPresentation {
  path: string;
  name: string;
  kind: 'file' | 'directory' | 'unknown';
}

export interface NativeGrepMatchPresentation {
  path: string;
  lineNumber: number;
  text: string;
  isMatch: boolean;
}

export interface NativeGrepGroupPresentation {
  path: string;
  matches: NativeGrepMatchPresentation[];
}

export interface NativeInspectionToolPresentation {
  kind: 'native_inspection_tool';
  nativeKind: NativeInspectionKind;
  title: string;
  summary: string;
  requestLabel: string;
  requestText: string;
  requestDetails: StructuredToolEntry[];
  badges: string[];
  path: string;
  pattern: string;
  outputText: string;
  readLines: NativeReadLinePresentation[];
  pathEntries: NativePathEntryPresentation[];
  grepGroups: NativeGrepGroupPresentation[];
  footer: string;
  error: string;
}

export type WebToolKind = 'fetch' | 'search';

export interface WebResultPresentation {
  title: string;
  url: string;
  domain: string;
  snippet: string;
  score: string;
  publishedDate: string;
  resultType: string;
  recommendation: string;
  freshness: string;
  sourceEngine: string;
}

export interface WebMediaPresentation {
  label: string;
  url: string;
  source: string;
  sourcePageUrl: string;
  artifactRef: string;
}

export interface WebToolPresentation {
  kind: 'web_tool';
  webKind: WebToolKind;
  title: string;
  summary: string;
  requestText: string;
  badges: string[];
  answer: string;
  content: string;
  results: WebResultPresentation[];
  media: WebMediaPresentation[];
  error: string;
  warning: string;
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
    || stringField(argumentsValue.task)
    || stringField(argumentsValue.instruction);
}

function isManagedConversationToolName(name: string): boolean {
  return name.startsWith('agentconversation');
}

function managedConversationToolTitle(name: string): string {
  if (name === 'agentconversationcreate') return 'Start managed conversation';
  if (name === 'agentconversationsend') return 'Send managed instruction';
  if (name === 'agentconversationwait') return 'Wait for managed conversation';
  if (name === 'agentconversationfork') return 'Fork managed conversation';
  if (name === 'agentconversationget') return 'Inspect managed conversation';
  if (name === 'agentconversationlist') return 'List managed conversations';
  if (name === 'agentconversationretry') return 'Retry managed conversation';
  if (name === 'agentconversationinterrupt') return 'Interrupt managed conversation';
  if (name === 'agentconversationclose') return 'Close managed conversation';
  return 'Managed conversation';
}

function managedRequestLabelAndText(name: string, args: Record<string, unknown>): { label: string; text: string } {
  const conversationId = stringField(args.conversation_id);
  const title = stringField(args.title);
  if (name === 'agentconversationcreate') return { label: 'Conversation', text: title };
  if (name === 'agentconversationfork') return { label: 'Fork', text: title || conversationId };
  if (name === 'agentconversationsend') return { label: 'Instruction', text: previewText(args.message, 180) || conversationId };
  if (name === 'agentconversationwait') return { label: 'Waiting for', text: conversationId };
  if (name === 'agentconversationlist') return { label: 'Filter', text: stringField(args.status) || 'all' };
  if (name === 'agentconversationget') return { label: 'Conversation', text: conversationId };
  if (name === 'agentconversationretry') return { label: 'Retry', text: conversationId };
  if (name === 'agentconversationinterrupt') return { label: 'Interrupt', text: conversationId };
  if (name === 'agentconversationclose') return { label: 'Close', text: conversationId };
  return { label: 'Conversation', text: conversationId || title };
}

function managedConversationDetails(args: Record<string, unknown>): StructuredToolEntry[] {
  const entries: StructuredToolEntry[] = [];
  addQueryEntry(entries, 'conversation id', stringField(args.conversation_id));
  addQueryEntry(entries, 'title', stringField(args.title));
  addQueryEntry(entries, 'agent', stringField(args.agent_id));
  addQueryEntry(entries, 'profile', stringField(args.agent_profile_id));
  addQueryEntry(entries, 'mode', stringField(args.chat_mode));
  addQueryEntry(entries, 'status', stringField(args.status));
  addQueryEntry(entries, 'limit', numberField(args.limit));
  addQueryEntry(entries, 'timeout', numberField(args.timeout_seconds));
  addQueryEntry(entries, 'reason', stringField(args.reason));
  addQueryEntry(entries, 'message', previewText(args.message ?? args.initial_message, 240));
  return entries;
}

function managedConversationBadges(args: Record<string, unknown>): string[] {
  const badges: string[] = [];
  addBadge(badges, 'agent', stringField(args.agent_id));
  addBadge(badges, 'mode', stringField(args.chat_mode));
  addBadge(badges, 'status', stringField(args.status));
  addBadge(badges, 'timeout', numberField(args.timeout_seconds) !== null ? `${numberField(args.timeout_seconds)}s` : '');
  return badges;
}

function managedConversationFromRecord(record: Record<string, unknown>, fallback: Record<string, unknown> = {}): ManagedConversationItemPresentation {
  const errorRecord = asRecord(record.error);
  const turnRecord = asRecord(record.turn);
  const conversationId = stringField(record.conversation_id)
    || stringField(record.managed_conversation_id)
    || stringField(record.id)
    || stringField(fallback.conversation_id);
  const title = stringField(record.title) || stringField(fallback.title);
  const turnState = stringField(record.turn_state);
  const conversationState = stringField(record.conversation_state);
  const status = turnState
    || stringField(record.status)
    || conversationState
    || stringField(fallback.status);
  const error = stringField(errorRecord.message) || stringField(record.last_error);
  const summary = stringField(record.last_result_summary)
    || stringField(record.result_summary)
    || previewText(turnRecord.final_content, 360)
    || stringField(record.message);
  return {
    conversationId,
    sessionId: stringField(record.session_id) || stringField(turnRecord.session_id),
    agentId: stringField(record.agent_id) || stringField(fallback.agent_id),
    title,
    status,
    turnState,
    conversationState,
    summary,
    error,
    controllerConversationId: stringField(record.controller_conversation_id),
    followUpConversationId: stringField(record.follow_up_conversation_id),
  };
}

function managedConversationRows(
  parsed: Record<string, unknown> | null,
  args: Record<string, unknown>,
  item: ToolCallSummaryInput,
): ManagedConversationItemPresentation[] {
  const rows: ManagedConversationItemPresentation[] = [];
  const indexes = new Map<string, number>();
  const append = (record: Record<string, unknown>, preferLive = false): void => {
    const row = managedConversationFromRecord(record, args);
    const key = row.conversationId || row.sessionId || row.title || `${rows.length}`;
    const existingIndex = indexes.get(key);
    if (existingIndex !== undefined) {
      if (preferLive) {
        const existing = rows[existingIndex];
        rows[existingIndex] = {
          ...existing,
          ...row,
          agentId: row.agentId || existing.agentId,
          title: row.title || existing.title,
          summary: row.summary || existing.summary,
          error: row.error || existing.error,
          controllerConversationId: row.controllerConversationId || existing.controllerConversationId,
          followUpConversationId: row.followUpConversationId || existing.followUpConversationId,
        };
      }
      return;
    }
    indexes.set(key, rows.length);
    rows.push(row);
  };

  const conversation = asRecord(parsed?.conversation);
  if (objectKeys(conversation).length > 0) append(conversation);
  for (const record of recordArray(parsed?.conversations)) append(record);

  // Async create/send calls settle before their child does. Their static
  // result supplies the initial record; the bounded live snapshot is the
  // current state and overlays it by conversation ID without adding a row.
  const liveConversation = asRecord(item.managedConversation?.conversation);
  if (objectKeys(liveConversation).length > 0) append(liveConversation, true);

  const parsedAsConversation = asRecord(parsed);
  if (
    rows.length === 0
    && (stringField(parsedAsConversation.conversation_id)
      || stringField(parsedAsConversation.managed_conversation_id)
      || stringField(parsedAsConversation.id))
  ) {
    append(parsedAsConversation);
  }

  if (rows.length === 0 && (stringField(args.conversation_id) || stringField(args.title) || stringField(args.agent_id))) {
    append({
      conversation_id: stringField(args.conversation_id),
      title: stringField(args.title),
      agent_id: stringField(args.agent_id),
      status: item.isError ? 'error' : (isActiveToolStatus(item.status) ? 'running' : 'unknown'),
    });
  }

  return rows;
}

function managedResultSummary(
  name: string,
  item: ToolCallSummaryInput,
  parsed: Record<string, unknown> | null,
  rows: ManagedConversationItemPresentation[],
): string {
  const errorRecord = asRecord(parsed?.error);
  const error = stringField(errorRecord.message) || stringField(parsed?.message);
  if (item.isError || parsed?.status === 'error') return error || firstOutputLine(cleanToolResult(item.result)) || 'Managed conversation operation failed.';
  if (item.status === 'started') {
    if (name === 'agentconversationwait' && rows[0]?.summary) return rows[0].summary;
    if (name === 'agentconversationwait') return 'Waiting for the managed turn to finish.';
    if (name === 'agentconversationlist') return 'Loading managed conversations.';
    return 'Managed conversation operation is running.';
  }
  if (name === 'agentconversationlist') {
    const count = numberField(parsed?.count) ?? rows.length;
    return `${count} managed conversation${count === 1 ? '' : 's'} found.`;
  }
  const primary = rows[0];
  if (primary?.error) return primary.error;
  if (primary?.summary) return primary.summary;
  const explicitMessage = stringField(parsed?.message);
  if (explicitMessage) return explicitMessage;
  const turn = asRecord(parsed?.turn);
  const finalContent = previewText(turn.final_content, 360);
  if (finalContent) return finalContent;
  const status = stringField(parsed?.status) || primary?.status;
  return status ? `Managed conversation ${status}.` : 'Managed conversation operation completed.';
}

export function managedConversationToolPresentation(item: ToolCallSummaryInput): ManagedConversationToolPresentation | null {
  const name = normalizedToolName(item.toolName);
  if (!isManagedConversationToolName(name)) return null;

  const args = item.arguments ?? {};
  const parsed = parsedToolResult(item.result) ?? item.managedConversation ?? null;
  const rows = managedConversationRows(parsed, args, item);
  const request = managedRequestLabelAndText(name, args);
  const errorRecord = asRecord(parsed?.error);
  const error = item.isError || parsed?.status === 'error'
    ? (stringField(errorRecord.message) || stringField(parsed?.message) || firstOutputLine(cleanToolResult(item.result)) || 'Managed conversation operation failed.')
    : '';
  const resultSummary = managedResultSummary(name, item, parsed, rows);
  const primary = rows[0] ?? null;
  const live = item.managedConversation;
  const liveTodos = todosFrom(live?.todos);
  const todos = todoItems(liveTodos.length > 0 ? liveTodos : todosFrom(parsed?.todos));
  const displayStatus = primary?.status
    || (isActiveToolStatus(item.status) ? 'running' : (item.status || 'unknown'));
  return {
    kind: 'managed_conversation_tool',
    title: managedConversationToolTitle(name),
    summary: request.text ? `${managedConversationToolTitle(name)} · ${previewText(request.text, 120)}` : managedConversationToolTitle(name),
    requestLabel: request.label,
    requestText: request.text,
    requestDetails: managedConversationDetails(args),
    badges: managedConversationBadges(args),
    conversations: rows,
    primaryConversation: primary,
    todos,
    todoSummary: todoSummary(liveTodos.length > 0 ? liveTodos : todosFrom(parsed?.todos)),
    toolCallCount: numberField(live?.tool_call_count) ?? numberField(parsed?.tool_call_count),
    lastTool: stringField(live?.last_tool) || stringField(parsed?.last_tool),
    resultSummary,
    error,
    displayStatus,
  };
}

export function managedConversationStatusIsRunning(status: string | null | undefined): boolean {
  const normalized = (status ?? '').trim().toLowerCase();
  return normalized === 'running' || normalized === 'queued';
}

function nativeInspectionKind(name: string): NativeInspectionKind | null {
  if (name === 'read') return 'read';
  if (name === 'listdirectory') return 'list_directory';
  if (name === 'grep') return 'grep';
  if (name === 'glob') return 'glob';
  if (name === 'lsp') return 'lsp';
  return null;
}

function nativeInspectionTitle(kind: NativeInspectionKind): string {
  if (kind === 'read') return 'Read file';
  if (kind === 'list_directory') return 'List directory';
  if (kind === 'grep') return 'Search files';
  if (kind === 'glob') return 'Find files';
  return 'Language server query';
}

function nativeRequestLabelAndText(kind: NativeInspectionKind, args: Record<string, unknown>): { label: string; text: string } {
  if (kind === 'read') return { label: 'File', text: stringField(args.file_path) || stringField(args.filePath) || stringField(args.path) };
  if (kind === 'list_directory') return { label: 'Directory', text: stringField(args.path) || '~' };
  if (kind === 'grep') return { label: 'Pattern', text: stringField(args.pattern) };
  if (kind === 'glob') return { label: 'Pattern', text: stringField(args.pattern) };
  return { label: 'Operation', text: stringField(args.operation) || stringField(args.query) };
}

function nativePath(kind: NativeInspectionKind, args: Record<string, unknown>): string {
  if (kind === 'read') return stringField(args.file_path) || stringField(args.filePath) || stringField(args.path);
  if (kind === 'lsp') return stringField(args.file_path) || stringField(args.path);
  return stringField(args.path);
}

function nativePattern(kind: NativeInspectionKind, args: Record<string, unknown>): string {
  if (kind === 'grep' || kind === 'glob') return stringField(args.pattern);
  if (kind === 'lsp') return stringField(args.query);
  return '';
}

function nativeInspectionDetails(kind: NativeInspectionKind, args: Record<string, unknown>): StructuredToolEntry[] {
  const entries: StructuredToolEntry[] = [];
  addQueryEntry(entries, 'path', nativePath(kind, args));
  addQueryEntry(entries, 'pattern', nativePattern(kind, args));
  addQueryEntry(entries, 'include', stringField(args.include));
  addQueryEntry(entries, 'operation', stringField(args.operation));
  addQueryEntry(entries, 'query', stringField(args.query));
  addQueryEntry(entries, 'offset', numberField(args.offset));
  addQueryEntry(entries, 'limit', numberField(args.limit));
  addQueryEntry(entries, 'line', numberField(args.line));
  addQueryEntry(entries, 'character', numberField(args.character));
  addQueryEntry(entries, 'case insensitive', booleanField(args.case_insensitive));
  addQueryEntry(entries, 'context lines', numberField(args.context_lines));
  addQueryEntry(entries, 'output mode', stringField(args.output_mode));
  addQueryEntry(entries, 'max per file', numberField(args.max_per_file));
  return entries;
}

function nativeInspectionBadges(kind: NativeInspectionKind, args: Record<string, unknown>): string[] {
  const badges: string[] = [];
  addBadge(badges, 'offset', numberField(args.offset));
  addBadge(badges, 'limit', numberField(args.limit));
  addBadge(badges, 'mode', stringField(args.output_mode));
  addBadge(badges, 'include', stringField(args.include));
  addBadge(badges, 'context', numberField(args.context_lines));
  if (kind === 'lsp') {
    addBadge(badges, 'operation', stringField(args.operation));
  }
  return badges;
}

function splitOutputFooter(cleaned: string): { body: string; footer: string } {
  const lines = cleaned.split('\n');
  const footerLines: string[] = [];
  while (lines.length > 0) {
    const last = lines[lines.length - 1]?.trim() ?? '';
    if (!last) {
      if (footerLines.length > 0) {
        footerLines.unshift(lines.pop() ?? '');
        continue;
      }
      break;
    }
    if (/^\(.+\)$/.test(last) || /^\.\.\.\s/i.test(last) || /^Search truncated\b/i.test(last) || /^Total matches:/i.test(last)) {
      footerLines.unshift(lines.pop() ?? '');
      continue;
    }
    break;
  }
  return {
    body: lines.join('\n').trimEnd(),
    footer: footerLines.join('\n').trim(),
  };
}

function parseReadLines(body: string): NativeReadLinePresentation[] {
  const lines: NativeReadLinePresentation[] = [];
  for (const line of body.split('\n')) {
    const match = line.match(/^(\d+):(?:\s?(.*))?$/);
    if (!match) return [];
    lines.push({
      lineNumber: Number(match[1]),
      content: match[2] ?? '',
    });
  }
  return lines;
}

function pathName(path: string): string {
  const cleaned = path.replace(/[\\/]+$/, '');
  return cleaned.split(/[\\/]/).pop() || path;
}

function parsePathEntries(body: string): NativePathEntryPresentation[] {
  return body
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !line.startsWith('[') && !line.startsWith('(') && !line.startsWith('...'))
    .map((path) => ({
      path,
      name: pathName(path),
      kind: path.endsWith('/') ? 'directory' as const : 'unknown' as const,
    }));
}

function parseGrepGroups(body: string): NativeGrepGroupPresentation[] {
  const groups = new Map<string, NativeGrepMatchPresentation[]>();
  const linePattern = /^(.*?)([:\-])(\d+)\2\s?(.*)$/;
  for (const line of body.split('\n')) {
    if (!line.trim() || line.trim().startsWith('...') || line.trim().startsWith('(')) continue;
    const match = line.match(linePattern);
    if (!match) continue;
    const path = match[1] ?? '';
    const lineNumber = Number(match[3]);
    if (!path || !Number.isFinite(lineNumber)) continue;
    const entries = groups.get(path) ?? [];
    entries.push({
      path,
      lineNumber,
      text: match[4] ?? '',
      isMatch: match[2] === ':',
    });
    groups.set(path, entries);
  }
  return Array.from(groups.entries()).map(([path, matches]) => ({ path, matches }));
}

function nativeInspectionSummary(kind: NativeInspectionKind, item: ToolCallSummaryInput, parsedOutput: {
  body: string;
  footer: string;
  readLines: NativeReadLinePresentation[];
  pathEntries: NativePathEntryPresentation[];
  grepGroups: NativeGrepGroupPresentation[];
}): string {
  if (item.status === 'started') return `${nativeInspectionTitle(kind)} is running.`;
  if (item.isError) return firstOutputLine(cleanToolResult(item.result)) || `${nativeInspectionTitle(kind)} failed.`;
  if (kind === 'read' && parsedOutput.readLines.length > 0) {
    const first = parsedOutput.readLines[0]?.lineNumber;
    const last = parsedOutput.readLines.at(-1)?.lineNumber;
    return `Read ${parsedOutput.readLines.length} line${parsedOutput.readLines.length === 1 ? '' : 's'}${first != null && last != null ? ` (${first}-${last})` : ''}.`;
  }
  if ((kind === 'list_directory' || kind === 'glob') && parsedOutput.pathEntries.length > 0) {
    return `${parsedOutput.pathEntries.length} path${parsedOutput.pathEntries.length === 1 ? '' : 's'} returned.`;
  }
  if (kind === 'grep' && parsedOutput.grepGroups.length > 0) {
    const count = parsedOutput.grepGroups.reduce((total, group) => total + group.matches.filter((match) => match.isMatch).length, 0);
    return `${count} match${count === 1 ? '' : 'es'} in ${parsedOutput.grepGroups.length} file${parsedOutput.grepGroups.length === 1 ? '' : 's'}.`;
  }
  if (kind === 'list_directory' && /^\(empty directory\)$/i.test(parsedOutput.footer)) return 'Directory is empty.';
  if (parsedOutput.body.trim()) return `${lineCount(parsedOutput.body)} output line${lineCount(parsedOutput.body) === 1 ? '' : 's'} returned.`;
  return 'No output was returned.';
}

export function nativeInspectionToolPresentation(item: ToolCallSummaryInput): NativeInspectionToolPresentation | null {
  const name = normalizedToolName(item.toolName);
  const kind = nativeInspectionKind(name);
  if (!kind) return null;

  const args = item.arguments ?? {};
  const cleaned = cleanToolResult(item.result);
  const split = splitOutputFooter(cleaned);
  const readLines = kind === 'read' ? parseReadLines(split.body) : [];
  const pathEntries = kind === 'list_directory' || kind === 'glob' ? parsePathEntries(split.body) : [];
  const grepGroups = kind === 'grep' ? parseGrepGroups(split.body) : [];
  const request = nativeRequestLabelAndText(kind, args);
  const summary = nativeInspectionSummary(kind, item, {
    body: split.body,
    footer: split.footer,
    readLines,
    pathEntries,
    grepGroups,
  });
  const error = item.isError ? firstOutputLine(cleaned) || `${nativeInspectionTitle(kind)} failed.` : '';

  return {
    kind: 'native_inspection_tool',
    nativeKind: kind,
    title: nativeInspectionTitle(kind),
    summary,
    requestLabel: request.label,
    requestText: request.text,
    requestDetails: nativeInspectionDetails(kind, args),
    badges: nativeInspectionBadges(kind, args),
    path: nativePath(kind, args),
    pattern: nativePattern(kind, args),
    outputText: split.body || cleaned,
    readLines,
    pathEntries,
    grepGroups,
    footer: split.footer,
    error,
  };
}

function anchoredSections(value: string): Map<string, string> {
  const sections = new Map<string, string>();
  let current = '';
  let lines: string[] = [];
  const commit = (): void => {
    if (current) sections.set(current, lines.join('\n').trim());
  };
  for (const line of value.split('\n')) {
    const marker = line.match(/^\[\[([^\]]+)\]\]$/);
    if (marker) {
      commit();
      current = marker[1] ?? '';
      lines = [];
    } else if (current) {
      lines.push(line);
    }
  }
  commit();
  return sections;
}

function sectionField(lines: string[], label: string): string {
  const prefix = `${label.toLowerCase()}:`;
  const line = lines.map((entry) => entry.trim()).find((entry) => entry.toLowerCase().startsWith(prefix));
  return line ? line.slice(prefix.length).trim() : '';
}

function parseWebMedia(sections: Map<string, string>): WebMediaPresentation[] {
  const media: WebMediaPresentation[] = [];
  for (const [anchor, body] of sections) {
    if (!anchor.startsWith('media:')) continue;
    const lines = body.split('\n');
    const url = sectionField(lines, 'URL');
    if (!url) continue;
    const artifactRef = lines
      .map((line) => line.match(/tool_artifact:[^\s"]+/)?.[0] ?? '')
      .find(Boolean) ?? '';
    media.push({
      label: sectionField(lines, 'Caption') || sectionField(lines, 'Alt') || url,
      url,
      source: sectionField(lines, 'Source'),
      sourcePageUrl: sectionField(lines, 'Source page'),
      artifactRef,
    });
  }
  return media;
}

export function webToolPresentation(item: ToolCallSummaryInput): WebToolPresentation | null {
  const name = normalizedToolName(item.toolName);
  const webKind: WebToolKind | null = name === 'webfetch' ? 'fetch' : name === 'websearch' ? 'search' : null;
  if (!webKind) return null;

  const args = item.arguments ?? {};
  const cleaned = cleanToolResult(item.result);
  const sections = anchoredSections(cleaned);
  const media = parseWebMedia(sections);
  const error = item.isError ? firstOutputLine(cleaned) || `Web ${webKind} failed.` : '';
  const badges: string[] = [];
  addBadge(badges, 'backend', stringField(args.backend) || 'default');
  addBadge(badges, 'format', stringField(args.format));
  addBadge(badges, 'timeout', numberField(args.timeout) !== null ? `${numberField(args.timeout)}s` : '');
  addBadge(badges, 'images', media.length > 0 ? media.length : args.include_images === true ? 'requested' : '');

  if (webKind === 'search') {
    const results: WebResultPresentation[] = [];
    for (const [anchor, body] of sections) {
      if (!anchor.startsWith('result:')) continue;
      const lines = body.split('\n');
      const heading = lines[0]?.match(/^\[\d+\]\s*(.*)$/)?.[1]?.trim() ?? '';
      results.push({
        title: heading || sectionField(lines, 'URL') || 'Search result',
        url: sectionField(lines, 'URL'),
        domain: sectionField(lines, 'Domain'),
         snippet: sectionField(lines, 'Snippet'),
         score: sectionField(lines, 'Relevance'),
         publishedDate: sectionField(lines, 'Published'),
         resultType: sectionField(lines, 'Type'),
         recommendation: sectionField(lines, 'Fetch recommendation'),
         freshness: sectionField(lines, 'Freshness'),
         sourceEngine: sectionField(lines, 'Source engine'),
      });
    }
    const answer = (sections.get('answer') ?? '').replace(/^Answer:\s*/i, '').trim();
    const warning = sections.get('search:status')?.trim() ?? '';
    const summary = item.status === 'started'
      ? 'Searching the web…'
      : error || `${results.length} result${results.length === 1 ? '' : 's'}${media.length ? ` and ${media.length} image reference${media.length === 1 ? '' : 's'}` : ''}.`;
    return {
      kind: 'web_tool',
      webKind,
      title: 'Web search',
      summary,
      requestText: stringField(args.query),
      badges,
      answer,
      content: '',
      results,
      media,
      error,
      warning,
    };
  }

  const page = sections.get('page:1') ?? '';
  const pageLines = page.split('\n');
  const content = pageLines
    .filter((line) => !/^(URL|Requested URL|Domain|Extractor):/i.test(line))
    .join('\n')
    .trim();
  const summary = item.status === 'started'
    ? 'Fetching page…'
    : error || `${content ? `${lineCount(content)} content line${lineCount(content) === 1 ? '' : 's'}` : 'No page content'}${media.length ? ` and ${media.length} image reference${media.length === 1 ? '' : 's'}` : ''}.`;
  return {
    kind: 'web_tool',
    webKind,
    title: 'Web fetch',
    summary,
    requestText: sectionField(pageLines, 'URL') || stringField(args.url),
    badges,
    answer: '',
    content,
    results: [],
    media,
    error,
    warning: '',
  };
}

export function workflowToolPresentation(item: ToolCallSummaryInput): WorkflowToolPresentation | null {
  if (!successfulToolResult(item)) return null;

  const name = normalizedToolName(item.toolName);
  const args = item.arguments ?? {};
  const parsed = parsedToolResult(item.result);

  if (name === 'writedeliverable') {
    const content = typeof args.content === 'string' ? args.content : '';
    return {
      kind: 'write_deliverable',
      title: stringField(args.title) || 'Deliverable',
      format: normalizedDeliverableFormat(args.format),
      status: stringField(parsed?.status) || item.status || 'completed',
      note: 'Final deliverable renders at turn end.',
      deliverableId: stringField(parsed?.deliverable_id),
      version: typeof parsed?.version === 'number' ? parsed.version : null,
      length: typeof parsed?.length === 'number' ? parsed.length : (content ? content.length : null),
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

function booleanField(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function recordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => (
    item !== null && typeof item === 'object' && !Array.isArray(item)
  ));
}

function previewText(value: unknown, max = 240): string {
  const text = stringField(value).replace(/\s+/g, ' ');
  return text.length > max ? `${text.slice(0, Math.max(0, max - 1)).trimEnd()}…` : text;
}

function addQueryEntry(entries: StructuredToolEntry[], key: string, value: unknown): void {
  if (value == null || value === '') return;
  entries.push({ key, value });
}

function addBadge(badges: string[], label: string, value: unknown): void {
  if (value == null || value === '') return;
  if (Array.isArray(value) && value.length === 0) return;
  if (typeof value === 'boolean') {
    if (value) badges.push(label);
    return;
  }
  if (Array.isArray(value)) {
    badges.push(`${label}: ${value.map((item) => String(item)).join(', ')}`);
    return;
  }
  badges.push(`${label}: ${value}`);
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

function memoryToolTitle(name: string): string {
  if (name === 'memorysearch') return 'Search memories';
  if (name === 'memoryfind') return 'Find memories';
  if (name === 'memoryask') return 'Ask memory';
  if (name === 'memoryaddbatch') return 'Save memories';
  if (name === 'memoryadd') return 'Save memory';
  if (name === 'memoryupdate') return 'Update memory';
  if (name === 'memorydelete') return 'Delete memory';
  if (name === 'memorylist') return 'List memories';
  if (name === 'memoryrecent') return 'Recent memories';
  if (name === 'memorycategories') return 'Memory categories';
  if (name === 'memorysaveartifact') return 'Save memory artifact';
  if (name === 'memorygetartifacturl') return 'Get artifact link';
  if (name === 'memorygetartifact') return 'Read memory artifact';
  if (name === 'memorylistartifacts') return 'List memory artifacts';
  if (name === 'memorydeleteartifact') return 'Delete memory artifact';
  return 'Memory';
}

function isMemorySaveTool(name: string): boolean {
  return name === 'memoryadd' || name === 'memoryaddbatch';
}

function memoryRequestLabelAndText(name: string, args: Record<string, unknown>): { label: string; text: string } {
  if (name === 'memorysearch') return { label: 'Search query', text: stringField(args.query) };
  if (name === 'memoryfind') return { label: 'Question', text: stringField(args.question) };
  if (name === 'memoryask') return { label: 'Question', text: stringField(args.question) };
  if (name === 'memoryadd') return { label: 'Memory to save', text: stringField(args.content) };
  if (name === 'memoryaddbatch') {
    const memories = recordArray(args.memories);
    return { label: 'Memories to save', text: `${memories.length} memor${memories.length === 1 ? 'y' : 'ies'}` };
  }
  if (name === 'memoryupdate') return { label: 'Memory to update', text: stringField(args.memory_id) };
  if (name === 'memorydelete') return { label: 'Memory to delete', text: stringField(args.memory_id) };
  if (name === 'memorylist') return { label: 'List filter', text: 'List stored memories' };
  if (name === 'memoryrecent') return { label: 'Recent memories', text: `${numberField(args.days) ?? 7} days` };
  if (name === 'memorycategories') return { label: 'Categories', text: 'List memory categories' };
  if (name.includes('artifact')) {
    const memoryId = stringField(args.memory_id);
    const artifactId = stringField(args.artifact_id);
    const filename = stringField(args.filename);
    return {
      label: artifactId ? 'Artifact' : 'Memory artifact',
      text: [filename, artifactId, memoryId].filter(Boolean).join(' · '),
    };
  }
  return { label: 'Request', text: '' };
}

function memoryRequestDetails(name: string, args: Record<string, unknown>): StructuredToolEntry[] {
  const entries: StructuredToolEntry[] = [];
  addQueryEntry(entries, 'memory id', stringField(args.memory_id));
  addQueryEntry(entries, 'artifact id', stringField(args.artifact_id));
  addQueryEntry(entries, 'filename', stringField(args.filename));
  addQueryEntry(entries, 'content type', stringField(args.content_type));
  addQueryEntry(entries, 'type', stringField(args.memory_type));
  addQueryEntry(entries, 'role', stringField(args.role));
  addQueryEntry(entries, 'importance', stringField(args.importance));
  addQueryEntry(entries, 'ttl days', numberField(args.ttl_days));
  addQueryEntry(entries, 'limit', numberField(args.limit));
  addQueryEntry(entries, 'offset', numberField(args.offset));
  addQueryEntry(entries, 'scope', stringField(args.scope));
  addQueryEntry(entries, 'include decayed', booleanField(args.include_decayed));
  addQueryEntry(entries, 'categories', stringArray(args.categories).join(', '));
  addQueryEntry(entries, 'labels', objectKeys(args.labels).join(', '));
  addQueryEntry(entries, 'context', previewText(args.context, 160));
  if (name === 'memoryaddbatch') {
    const memories = recordArray(args.memories);
    addQueryEntry(entries, 'count', memories.length);
    const first = previewText(memories[0]?.content, 160);
    addQueryEntry(entries, 'first memory', first);
  }
  return entries;
}

function memoryBadges(args: Record<string, unknown>): string[] {
  const badges: string[] = [];
  addBadge(badges, 'type', stringField(args.memory_type));
  addBadge(badges, 'role', stringField(args.role));
  addBadge(badges, 'importance', stringField(args.importance));
  addBadge(badges, 'pinned', booleanField(args.pinned));
  addBadge(badges, 'ttl', numberField(args.ttl_days) !== null ? `${numberField(args.ttl_days)} days` : '');
  addBadge(badges, 'categories', stringArray(args.categories));
  addBadge(badges, 'limit', numberField(args.limit));
  addBadge(badges, 'scope', stringField(args.scope));
  return badges;
}

function memoryItemMeta(record: Record<string, unknown>): StructuredToolEntry[] {
  const metadata = asRecord(record.metadata);
  const meta: StructuredToolEntry[] = [];
  addQueryEntry(meta, 'event', stringField(record.event));
  addQueryEntry(meta, 'score', numberField(record.score)?.toFixed(2));
  addQueryEntry(meta, 'type', stringField(metadata.memory_type) || stringField(metadata.type));
  addQueryEntry(meta, 'role', stringField(metadata.role));
  addQueryEntry(meta, 'importance', stringField(metadata.importance));
  addQueryEntry(meta, 'categories', stringArray(metadata.categories).join(', '));
  addQueryEntry(meta, 'created', stringField(metadata.created_at));
  addQueryEntry(meta, 'artifacts', record.has_artifacts === true || record.hasArtifacts === true ? 'attached' : '');
  addQueryEntry(meta, 'content type', stringField(record.content_type));
  addQueryEntry(meta, 'size', numberField(record.size));
  addQueryEntry(meta, 'linked memories', numberField(record.linked_memories));
  addQueryEntry(meta, 'has more', booleanField(record.has_more));
  return meta;
}

function appendMemoryResultItem(items: MemoryResultItemPresentation[], record: Record<string, unknown>): void {
  const memory = stringField(record.memory) || stringField(record.content);
  const filename = stringField(record.filename);
  const name = stringField(record.name);
  const description = stringField(record.description);
  const id = stringField(record.id);
  if (record.error === true || stringField(record.message)) {
    items.push({
      title: stringField(record.message) || 'Memory save failed',
      body: memory || stringField(record.detail) || stringField(record.error_message),
      accent: 'generic',
      meta: memoryItemMeta(record),
    });
    return;
  }
  if (memory || record.has_artifacts !== undefined || record.metadata !== undefined || record.content !== undefined) {
    const event = stringField(record.event);
    items.push({
      title: [event, id].filter(Boolean).join(' · ') || 'Memory',
      body: memory,
      accent: 'memory',
      meta: memoryItemMeta(record),
    });
    return;
  }
  if (filename) {
    items.push({
      title: [filename, id].filter(Boolean).join(' · '),
      body: stringField(record.content_type),
      accent: 'artifact',
      meta: memoryItemMeta(record),
    });
    return;
  }
  if (name) {
    items.push({
      title: name,
      body: description,
      accent: 'category',
      meta: [
        { key: 'count', value: numberField(record.count) ?? 0 },
      ],
    });
  }
}

function appendMemoryArtifactItems(items: MemoryResultItemPresentation[], source: Record<string, unknown>): void {
  for (const artifact of recordArray(source.artifacts)) {
    items.push({
      title: [stringField(artifact.filename), stringField(artifact.id)].filter(Boolean).join(' · ') || 'Artifact',
      body: stringField(artifact.content_type),
      accent: 'artifact',
      meta: memoryItemMeta(artifact),
    });
  }

  const artifact = asRecord(source.artifact);
  if (objectKeys(artifact).length > 0) {
    items.push({
      title: [stringField(artifact.filename), stringField(artifact.id)].filter(Boolean).join(' · ') || 'Artifact',
      body: stringField(artifact.content_type),
      accent: 'artifact',
      meta: memoryItemMeta(artifact),
    });
  }
}

function memoryResultItems(parsed: Record<string, unknown> | null, name = ''): MemoryResultItemPresentation[] {
  if (!parsed) return [];

  const items: MemoryResultItemPresentation[] = [];
  const results = recordArray(parsed.results);
  for (const record of results) {
    const nestedResults = recordArray(record.results);
    if (nestedResults.length > 0) {
      for (const nested of nestedResults) {
        appendMemoryResultItem(items, nested);
      }
      appendMemoryArtifactItems(items, record);
      if (record.error === true || stringField(record.message)) {
        appendMemoryResultItem(items, record);
      }
      continue;
    }
    appendMemoryResultItem(items, record);
  }

  for (const category of recordArray(parsed.categories)) {
    items.push({
      title: stringField(category.name) || 'Category',
      body: stringField(category.description),
      accent: 'category',
      meta: [{ key: 'count', value: numberField(category.count) ?? 0 }],
    });
  }

  appendMemoryArtifactItems(items, parsed);

  if (name !== 'memorygetartifact' && stringField(parsed.content)) {
    items.push({
      title: 'Artifact content',
      body: previewText(parsed.content, 700),
      accent: 'artifact',
      meta: [
        { key: 'total size', value: numberField(parsed.total_size) ?? stringField(parsed.size) },
        { key: 'has more', value: booleanField(parsed.has_more) ?? false },
      ].filter((entry) => entry.value !== ''),
    });
  }

  return items;
}

function savedMemoryCount(resultItems: MemoryResultItemPresentation[]): number {
  return resultItems.filter((item) => item.accent === 'memory').length;
}

function failedMemorySaveCount(resultItems: MemoryResultItemPresentation[]): number {
  return resultItems.filter((item) => item.accent === 'generic').length;
}

function memorySaveSummary(resultItems: MemoryResultItemPresentation[], fallback: string): string {
  const savedCount = savedMemoryCount(resultItems);
  const failedCount = failedMemorySaveCount(resultItems);
  if (savedCount > 0 && failedCount > 0) {
    return `${savedCount} memor${savedCount === 1 ? 'y' : 'ies'} saved, ${failedCount} failed.`;
  }
  if (failedCount > 0) {
    return `${failedCount} memor${failedCount === 1 ? 'y' : 'ies'} failed to save.`;
  }
  if (savedCount > 0) {
    return `${savedCount} memor${savedCount === 1 ? 'y' : 'ies'} saved.`;
  }
  return fallback;
}

function memorySummary(
  name: string,
  requestText: string,
  item: ToolCallSummaryInput,
  parsed: Record<string, unknown> | null,
  resultItems: MemoryResultItemPresentation[],
): string {
  if (isMemorySaveTool(name)) {
    if (item.status === 'started') return name === 'memoryaddbatch' ? 'Saving memories…' : 'Saving memory…';
    if (item.isError || parsed?.error === true) return name === 'memoryaddbatch' ? 'Could not save memories.' : 'Could not save memory.';
    return memorySaveSummary(resultItems, name === 'memoryaddbatch' ? 'Memories saved.' : 'Memory saved.');
  }
  return requestText ? `${memoryToolTitle(name)} · ${previewText(requestText, 120)}` : memoryToolTitle(name);
}

function memoryResultSummary(
  name: string,
  item: ToolCallSummaryInput,
  parsed: Record<string, unknown> | null,
  resultItems: MemoryResultItemPresentation[],
): string {
  if (item.status === 'started') return 'Waiting for memory operation result.';
  const message = stringField(parsed?.message);
  if (item.isError || parsed?.error === true) return message || firstOutputLine(cleanToolResult(item.result)) || 'Memory operation failed.';
  if (message) return message;
  const count = resultItems.length;
  if (name === 'memoryask') {
    const answer = stringField(parsed?.answer);
    return answer ? 'Answered from stored memories.' : 'No answer was returned.';
  }
  if (name === 'memorysearch' || name === 'memoryfind' || name === 'memorylist' || name === 'memoryrecent') {
    return `${count} memor${count === 1 ? 'y' : 'ies'} found.`;
  }
  if (name === 'memoryaddbatch' || name === 'memoryadd' || name === 'memoryupdate' || name === 'memorydelete') {
    return isMemorySaveTool(name)
      ? memorySaveSummary(resultItems, 'Memory operation completed.')
      : count > 0 ? `${count} memor${count === 1 ? 'y' : 'ies'} changed.` : 'Memory operation completed.';
  }
  if (name === 'memorycategories') return `${count} categor${count === 1 ? 'y' : 'ies'} available.`;
  const text = stringField(parsed?.text) || (name === 'memorygetartifact' ? stringField(parsed?.content) : '');
  if (name.includes('artifact')) return count > 0 || text ? 'Artifact operation completed.' : 'Memory artifact operation completed.';
  if (text) return 'Memory text returned.';
  return cleanToolResult(item.result).trim() ? 'Memory operation completed.' : 'No memory output was returned.';
}

function memoryResultDetails(parsed: Record<string, unknown> | null): StructuredToolEntry[] {
  if (!parsed) return [];
  const entries: StructuredToolEntry[] = [];
  addQueryEntry(entries, 'count', numberField(parsed.count));
  addQueryEntry(entries, 'queries', stringArray(parsed.queries).join(', '));
  addQueryEntry(entries, 'url', stringField(parsed.url));
  addQueryEntry(entries, 'expires in', numberField(parsed.expires_in));
  addQueryEntry(entries, 'has more', booleanField(parsed.has_more));
  addQueryEntry(entries, 'total size', numberField(parsed.total_size));
  for (const entry of objectEntries(parsed.stats)) {
    addQueryEntry(entries, `stats.${entry.key}`, entry.value);
  }
  return entries;
}

export function memoryToolPresentation(item: ToolCallSummaryInput): MemoryToolPresentation | null {
  const name = normalizedToolName(item.toolName);
  if (!name.startsWith('memory')) return null;

  const args = item.arguments ?? {};
  const parsed = parsedMemoryToolResult(item.result);
  const request = memoryRequestLabelAndText(name, args);
  const resultItems = memoryResultItems(parsed, name);
  const answer = stringField(parsed?.answer);
  const text = stringField(parsed?.text) || (name === 'memorygetartifact' ? stringField(parsed?.content) : '');
  const error = item.isError || parsed?.error === true
    ? (stringField(parsed?.message) || firstOutputLine(cleanToolResult(item.result)) || 'Memory operation failed.')
    : '';
  return {
    kind: 'memory_tool',
    variant: isMemorySaveTool(name) ? 'saved' : 'default',
    title: memoryToolTitle(name),
    summary: memorySummary(name, request.text, item, parsed, resultItems),
    requestLabel: request.label,
    requestText: request.text,
    requestDetails: memoryRequestDetails(name, args),
    badges: memoryBadges(args),
    resultLabel: error ? 'Problem' : 'Result',
    resultSummary: memoryResultSummary(name, item, parsed, resultItems),
    resultDetails: memoryResultDetails(parsed),
    resultItems,
    answer,
    text,
    error,
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
