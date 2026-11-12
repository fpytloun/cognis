<script lang="ts">
  import { goto } from '$app/navigation';

  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { useSectionConnectivity } from '$lib/connectivity-section.svelte';
  import DashboardModalShell from '$lib/components/dashboard/DashboardModalShell.svelte';
  import WorkflowPlanPreview from '$lib/components/workflows/WorkflowPlanPreview.svelte';
  import { api, asApiError } from '$lib/api/client';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import { auth } from '$lib/stores/auth';
  import type { Agent, Schedule, Workflow } from '$lib/types/api';

  let {
    scheduleId,
    agents = [],
    modalRefreshToken = 0,
    embedded = false,
    onClose,
    onMetadataChange,
  } = $props<{
    scheduleId: string;
    agents?: Agent[];
    modalRefreshToken?: number;
    embedded?: boolean;
    onClose: () => void;
    onMetadataChange?: (metadata: {
      title: string;
      status: string | null;
      agentId: string | null;
    }) => void;
  }>();

  let schedule = $state<Schedule | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let refreshing = $state(false);
  let mutating = $state(false);
  let refreshError = $state<string | null>(null);
  // Hidden while the global connection banner applies; reload on restore.
  const net = useSectionConnectivity({ hasError: () => Boolean(error || refreshError), retry: () => load() });
  const visibleRefreshError = $derived(net.hideErrors ? null : refreshError);
  let activeTab = $state<'description' | 'steps'>('description');
  let workflow = $state<Workflow | null>(null);
  let workflowLoading = $state(false);
  let workflowError = $state<string | null>(null);
  let loadedWorkflowId = '';
  let loadRequest = 0;
  let workflowRequest = 0;
  let boundScheduleId = '';
  let seenRefreshToken = $state<number | null>(null);

  const tabs = [
    { id: 'description', label: 'Description' },
    { id: 'steps', label: 'Planned steps' }
  ];
  const agent = $derived(agents.find((item: Agent) => item.agent_id === schedule?.agent_id) ?? null);
  const templateTitle = $derived(
    typeof schedule?.task_template.title === 'string'
      ? schedule.task_template.title
      : schedule?.name ?? 'Schedule',
  );
  const templateDescription = $derived(
    typeof schedule?.task_template.description === 'string'
      ? schedule.task_template.description
      : schedule?.description
  );

  async function toggleEnabled(): Promise<void> {
    if (!schedule || mutating || $auth.user?.role === 'viewer') return;
    mutating = true;
    try {
      if (schedule.enabled) await api.schedules.disable(scheduleId);
      else await api.schedules.enable(scheduleId);
      await load();
    } catch (caught) { refreshError = asApiError(caught).message; }
    finally { mutating = false; }
  }

  /** The schedule's own state; the last run's outcome is shown separately. */
  function activityStatus(detail: Schedule): string {
    if (!detail.enabled) return 'disabled';
    if (detail.consecutive_errors > 0) return 'failed';
    return 'scheduled';
  }

  async function load(): Promise<void> {
    const request = ++loadRequest;
    const requestId = scheduleId;
    const blocking = schedule === null;
    if (blocking) {
      loading = true;
      error = null;
    } else {
      refreshing = true;
      refreshError = null;
    }
    try {
      const detail = await api.schedules.detail(requestId);
      if (request !== loadRequest || requestId !== scheduleId) return;
      const previousWorkflowId = schedule?.workflow_id ?? null;
      schedule = detail;
      onMetadataChange?.({
        title: detail.name,
        status: activityStatus(detail),
        agentId: detail.agent_id,
      });
      if (detail.workflow_id !== previousWorkflowId) {
        workflowRequest += 1;
        workflow = null;
        loadedWorkflowId = '';
        workflowLoading = false;
        workflowError = null;
        if (activeTab === 'steps') void loadWorkflow();
      }
    } catch (caught) {
      if (request !== loadRequest || requestId !== scheduleId) return;
      const message = asApiError(caught).message || 'Could not load this schedule.';
      if (blocking) error = message;
      else refreshError = message;
    } finally {
      if (request !== loadRequest || requestId !== scheduleId) return;
      if (blocking) loading = false;
      else refreshing = false;
    }
  }

  async function loadWorkflow(): Promise<void> {
    const workflowId = schedule?.workflow_id;
    if (!workflowId || workflowId === loadedWorkflowId) return;
    const request = ++workflowRequest;
    const requestScheduleId = scheduleId;
    workflowLoading = true;
    workflowError = null;
    try {
      const detail = await api.workflows.detail(workflowId);
      if (request !== workflowRequest || requestScheduleId !== scheduleId) return;
      workflow = detail;
      loadedWorkflowId = workflowId;
    } catch (caught) {
      if (request !== workflowRequest || requestScheduleId !== scheduleId) return;
      workflowError = asApiError(caught).message || 'Could not load the planned workflow.';
    } finally {
      if (request === workflowRequest && requestScheduleId === scheduleId) workflowLoading = false;
    }
  }

  function changeTab(id: string): void {
    activeTab = id as 'description' | 'steps';
    if (activeTab === 'steps') void loadWorkflow();
  }

  function openCanonical(event: MouseEvent): void {
    event.stopPropagation();
    if (!embedded) onClose();
    void goto(`/schedules/${scheduleId}`);
  }

  $effect(() => {
    if (scheduleId === boundScheduleId) return;
    boundScheduleId = scheduleId;
    workflowRequest += 1;
    schedule = null;
    workflow = null;
    loadedWorkflowId = '';
    activeTab = 'description';
    void load();
  });

  $effect(() => {
    const token = modalRefreshToken;
    if (seenRefreshToken === null) {
      seenRefreshToken = token;
      return;
    }
    if (token === seenRefreshToken) return;
    seenRefreshToken = token;
    void load();
  });
