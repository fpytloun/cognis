<script lang="ts">
  import { DEFAULT_INTARIS_EVALUATION } from '$lib/session-policy';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount, onDestroy, untrack } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import { COMPLETION_MODE_OPTIONS, completionModeLabel } from '$lib/task-delivery';
  import type { CompletionModeFamily } from '$lib/types/api';
  import {
    buildWorkflowSourceOptions,
    decodeWorkflowSourceValue
  } from '$lib/workflow-sources';
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import TaskFinishInstructions from '$lib/components/tasks/TaskFinishInstructions.svelte';
  import AgentProfileSelect from '$lib/components/AgentProfileSelect.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import SessionPolicyEditor from '$lib/components/SessionPolicyEditor.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import { normalizeSelectedAgentProfileId } from '$lib/agents';
  import { policyFromText } from '$lib/session-policy';
  import { confirmScheduleRunNow, matchesScheduleFilters, type ScheduleVisibilityFilter } from '$lib/schedules';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, Conversation, Project, Schedule, Skill, Workflow } from '$lib/types/api';
  import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
import Calendar from 'lucide-svelte/icons/calendar';
import Clock from 'lucide-svelte/icons/clock';
import Pause from 'lucide-svelte/icons/pause';
import Play from 'lucide-svelte/icons/play';
import Plus from 'lucide-svelte/icons/plus';
import RefreshCw from 'lucide-svelte/icons/refresh-cw';
import Timer from 'lucide-svelte/icons/timer';
import Trash2 from 'lucide-svelte/icons/trash-2';
import Zap from 'lucide-svelte/icons/zap';

  let { projectId = null, embedded = false, onOpenSchedule = (id: string) => goto(`/schedules/${id}`),
    onOpenTask = (id: string) => goto(`/tasks/${id}`), onChanged, canMutate = true } = $props<{
    projectId?: string | null;
    embedded?: boolean;
    onOpenSchedule?: (id: string) => void;
    onOpenTask?: (id: string) => void;
    onChanged?: () => void;
    canMutate?: boolean;
  }>();
  let alive = true;
  onDestroy(() => {
    alive = false;
    if (urlSyncTimer !== null) window.clearTimeout(urlSyncTimer);
  });

  let loading = $state(true);
  let error = $state('');
  let schedules = $state<Schedule[]>([]);
  let agents = $state<Agent[]>([]);
  let projectWorkflows = $state<Workflow[]>([]);
  let projects = $state<Project[]>([]);
  let skills = $state<Skill[]>([]);
  let conversations = $state<Conversation[]>([]);
  let search = $state('');
  let filterType = $state<string>('');
  let filterEnabled = $state<string>('');
  let filterProjectId = $state<string>('');
  let filterVisibility = $state<ScheduleVisibilityFilter>('active');
  let showCreateModal = $state(false);
  let createBaseline = $state('');
  $effect(() => {
    if (!showCreateModal) createBaseline = '';
    else if (!createBaseline) createBaseline = JSON.stringify(form);
  });
  let createOptionsLoading = $state(false);
  let createOptionsLoaded = $state(false);
  let createOptionsError = $state('');
  let workflowOptionsLoading = $state(false);
  let workflowOptionsError = $state('');
  let conversationsLoaded = false;
  let conversationsLoading = $state(false);
  let conversationsError = $state('');
  let creating = $state(false);
  let lastAutoProjectWorkflow = $state('');

  // Detect local IANA timezone
  const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  const timezoneOptions = [
    localTimezone,
    'UTC',
    'Europe/London',
    'Europe/Berlin',
    'Europe/Paris',
    'Europe/Prague',
    'Europe/Moscow',
    'America/New_York',
    'America/Chicago',
    'America/Denver',
    'America/Los_Angeles',
    'America/Sao_Paulo',
    'Asia/Tokyo',
    'Asia/Shanghai',
    'Asia/Kolkata',
    'Asia/Dubai',
    'Australia/Sydney',
    'Pacific/Auckland'
  ].filter((tz, i, arr) => arr.indexOf(tz) === i); // deduplicate if local matches one

  // Create form state
  let form = $state({
    name: '',
    description: '',
    schedule_type: 'cron',
    cron_expr: '0 9 * * *',
    interval_seconds: 1800,
    one_shot_at: '',
    timezone: localTimezone,
    agent_id: '',
    agent_profile_id: '',
    workflow_source: '',
    project_id: '',
    task_title: '',
    task_description: '',
    priority: 0,
    expected_output: '',
    delivery_mode: 'preferred_channel',
    delivery_target: '',
    completion_mode_family: 'auto' as CompletionModeFamily,
    allow_silent_completion: false,
    on_finish_instructions: '',
    interaction_mode_override: 'none' as 'none' | 'explicit_gates' | 'step_requests',
    allow_policy_text: '',
    intaris_evaluation: '' as '' | 'legacy' | 'strict' | 'balanced' | 'bounded_autonomy',
    deny_policy_text: ''
  });

  const typeIcons: Record<string, typeof Clock> = {
    cron: Calendar,
    interval: Timer,
    one_shot: Clock
  };

  const typeLabels: Record<string, string> = {
    cron: 'Cron',
    interval: 'Interval',
    one_shot: 'One-shot'
  };

  let filtered = $derived(
    schedules.filter((schedule) =>
      matchesScheduleFilters(schedule, {
        search,
        projectId: filterProjectId,
        scheduleType: filterType,
        enabled: filterEnabled,
        visibility: filterVisibility
      })
    )
  );

  let selectedAgent = $derived(agents.find((agent) => agent.agent_id === form.agent_id) ?? null);
  let workflowSourceOptions = $derived(buildWorkflowSourceOptions(projectWorkflows, skills, selectedAgent));
  let workflowLoadKey = 0;
  let scheduleLoadKey = 0;
  let urlHydrated = false;
  let urlSyncTimer: number | null = null;
  let lastLoadedProjectId: string | null = null;

  function hydrateProjectFilterFromUrl(): void {
    // Track only navigation inputs, not the selection this effect hydrates.
    filterProjectId = projectId ?? $page.url.searchParams.get('project_id') ?? $page.url.searchParams.get('project') ?? '';
  }

  function scheduleFilterUrlSync(): void {
    if (embedded || typeof window === 'undefined' || !urlHydrated) return;
    if (urlSyncTimer !== null) window.clearTimeout(urlSyncTimer);
    urlSyncTimer = window.setTimeout(() => {
      urlSyncTimer = null;
      const sp = new URLSearchParams($page.url.searchParams);
      if (filterProjectId) {
        sp.set('project_id', filterProjectId);
      } else {
        sp.delete('project_id');
        sp.delete('project');
      }
      const query = sp.toString();
      const next = query ? `/schedules?${query}` : '/schedules';
      const current = $page.url.pathname + $page.url.search;
      if (next !== current) {
        void goto(next, { replaceState: true, noScroll: true, keepFocus: true });
      }
    }, 200);
  }

  $effect(() => {
    if (projectId) form.project_id = projectId;
  });

  $effect(() => {
    void $page.url.search;
    hydrateProjectFilterFromUrl();
    urlHydrated = true;
  });

  $effect(() => {
    void filterProjectId;
    scheduleFilterUrlSync();
    if (urlHydrated && lastLoadedProjectId !== filterProjectId) {
      untrack(() => void loadData());
    }
  });

  async function loadData(): Promise<void> {
    const key = ++scheduleLoadKey;
    const requestedProjectId = filterProjectId;
    loading = true;
    error = '';
    try {
      const nextSchedules = await api.schedules.list({ project_id: requestedProjectId || null });
      if (!alive || key !== scheduleLoadKey) return;
      schedules = nextSchedules;
      onChanged?.();
      lastLoadedProjectId = requestedProjectId;
    } catch (e) {
      if (key !== scheduleLoadKey) return;
      error = asApiError(e).message;
    } finally {
      if (key === scheduleLoadKey) {
        loading = false;
      }
    }
  }

  async function loadCreateOptions(): Promise<void> {
    if (createOptionsLoading || createOptionsLoaded) return;
    createOptionsLoading = true;
    createOptionsError = '';
    try {
      const nextSkills = await api.skills.list();
      if (!alive) return;
      skills = nextSkills;
      createOptionsLoaded = true;
    } catch (e) {
      createOptionsError = asApiError(e).message;
    } finally {
      createOptionsLoading = false;
    }
  }

  $effect(() => {
    if (showCreateModal) untrack(() => void loadCreateOptions());
  });

  $effect(() => {
    if (showCreateModal && form.delivery_mode === 'specific_conversation') untrack(() => void loadConversations());
  });

  async function loadConversations(): Promise<void> {
    if (conversationsLoaded || conversationsLoading) return;
    conversationsLoading = true;
    conversationsError = '';
    try {
      const items = await api.conversations.listAll();
      if (alive) conversations = items;
      conversationsLoaded = true;
    } catch (e) {
      conversationsError = asApiError(e).message;
    } finally {
      conversationsLoading = false;
    }
  }

  async function loadWorkflowsForProject(projectId: string): Promise<void> {
    const key = ++workflowLoadKey;
    workflowOptionsLoading = true;
    workflowOptionsError = '';
    try {
      const next = await api.workflows.listAll({ project_id: projectId || null });
      if (key !== workflowLoadKey) return;
      projectWorkflows = next;
      const project = projects.find((item) => item.project_id === projectId);
      if (form.workflow_source && form.workflow_source === lastAutoProjectWorkflow) {
        form.workflow_source = '';
        lastAutoProjectWorkflow = '';
      }
      if (project?.default_workflow_id && !form.workflow_source) {
        lastAutoProjectWorkflow = `workflow:${project.default_workflow_id}`;
        form.workflow_source = lastAutoProjectWorkflow;
      }
      const values = new Set(buildWorkflowSourceOptions(projectWorkflows, skills, selectedAgent).map((option) => option.value));
      if (form.workflow_source && !values.has(form.workflow_source)) {
        form.workflow_source = '';
        lastAutoProjectWorkflow = '';
      }
    } catch {
      if (key === workflowLoadKey) workflowOptionsError = 'Unable to load workflow choices';
    } finally {
      if (key === workflowLoadKey) workflowOptionsLoading = false;
    }
  }

  async function handleCreate(): Promise<void> {
    if (!form.name.trim() || !form.agent_id) return;
    creating = true;
    try {
      const taskTemplate: Record<string, unknown> = {
        title: form.task_title || form.name,
        description: form.task_description,
        priority: form.priority,
        delivery: {
          mode: form.delivery_mode,
          target: form.delivery_mode === 'specific_conversation' ? form.delivery_target || null : null
        }
      };
      if (form.expected_output) taskTemplate.expected_output = form.expected_output;
      taskTemplate.session_policy = policyFromText(form.allow_policy_text, form.deny_policy_text, form.intaris_evaluation);
      const workflowSource = decodeWorkflowSourceValue(form.workflow_source);
      await api.schedules.create({
        name: form.name,
        description: form.description || null,
        schedule_type: form.schedule_type,
        cron_expr: form.schedule_type === 'cron' ? form.cron_expr : null,
        interval_seconds: form.schedule_type === 'interval' ? form.interval_seconds : null,
        one_shot_at: form.schedule_type === 'one_shot' ? form.one_shot_at : null,
        timezone: form.timezone,
        agent_id: form.agent_id,
        agent_profile_id: form.agent_profile_id || null,
        workflow_id: workflowSource.workflow_id,
        skill_id: workflowSource.skill_id,
        project_id: projectId ?? (form.project_id || null),
        task_template: taskTemplate,
        completion_mode_family: form.completion_mode_family,
        allow_silent_completion: form.allow_silent_completion,
        on_finish_instructions: form.completion_mode_family !== 'direct' ? form.on_finish_instructions.trim() || null : null,
        interaction_mode_override: form.interaction_mode_override
      });
      addToast('Schedule created', 'success');
      showCreateModal = false;
      resetForm();
      await loadData();
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    } finally {
      creating = false;
    }
  }

  function resetForm(): void {
    form = {
      name: '',
      description: '',
      schedule_type: 'cron',
      cron_expr: '0 9 * * *',
      interval_seconds: 1800,
      one_shot_at: '',
      timezone: localTimezone,
      agent_id: agents.find((a) => a.status === 'active')?.agent_id ?? agents[0]?.agent_id ?? '',
      agent_profile_id: '',
      workflow_source: '',
      project_id: projectId ?? '',
      task_title: '',
      task_description: '',
      priority: 0,
      expected_output: '',
      delivery_mode: 'preferred_channel',
      delivery_target: '',
      completion_mode_family: 'auto',
      allow_silent_completion: false,
      on_finish_instructions: '',
      interaction_mode_override: 'none',
      allow_policy_text: '',
      intaris_evaluation: '',
      deny_policy_text: ''
    };
  }

  function projectName(projectId: string | null): string | null {
    if (!projectId) return null;
    return projects.find((project) => project.project_id === projectId)?.name ?? projectId;
  }

  $effect(() => {
    const requestedProject = form.project_id;
    if (showCreateModal && createOptionsLoaded) untrack(() => void loadWorkflowsForProject(requestedProject));
  });

  $effect(() => {
    form.agent_profile_id = normalizeSelectedAgentProfileId(selectedAgent, form.agent_profile_id);
  });

  function openHeartbeatPreset(): void {
    resetForm();
    form.name = 'Heartbeat';
    form.description = 'Periodic check-in: review pending items, check messages, and report anything that needs attention.';
    form.schedule_type = 'interval';
    form.interval_seconds = 1800;
    form.allow_silent_completion = true;
    form.task_title = 'Heartbeat check';
    form.task_description = 'Review pending items, check for new messages or events, and report anything that needs attention. If nothing requires action, respond with a brief "nothing to report" summary.';
    showCreateModal = true;
  }

  async function toggleEnabled(schedule: Schedule): Promise<void> {
    try {
      if (schedule.enabled) {
        await api.schedules.disable(schedule.schedule_id);
        addToast(`${schedule.name} disabled`, 'success');
      } else {
        await api.schedules.enable(schedule.schedule_id);
        addToast(`${schedule.name} enabled`, 'success');
      }
      await loadData();
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
  }

  async function triggerNow(schedule: Schedule): Promise<void> {
    if (!canMutate || !alive) return;
    if (!(await confirmScheduleRunNow(schedule.name)) || !alive) return;
    try {
      const result = await api.schedules.trigger(schedule.schedule_id);
      // Stay on the list; the new task is one click away.
      addToast('', 'success', 6000, `${schedule.name} started`, result.task_id ? { label: 'Open task', href: `/tasks/${result.task_id}` } : null);
      if (!alive) return;
      await loadData();
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
  }

  async function deleteSchedule(schedule: Schedule): Promise<void> {
    const confirmed = await confirmAction({
      title: `Delete "${schedule.name}"?`,
      message: 'This will permanently remove the schedule. Existing tasks created by it will not be affected.',
      confirmLabel: 'Delete'
    });
    if (!confirmed || !alive || !canMutate) return;
    try {
      await api.schedules.remove(schedule.schedule_id);
      addToast(`${schedule.name} deleted`, 'success');
      await loadData();
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
  }

  function conversationLabel(conv: Conversation): string {
    const title = conv.title ?? conv.conversation_id;
    const channelType = conv.context?.type;
    if (channelType && channelType !== 'web') {
      return `${title} (${channelType})`;
    }
    return title;
  }

  function agentName(agentId: string): string {
    const agent = agents.find((a) => a.agent_id === agentId);
    return agent?.display_name ?? agent?.name ?? agentId;
  }

  function formatNextFire(iso: string | null): string {
    if (!iso) return 'N/A';
    const d = new Date(iso);
    const now = new Date();
    const diffMs = d.getTime() - now.getTime();
    if (diffMs < 0) return 'overdue';
    if (diffMs < 60_000) return 'in less than a minute';
    if (diffMs < 3_600_000) return `in ${Math.round(diffMs / 60_000)}m`;
    if (diffMs < 86_400_000) return `in ${Math.round(diffMs / 3_600_000)}h`;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function formatDateTime(iso: string): string {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  // Cron presets for the UI
  const cronPresets = [
    { label: 'Every minute', value: '* * * * *' },
    { label: 'Every 5 minutes', value: '*/5 * * * *' },
    { label: 'Every 30 minutes', value: '*/30 * * * *' },
    { label: 'Every hour', value: '0 * * * *' },
    { label: 'Daily at 8:00', value: '0 8 * * *' },
    { label: 'Daily at 9:00', value: '0 9 * * *' },
    { label: 'Weekdays at 9:00', value: '0 9 * * 1-5' },
    { label: 'Every Monday', value: '0 9 * * 1' },
    { label: 'Monthly', value: '0 0 1 * *' }
  ];

  const intervalPresets = [
    { label: '5 minutes', value: 300 },
    { label: '15 minutes', value: 900 },
    { label: '30 minutes', value: 1800 },
    { label: '1 hour', value: 3600 },
    { label: '2 hours', value: 7200 },
    { label: '6 hours', value: 21600 },
    { label: '12 hours', value: 43200 },
    { label: '24 hours', value: 86400 }
  ];

  onMount(() => {
    void api.agents.listAll({ agent_type: 'primary' }).then((items) => {
      if (!alive) return;
      agents = items;
      if (!form.agent_id) form.agent_id = items.find((a) => a.status === 'active')?.agent_id ?? items[0]?.agent_id ?? '';
    }).catch((e) => addToast(asApiError(e).message, 'error'));
    void api.projects.list().then((items) => {
      if (alive) projects = items;
    }).catch((e) => addToast(asApiError(e).message, 'error'));
  });
</script>

<svelte:head>
  {#if !embedded}<title>Schedules - Cognis</title>{/if}
</svelte:head>

{#if loading}
  <LoadingState />
{:else if error}
  <div class="flex items-center justify-center p-12">
    <Card class="max-w-md p-6 text-center">
      <p class="text-danger">{error}</p>
      <Button class="mt-4" onclick={loadData}>Retry</Button>
    </Card>
  </div>
{:else}
  <div class={embedded ? 'min-w-0 space-y-6' : 'mx-auto max-w-6xl space-y-6 p-4 sm:p-6'}>
    <!-- Header -->
    <div class="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-2xl font-bold text-strong">Schedules</h1>
        <p class="mt-1 text-sm text-muted">Automated task creation on cron, interval, or one-shot triggers</p>
      </div>
      {#if canMutate}<div class="flex flex-wrap gap-2">
        <Tooltip text="Create a periodic heartbeat check">
          <Button variant="secondary" onclick={openHeartbeatPreset}>
            <RefreshCw class="mr-1.5 h-4 w-4" />
            Heartbeat
          </Button>
        </Tooltip>
        <Button onclick={() => (showCreateModal = true)}>
          <Plus class="mr-1.5 h-4 w-4" />
          New schedule
        </Button>
      </div>{/if}
    </div>

    <!-- Filters -->
    <div class="flex flex-wrap gap-3">
      <Input find bind:value={search} placeholder="Search schedules..." class="w-64" />
      <select
        bind:value={filterType}
        class="rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
      >
        <option value="">All types</option>
        <option value="cron">Cron</option>
        <option value="interval">Interval</option>
        <option value="one_shot">One-shot</option>
      </select>
      <select
        bind:value={filterEnabled}
        class="rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
      >
        <option value="">All states</option>
        <option value="enabled">Enabled</option>
        <option value="disabled">Disabled</option>
      </select>
      <select
        bind:value={filterProjectId}
        aria-label="Project filter"
        disabled={Boolean(projectId)}
        class="rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
      >
        <option value="">All projects</option>
        {#each projects as project}
          <option value={project.project_id}>{project.name}</option>
        {/each}
      </select>
      <select
        bind:value={filterVisibility}
        class="rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
      >
        <option value="active">Active and recent</option>
        <option value="expired">Expired</option>
        <option value="all">All schedules</option>
      </select>
    </div>

    <!-- Schedule list -->
    {#if filtered.length === 0}
      <Card class="p-12 text-center">
        <Clock class="mx-auto mb-3 h-10 w-10 text-quiet" />
        <p class="text-muted">
          {schedules.length === 0 ? 'No schedules yet. Create one to automate task creation.' : 'No schedules match your filters.'}
        </p>
      </Card>
    {:else}
      <div class="space-y-3">
        {#each filtered as schedule (schedule.schedule_id)}
          {@const TypeIcon = typeIcons[schedule.schedule_type] ?? Clock}
          <Card class="group overflow-hidden transition-colors hover:border-control-border">
            <div class="flex flex-col gap-3 p-4 sm:flex-row sm:items-start sm:justify-between">
              <button
                class="min-w-0 flex-1 text-left"
                onclick={() => onOpenSchedule(schedule.schedule_id)}
              >
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-2">
                    <TypeIcon class="h-4 w-4 shrink-0 text-muted" />
                    <h3 class="break-words font-medium text-strong lg:truncate lg:group-hover:whitespace-normal lg:group-focus-within:whitespace-normal" title={schedule.name}>{schedule.name}</h3>
                    {#if !schedule.enabled}
                      <Badge class="bg-inset/50 text-muted">Disabled</Badge>
                    {/if}
                    {#if schedule.is_expired}
                      <Badge class="bg-warning-subtle text-warning">Expired</Badge>
                    {/if}
                    {#if schedule.completion_mode_family !== 'auto'}
                      <Badge class="bg-cyan-subtle text-cyan-tone">{completionModeLabel(schedule.completion_mode_family)} delivery</Badge>
                    {/if}
                    {#if schedule.allow_silent_completion}
                      <Badge class="bg-cyan-subtle text-cyan-tone">Silent allowed</Badge>
                    {/if}
                    {#if schedule.project_id}
                      <Badge class="bg-violet-subtle text-violet-tone">{projectName(schedule.project_id)}</Badge>
                    {/if}
                    {#if schedule.consecutive_errors > 0}
                      <Badge class="bg-danger-subtle text-danger">
                        <AlertTriangle class="mr-1 h-3 w-3" />
                        {schedule.consecutive_errors} errors
                      </Badge>
                    {/if}
                  </div>
                  <p class="mt-1 text-sm text-muted">
                    {schedule.human_schedule ?? schedule.cron_expr ?? `Every ${schedule.interval_seconds}s`}
                    <span class="mx-1.5 text-quiet">·</span>
                    {agentName(schedule.agent_id)}
                  </p>
                  {#if schedule.description}
                    <p class="mt-1 truncate text-xs text-quiet">{schedule.description}</p>
                  {/if}
                </div>
              </button>

              <div class="flex shrink-0 flex-col gap-3 sm:items-end">
                <div class="text-xs text-quiet sm:text-right">
                  {#if schedule.last_run_status}
                    <div class="inline-flex flex-wrap items-center gap-1.5" data-testid={`schedule-last-run-${schedule.schedule_id}`}>
                      <span>Last run</span>
                      <StatusBadge status={schedule.last_run_status === 'success' ? 'completed' : schedule.last_run_status} />
                      {#if schedule.last_fired_at}<span title={formatAbsoluteTime(schedule.last_fired_at)}>{formatRelativeTime(schedule.last_fired_at)}</span>{/if}
                    </div>
                  {/if}
                  {#if schedule.is_expired}
                    <div class="mt-1">Expired</div>
                  {:else if !schedule.next_fire_at && schedule.expiration_grace_until}
                    <div class="mt-1">Visible until: {formatDateTime(schedule.expiration_grace_until)}</div>
                  {:else if schedule.next_fire_at && schedule.enabled}
                    <div class="mt-1" title={formatAbsoluteTime(schedule.next_fire_at)}>Next run {formatNextFire(schedule.next_fire_at)}</div>
                  {/if}
                </div>

                <!-- Quick actions stay in normal flow so they cannot overlap
                     the status badges or next-run summary. -->
                  {#if canMutate}<div class="flex flex-wrap gap-1">
                    <button
                      class="inline-flex min-h-8 items-center gap-1.5 rounded-xl px-2.5 text-xs text-secondary hover:bg-raised hover:text-strong"
                      onclick={(e: MouseEvent) => { e.stopPropagation(); toggleEnabled(schedule); }}
                    >
                      {#if schedule.enabled}<Pause class="h-3.5 w-3.5" />Disable{:else}<Play class="h-3.5 w-3.5" />Enable{/if}
                    </button>
                    <button
                      class="inline-flex min-h-8 items-center gap-1.5 rounded-xl px-2.5 text-xs text-secondary hover:bg-raised hover:text-strong"
                      onclick={(e: MouseEvent) => { e.stopPropagation(); triggerNow(schedule); }}
                    >
                      <Zap class="h-3.5 w-3.5" />Run now
                    </button>
                    <button
                      class="inline-flex min-h-8 items-center gap-1.5 rounded-xl px-2.5 text-xs text-secondary hover:bg-danger-subtle hover:text-danger"
                      aria-label={`Delete ${schedule.name}`}
                      onclick={(e: MouseEvent) => { e.stopPropagation(); deleteSchedule(schedule); }}
                    >
                      <Trash2 class="h-3.5 w-3.5" />Delete
                    </button>
                  </div>{/if}
              </div>
            </div>
          </Card>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<!-- Create schedule modal -->
{#if showCreateModal}
  <BlockingDialog label="Create schedule" onClose={() => (showCreateModal = false)} titleId="create-schedule-title" dismissible={!creating} backdropDismissible={JSON.stringify(form) === createBaseline}>
    {#snippet header()}
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-lg font-semibold text-strong" id="create-schedule-title">Create schedule</h2>
        <button
          class="inline-flex h-11 w-11 items-center justify-center rounded-xl text-muted hover:bg-raised hover:text-strong"
          onclick={() => (showCreateModal = false)}
          aria-label="Close"
          type="button"
        >&times;</button>
      </div>
    {/snippet}

    {#snippet children()}
      <div class="space-y-4">
        <!-- Name -->
        <div class="space-y-1">
          <label for="sched-name" class="text-sm font-medium text-foreground">Name</label>
          <Input id="sched-name" bind:value={form.name} placeholder="e.g. Daily review, Email check" />
        </div>

        <!-- Description -->
        <div class="space-y-1">
          <label for="sched-desc" class="text-sm font-medium text-foreground">Description</label>
          <textarea
            id="sched-desc"
            bind:value={form.description}
            class="min-h-[60px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet"
            placeholder="What does this schedule do?"
          ></textarea>
        </div>

        <!-- Schedule type -->
        <div class="space-y-1">
          <p class="text-sm font-medium text-foreground">Schedule type</p>
          <div class="flex gap-2">
            {#each [['cron', 'Cron'], ['interval', 'Interval'], ['one_shot', 'One-shot']] as [value, label]}
              <button
                class="flex-1 rounded-xl border px-3 py-2 text-sm transition-colors {form.schedule_type === value ? 'border-accent bg-accent-subtle text-accent' : 'border-control-border bg-canvas/80 text-muted hover:border-control-border'}"
                onclick={() => (form.schedule_type = value)}
              >
                {label}
              </button>
            {/each}
          </div>
        </div>

        <!-- Type-specific fields -->
        {#if form.schedule_type === 'cron'}
          <div class="space-y-1">
            <label for="sched-cron" class="text-sm font-medium text-foreground">Cron expression</label>
            <Input id="sched-cron" bind:value={form.cron_expr} placeholder="0 9 * * *" />
            <div class="flex flex-wrap gap-2 pt-1">
              {#each cronPresets as preset}
                <button
                  class="rounded-xl border border-control-border px-3 py-2 text-sm text-secondary transition-colors hover:border-border-strong hover:text-foreground md:py-1 md:text-xs {form.cron_expr === preset.value ? 'border-accent/50 bg-accent-subtle text-accent' : ''}"
                  onclick={() => (form.cron_expr = preset.value)}
                  type="button"
                >
                  {preset.label}
                </button>
              {/each}
            </div>
          </div>
          <div class="space-y-1">
            <label for="sched-tz" class="text-sm font-medium text-foreground">Timezone</label>
            <select id="sched-tz" bind:value={form.timezone} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              {#each timezoneOptions as tz}
                <option value={tz}>{tz}{tz === localTimezone ? ' (local)' : ''}</option>
              {/each}
            </select>
          </div>
        {:else if form.schedule_type === 'interval'}
          <div class="space-y-1">
            <p class="text-sm font-medium text-foreground">Interval</p>
            <div class="flex flex-wrap gap-2">
              {#each intervalPresets as preset}
                <button
                  class="rounded-xl border border-control-border px-3 py-2 text-sm text-secondary transition-colors hover:border-border-strong hover:text-foreground md:py-1 md:text-xs {form.interval_seconds === preset.value ? 'border-accent/50 bg-accent-subtle text-accent' : ''}"
                  onclick={() => (form.interval_seconds = preset.value)}
                  type="button"
                >
                  {preset.label}
                </button>
              {/each}
            </div>
            <Input id="sched-interval" bind:value={form.interval_seconds} type="number" placeholder="Seconds" />
          </div>
        {:else}
          <div class="space-y-1">
            <label for="sched-at" class="text-sm font-medium text-foreground">Run at</label>
            <Input id="sched-at" bind:value={form.one_shot_at} type="datetime-local" />
          </div>
        {/if}

        <!-- Agent + Workflow. Stacks on narrow phones; two columns at sm+. -->
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div class="space-y-1">
            <span class="block text-sm font-medium text-foreground">Agent</span>
            <AgentSelect
              id="sched-agent"
              agents={agents}
              value={form.agent_id}
              onchange={(next) => { form.agent_id = next; }}
            />
          </div>
          <div class="space-y-1">
            <label for="sched-workflow" class="text-sm font-medium text-foreground">Workflow</label>
            <select id="sched-workflow" bind:value={form.workflow_source} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="">General task</option>
              {#each workflowSourceOptions as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          </div>
          <AgentProfileSelect
            agents={agents}
            agentId={form.agent_id}
            bind:value={form.agent_profile_id}
          />
          <div class="space-y-1 sm:col-span-2">
            <label for="sched-project" class="text-sm font-medium text-foreground">Project</label>
            <select id="sched-project" bind:value={form.project_id} disabled={Boolean(projectId)} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="">None</option>
              {#each projects as project}
                <option value={project.project_id}>{project.name}</option>
              {/each}
            </select>
          </div>
        </div>

        <!-- Task template -->
        <div class="space-y-1">
          <label for="sched-task-title" class="text-sm font-medium text-foreground">Task title</label>
          <Input id="sched-task-title" bind:value={form.task_title} placeholder="Title for created tasks (defaults to schedule name)" />
        </div>
        <div class="space-y-1">
          <label for="sched-task-desc" class="text-sm font-medium text-foreground">Task prompt</label>
          <textarea
            id="sched-task-desc"
            bind:value={form.task_description}
            class="min-h-[80px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet"
            placeholder="Instructions for the agent when this schedule fires"
          ></textarea>
        </div>

        <!-- Task options. Stacks on narrow phones. -->
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div class="space-y-1">
            <label for="sched-priority" class="text-sm font-medium text-foreground">Priority</label>
            <Input id="sched-priority" bind:value={form.priority} type="number" />
          </div>
          <div class="space-y-1">
            <label for="sched-delivery" class="text-sm font-medium text-foreground">Delivery</label>
            <select id="sched-delivery" bind:value={form.delivery_mode} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="preferred_channel">Preferred channel</option>
              <option value="in_app_notifications">In-app notifications (direct only)</option>
              <option value="latest_active_for_agent">Latest active conversation</option>
              <option value="specific_conversation">Specific conversation</option>
            </select>
          </div>
        </div>

        {#if form.delivery_mode === 'specific_conversation'}
          <div class="space-y-1">
            <label for="sched-target" class="text-sm font-medium text-foreground">Target conversation</label>
            <select id="sched-target" bind:value={form.delivery_target} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="">Select conversation</option>
              {#each conversations as conv}
                <option value={conv.conversation_id}>{conversationLabel(conv)}</option>
              {/each}
            </select>
          </div>
        {/if}

        <div class="space-y-1">
          <label for="sched-expected" class="text-sm font-medium text-foreground">Expected output <span class="normal-case tracking-normal text-quiet">(optional)</span></label>
          <textarea
            id="sched-expected"
            bind:value={form.expected_output}
            class="min-h-[60px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet"
            placeholder="What should the agent produce? Used for step evaluation."
          ></textarea>
        </div>

        <div class="space-y-1">
          <label for="sched-completion-family" class="text-sm font-medium text-foreground">Completion notification behavior</label>
          <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
            <select id="sched-completion-family" bind:value={form.completion_mode_family} disabled={form.delivery_mode === 'in_app_notifications'} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              {#each COMPLETION_MODE_OPTIONS as option}
                <option value={option.value}>{option.name} — {option.description}</option>
              {/each}
            </select>
            <label class="inline-flex items-center gap-2 rounded-xl border border-control-border bg-canvas/60 px-3 py-2 text-sm text-foreground">
              <input type="checkbox" bind:checked={form.allow_silent_completion} class="rounded border-control-border bg-raised" />
              <span>Allow silent completion</span>
            </label>
          </div>
          <TaskFinishInstructions deliveryMode={form.delivery_mode} bind:completionMode={form.completion_mode_family} bind:instructions={form.on_finish_instructions} />
        </div>

        <div class="space-y-1">
          <label for="sched-interaction" class="text-sm font-medium text-foreground">Interaction policy</label>
          <select id="sched-interaction" bind:value={form.interaction_mode_override} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
            <option value="none">Fully autonomous</option>
            <option value="step_requests">Allow planning questions</option>
            <option value="explicit_gates">Gates only</option>
          </select>
          <p class="text-xs text-quiet">Scheduled tasks default to fully autonomous so unattended runs do not pause for clarification.</p>
        </div>

        <SessionPolicyEditor
          showEvaluation
          bind:evaluation={form.intaris_evaluation}
          inheritedEvaluation={selectedAgent?.capabilities?.intaris_evaluation ?? DEFAULT_INTARIS_EVALUATION}
          bind:allowText={form.allow_policy_text}
          bind:denyText={form.deny_policy_text}
          title="Intaris session policies"
          help="Policies are copied to each task created by this schedule."
        />
      </div>
    {/snippet}

    {#snippet footer()}
      <div class="flex justify-end gap-3">
        <Button variant="secondary" onclick={() => (showCreateModal = false)}>Cancel</Button>
        {#if createOptionsError}
          <p role="alert">{createOptionsError}</p>
          <Button variant="secondary" onclick={() => void loadCreateOptions()}>Retry choices</Button>
        {/if}
        {#if workflowOptionsError}
          <p role="alert">{workflowOptionsError}</p>
          <Button variant="secondary" onclick={() => void loadWorkflowsForProject(form.project_id)}>Retry workflows</Button>
        {/if}
        {#if form.delivery_mode === 'specific_conversation' && conversationsError}
          <p role="alert">{conversationsError}</p>
          <Button variant="secondary" onclick={() => void loadConversations()}>Retry conversations</Button>
        {/if}
        <Button disabled={!createOptionsLoaded || createOptionsLoading || workflowOptionsLoading || !!workflowOptionsError || (form.delivery_mode === 'specific_conversation' && (conversationsLoading || !!conversationsError || !form.delivery_target)) || !form.name.trim() || !form.agent_id || creating} onclick={handleCreate}>
          {creating ? 'Creating...' : 'Create schedule'}
        </Button>
      </div>
    {/snippet}
  </BlockingDialog>
{/if}
