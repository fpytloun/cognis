<script lang="ts">
  import { goto } from '$app/navigation';
  import AlertTriangle from 'lucide-svelte/icons/alert-triangle';

  import Card from '$lib/components/ui/Card.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { api, asApiError } from '$lib/api/client';
  import { summarizeDashboardIssues } from '$lib/dashboard/dashboard';
  import type { DashboardIssue, DashboardIssuesResponse } from '$lib/types/api';

  let {
    issues,
    loading = false,
    error = null,
    onRetry = () => undefined,
    onDismiss = () => undefined,
  } = $props<{
    issues: DashboardIssuesResponse | null;
    loading?: boolean;
    error?: string | null;
    onRetry?: () => void;
    onDismiss?: () => void | Promise<void>;
  }>();
  let dismissing = $state<string | null>(null);
  let leavingIssue = $state<string | null>(null);
  let dismissError = $state<string | null>(null);

  const summary = $derived(summarizeDashboardIssues(issues));
  const actionableIssues = $derived(issues?.issues ?? []);
  const toneClasses: Record<string, string> = {
    healthy: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100',
    info: 'border-sky-500/30 bg-sky-500/10 text-sky-100',
    warning: 'border-amber-500/30 bg-amber-500/10 text-amber-100',
    critical: 'border-rose-500/30 bg-rose-500/10 text-rose-100'
  };
  const severityDot: Record<string, string> = {
    critical: 'bg-rose-400',
    warning: 'bg-amber-400',
    info: 'bg-sky-400'
  };

  function openAction(issue: DashboardIssue): void {
    void goto(issue.action_url);
  }

  async function dismiss(issue: DashboardIssue): Promise<void> {
    if (issue.resource.type !== 'schedule' || !issue.dismiss_token) return;
    dismissing = issue.id;
    dismissError = null;
    try {
      await api.dashboard.dismissScheduleIssue(issue.resource.id, issue.dismiss_token);
      leavingIssue = issue.id;
      if (!(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false)) {
        await new Promise((resolve) => window.setTimeout(resolve, 140));
      }
      if (issues) {
        const remaining = issues.issues.filter((item: DashboardIssue) => item.id !== issue.id);
        issues = {
          ...issues,
          issues: remaining,
          summary: {
            ...issues.summary,
            total: Math.max(0, issues.summary.total - 1),
            [issue.severity]: Math.max(0, issues.summary[issue.severity] - 1)
          }
        };
      }
      await onDismiss();
    } catch (caught) {
      dismissError = asApiError(caught).message || 'Could not dismiss the issue.';
    } finally {
      dismissing = null;
      leavingIssue = null;
    }
  }
</script>

{#if error || actionableIssues.length > 0}
<div class="min-w-0 max-w-full" data-testid="dashboard-issues-strip">
  {#if error}
    <div data-testid="dashboard-issues-error">
      <Card class="border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-100">
        <p>Could not load system issues: {error}</p>
        <Button class="mt-3" size="sm" variant="secondary" onclick={onRetry}>Try again</Button>
      </Card>
    </div>
  {/if}
  {#if dismissError}
    <p class="mb-2 text-sm text-rose-200" role="alert">{dismissError}</p>
  {/if}
  {#if actionableIssues.length > 0}
    <div class="min-w-0 max-w-full" data-testid="dashboard-issues-needs-attention">
      <Card class={`min-w-0 max-w-full border p-4 ${toneClasses[summary.tone]} md:flex md:max-h-40 md:min-h-0 md:flex-col md:overflow-hidden`}>
        <div class="flex min-w-0 shrink-0 items-start gap-2 text-sm font-semibold">
          <AlertTriangle class="h-4 w-4 shrink-0" />
          <span class="min-w-0 break-words [overflow-wrap:anywhere]" data-testid="dashboard-issues-headline">{summary.headline}</span>
        </div>
        <ul
          class="mt-3 grid min-w-0 max-w-full gap-2 sm:grid-cols-2 md:min-h-0 md:flex-1 md:auto-rows-min md:overflow-y-auto"
          data-testid="dashboard-issues-list"
          aria-label="System issues needing attention"
        >
          {#each actionableIssues as issue (issue.id)}
            <li
              class="dashboard-issue-row flex min-w-0 max-w-full flex-col items-stretch gap-2 rounded-xl bg-slate-950/40 px-3 py-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3"
              data-leaving={leavingIssue === issue.id}
              data-testid={`dashboard-issue-${issue.id}`}
            >
              <div class="flex min-w-0 max-w-full items-start gap-2">
                <span class={`h-2 w-2 shrink-0 rounded-full ${severityDot[issue.severity] ?? 'bg-slate-400'}`} aria-hidden="true"></span>
                <div class="min-w-0 max-w-full">
                  <p class="break-words text-sm font-medium [overflow-wrap:anywhere]">{issue.title}</p>
                  <p class="break-words text-xs opacity-80 [overflow-wrap:anywhere]">{issue.detail}</p>
                </div>
              </div>
              <div class="flex min-w-0 flex-wrap items-center justify-end gap-2 sm:shrink-0">
                {#if issue.dismiss_token && issue.resource.type === 'schedule'}
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={dismissing === issue.id}
                    aria-label={`Dismiss ${issue.title}`}
                    data-testid={`dashboard-issue-dismiss-${issue.id}`}
                    onclick={() => void dismiss(issue)}
                  >
                    Dismiss
                  </Button>
                {/if}
                <Button size="sm" variant="secondary" data-testid={`dashboard-issue-action-${issue.id}`} onclick={() => openAction(issue)}>
                  {issue.action_label ?? 'View'}
                </Button>
              </div>
            </li>
          {/each}
        </ul>
      </Card>
    </div>
  {/if}
</div>
{/if}

<style>
  @media (prefers-reduced-motion: no-preference) {
    .dashboard-issue-row {
      animation: dashboard-issue-enter 220ms cubic-bezier(0.2, 0.8, 0.2, 1);
      transition: opacity 200ms ease, transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1);
    }

    .dashboard-issue-row[data-leaving='true'] {
      opacity: 0;
      transform: translateY(-8px) scale(0.97);
    }
  }

  @keyframes dashboard-issue-enter {
    from {
      opacity: 0;
      transform: translateY(-8px) scale(0.97);
    }
  }
</style>