</script>

<DashboardModalShell
  title={schedule?.name ?? 'Schedule'}
  status={schedule ? (schedule.enabled ? 'Scheduled' : 'Disabled') : null}
  externalLabel="Open schedule"
  onExternal={openCanonical}
  {onClose}
  testId="dashboard-schedule-modal"
  {embedded}
>
  {#if !loading && (refreshing || visibleRefreshError)}
    <div class={`shrink-0 border-b px-4 py-1.5 text-xs ${visibleRefreshError ? 'border-warning-border bg-warning-subtle text-warning' : 'border-separator text-quiet'}`}>
      {visibleRefreshError ? `Could not refresh: ${visibleRefreshError}` : 'Refreshing…'}
    </div>
  {/if}
  {#if loading || (error && net.hideErrors)}
    <p class="p-6 text-center text-sm text-muted">Loading…</p>
  {:else if error}
    <div class="m-4 rounded-xl border border-danger-border bg-danger-subtle p-4 text-sm text-danger" role="alert">
      <p>{error}</p>
      <Button class="mt-3" size="sm" variant="secondary" onclick={() => void load()}>Try again</Button>
    </div>
  {:else if schedule}
    <AccessibleTabs
      {tabs}
      activeId={activeTab}
      idPrefix="dashboard-schedule-modal"
      ariaLabel="Upcoming schedule"
      testIdPrefix="dashboard-schedule-modal-tab"
      onChange={changeTab}
    />
    <div class="min-h-0 flex-1 overflow-y-auto overscroll-contain p-4 sm:p-6" data-testid={`dashboard-schedule-modal-panel-${activeTab}`}>
    {#if activeTab === 'description'}
      <div class="space-y-5">
        <div class="flex items-center gap-3">
          <AgentAvatar name={agent?.display_name ?? agent?.name ?? schedule.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-10 w-10" />
          <div class="min-w-0 flex-1">
            <p class="truncate font-medium text-strong">{agent?.display_name ?? agent?.name ?? schedule.agent_id}</p>
            <p class="text-xs text-muted">{schedule.human_schedule ?? schedule.cron_expr ?? schedule.schedule_type} · {schedule.timezone}</p>
          </div>
        </div>
        <dl class="grid gap-4 rounded-2xl border border-separator p-4 text-xs sm:grid-cols-2">
          <div><dt class="text-quiet">Next run</dt><dd class="mt-1 text-foreground">{schedule.next_fire_at ? `${formatAbsoluteTime(schedule.next_fire_at)} (${formatRelativeTime(schedule.next_fire_at)})` : 'Not scheduled'}</dd></div>
          <div data-testid="dashboard-schedule-last-run"><dt class="text-quiet">Last run</dt><dd class="mt-1 flex flex-wrap items-center gap-1.5 text-foreground">{#if schedule.last_run_status}<StatusBadge status={schedule.last_run_status === 'success' ? 'completed' : schedule.last_run_status} />{#if schedule.last_fired_at}<span>{formatRelativeTime(schedule.last_fired_at)}</span>{/if}{:else}Not run yet{/if}</dd></div>
          <div><dt class="text-quiet">Workflow</dt><dd class="mt-1 text-foreground">{workflow?.name ?? (schedule.workflow_id ? 'See planned steps' : 'Ad-hoc task')}</dd></div>
          {#if schedule.project_id}
            <div><dt class="text-quiet">Project</dt><dd class="mt-1"><a class="text-accent hover:underline" href={`/projects/${schedule.project_id}`}>Open project</a></dd></div>
          {/if}
          {#if schedule.skill_id}
            <div><dt class="text-quiet">Skill</dt><dd class="mt-1 break-all text-foreground">{schedule.skill_id}</dd></div>
          {/if}
        </dl>
        <section class="rounded-2xl border border-separator bg-surface/60 p-4">
          <h3 class="font-medium text-strong">{templateTitle}</h3>
          <p class="mt-2 line-clamp-[12] whitespace-pre-wrap text-sm leading-6 text-secondary">{templateDescription ?? 'No task description was provided.'}</p>
        </section>
        {#if $auth.user && $auth.user.role !== 'viewer'}
          <Button size="sm" variant="secondary" disabled={mutating} onclick={() => void toggleEnabled()}>
            {schedule.enabled ? 'Disable schedule' : 'Enable schedule'}
          </Button>
        {/if}
      </div>
    {:else if !schedule.workflow_id}
      <div class="rounded-2xl border border-dashed border-control-border p-8 text-center" data-testid="dashboard-schedule-ad-hoc">
        <p class="font-medium text-foreground">Ad-hoc task</p>
        <p class="mt-1 text-sm text-muted">This schedule creates a task without a workflow.</p>
      </div>
    {:else if workflowLoading}
      <p class="text-sm text-muted">Loading planned steps…</p>
    {:else if workflowError}
      <div class="rounded-xl border border-warning-border bg-warning-subtle p-3 text-sm text-warning" role="alert">
        <p>{workflowError}</p>
        <Button class="mt-3" size="sm" variant="secondary" onclick={() => void loadWorkflow()}>Try again</Button>
      </div>
    {:else if workflow}
      <WorkflowPlanPreview
        {workflow}
        taskDescription={templateDescription ?? ''}
        context={{
          interactionMode: schedule.interaction_mode_override ?? 'none',
          allowSilentCompletion: schedule.allow_silent_completion,
          deliveryMode: typeof schedule.task_template.delivery === 'object' && schedule.task_template.delivery !== null
            && 'mode' in schedule.task_template.delivery && typeof schedule.task_template.delivery.mode === 'string'
              ? schedule.task_template.delivery.mode : 'preferred_channel',
        }}
      />
    {/if}
    </div>
  {/if}
</DashboardModalShell>
