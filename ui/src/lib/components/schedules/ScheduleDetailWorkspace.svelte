<script lang="ts">
  import TaskFinishInstructions from '$lib/components/tasks/TaskFinishInstructions.svelte';
  import { COMPLETION_MODE_OPTIONS, completionModeLabel } from '$lib/task-delivery';
  import type { CompletionModeFamily } from '$lib/types/api';
  import { DEFAULT_INTARIS_EVALUATION } from '$lib/session-policy';
  import { goto } from '$app/navigation';
  import { onMount, onDestroy, untrack } from 'svelte';
  import { auth } from '$lib/stores/auth';

  import { api, asApiError } from '$lib/api/client';
  import {
    buildWorkflowSourceOptions,
    decodeWorkflowSourceValue,
    workflowSourceValueForSelection
  } from '$lib/workflow-sources';
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import AgentProfileSelect from '$lib/components/AgentProfileSelect.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import SessionPolicyEditor from '$lib/components/SessionPolicyEditor.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';

  const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const timezoneOptions = [
    localTimezone, 'UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
    'Europe/Prague', 'Europe/Moscow', 'America/New_York', 'America/Chicago',
    'America/Denver', 'America/Los_Angeles', 'America/Sao_Paulo',
    'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dubai',
    'Australia/Sydney', 'Pacific/Auckland'
  ].filter((tz, i, arr) => arr.indexOf(tz) === i);
  import Input from '$lib/components/ui/Input.svelte';
  import { confirmScheduleRunNow } from '$lib/schedules';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import { normalizeSelectedAgentProfileId } from '$lib/agents';
  import { policyFromText, policyText, policyEvaluation } from '$lib/session-policy';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, Conversation, Project, Schedule, ScheduleRun, Skill, Workflow } from '$lib/types/api';
  import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
