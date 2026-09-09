import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { DashboardIssuesResponse } from '$lib/types/api';
import IssuesStrip from './IssuesStrip.svelte';

const { dismissScheduleIssue, goto } = vi.hoisted(() => ({
  dismissScheduleIssue: vi.fn(),
  goto: vi.fn(),
}));

vi.mock('$app/navigation', () => ({ goto }));
vi.mock('$lib/api/client', () => ({
  api: { dashboard: { dismissScheduleIssue } },
  asApiError: (error: unknown) => error instanceof Error ? error : new Error(String(error)),
}));

describe('IssuesStrip', () => {
  beforeEach(() => {
    goto.mockReset();
    dismissScheduleIssue.mockReset();
  });

  it('uses the backend action URL for issue actions', async () => {
    const issues: DashboardIssuesResponse = {
      generated_at: '2026-08-22T00:00:00Z',
      summary: { total: 1, critical: 0, warning: 1, info: 0, truncated: false },
      issues: [{
        id: 'executor-1',
        severity: 'warning',
        kind: 'executor_degraded',
        title: 'Executor is degraded',
        detail: 'The executor reported a degraded runtime state.',
        resource: { type: 'executor', id: 'executor-1', label: 'Executor one' },
        observed_at: '2026-08-22T00:00:00Z',
        action_url: '/settings/executors/executor-1'
      }]
    };

    render(IssuesStrip, { issues });
    await fireEvent.click(screen.getByTestId('dashboard-issue-action-executor-1'));

    expect(goto).toHaveBeenCalledWith('/settings/executors/executor-1');
    expect(screen.getByTestId('dashboard-issues-needs-attention')).toBeInTheDocument();
  });

  it('renders no dashboard space when healthy or loading', async () => {
    render(IssuesStrip, {
      issues: {
        generated_at: '2026-08-22T00:00:00Z',
        summary: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
        issues: []
      }
    });

    expect(screen.queryByTestId('dashboard-issues-strip')).not.toBeInTheDocument();
    expect(screen.queryByText('All systems healthy')).not.toBeInTheDocument();
  });

  it('retains regular warning and information notifications while showing a refresh error', () => {
    const issues: DashboardIssuesResponse = {
      generated_at: '2026-08-22T00:00:00Z',
      summary: { total: 2, critical: 0, warning: 1, info: 1, truncated: false },
      issues: [
        {
          id: 'warning-1', severity: 'warning', kind: 'executor_degraded', title: 'Warning',
          detail: 'Needs attention', resource: { type: 'executor', id: 'e1', label: 'E1' },
          observed_at: '2026-08-22T00:00:00Z', action_url: '/settings'
        },
        {
          id: 'info-1', severity: 'info', kind: 'tool_observation_stale', title: 'Information',
          detail: 'Not actionable', resource: { type: 'executor', id: 'e2', label: 'E2' },
          observed_at: '2026-08-22T00:00:00Z', action_url: '/settings'
        }
      ]
    };

    render(IssuesStrip, { issues, error: 'refresh unavailable' });
    expect(screen.getByTestId('dashboard-issues-error')).toHaveTextContent('refresh unavailable');
    expect(screen.getByTestId('dashboard-issue-warning-1')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-issue-info-1')).toBeInTheDocument();
  });

  it('animates and removes a dismissed schedule incident', async () => {
    dismissScheduleIssue.mockResolvedValue(undefined);
    const onDismiss = vi.fn();
    const issues: DashboardIssuesResponse = {
      generated_at: '2026-08-22T00:00:00Z',
      summary: { total: 1, critical: 0, warning: 1, info: 0, truncated: false },
      issues: [{
        id: 'schedule-1',
        severity: 'warning',
        kind: 'schedule_failed',
        title: 'Schedule failed',
        detail: 'The scheduled run failed.',
        resource: { type: 'schedule', id: 'schedule-1', label: 'Schedule one' },
        observed_at: '2026-08-22T00:00:00Z',
        action_url: '/schedules/schedule-1',
        action_label: 'View schedule',
        dismiss_token: 'incident-token',
      }],
    };

    render(IssuesStrip, { issues, onDismiss });
    await fireEvent.click(screen.getByTestId('dashboard-issue-dismiss-schedule-1'));

    await waitFor(() => {
      expect(screen.getByTestId('dashboard-issue-schedule-1')).toHaveAttribute('data-leaving', 'true');
    });
    await waitFor(() => {
      expect(screen.queryByTestId('dashboard-issue-schedule-1')).not.toBeInTheDocument();
    });
    expect(dismissScheduleIssue).toHaveBeenCalledWith('schedule-1', 'incident-token');
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it('exposes the issue rows as a labeled, boundable list region', () => {
    const issues: DashboardIssuesResponse = {
      generated_at: '2026-08-22T00:00:00Z',
      summary: { total: 1, critical: 0, warning: 1, info: 0, truncated: false },
      issues: [{
        id: 'executor-1',
        severity: 'warning',
        kind: 'executor_degraded',
        title: 'Executor is degraded',
        detail: 'The executor reported a degraded runtime state.',
        resource: { type: 'executor', id: 'executor-1', label: 'Executor one' },
        observed_at: '2026-08-22T00:00:00Z',
        action_url: '/settings/executors/executor-1'
      }]
    };

    render(IssuesStrip, { issues });
    const list = screen.getByTestId('dashboard-issues-list');
    expect(list).toHaveAccessibleName('System issues needing attention');
    expect(list.tagName).toBe('UL');
    expect(list).toContainElement(screen.getByTestId('dashboard-issue-executor-1'));
  });

  it('wraps long issue content and keeps mobile actions reachable', () => {
    const issues: DashboardIssuesResponse = {
      generated_at: '2026-08-22T00:00:00Z',
      summary: { total: 1, critical: 1, warning: 0, info: 0, truncated: false },
      issues: [{
        id: 'schedule-release',
        severity: 'critical',
        kind: 'schedule_failed',
        title: 'Weekly-Cognis-public-GitHub-release-with-an-uninterrupted-identifier-failed-automatically',
        detail: 'https://internal.example.invalid/releases/a-very-long-unbroken-diagnostic-token-that-must-wrap',
        resource: { type: 'schedule', id: 'schedule-release', label: 'Weekly release' },
        observed_at: '2026-08-22T00:00:00Z',
        action_url: '/schedules/schedule-release',
        action_label: 'View schedule',
        dismiss_token: 'incident-token',
      }],
    };

    render(IssuesStrip, { issues });

    const row = screen.getByTestId('dashboard-issue-schedule-release');
    expect(row).toHaveClass('min-w-0', 'max-w-full', 'flex-col');
    expect(screen.getByText(issues.issues[0].title)).toHaveClass('[overflow-wrap:anywhere]');
    expect(screen.getByText(issues.issues[0].detail)).toHaveClass('[overflow-wrap:anywhere]');
    expect(screen.getByTestId('dashboard-issue-dismiss-schedule-release')).toBeVisible();
    expect(screen.getByTestId('dashboard-issue-action-schedule-release')).toBeVisible();
  });
});
