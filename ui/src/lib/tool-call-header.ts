import { isActiveToolStatus, type ToolCallTimelineItem } from '$lib/timeline-render-model';
import {
  delegationToolCallDisplayTitle,
  managedConversationToolPresentation,
  memoryToolPresentation,
  nativeInspectionToolPresentation,
  skillLoadDisplayName,
  stepTodoWriteStatusSummary,
  toolOutputHelperPresentation,
  webToolPresentation,
  workflowToolPresentation,
} from '$lib/tool-call-summary';
import { displayToolName } from '$lib/tools-display';

export function normalizedToolName(item: ToolCallTimelineItem): string {
  return item.toolName.toLowerCase().replace(/_/g, '');
}

export function isPythonKernelResetTool(item: ToolCallTimelineItem): boolean {
  return normalizedToolName(item) === 'pythonkernelreset';
}

export function toolCallDisplayTitle(item: ToolCallTimelineItem): string {
  return isPythonKernelResetTool(item)
    ? 'Python kernel reset'
    : displayToolName(item.displayToolName ?? item.toolName);
}

export function isDelegationTool(item: ToolCallTimelineItem): boolean {
  return ['delegate', 'retrysubsession', 'followupsubsession', 'forksubsession', 'fork']
    .includes(normalizedToolName(item));
}

export function delegationIsActive(item: ToolCallTimelineItem): boolean {
  return ['started', 'running', 'active'].includes(item.delegation?.status ?? '');
}

export function delegationIsQueued(item: ToolCallTimelineItem): boolean {
  return isDelegationTool(item)
    && isActiveToolStatus(item.status)
    && !item.delegation?.childSessionId
    && !item.delegation?.status;
}

export function toolDetailsStartExpanded(item: ToolCallTimelineItem): boolean {
  const name = normalizedToolName(item);
  return (['steprequestquestions', 'requestuserinput'].includes(name) && item.status !== 'started')
    || (name === 'pythonexecute' && (isActiveToolStatus(item.status) || item.isError === true))
    || ['requestauthchallenge', 'requestcredential'].includes(name)
    || (['writedeliverable', 'stepcomplete'].includes(name) && workflowToolPresentation(item) !== null)
    || (isDelegationTool(item) && (isActiveToolStatus(item.status) || delegationIsActive(item)))
    || (name.startsWith('agentconversation') && isActiveToolStatus(item.status));
}

const truncate = (value: string, max = 80) => value.length > max ? `${value.slice(0, max)}...` : value;

