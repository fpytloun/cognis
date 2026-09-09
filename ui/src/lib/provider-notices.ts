import type {
  DashboardIssue,
  DashboardIssuesResponse,
  HealthResponse,
  ProviderHealth,
} from '$lib/types/api';

export interface ProviderNotice {
  id: 'memory' | 'guardrails' | 'llm';
  service: 'Mnemory' | 'Intaris' | 'LLM';
  severity: 'warning' | 'critical';
  title: string;
  detail: string;
  reason: string | null;
  actionUrl: string;
  actionLabel: string;
}

function providerReason(provider: ProviderHealth | undefined): string | null {
  const candidates = [
    provider?.error,
    provider?.details?.reason,
    provider?.details?.error,
    provider?.details?.message,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
  }
  return null;
}

function unavailable(provider: ProviderHealth | undefined): boolean {
  const status = String(provider?.status ?? 'unknown');
  return status !== 'healthy' && status !== 'unknown';
}

export function providerNotices(health: HealthResponse | null): ProviderNotice[] {
  if (!health) return [];
  const notices: ProviderNotice[] = [];
  const memory = health.providers?.memory;
  if (unavailable(memory)) {
    notices.push({
      id: 'memory',
      service: 'Mnemory',
      severity: 'warning',
      title: 'Mnemory unavailable',
      detail: 'Chat still works, but memory recall is unavailable for this conversation.',
      reason: providerReason(memory),
      actionUrl: '/settings?tab=system',
      actionLabel: 'Diagnostics',
    });
  }

  const guardrails = health.providers?.guardrails;
  if (unavailable(guardrails)) {
    notices.push({
      id: 'guardrails',
      service: 'Intaris',
      severity: 'critical',
      title: 'Intaris unavailable',
      detail: 'Tool execution is blocked until Intaris recovers.',
      reason: providerReason(guardrails),
      actionUrl: '/settings?tab=system',
      actionLabel: 'Diagnostics',
    });
  }

  const llm = health.providers?.llm;
  if (unavailable(llm)) {
    const details = JSON.stringify(llm ?? {}).toLowerCase();
    const notConfigured = details.includes('not configured') || details.includes('no llm model configured');
    notices.push({
      id: 'llm',
      service: 'LLM',
      severity: 'critical',
      title: notConfigured ? 'No LLM provider configured' : 'LLM provider unavailable',
      detail: notConfigured
        ? 'Configure an LLM provider before using chat and tasks.'
        : 'Chat and tasks are unavailable until the configured provider recovers.',
      reason: providerReason(llm),
      actionUrl: '/settings?tab=providers',
      actionLabel: 'Configure',
    });
  }
  return notices;
}

export function providerNoticeOwnsSocketError(
  code: string | undefined,
  health: HealthResponse | null,
): boolean {
  if (code === 'provider_unreachable:guardrails' || code === 'provider_unreachable:memory') {
    return true;
  }
  if (code !== 'provider_not_configured:llm' && code !== 'provider_error:llm') {
    return false;
  }
  return providerNotices(health).some((notice) => notice.id === 'llm');
}

export function providerNoticeIssues(health: HealthResponse | null): DashboardIssue[] {
  return providerNotices(health).map((notice) => ({
    id: `provider-${notice.id}`,
    severity: notice.severity,
    kind: 'provider_unavailable',
    title: notice.title,
    detail: notice.reason ? `${notice.detail} ${notice.reason}` : notice.detail,
    resource: {
      type: 'provider',
      id: notice.id,
      label: notice.service,
    },
    observed_at: null,
    action_url: notice.actionUrl,
    action_label: notice.actionLabel,
    dismiss_token: null,
  }));
}

export function gettingStartedIssue(visible: boolean): DashboardIssue[] {
  if (!visible) return [];
  return [{
    id: 'getting-started',
    severity: 'info',
    kind: 'getting_started',
    title: 'Finish first-run setup',
    detail: 'Cognis still needs providers, agents, or companion services before the workspace is fully ready.',
    resource: {
      type: 'setup',
      id: 'getting-started',
      label: 'Getting started',
    },
    observed_at: null,
    action_url: '/getting-started',
    action_label: 'Open guide',
    dismiss_token: null,
  }];
}

export function mergeDashboardIssues(
  response: DashboardIssuesResponse | null,
  additional: DashboardIssue[],
): DashboardIssuesResponse | null {
  if (!response && additional.length === 0) return null;
  const issues = [...additional, ...(response?.issues ?? [])];
  return {
    generated_at: response?.generated_at ?? new Date().toISOString(),
    issues,
    summary: {
      total: issues.length,
      critical: issues.filter((issue) => issue.severity === 'critical').length,
      warning: issues.filter((issue) => issue.severity === 'warning').length,
      info: issues.filter((issue) => issue.severity === 'info').length,
      truncated: response?.summary.truncated ?? false,
    },
  };
}
