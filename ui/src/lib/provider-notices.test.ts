import { describe, expect, it } from 'vitest';

import {
  gettingStartedIssue,
  mergeDashboardIssues,
  providerNoticeOwnsSocketError,
  providerNotices,
  providerNoticeIssues,
} from '$lib/provider-notices';
import type { HealthResponse } from '$lib/types/api';

function health(providers: HealthResponse['providers']): HealthResponse {
  return { status: 'degraded', providers };
}

describe('provider notices', () => {
  it('identifies services, explains impact, and includes the provider reason', () => {
    const current = health({
      memory: {
        name: 'memory',
        status: 'unavailable',
        error: 'Connection refused',
      },
      guardrails: {
        name: 'guardrails',
        status: 'unavailable',
        details: { reason: 'Circuit breaker is open' },
      },
    });

    expect(providerNotices(current)).toEqual([
      expect.objectContaining({
        service: 'Mnemory',
        title: 'Mnemory unavailable',
        reason: 'Connection refused',
      }),
      expect.objectContaining({
        service: 'Intaris',
        title: 'Intaris unavailable',
        detail: 'Tool execution is blocked until Intaris recovers.',
        reason: 'Circuit breaker is open',
      }),
    ]);
    expect(providerNoticeIssues(current)[1]?.detail).toContain('Circuit breaker is open');
  });

  it('automatically produces no notice after services recover', () => {
    expect(providerNotices(health({
      memory: { name: 'memory', status: 'healthy' },
      guardrails: { name: 'guardrails', status: 'healthy' },
      llm: { name: 'llm', status: 'healthy' },
    }))).toEqual([]);
  });

  it('suppresses only socket errors represented by the current service notice', () => {
    const unavailableLlm = health({
      llm: { name: 'llm', status: 'unavailable', error: 'Connection refused' },
    });
    const healthyLlm = health({
      llm: { name: 'llm', status: 'healthy' },
    });

    expect(providerNoticeOwnsSocketError('provider_unreachable:guardrails', null)).toBe(true);
    expect(providerNoticeOwnsSocketError('provider_unreachable:memory', null)).toBe(true);
    expect(providerNoticeOwnsSocketError('provider_not_configured:llm', unavailableLlm)).toBe(true);
    expect(providerNoticeOwnsSocketError('provider_error:llm', unavailableLlm)).toBe(true);
    expect(providerNoticeOwnsSocketError('provider_error:llm', healthyLlm)).toBe(false);
  });

  it('merges service and quickstart state into regular dashboard notifications', () => {
    const merged = mergeDashboardIssues(null, [
      ...providerNoticeIssues(health({
        guardrails: { name: 'guardrails', status: 'unavailable' },
      })),
      ...gettingStartedIssue(true),
    ]);

    expect(merged?.issues.map((issue) => issue.id)).toEqual([
      'provider-guardrails',
      'getting-started',
    ]);
    expect(merged?.summary).toMatchObject({ total: 2, critical: 1, info: 1 });
  });
});
