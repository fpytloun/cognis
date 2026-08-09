import type { WorkCommandEvent, WorkMutationEvent } from '$lib/chat-v2/types';
import type {
  ToolCallEvaluation,
  ToolCallTimelineItem,
} from '$lib/timeline-render-model';

const SECRET_KEY = /(access.?token|api.?key|authorization|cookie|credential|env|password|secret|token)/i;
const SECRET_TEXT = /((?:authorization\s*[:=]\s*(?:bearer|basic)\s+|(?:\\?["']?)?(?:access[_-]?token|api[_-]?key|apikey|password|secret|token)(?:\\?["']?)?\s*[:=]\s*))("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[[^\]]*\]|\{[^}]*\}|[^\s,&}"']+)/gi;

function safeText(value: string | null | undefined): string | undefined {
  if (value === null || value === undefined) return undefined;
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed === 'object') return JSON.stringify(safeValue(parsed));
  } catch {
    // Use the text fallback for command lines, descriptions, and non-JSON output.
  }
  return value.replace(SECRET_TEXT, '$1[redacted]');
}

function safeValue(value: unknown, depth = 0): unknown {
  if (depth >= 8) return '[truncated]';
  if (Array.isArray(value)) return value.slice(0, 100).map((item) => safeValue(item, depth + 1));
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).slice(0, 100).map(([key, item]) => [
        key,
        SECRET_KEY.test(key) ? '[redacted]' : safeValue(item, depth + 1),
      ]),
    );
  }
  return typeof value === 'string' ? safeText(value) : value;
}

function evaluation(value: Record<string, unknown> | null | undefined): ToolCallEvaluation | undefined {
  if (!value || typeof value.decision !== 'string') return undefined;
  return {
    decision: value.decision,
    reasoning: typeof value.reasoning === 'string' ? safeText(value.reasoning) : undefined,
    risk: typeof value.risk === 'string' ? value.risk : undefined,
    path: typeof value.path === 'string' ? value.path : undefined,
    latency_ms: typeof value.latency_ms === 'number' ? value.latency_ms : undefined,
  };
}

function toolCallStatus(status: WorkCommandEvent['status'] | WorkMutationEvent['status']) {
  return status === 'complete' ? 'completed' : status;
}

export function commandToToolCall(command: WorkCommandEvent): ToolCallTimelineItem {
  return {
    id: command.id,
    kind: 'tool_call',
    callId: command.call_id,
    toolName: command.tool_name || 'bash',
    displayToolName: command.display_name ?? 'Command',
    status: toolCallStatus(command.status),
    timestamp: command.created_at ?? command.updated_at ?? null,
    arguments: safeValue({
      ...(command.arguments ?? {}),
      command: command.command,
      description: command.description,
      workdir: command.workdir,
    }) as Record<string, unknown>,
    result: safeText(command.preview ?? command.error),
    streamedOutput: safeText(command.preview),
    isError: command.status === 'failed',
    durationMs: command.duration_ms ?? undefined,
    evaluation: evaluation(command.evaluation),
    outputSize: command.output_size ?? undefined,
    truncated: command.preview_truncated,
    // Work uses the bounded projection. Recovery remains available from the
    // canonical timeline and is not exposed as a duplicate "Full output" action.
    hasFullOutput: false,
    recoveryCallId: command.recovery_call_id ?? null,
    toolOutputArtifactId: command.tool_output_artifact_id ?? null,
    orderKey: command.sort_key,
  };
}

export function mutationToToolCall(event: WorkMutationEvent): ToolCallTimelineItem {
  return {
    id: event.id,
    kind: 'tool_call',
    callId: event.call_id,
    toolName: event.tool_name,
    displayToolName: event.display_name ?? undefined,
    status: toolCallStatus(event.status),
    timestamp: event.created_at ?? event.updated_at ?? null,
    arguments: safeValue(event.arguments) as Record<string, unknown>,
    result: safeText(event.result_preview ?? event.error),
    streamedOutput: safeText(event.streamed_output ?? event.result_preview),
    isError: event.status === 'failed',
    durationMs: event.duration_ms ?? undefined,
    evaluation: evaluation(event.evaluation),
    outputSize: event.output_size ?? undefined,
    truncated: event.truncated ?? event.diffs_truncated,
    hasFullOutput: false,
    recoveryCallId: event.recovery_call_id ?? null,
    toolOutputArtifactId: event.tool_output_artifact_id ?? null,
    orderKey: event.sort_key,
  };
}
