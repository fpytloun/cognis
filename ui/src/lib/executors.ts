import type { ExecutorConfig, ExecutorMCPServerRuntimeStatus, ExecutorRuntimeIssue } from '$lib/types/api';

export interface ExecutorDegradedIssue {
  source: string;
  title: string;
  detail: string | null;
}

export function executorRuntimeBadgeStatus(executor: ExecutorConfig): 'healthy' | 'degraded' | 'unhealthy' {
  if (executor.status !== 'active') return 'degraded';
  if (executor.runtime_state === 'active') return 'healthy';
  if (executor.runtime_state === 'degraded' || executor.runtime_state === 'reconfiguring' || executor.runtime_state === 'stale') {
    return 'degraded';
  }
  return 'unhealthy';
}

export function executorRuntimeLabel(executor: ExecutorConfig): string {
  if (executor.status !== 'active') return 'disabled';
  if (executor.runtime_state === 'active') return 'connected';
  if (executor.runtime_state === 'degraded') return 'degraded';
  if (executor.runtime_state === 'reconfiguring') return 'reconfiguring';
  if (executor.runtime_state === 'stale') return 'pending reconfigure';
  if (executor.runtime_state === 'blocked') return 'blocked';
  return 'offline';
}

export function executorObservedNote(executor: ExecutorConfig): string | null {
  if (!executor.last_observed_at) return null;
  return `last seen ${new Date(executor.last_observed_at).toLocaleString()}`;
}

export function executorRuntimeSummary(executor: ExecutorConfig): string | null {
  if (executor.runtime_metadata.legacy_metadata) {
    return 'Legacy executor metadata: detailed MCP runtime status is unavailable.';
  }
  const issues = executorDegradedIssues(executor);
  if (issues.length > 0) {
    return `${issues.length} degraded issue(s): ${issues.map((issue) => issue.title).join(', ')}`;
  }
  const failed = (executor.runtime_metadata.mcp_servers ?? []).filter((server) => server.status !== 'ready');
  if (failed.length > 0) {
    return `${failed.length} MCP server(s) degraded: ${failed.map((server) => server.name).join(', ')}`;
  }
  const warnings = executor.runtime_metadata.warnings ?? [];
  return warnings[0] ?? null;
}

function formatMcpFailure(server: ExecutorMCPServerRuntimeStatus): string {
  const details = [server.phase, server.error_class, server.timed_out ? 'timed out' : null].filter(Boolean).join(' · ');
  const summary = server.stderr_summary ?? server.message ?? null;
  return summary ? `${server.name}: ${details}${details ? ' · ' : ''}${summary}` : `${server.name}: ${details || 'failed'}`;
}

function runtimeIssueTitle(issue: ExecutorRuntimeIssue, fallback: string): string {
  return stringValue(issue.title) ?? stringValue(issue.kind) ?? stringValue(issue.source) ?? fallback;
}

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function runtimeIssueDetail(issue: ExecutorRuntimeIssue): string | null {
  return stringValue(issue.details) ?? stringValue(issue.message);
}

export function executorDegradedIssues(executor: ExecutorConfig): ExecutorDegradedIssue[] {
  if (executor.runtime_metadata.legacy_metadata) {
    return [];
  }
  const issues = (executor.runtime_metadata.degraded_issues ?? []).map((issue, index) => ({
    source: issue.source || 'executor',
    title: runtimeIssueTitle(issue, `runtime issue ${index + 1}`),
    detail: runtimeIssueDetail(issue)
  }));
  return issues;
}

export function executorMcpFailureDetails(executor: ExecutorConfig): string[] {
  if (executor.runtime_metadata.legacy_metadata) {
    return [];
  }
  return (executor.runtime_metadata.mcp_servers ?? [])
    .filter((server) => server.status !== 'ready')
    .map(formatMcpFailure);
}

export function executorDegradedDetails(executor: ExecutorConfig): string[] {
  return [
    ...executorDegradedIssues(executor).map((issue) =>
      issue.detail ? `${issue.source}: ${issue.title} · ${issue.detail}` : `${issue.source}: ${issue.title}`
    ),
    ...executorMcpFailureDetails(executor),
    ...(executor.runtime_metadata.warnings ?? []).map((warning) => `warning: ${warning}`)
  ];
}

export function degradedExecutors(executors: ExecutorConfig[]): ExecutorConfig[] {
  return executors.filter(
    (executor) =>
      executor.status === 'active' &&
      (executor.runtime_state === 'degraded'
        || executor.runtime_state === 'blocked'
        || executor.runtime_state === 'stale'
        || executor.runtime_state === 'reconfiguring')
  );
}

export function validateStdioCommand(command: string): string | null {
  const trimmed = command.trim();
  if (!trimmed) {
    return 'Command is required for stdio MCP servers.';
  }
  if (/\s/.test(trimmed)) {
    return 'Command must be only the executable name or path. Put flags and package names into Arguments.';
  }
  return null;
}
