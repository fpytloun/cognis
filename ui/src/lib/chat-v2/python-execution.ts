import type { ToolCallTimelineItem } from '$lib/timeline-render-model';

/** Interpret only the versioned Python result; ordinary JSON remains user output. */
export function pythonExecutionOutput(item: Pick<ToolCallTimelineItem, 'status' | 'result' | 'streamedOutput'>): {
  output: string;
  result: string | null;
  error: string | null;
  envelope: boolean;
} {
  const raw = item.result;
  let value: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      && (parsed as Record<string, unknown>).protocol_version === 'cognis.program.v1') {
      value = parsed as Record<string, unknown>;
    }
  } catch {
    // Interrupted/truncated results and pre-protocol errors are ordinary text.
  }
  if (!value) {
    const active = ['started', 'running', 'waiting'].includes(item.status);
    const partialEnvelope = /"protocol_version"\s*:\s*"cognis\.program\.v1"/.test(raw ?? '');
    return {
      output: active ? item.streamedOutput ?? raw ?? ''
        : item.streamedOutput !== raw ? item.streamedOutput ?? '' : '',
      result: active || partialEnvelope ? null : raw ?? null,
      error: partialEnvelope ? 'Execution data is truncated. Expand raw execution data or open full execution data for details.' : null,
      envelope: partialEnvelope,
    };
  }
  const error = value.error && typeof value.error === 'object'
    ? (value.error as Record<string, unknown>).bounded_message : null;
  return {
    output: typeof value.stdout_preview === 'string' && value.stdout_preview
      ? value.stdout_preview : item.streamedOutput !== raw ? item.streamedOutput ?? '' : '',
    result: value.result == null ? null : JSON.stringify(value.result, null, 2),
    error: typeof error === 'string' ? error : null,
    envelope: true,
  };
}
