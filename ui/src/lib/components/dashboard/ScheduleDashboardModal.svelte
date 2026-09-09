<script lang="ts">
  import { goto } from '$app/navigation';

  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import DashboardModalShell from '$lib/components/dashboard/DashboardModalShell.svelte';
  import { api, asApiError } from '$lib/api/client';
  import { formatAbsoluteTime } from '$lib/time';
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
  let refreshError = $state<string | null>(null);
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

  function activityStatus(detail: Schedule): string | null {
    if (detail.consecutive_errors > 0 || detail.last_run_status === 'failed') return 'failed';
    if (detail.last_run_status === 'running') return 'running';
    return detail.enabled ? detail.last_run_status : 'disabled';
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
  {#if !loading && (refreshing || refreshError)}
    <div class={`shrink-0 border-b px-4 py-1.5 text-xs ${refreshError ? 'border-amber-500/20 bg-amber-500/5 text-amber-200' : 'border-slate-800 text-slate-500'}`}>
      {refreshError ? `Could not refresh: ${refreshError}` : 'Refreshing…'}
    </div>
  {/if}
  {#if loading}
    <p class="p-6 text-center text-sm text-slate-400">Loading…</p>
  {:else if error}
    <div class="m-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-100" role="alert">
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
          <div class="min-w-0">
            <p class="truncate font-medium text-white">{agent?.display_name ?? agent?.name ?? schedule.agent_id}</p>
            <p class="text-xs text-slate-400">{schedule.human_schedule ?? schedule.schedule_type}</p>
          </div>
        </div>
        <section class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
          <h3 class="font-medium text-white">{templateTitle}</h3>
          <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-300">{templateDescription ?? 'No task description was provided.'}</p>
        </section>
        <dl class="grid gap-4 rounded-2xl border border-slate-800 p-4 text-xs sm:grid-cols-2">
          <div><dt class="text-slate-500">Next run</dt><dd class="mt-1 text-slate-200">{schedule.next_fire_at ? formatAbsoluteTime(schedule.next_fire_at) : 'Not scheduled'}</dd></div>
          <div><dt class="text-slate-500">Workflow</dt><dd class="mt-1 break-all text-slate-200">{schedule.workflow_id ?? 'Ad hoc'}</dd></div>
          <div><dt class="text-slate-500">Project</dt><dd class="mt-1 break-all text-slate-200">{schedule.project_id ?? 'None'}</dd></div>
          <div><dt class="text-slate-500">Skill</dt><dd class="mt-1 break-all text-slate-200">{schedule.skill_id ?? 'None'}</dd></div>
        </dl>
      </div>
    {:else if !schedule.workflow_id}
      <div class="rounded-2xl border border-dashed border-slate-700 p-8 text-center" data-testid="dashboard-schedule-ad-hoc">
        <p class="font-medium text-slate-200">Ad-hoc task</p>
        <p class="mt-1 text-sm text-slate-400">This schedule creates a task without a workflow.</p>
      </div>
    {:else if workflowLoading}
      <p class="text-sm text-slate-400">Loading planned steps…</p>
    {:else if workflowError}
      <div class="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100" role="alert">
        <p>{workflowError}</p>
        <Button class="mt-3" size="sm" variant="secondary" onclick={() => void loadWorkflow()}>Try again</Button>
      </div>
    {:else if workflow}
      <ol class="space-y-2" data-testid="dashboard-schedule-planned-steps">
        {#each workflow.steps as step, index (step.name)}
          <li class="flex gap-3 rounded-xl border border-slate-800 bg-slate-900/50 p-3">
            <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-800 text-xs text-slate-300">{index + 1}</span>
            <div class="min-w-0">
              <p class="font-medium text-white">{step.name}</p>
              <p class="mt-1 text-sm text-slate-400">{step.description ?? step.objective ?? step.type}</p>
            </div>
          </li>
        {/each}
      </ol>
    {/if}
    </div>
  {/if}
</DashboardModalShell>
