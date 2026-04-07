import type { ExecutorConfig } from '$lib/types/api';

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
  const failed = (executor.runtime_metadata.mcp_servers ?? []).filter((server) => server.status !== 'ready');
  if (failed.length > 0) {
    return `${failed.length} MCP server(s) degraded: ${failed.map((server) => server.name).join(', ')}`;
  }
  const warnings = executor.runtime_metadata.warnings ?? [];
  return warnings[0] ?? null;
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