export function toolCallSubtitle(item: ToolCallTimelineItem): string {
  const name = normalizedToolName(item);
  if (name === 'pythonkernelreset' && item.kernelGeneration != null) {
    return `Kernel generation ${item.kernelGeneration}`;
  }
  const output = toolOutputHelperPresentation(item);
  if (output) return truncate(output.summary, 120);
  const memory = memoryToolPresentation(item);
  if (memory) return truncate(memory.summary, 120);
  const workflow = workflowToolPresentation(item);
  if (workflow?.kind === 'write_deliverable') return truncate(workflow.title, 120);
  if (workflow?.kind === 'step_complete') return truncate(workflow.summary, 120);
  if (workflow?.kind === 'step_todo_write') return truncate(workflow.statusSummary || `${workflow.count} todos`, 120);
  const managed = managedConversationToolPresentation(item);
  if (managed) return truncate(managed.requestText || managed.resultSummary || managed.summary, 120);
  const native = nativeInspectionToolPresentation(item);
  if (native) return truncate(native.requestText || native.summary, 120);
  const web = webToolPresentation(item);
  if (web) return truncate(web.requestText || web.summary, 120);
  if (name === 'skillload') {
    const skill = skillLoadDisplayName(item);
    if (skill) return truncate(skill, 120);
  }
  if (name === 'steptodowrite') {
    const summary = stepTodoWriteStatusSummary(item);
    if (summary) return truncate(summary, 120);
  }
  const args = item.arguments;
  if (!args) return '';
  if (name === 'skillload') {
    if (typeof args.skill === 'string') return truncate(args.skill);
    if (typeof args.skill_id === 'string') return truncate(args.skill_id);
  }
  if (['read', 'write', 'edit', 'patch', 'multiedit'].some(part => name.includes(part)) || name === 'listdirectory') {
    if (name.includes('applypatch') && typeof args.patchText === 'string') {
      const paths = new Set<string>();
      for (const line of args.patchText.split('\n')) {
        const match = line.match(/^(?:\*\*\* Update File:|\*\*\* Add File:|\*\*\* Delete File:|--- a\/|\+\+\+ b\/)(.+)$/);
        const path = match?.[1]?.trim();
        if (path && path !== '/dev/null') paths.add(path);
      }
      if (paths.size) return truncate([...paths].join(', '), 120);
    }
    if (typeof args.filePath === 'string') return args.filePath;
    if (typeof args.path === 'string') return args.path;
  }
  if (name.includes('bash') || name.includes('shell')) {
    const description = typeof args.description === 'string' ? args.description.trim() : '';
    if (description) return truncate(description, 120);
    if (typeof args.command === 'string') return truncate(args.command);
  }
  if ((name.includes('grep') || name.includes('glob')) && typeof args.pattern === 'string') return truncate(args.pattern);
  if ((name.includes('websearch') || name === 'search') && typeof args.query === 'string') return truncate(args.query);
  if (['webfetch', 'navigate', 'fetch'].some(part => name.includes(part)) && typeof args.url === 'string') return truncate(args.url);
  if ((name.includes('memorysearch') || name.includes('memoryfind')) && typeof args.query === 'string') return truncate(args.query);
  if (name.includes('memoryask') && typeof args.question === 'string') return truncate(args.question);
  if (name.includes('memoryadd') && typeof args.content === 'string') return truncate(args.content);
  if (isDelegationTool(item) || name.includes('spawn')) {
    const title = delegationToolCallDisplayTitle(args);
    if (title) return truncate(title);
  }
  if (name.includes('stepcomplete') && typeof args.summary === 'string') return truncate(args.summary);
  for (const value of Object.values(args)) {
    if (typeof value === 'string' && value.length) return truncate(value);
  }
  return '';
}

export function formatToolDuration(duration: number | null | undefined): string {
  if (duration == null) return '';
  if (duration < 1000) return `${duration}ms`;
  if (duration < 60_000) return `${(duration / 1000).toFixed(1)}s`;
  const seconds = Math.floor(duration / 1000);
  return `${Math.floor(seconds / 60)}m ${(seconds % 60).toString().padStart(2, '0')}s`;
}

export function toolHeaderDuration(item: ToolCallTimelineItem, now: number): string {
  if (delegationIsQueued(item)) return '';
  if (isDelegationTool(item)) {
    const delegation = item.delegation;
    if (typeof delegation?.durationMs === 'number') return formatToolDuration(delegation.durationMs);
    const terminal = ['completed', 'complete', 'failed', 'cancelled', 'canceled'].includes(delegation?.status ?? '');
    if (typeof item.durationMs === 'number' && terminal) return formatToolDuration(item.durationMs);
    const start = Date.parse(delegation?.startedAt ?? item.timestamp ?? '');
    const active = isActiveToolStatus(item.status) || delegationIsActive(item);
    const timestamp = Date.parse(item.timestamp ?? '');
    const end = active || Number.isNaN(timestamp) ? now : timestamp;
    if (!Number.isNaN(start)) return formatToolDuration(Math.max(0, end - start));
  }
  if (item.durationMs != null) return formatToolDuration(item.durationMs);
  if (!isActiveToolStatus(item.status)) return '';
  const start = Date.parse(item.timestamp ?? '');
  return Number.isNaN(start) ? '' : formatToolDuration(Math.max(0, now - start));
}

export function toolFrameClass(item: ToolCallTimelineItem, density = 'default'): string {
  return `tool-frame min-w-0 overflow-hidden border text-foreground ${density === 'compact'
    ? 'rounded-lg bg-canvas/45 text-xs shadow-none' : 'rounded-2xl bg-surface/80 text-sm shadow-card'} ${item.isError ? 'border-danger-border' : 'border-separator'}`;
}
