import type { ExecutorConfig, ExecutorMCPServerRuntimeStatus, ExecutorRuntimeIssue } from '$lib/types/api';

export interface ExecutorDegradedIssue {
  source: string;
  title: string;
  detail: string | null;
}

export type ExecutorHealthState = 'healthy' | 'pressure' | 'critical' | 'offline' | 'stale' | 'unknown';

export interface ExecutorHealth {
  state: ExecutorHealthState;
  label: string;
  detail: string;
}

export function providerInferenceExecutors(
  executors: ExecutorConfig[],
  selectedExecutorId = ''
): ExecutorConfig[] {
  return executors.filter(
    (executor) =>
      executor.executor_type === 'websocket'
      && (executor.local_inference_enabled || executor.executor_id === selectedExecutorId)
  );
}

export function providerSelectorCapabilityWarning(
  executors: ExecutorConfig[],
  executorId: string,
  labels: Record<string, string> | null
): string | null {
  if (executorId) {
    const executor = executors.find((item) => item.executor_id === executorId);
    if (!executor || executor.local_inference_enabled) return null;
    return 'This saved executor reference has local inference disabled. It remains editable, but new requests will not route there.';
  }
  if (!labels) return null;
  const matches = executors.filter(
    (executor) =>
      executor.executor_type === 'websocket'
      && Object.entries(labels).every(([key, value]) => executor.labels?.[key] === value)
  );
  if (matches.length > 0 && matches.every((executor) => !executor.local_inference_enabled)) {
    return 'This saved label selector matches only executors with local inference disabled. It remains editable, but new requests will not route there.';
  }
  return null;
}

interface PressureSignal {
  label: string;
  percent: number;
  criticalAt: number;
  pressureAt: number;
}

export function executorHealth(executor: ExecutorConfig): ExecutorHealth {
  if (executor.status !== 'active') {
    return { state: 'unknown', label: 'Disabled', detail: 'This executor is not enabled.' };
  }
  if (executor.runtime_state === 'offline') {
    return { state: 'offline', label: 'Offline', detail: 'The executor is not connected.' };
  }
  if (executor.runtime_state === 'blocked') {
    return { state: 'critical', label: 'Blocked', detail: 'The executor could not apply its configuration.' };
  }
  if (executor.runtime_state === 'stale' || executor.runtime_state === 'reconfiguring') {
    return {
      state: 'stale',
      label: executor.runtime_state === 'reconfiguring' ? 'Updating' : 'Stale configuration',
      detail: 'The executor is waiting for its current configuration.'
    };
  }
  const snapshot = executor.resource_snapshot;
  if (!snapshot) {
    if (executor.runtime_state === 'degraded') {
      return {
        state: 'pressure',
        label: 'Needs attention',
        detail: 'One or more executor services are degraded.'
      };
    }
    return {
      state: 'unknown',
      label: 'Resource data unavailable',
      detail: 'Connected; current resource details are not available.'
    };
  }
  if (snapshot.freshness?.stale) {
    return {
      state: 'stale',
      label: 'Stale data',
      detail: `The latest resource update is ${formatDuration(snapshot.freshness.age_seconds)} old.`
    };
  }

  const signals = executorPressureSignals(executor);
  const critical = signals.find((signal) => signal.percent >= signal.criticalAt);
  if (critical) {
    return {
      state: 'critical',
      label: 'Critical pressure',
      detail: `${critical.label} is at ${Math.round(critical.percent)}%.`
    };
  }
  const pressure = signals.find((signal) => signal.percent >= signal.pressureAt);
  if (pressure || executor.runtime_state === 'degraded') {
    return {
      state: 'pressure',
      label: 'Needs attention',
      detail: pressure
        ? `${pressure.label} is at ${Math.round(pressure.percent)}%.`
        : 'One or more executor services are degraded.'
    };
  }
  return { state: 'healthy', label: 'Healthy', detail: 'Connected and reporting current resources.' };
}

export function executorPressureSignals(executor: ExecutorConfig): PressureSignal[] {
  const snapshot = executor.resource_snapshot;
  if (!snapshot) return [];
  const signals: PressureSignal[] = [];
  addSignal(signals, 'CPU', snapshot.cpu?.utilization_percent, 95, 80);
  addSignal(
    signals,
    snapshot.memory?.unified ? 'Unified memory' : 'Memory',
    percentUsed(snapshot.memory?.used_bytes, snapshot.memory?.total_bytes),
    95,
    85
  );
  for (const accelerator of snapshot.accelerators ?? []) {
    addSignal(signals, accelerator.name ?? 'GPU', accelerator.utilization_percent, 95, 90);
    addSignal(
      signals,
      `${accelerator.name ?? 'GPU'} memory`,
      percentUsed(accelerator.used_memory_bytes, accelerator.total_memory_bytes),
      95,
      90
    );
  }
  addSignal(
    signals,
    'Model storage',
    percentUsed(
      snapshot.ollama_model_store?.total_bytes != null && snapshot.ollama_model_store.free_bytes != null
        ? snapshot.ollama_model_store.total_bytes - snapshot.ollama_model_store.free_bytes
        : null,
      snapshot.ollama_model_store?.total_bytes
    ),
    95,
    90
  );
  return signals.sort((left, right) => right.percent - left.percent);
}

export function executorRuntimeBadgeStatus(executor: ExecutorConfig): 'healthy' | 'degraded' | 'unhealthy' {
  const state = executorHealth(executor).state;
  if (state === 'healthy') return 'healthy';
  if (state === 'critical' || state === 'offline') return 'unhealthy';
  return 'degraded';
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
  const observedAt = executor.last_observed_at ?? executor.resource_snapshot?.observed_at;
  if (!observedAt) return null;
  return `Last updated ${new Date(observedAt).toLocaleString()}`;
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

export function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return 'Not reported';
  if (value === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return 'unknown';
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export function percentUsed(used: number | null | undefined, total: number | null | undefined): number | null {
  if (used == null || total == null || total <= 0 || used < 0) return null;
  return Math.max(0, Math.min(100, (used / total) * 100));
}

function addSignal(
  signals: PressureSignal[],
  label: string,
  percent: number | null | undefined,
  criticalAt: number,
  pressureAt: number
): void {
  if (percent == null || !Number.isFinite(percent)) return;
  signals.push({ label, percent, criticalAt, pressureAt });
}
