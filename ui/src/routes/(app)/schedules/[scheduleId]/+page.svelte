<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
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
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, Schedule, ScheduleRun, Workflow } from '$lib/types/api';
  import {
    AlertTriangle,
    ArrowLeft,
    Calendar,
    Check,
    Clock,
    Pause,
    Play,
    Save,
    Timer,
    Trash2,
    X,
    Zap
  } from 'lucide-svelte';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let schedule = $state<Schedule | null>(null);
  let runs = $state<ScheduleRun[]>([]);
  let agents = $state<Agent[]>([]);
  let workflows = $state<Workflow[]>([]);

  let form = $state({
    name: '',
    description: '',
    schedule_type: 'cron',
    cron_expr: '',
    interval_seconds: 0,
    one_shot_at: '',
    timezone: 'UTC',
    agent_id: '',
    workflow_id: '',
    task_title: '',
    task_description: '',
    suppress_empty: false,
    max_concurrent_runs: 1
  });

  const statusColors: Record<string, string> = {
    success: 'bg-emerald-500/20 text-emerald-400',
    failed: 'bg-red-500/20 text-red-400',
    skipped: 'bg-amber-500/20 text-amber-400',
    completed: 'bg-emerald-500/20 text-emerald-400',
    running: 'bg-blue-500/20 text-blue-400',
    queued: 'bg-slate-500/20 text-slate-400',
    ready: 'bg-slate-500/20 text-slate-400',
    cancelled: 'bg-slate-500/20 text-slate-400'
  };

  function scheduleId(): string {
    return $page.params.scheduleId;
  }

  async function loadData(): Promise<void> {
    loading = true;
    error = '';
    try {
      const [sched, agentList, workflowList, runList] = await Promise.all([
        api.schedules.detail(scheduleId()),
        api.agents.listAll({ agent_type: 'primary' }),
        api.workflows.listAll(),
        api.schedules.runs(scheduleId())
      ]);
      schedule = sched;
      agents = agentList;
      workflows = workflowList;
      runs = runList;
      populateForm(sched);
    } catch (e) {
      error = asApiError(e).message;
    } finally {
      loading = false;
    }
  }

  function populateForm(s: Schedule): void {
    const tmpl = s.task_template ?? {};
    form = {
      name: s.name,
      description: s.description ?? '',
      schedule_type: s.schedule_type,
      cron_expr: s.cron_expr ?? '',
      interval_seconds: s.interval_seconds ?? 0,
      one_shot_at: s.one_shot_at ?? '',
      timezone: s.timezone,
      agent_id: s.agent_id,
      workflow_id: s.workflow_id ?? '',
      task_title: (tmpl.title as string) ?? '',
      task_description: (tmpl.description as string) ?? '',
      suppress_empty: s.suppress_empty,
      max_concurrent_runs: s.max_concurrent_runs
    };
  }

  async function handleSave(): Promise<void> {
    if (!schedule) return;
    saving = true;
    try {
      const taskTemplate: Record<string, unknown> = {
        ...(schedule.task_template ?? {}),
        title: form.task_title || form.name,
        description: form.task_description
      };
      const updated = await api.schedules.update(schedule.schedule_id, {
        name: form.name,
        description: form.description || null,
        schedule_type: form.schedule_type,
        cron_expr: form.schedule_type === 'cron' ? form.cron_expr : null,
        interval_seconds: form.schedule_type === 'interval' ? form.interval_seconds : null,
        one_shot_at: form.schedule_type === 'one_shot' ? form.one_shot_at : null,
        timezone: form.timezone,
        agent_id: form.agent_id,
        workflow_id: form.workflow_id || null,
        task_template: taskTemplate,
        suppress_empty: form.suppress_empty,
        max_concurrent_runs: form.max_concurrent_runs
      });
      schedule = updated;
      addToast('Schedule saved', 'success');
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    } finally {
      saving = false;
    }
  }

  async function toggleEnabled(): Promise<void> {
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
    if (!schedule) return;
    try {
      schedule = await api.schedules.trigger(schedule.schedule_id);
      addToast('Schedule triggered', 'success');
      runs = await api.schedules.runs(schedule.schedule_id);
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
  }

  async function handleDelete(): Promise<void> {
    if (!schedule) return;
    const confirmed = await confirmAction({
      title: `Delete "${schedule.name}"?`,
      message: 'This will permanently remove the schedule.',
      confirmLabel: 'Delete'
    });
    if (!confirmed) return;
    try {
      await api.schedules.remove(schedule.schedule_id);
      addToast('Schedule deleted', 'success');
      goto('/schedules');
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
  }

  function agentName(agentId: string): string {
    const agent = agents.find((a) => a.agent_id === agentId);
    return agent?.display_name ?? agent?.name ?? agentId;
  }

  function formatDate(iso: string | null): string {
    if (!iso) return 'N/A';
    return new Date(iso).toLocaleString();
  }

  onMount(loadData);
</script>

<svelte:head>
  <title>{schedule?.name ?? 'Schedule'} - Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState />
{:else if error}
  <div class="flex items-center justify-center p-12">
    <Card class="max-w-md p-6 text-center">
      <p class="text-red-400">{error}</p>
      <Button class="mt-4" onclick={loadData}>Retry</Button>
    </Card>
  </div>
{:else if schedule}
  <div class="mx-auto max-w-4xl space-y-6 p-4 sm:p-6">
    <!-- Header -->
    <div class="flex items-center gap-3">
      <button class="text-slate-400 hover:text-white" onclick={() => goto('/schedules')}>
        <ArrowLeft class="h-5 w-5" />
      </button>
      <div class="flex-1">
        <div class="flex items-center gap-2">
          <h1 class="text-2xl font-bold text-white">{schedule.name}</h1>
          {#if !schedule.enabled}
            <Badge class="bg-slate-700/50 text-slate-400">Disabled</Badge>
          {/if}
          {#if schedule.suppress_empty}
            <Badge class="bg-indigo-500/20 text-indigo-400">Heartbeat</Badge>
          {/if}
        </div>
        <p class="mt-0.5 text-sm text-slate-400">{schedule.human_schedule ?? schedule.cron_expr}</p>
      </div>
      <div class="flex gap-2">
        <Tooltip text={schedule.enabled ? 'Disable' : 'Enable'}>
          <Button variant="secondary" onclick={toggleEnabled}>
            {#if schedule.enabled}
              <Pause class="h-4 w-4" />
            {:else}
              <Play class="h-4 w-4" />
            {/if}
          </Button>
        </Tooltip>
        <Tooltip text="Trigger now">
          <Button variant="secondary" onclick={triggerNow}>
            <Zap class="h-4 w-4" />
          </Button>
        </Tooltip>
        <Tooltip text="Delete">
          <Button variant="secondary" onclick={handleDelete}>
            <Trash2 class="h-4 w-4" />
          </Button>
        </Tooltip>
      </div>
    </div>

    <!-- Status card -->
    {#if schedule.disabled_reason || schedule.consecutive_errors > 0}
      <Card class="border-red-900/50 bg-red-950/20 p-4">
        <div class="flex items-start gap-3">
          <AlertTriangle class="mt-0.5 h-5 w-5 shrink-0 text-red-400" />
          <div>
            {#if schedule.disabled_reason}
              <p class="font-medium text-red-400">{schedule.disabled_reason}</p>
            {/if}
            {#if schedule.consecutive_errors > 0}
              <p class="text-sm text-red-400/80">{schedule.consecutive_errors} consecutive error(s)</p>
            {/if}
          </div>
        </div>
      </Card>
    {/if}

    <!-- Status overview -->
    <div class="grid grid-cols-2 gap-4 sm:grid-cols-4">
      <Card class="p-4">
        <p class="text-xs font-medium uppercase tracking-widest text-slate-500">Next fire</p>
        <p class="mt-1 text-sm text-white">{formatDate(schedule.next_fire_at)}</p>
      </Card>
      <Card class="p-4">
        <p class="text-xs font-medium uppercase tracking-widest text-slate-500">Last fired</p>
        <p class="mt-1 text-sm text-white">{formatDate(schedule.last_fired_at)}</p>
      </Card>
      <Card class="p-4">
        <p class="text-xs font-medium uppercase tracking-widest text-slate-500">Last status</p>
        <p class="mt-1">
          {#if schedule.last_run_status}
            <span class={`inline-block rounded-full px-2 py-0.5 text-xs ${statusColors[schedule.last_run_status] ?? 'text-slate-400'}`}>
              {schedule.last_run_status}
            </span>
          {:else}
            <span class="text-sm text-slate-500">Never run</span>
          {/if}
        </p>
      </Card>
      <Card class="p-4">
        <p class="text-xs font-medium uppercase tracking-widest text-slate-500">Agent</p>
        <p class="mt-1 text-sm text-white">{agentName(schedule.agent_id)}</p>
      </Card>
    </div>

    <!-- Edit form -->
    <Card class="p-6">
      <h2 class="mb-4 text-lg font-semibold text-white">Configuration</h2>
      <div class="space-y-4">
        <div class="grid grid-cols-2 gap-4">
          <div class="space-y-1">
            <label for="edit-name" class="text-xs font-medium uppercase tracking-widest text-slate-400">Name</label>
            <Input id="edit-name" bind:value={form.name} />
          </div>
          <div class="space-y-1">
            <label for="edit-agent" class="text-xs font-medium uppercase tracking-widest text-slate-400">Agent</label>
            <select id="edit-agent" bind:value={form.agent_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              {#each agents as agent}
                <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
              {/each}
            </select>
          </div>
        </div>

        <div class="space-y-1">
          <label for="edit-desc" class="text-xs font-medium uppercase tracking-widest text-slate-400">Description</label>
          <textarea
            id="edit-desc"
            bind:value={form.description}
            class="min-h-[60px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
          ></textarea>
        </div>

        {#if form.schedule_type === 'cron'}
          <div class="grid grid-cols-2 gap-4">
            <div class="space-y-1">
              <label for="edit-cron" class="text-xs font-medium uppercase tracking-widest text-slate-400">Cron expression</label>
              <Input id="edit-cron" bind:value={form.cron_expr} />
            </div>
            <div class="space-y-1">
              <label for="edit-tz" class="text-xs font-medium uppercase tracking-widest text-slate-400">Timezone</label>
              <select id="edit-tz" bind:value={form.timezone} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                {#each [...new Set([form.timezone, ...timezoneOptions])] as tz}
                  <option value={tz}>{tz}{tz === localTimezone ? ' (local)' : ''}</option>
                {/each}
              </select>
            </div>
          </div>
        {:else if form.schedule_type === 'interval'}
          <div class="space-y-1">
            <label for="edit-interval" class="text-xs font-medium uppercase tracking-widest text-slate-400">Interval (seconds)</label>
            <Input id="edit-interval" bind:value={form.interval_seconds} type="number" />
          </div>
        {/if}

        <div class="grid grid-cols-2 gap-4">
          <div class="space-y-1">
            <label for="edit-workflow" class="text-xs font-medium uppercase tracking-widest text-slate-400">Workflow</label>
            <select id="edit-workflow" bind:value={form.workflow_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Auto</option>
              {#each workflows as workflow}
                <option value={workflow.workflow_id}>{workflow.name}</option>
              {/each}
            </select>
          </div>
          <div class="space-y-1">
            <label for="edit-concurrent" class="text-xs font-medium uppercase tracking-widest text-slate-400">Max concurrent runs</label>
            <Input id="edit-concurrent" bind:value={form.max_concurrent_runs} type="number" />
          </div>
        </div>

        <div class="space-y-1">
          <label for="edit-task-title" class="text-xs font-medium uppercase tracking-widest text-slate-400">Task title</label>
          <Input id="edit-task-title" bind:value={form.task_title} placeholder="Title for created tasks" />
        </div>

        <div class="space-y-1">
          <label for="edit-task-desc" class="text-xs font-medium uppercase tracking-widest text-slate-400">Task prompt</label>
          <textarea
            id="edit-task-desc"
            bind:value={form.task_description}
            class="min-h-[80px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
            placeholder="Instructions for the agent"
          ></textarea>
        </div>

        <label class="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
          <input type="checkbox" bind:checked={form.suppress_empty} class="rounded border-slate-600 bg-slate-800" />
          Suppress empty results (heartbeat mode)
        </label>

        <div class="flex justify-end">
          <Button disabled={saving} onclick={handleSave}>
            <Save class="mr-1.5 h-4 w-4" />
            {saving ? 'Saving...' : 'Save changes'}
          </Button>
        </div>
      </div>
    </Card>

    <!-- Run history -->
    <Card class="p-6">
      <h2 class="mb-4 text-lg font-semibold text-white">Run history</h2>
      {#if runs.length === 0}
        <p class="text-sm text-slate-500">No runs yet.</p>
      {:else}
        <div class="space-y-2">
          {#each runs as run (run.task_id)}
            <button
              class="flex w-full items-center justify-between rounded-xl border border-slate-800 p-3 text-left transition-colors hover:border-slate-700"
              onclick={() => goto(`/tasks/${run.task_id}`)}
            >
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm text-white">{run.title}</p>
                <p class="text-xs text-slate-500">{formatDate(run.created_at)}</p>
              </div>
              <span class={`shrink-0 rounded-full px-2 py-0.5 text-xs ${statusColors[run.status] ?? 'text-slate-400'}`}>
                {run.status}
              </span>
            </button>
          {/each}
        </div>
      {/if}
    </Card>
  </div>
{/if}