import ArrowLeft from 'lucide-svelte/icons/arrow-left';
import Calendar from 'lucide-svelte/icons/calendar';
import Check from 'lucide-svelte/icons/check';
import Clock from 'lucide-svelte/icons/clock';
import Pause from 'lucide-svelte/icons/pause';
import Play from 'lucide-svelte/icons/play';
import Save from 'lucide-svelte/icons/save';
import Timer from 'lucide-svelte/icons/timer';
import Trash2 from 'lucide-svelte/icons/trash-2';
import X from 'lucide-svelte/icons/x';
import Zap from 'lucide-svelte/icons/zap';

  let { id, embedded = false, canMutate = true, onBack = () => goto('/schedules'), onOpenTask = (taskId: string) => goto(`/tasks/${taskId}`) } = $props<{
    id: string;
    embedded?: boolean;
    canMutate?: boolean;
    onBack?: () => void;
    onOpenTask?: (taskId: string) => void;
  }>();
  const editable = $derived(canMutate && Boolean($auth.user) && $auth.user?.role !== 'viewer');
  let alive = true;
  onDestroy(() => { alive = false; });

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let schedule = $state<Schedule | null>(null);
  let runs = $state<ScheduleRun[]>([]);
  let agents = $state<Agent[]>([]);
  let projectAwareWorkflows = $state<Workflow[]>([]);
  let projects = $state<Project[]>([]);
  let skills = $state<Skill[]>([]);
  let conversations = $state<Conversation[]>([]);
  let projectWorkflowLoadKey = 0;
  let loadKey = 0;
  let optionsLoading = $state(true);
  let optionsError = $state('');
  let workflowOptionsLoading = $state(true);
  let workflowOptionsError = $state('');
  let runsLoading = $state(true);
  let runsError = $state('');
  let conversationsRequested = false;
  let conversationsLoading = $state(false);
  let conversationsError = $state('');

  let form = $state({
    name: '',
    description: '',
    schedule_type: 'cron',
    cron_expr: '',
    interval_seconds: 0,
    one_shot_at: '',
    timezone: 'UTC',
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
    deny_policy_text: '',
    max_concurrent_runs: 1,
    retry_failed_tasks: false,
    fail_paused_task_on_next_fire: true
  });

  // The detail opens as a read view; editing is an explicit mode so long prompts are not changed by accident.
  let editing = $state(false);
  let promptExpanded = $state(false);
  const promptCollapsible = $derived(
    form.task_description.length > 400 || form.task_description.split('\n').length > 6,
  );

  function runStatus(status: string): string {
    return status === 'success' ? 'completed' : status;
  }

  function cancelEditing(): void {
    if (schedule) populateForm(schedule);
    editing = false;
  }

  function projectLabel(projectId: string | null): string {
    if (!projectId) return 'No project';
    return projects.find((project) => project.project_id === projectId)?.name ?? projectId;
  }

  function workflowLabel(s: Schedule): string {
    if (!s.workflow_id) return s.skill_id ? 'Skill task' : 'Ad-hoc task';
    return projectAwareWorkflows.find((workflow) => workflow.workflow_id === s.workflow_id)?.name ?? s.workflow_id;
  }

  let selectedAgent = $derived(agents.find((agent) => agent.agent_id === form.agent_id) ?? null);
  let workflowSourceOptions = $derived(buildWorkflowSourceOptions(projectAwareWorkflows, skills, selectedAgent));

  function scheduleId(): string {
    return id;
  }

  async function loadData(): Promise<void> {
    const key = ++loadKey;
    loading = true;
    error = '';
    try {
      const sched = await api.schedules.detail(scheduleId());
      if (!alive || key !== loadKey) return;
      schedule = sched;
      populateForm(sched);
      void loadOptions(key);
      runsLoading = true;
      runsError = '';
      void api.schedules.runs(sched.schedule_id).then((items) => {
        if (alive && key === loadKey) runs = items;
      }).catch((e) => {
        if (alive && key === loadKey) runsError = asApiError(e).message;
      }).finally(() => {
        if (alive && key === loadKey) runsLoading = false;
      });
    } catch (e) {
      if (key === loadKey) error = asApiError(e).message;
    } finally {
      if (key === loadKey) loading = false;
    }
  }

  async function loadOptions(key = loadKey): Promise<void> {
    optionsLoading = true;
    optionsError = '';
    try {
      const [agentList, skillList, projectList] = await Promise.all([
        api.agents.listAll({ agent_type: 'primary' }),
        api.skills.list(),
        api.projects.list()
      ]);
      if (!alive || key !== loadKey) return;
      agents = agentList;
      skills = skillList;
      projects = projectList;
    } catch (e) {
      if (key === loadKey) optionsError = asApiError(e).message;
    } finally {
      if (key === loadKey) optionsLoading = false;
    }
  }

  $effect(() => {
    if (schedule && form.delivery_mode === 'specific_conversation') untrack(() => void loadConversations());
  });

  async function loadConversations(): Promise<void> {
    if (conversationsRequested) return;
    conversationsRequested = true;
    conversationsLoading = true;
    conversationsError = '';
    try {
      const items = await api.conversations.listAll();
      if (alive) conversations = items;
    } catch (e) {
      conversationsRequested = false;
      conversationsError = asApiError(e).message;
    } finally {
      conversationsLoading = false;
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

  function populateForm(s: Schedule): void {
    const tmpl = s.task_template ?? {};
    const delivery = (tmpl.delivery as Record<string, unknown>) ?? {};
    form = {
      name: s.name,
      description: s.description ?? '',
      schedule_type: s.schedule_type,
      cron_expr: s.cron_expr ?? '',
      interval_seconds: s.interval_seconds ?? 0,
      one_shot_at: s.one_shot_at ?? '',
      timezone: s.timezone,
      agent_id: s.agent_id,
      agent_profile_id: s.agent_profile_id ?? '',
      workflow_source: workflowSourceValueForSelection(s.workflow_id, s.skill_id),
      project_id: s.project_id ?? '',
      task_title: (tmpl.title as string) ?? '',
      task_description: (tmpl.description as string) ?? '',
      priority: (tmpl.priority as number) ?? 0,
      expected_output: (tmpl.expected_output as string) ?? '',
      delivery_mode: (delivery.mode as string) ?? 'preferred_channel',
      delivery_target: (delivery.target as string) ?? '',
      completion_mode_family: s.completion_mode_family,
      allow_silent_completion: s.allow_silent_completion,
      on_finish_instructions: s.on_finish_instructions ?? '',
      interaction_mode_override: s.interaction_mode_override ?? 'none',
      allow_policy_text: policyText((tmpl.session_policy as Record<string, unknown>) ?? s.session_policy, 'allow_policies'),
      intaris_evaluation: policyEvaluation((tmpl.session_policy as Record<string, unknown>) ?? s.session_policy),
      deny_policy_text: policyText((tmpl.session_policy as Record<string, unknown>) ?? s.session_policy, 'deny_policies'),
      max_concurrent_runs: s.max_concurrent_runs,
      retry_failed_tasks: s.retry_failed_tasks,
      fail_paused_task_on_next_fire: s.fail_paused_task_on_next_fire
    };
  }

  async function loadProjectWorkflows(projectId: string): Promise<void> {
    const key = ++projectWorkflowLoadKey;
    workflowOptionsLoading = true;
    workflowOptionsError = '';
    try {
      const next = await api.workflows.listAll({ project_id: projectId || null });
      if (!alive || key !== projectWorkflowLoadKey) return;
      projectAwareWorkflows = next;
      // Drop the workflow source if it no longer maps to a workflow in the
      // current project-aware list (skill sources keep working).
      if (form.workflow_source.startsWith('workflow:')) {
        const wfId = form.workflow_source.slice('workflow:'.length);
        if (!next.some((workflow) => workflow.workflow_id === wfId)) {
          form.workflow_source = '';
        }
      }
    } catch {
      if (key === projectWorkflowLoadKey) workflowOptionsError = 'Unable to load workflow choices';
    } finally {
      if (key === projectWorkflowLoadKey) workflowOptionsLoading = false;
    }
  }

  $effect(() => {
    if (!schedule) return;
    const key = form.project_id;
    untrack(() => void loadProjectWorkflows(key));
  });

  $effect(() => {
    if (optionsLoading || optionsError) return;
    form.agent_profile_id = normalizeSelectedAgentProfileId(selectedAgent, form.agent_profile_id);
  });

  async function handleSave(): Promise<void> {
    if (!editable || !alive || optionsLoading || optionsError || workflowOptionsLoading || workflowOptionsError) return;
    if (!schedule) return;
    saving = true;
    try {
      const taskTemplate: Record<string, unknown> = {
        ...(schedule.task_template ?? {}),
        title: form.task_title || form.name,
        description: form.task_description,
        priority: form.priority,
        delivery: {
          mode: form.delivery_mode,
          target: form.delivery_mode === 'specific_conversation' ? form.delivery_target || null : null
        }
      };
      if (form.expected_output) {
        taskTemplate.expected_output = form.expected_output;
      } else {
        delete taskTemplate.expected_output;
      }
      taskTemplate.session_policy = policyFromText(form.allow_policy_text, form.deny_policy_text, form.intaris_evaluation);
      const workflowSource = decodeWorkflowSourceValue(form.workflow_source);
      const updated = await api.schedules.update(schedule.schedule_id, {
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
        project_id: form.project_id || null,
        task_template: taskTemplate,
        completion_mode_family: form.completion_mode_family,
        allow_silent_completion: form.allow_silent_completion,
        on_finish_instructions: form.completion_mode_family !== 'direct' ? form.on_finish_instructions.trim() || null : null,
        interaction_mode_override: form.interaction_mode_override,
        max_concurrent_runs: form.max_concurrent_runs,
        retry_failed_tasks: form.retry_failed_tasks,
        fail_paused_task_on_next_fire: form.fail_paused_task_on_next_fire
      });
      await loadData();
      editing = false;
      addToast('Schedule saved', 'success');
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    } finally {
      saving = false;
    }
  }

  async function toggleEnabled(): Promise<void> {
    if (!editable || !alive) return;
    if (!schedule) return;
    try {
      if (schedule.enabled) {
        schedule = await api.schedules.disable(schedule.schedule_id);
        addToast('Schedule disabled', 'success');
      } else {
        schedule = await api.schedules.enable(schedule.schedule_id);
        addToast('Schedule enabled', 'success');
      }
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
  }

  async function triggerNow(): Promise<void> {
    if (!editable || !alive) return;
    if (!schedule) return;
    if (!(await confirmScheduleRunNow(schedule.name)) || !alive || !schedule) return;
    try {
      const result = await api.schedules.trigger(schedule.schedule_id);
      addToast('', 'success', 6000, `${schedule.name} started`, result.task_id ? { label: 'Open task', href: `/tasks/${result.task_id}` } : null);
      if (!alive) return;
      schedule = result;
      runs = await api.schedules.runs(schedule.schedule_id);
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
  }

  async function handleDelete(): Promise<void> {
    if (!editable || !alive) return;
    if (!schedule) return;
    const confirmed = await confirmAction({
      title: `Delete "${schedule.name}"?`,
      message: 'This will permanently remove the schedule.',
      confirmLabel: 'Delete'
    });
    if (!confirmed || !alive || !editable) return;
    try {
      await api.schedules.remove(schedule.schedule_id);
      addToast('Schedule deleted', 'success');
      if (alive) onBack();
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
  }

  function agentName(agentId: string): string {
    const agent = agents.find((a) => a.agent_id === agentId);
    return agent?.display_name ?? agent?.name ?? agentId;
  }

  function formatDate(iso: string | null): string {
    if (!iso) return 'Not scheduled';
    return `${formatAbsoluteTime(iso)} (${formatRelativeTime(iso)})`;
  }

  onMount(loadData);
</script>

<svelte:head>
  {#if !embedded}<title>{schedule?.name ?? 'Schedule'} - Cognis</title>{/if}
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
{:else if schedule}
  <div class="mx-auto max-w-4xl space-y-6 p-4 sm:p-6">
    <!-- Header -->
    <div class="flex min-w-0 flex-wrap items-center gap-3">
      <button aria-label="Back to schedules" class="text-muted hover:text-strong" onclick={onBack}>
        <ArrowLeft class="h-5 w-5" />
      </button>
      <div class="min-w-0 flex-1">
        <div class="flex min-w-0 flex-wrap items-center gap-2">
          <h1 class="min-w-0 break-words text-2xl font-bold text-strong [overflow-wrap:anywhere]">{schedule.name}</h1>
          {#if !schedule.enabled}
            <Badge class="bg-inset/50 text-muted">Disabled</Badge>
          {/if}
          {#if schedule.completion_mode_family !== 'auto'}
            <Badge class="bg-cyan-subtle text-cyan-tone">{completionModeLabel(schedule.completion_mode_family)} delivery</Badge>
          {/if}
          {#if schedule.allow_silent_completion}
            <Badge class="bg-cyan-subtle text-cyan-tone">Silent allowed</Badge>
          {/if}
        </div>
        <p class="mt-0.5 text-sm text-muted">{schedule.human_schedule ?? schedule.cron_expr}</p>
      </div>
      {#if editable}<div class="flex gap-2">
        <Button size="sm" variant="secondary" onclick={toggleEnabled}>
          {#if schedule.enabled}<Pause class="mr-1.5 h-3.5 w-3.5" />Disable{:else}<Play class="mr-1.5 h-3.5 w-3.5" />Enable{/if}
        </Button>
        <Button size="sm" variant="secondary" onclick={triggerNow}>
          <Zap class="mr-1.5 h-3.5 w-3.5" />Run now
        </Button>
        <Button size="sm" variant="ghost" aria-label="Delete schedule" onclick={handleDelete}>
          <Trash2 class="mr-1.5 h-3.5 w-3.5 text-danger" /><span class="text-danger">Delete</span>
        </Button>
      </div>{/if}
    </div>

    <!-- Status card -->
    {#if schedule.disabled_reason || schedule.consecutive_errors > 0}
      <Card class="border-danger-border bg-danger-subtle p-4">
        <div class="flex items-start gap-3">
          <AlertTriangle class="mt-0.5 h-5 w-5 shrink-0 text-danger" />
          <div>
            {#if schedule.disabled_reason}
              <p class="font-medium text-danger">{schedule.disabled_reason}</p>
            {/if}
            {#if schedule.consecutive_errors > 0}
              <p class="text-sm text-danger/80">{schedule.consecutive_errors} consecutive error(s)</p>
            {/if}
          </div>
        </div>
      </Card>
    {/if}

    <!-- Status overview -->
    <div class="grid grid-cols-2 gap-4 sm:grid-cols-4">
      <Card class="p-4">
        <p class="text-xs font-medium uppercase tracking-widest text-quiet">Next run</p>
        <p class="mt-1 text-sm text-strong">{formatDate(schedule.next_fire_at)}</p>
      </Card>
      <Card class="p-4">
        <p class="text-xs font-medium uppercase tracking-widest text-quiet">Last run</p>
        <p class="mt-1 text-sm text-strong">{schedule.last_fired_at ? formatDate(schedule.last_fired_at) : 'Never'}</p>
      </Card>
      <Card class="p-4">
        <p class="text-xs font-medium uppercase tracking-widest text-quiet">Last run outcome</p>
        <p class="mt-1">
          {#if schedule.last_run_status}
            <StatusBadge status={runStatus(schedule.last_run_status)} />
          {:else}
            <span class="text-sm text-quiet">Never run</span>
          {/if}
        </p>
      </Card>
      <Card class="p-4">
        <p class="text-xs font-medium uppercase tracking-widest text-quiet">Agent</p>
        <p class="mt-1 text-sm text-strong">{agentName(schedule.agent_id)}</p>
      </Card>
    </div>

    <!-- Edit form -->
    {#if !editing}
    <Card class="p-6">
      <div class="flex flex-wrap items-start justify-between gap-3" data-testid="schedule-read-view">
        <h2 class="text-lg font-semibold text-strong">What this schedule does</h2>
        {#if editable}<Button size="sm" variant="secondary" onclick={() => (editing = true)}>Edit schedule</Button>{/if}
      </div>
      {#if schedule.description}<p class="mt-2 text-sm text-secondary">{schedule.description}</p>{/if}
      <dl class="mt-4 grid gap-4 text-sm sm:grid-cols-2">
        <div><dt class="text-xs text-quiet">Runs</dt><dd class="mt-1 text-foreground">{schedule.human_schedule ?? `${schedule.cron_expr} (${schedule.timezone})`}</dd></div>
        <div><dt class="text-xs text-quiet">Workflow</dt><dd class="mt-1 text-foreground">{workflowLabel(schedule)}</dd></div>
        <div><dt class="text-xs text-quiet">Project</dt><dd class="mt-1 text-foreground">{projectLabel(schedule.project_id)}</dd></div>
        <div><dt class="text-xs text-quiet">Delivery</dt><dd class="mt-1 text-foreground">{completionModeLabel(schedule.completion_mode_family)}{schedule.allow_silent_completion ? ' · may finish silently' : ''}</dd></div>
      </dl>
      <section class="mt-5 rounded-2xl border border-separator bg-canvas/50 p-4">
        <h3 class="text-sm font-semibold text-strong">{form.task_title || 'Task prompt'}</h3>
        {#if form.task_description}
          <p class={`mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-secondary ${promptExpanded || !promptCollapsible ? '' : 'line-clamp-6'}`} data-testid="schedule-prompt">{form.task_description}</p>
          {#if promptCollapsible}
            <Button class="mt-2" variant="link" size="inline" aria-expanded={promptExpanded} onclick={() => (promptExpanded = !promptExpanded)}>{promptExpanded ? 'Show less' : 'Show full prompt'}</Button>
          {/if}
        {/if}
      </section>
    </Card>
    {:else if optionsError || workflowOptionsError}
      <p role="alert">{optionsError || workflowOptionsError}</p>
      <Button onclick={() => { void loadOptions(); void loadProjectWorkflows(form.project_id); }}>Retry configuration choices</Button>
    {:else if optionsLoading || workflowOptionsLoading}
      <LoadingState label="Loading configuration choices" />
    {:else}
    <fieldset disabled={!editable} class="min-w-0">
    <Card class="p-6">
      <h2 class="mb-4 text-lg font-semibold text-strong">Edit schedule</h2>
      <div class="space-y-4">
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div class="space-y-1">
            <label for="edit-name" class="text-sm font-medium text-foreground">Name</label>
            <Input id="edit-name" bind:value={form.name} />
          </div>
          <div class="space-y-1">
            <span class="block text-sm font-medium text-foreground">Agent</span>
            <AgentSelect
              id="edit-agent"
              agents={agents}
              value={form.agent_id}
              onchange={(next) => { form.agent_id = next; }}
            />
          </div>
          <AgentProfileSelect
            agents={agents}
            agentId={form.agent_id}
            bind:value={form.agent_profile_id}
          />
        </div>

        <div class="space-y-1">
          <label for="edit-desc" class="text-sm font-medium text-foreground">Description</label>
          <textarea
            id="edit-desc"
            bind:value={form.description}
            class="min-h-[60px] max-h-96 w-full rounded-2xl [field-sizing:content] border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet"
          ></textarea>
        </div>

        {#if form.schedule_type === 'cron'}
          <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div class="space-y-1">
              <label for="edit-cron" class="text-sm font-medium text-foreground">Cron expression</label>
              <Input id="edit-cron" bind:value={form.cron_expr} />
            </div>
            <div class="space-y-1">
              <label for="edit-tz" class="text-sm font-medium text-foreground">Timezone</label>
              <select id="edit-tz" bind:value={form.timezone} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                {#each [...new Set([form.timezone, ...timezoneOptions])] as tz}
                  <option value={tz}>{tz}{tz === localTimezone ? ' (local)' : ''}</option>
                {/each}
              </select>
            </div>
          </div>
        {:else if form.schedule_type === 'interval'}
          <div class="space-y-1">
            <label for="edit-interval" class="text-sm font-medium text-foreground">Interval (seconds)</label>
            <Input id="edit-interval" bind:value={form.interval_seconds} type="number" />
          </div>
        {/if}

        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div class="space-y-1">
            <label for="edit-workflow" class="text-sm font-medium text-foreground">Workflow</label>
            <select id="edit-workflow" bind:value={form.workflow_source} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="">General task</option>
              {#each workflowSourceOptions as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          </div>
          <div class="space-y-1">
            <label for="edit-project" class="text-sm font-medium text-foreground">Project</label>
            <select id="edit-project" bind:value={form.project_id} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="">None</option>
              {#each projects as project}
                <option value={project.project_id}>{project.name}</option>
              {/each}
            </select>
          </div>
          <div class="space-y-1 sm:col-span-2">
            <label for="edit-concurrent" class="text-sm font-medium text-foreground">Max concurrent runs</label>
            <Input id="edit-concurrent" bind:value={form.max_concurrent_runs} type="number" />
          </div>
        </div>

        <label class="flex items-start gap-3 text-sm text-secondary">
          <input type="checkbox" bind:checked={form.retry_failed_tasks} class="mt-1" />
          <span>Retry failed tasks before the next regular schedule time.</span>
        </label>
        <label class="flex items-start gap-3 text-sm text-secondary">
          <input type="checkbox" bind:checked={form.fail_paused_task_on_next_fire} class="mt-1" />
          <span>Fail paused tasks when the next regular run starts.</span>
        </label>

        <div class="space-y-1">
          <label for="edit-task-title" class="text-sm font-medium text-foreground">Task title</label>
          <Input id="edit-task-title" bind:value={form.task_title} placeholder="Title for created tasks" />
        </div>

        <div class="space-y-1">
          <label for="edit-task-desc" class="text-sm font-medium text-foreground">Task prompt</label>
          <textarea
            id="edit-task-desc"
            bind:value={form.task_description}
            class="min-h-[80px] max-h-96 w-full rounded-2xl [field-sizing:content] border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet"
            placeholder="Instructions for the agent"
          ></textarea>
        </div>

        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div class="space-y-1">
            <label for="edit-priority" class="text-sm font-medium text-foreground">Priority</label>
            <Input id="edit-priority" bind:value={form.priority} type="number" />
          </div>
          <div class="space-y-1">
            <label for="edit-delivery" class="text-sm font-medium text-foreground">Delivery</label>
            <select id="edit-delivery" bind:value={form.delivery_mode} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="preferred_channel">Preferred channel</option>
              <option value="in_app_notifications">In-app notifications (direct only)</option>
              <option value="latest_active_for_agent">Latest active conversation</option>
              <option value="specific_conversation">Specific conversation</option>
            </select>
          </div>
        </div>

        {#if form.delivery_mode === 'specific_conversation'}
          <div class="space-y-1">
            <label for="edit-target" class="text-sm font-medium text-foreground">Target conversation</label>
            <select id="edit-target" bind:value={form.delivery_target} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
              <option value="">Select conversation</option>
              {#if form.delivery_target && !conversations.some((item) => item.conversation_id === form.delivery_target)}
                <option value={form.delivery_target}>{form.delivery_target}</option>
              {/if}
              {#each conversations as conv}
                <option value={conv.conversation_id}>{conversationLabel(conv)}</option>
              {/each}
            </select>
          </div>
        {/if}

        <div class="space-y-1">
          <label for="edit-expected" class="text-sm font-medium text-foreground">Expected output <span class="normal-case tracking-normal text-quiet">(optional)</span></label>
          <textarea
            id="edit-expected"
            bind:value={form.expected_output}
            class="min-h-[60px] max-h-96 w-full rounded-2xl [field-sizing:content] border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet"
            placeholder="What should the agent produce? Used for step evaluation."
          ></textarea>
        </div>

        <div class="space-y-1">
          <label for="edit-completion-family" class="text-sm font-medium text-foreground">Completion notification behavior</label>
          <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
            <select id="edit-completion-family" bind:value={form.completion_mode_family} disabled={form.delivery_mode === 'in_app_notifications'} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
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
          <label for="edit-interaction" class="text-sm font-medium text-foreground">Interaction policy</label>
          <select id="edit-interaction" bind:value={form.interaction_mode_override} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
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

        <div class="sticky bottom-0 z-10 -mx-6 -mb-6 flex flex-wrap justify-end gap-2 rounded-b-3xl border-t border-separator bg-surface/95 px-6 py-3 backdrop-blur">
          <Button variant="secondary" onclick={cancelEditing}>Cancel</Button>
          {#if form.delivery_mode === 'specific_conversation' && conversationsError}
            <p role="alert">{conversationsError}</p>
            <Button variant="secondary" onclick={() => void loadConversations()}>Retry conversations</Button>
          {/if}
          <Button disabled={saving || (form.delivery_mode === 'specific_conversation' && (conversationsLoading || !!conversationsError || !form.delivery_target))} onclick={handleSave}>
            <Save class="mr-1.5 h-4 w-4" />
            {saving ? 'Saving...' : 'Save changes'}
          </Button>
        </div>
      </div>
    </Card>

    </fieldset>
    {/if}
    <!-- Run history -->
    <Card class="p-6">
      <h2 class="mb-4 text-lg font-semibold text-strong">Run history</h2>
      {#if runsLoading}
        <p class="text-sm text-quiet">Loading runs...</p>
      {:else if runsError}
        <p role="alert">{runsError}</p>
      {:else if runs.length === 0}
        <p class="text-sm text-quiet">No runs yet.</p>
      {:else}
        <div class="space-y-2">
          {#each runs as run (run.task_id)}
            <button
              class="flex w-full items-center justify-between rounded-xl border border-separator p-3 text-left transition-colors hover:border-control-border"
              onclick={() => onOpenTask(run.task_id)}
            >
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm text-strong">{run.title}</p>
                <p class="text-xs text-quiet">{formatDate(run.created_at)}</p>
              </div>
              <span class="shrink-0"><StatusBadge status={runStatus(run.status)} /></span>
            </button>
          {/each}
        </div>
      {/if}
    </Card>
  </div>
{/if}
