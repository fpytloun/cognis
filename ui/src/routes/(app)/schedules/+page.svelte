<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { Agent, Schedule, Workflow } from '$lib/types/api';
  import {
    AlertTriangle,
    Calendar,
    Clock,
    Pause,
    Play,
    Plus,
    RefreshCw,
    Timer,
    Trash2,
    Zap
  } from 'lucide-svelte';

  let loading = $state(true);
  let error = $state('');
  let schedules = $state<Schedule[]>([]);
  let agents = $state<Agent[]>([]);
  let workflows = $state<Workflow[]>([]);
  let search = $state('');
  let filterType = $state<string>('');
  let filterEnabled = $state<string>('');
  let showCreateModal = $state(false);
  let creating = $state(false);

  // Create form state
  let form = $state({
    name: '',
    description: '',
    schedule_type: 'cron',
    cron_expr: '0 9 * * *',
    interval_seconds: 1800,
    one_shot_at: '',
    timezone: 'UTC',
    agent_id: '',
    workflow_id: '',
    task_title: '',
    task_description: '',
    priority: 0,
    suppress_empty: false,
    delivery_mode: 'latest_active_for_agent'
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

  const statusColors: Record<string, string> = {
    success: 'bg-emerald-500/20 text-emerald-400',
    failed: 'bg-red-500/20 text-red-400',
    skipped: 'bg-amber-500/20 text-amber-400'
  };

  let filtered = $derived(
    schedules.filter((s) => {
      if (search && !s.name.toLowerCase().includes(search.toLowerCase())) return false;
      if (filterType && s.schedule_type !== filterType) return false;
      if (filterEnabled === 'enabled' && !s.enabled) return false;
      if (filterEnabled === 'disabled' && s.enabled) return false;
      return true;
    })
  );

  async function loadData(): Promise<void> {
    loading = true;
    error = '';
    try {
      [schedules, agents, workflows] = await Promise.all([
        api.schedules.list(),
        api.agents.listAll({ agent_type: 'primary' }),
        api.workflows.listAll()
      ]);
      if (!form.agent_id && agents.length > 0) {
        const active = agents.find((a) => a.status === 'active');
        form.agent_id = active?.agent_id ?? agents[0]?.agent_id ?? '';
      }
    } catch (e) {
      error = asApiError(e).message;
    } finally {
      loading = false;
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
        delivery: { mode: form.delivery_mode, target: null }
      };
      await api.schedules.create({
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
        suppress_empty: form.suppress_empty
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
      timezone: 'UTC',
      agent_id: agents.find((a) => a.status === 'active')?.agent_id ?? agents[0]?.agent_id ?? '',
      workflow_id: '',
      task_title: '',
      task_description: '',
      priority: 0,
      suppress_empty: false,
      delivery_mode: 'latest_active_for_agent'
    };
  }

  function openHeartbeatPreset(): void {
    form.name = 'Heartbeat';
    form.description = 'Periodic check-in: review pending items, check messages, and report anything that needs attention.';
    form.schedule_type = 'interval';
    form.interval_seconds = 1800;
    form.suppress_empty = true;
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
    try {
      await api.schedules.trigger(schedule.schedule_id);
      addToast(`${schedule.name} triggered`, 'success');
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
    if (!confirmed) return;
    try {
      await api.schedules.remove(schedule.schedule_id);
      addToast(`${schedule.name} deleted`, 'success');
      await loadData();
    } catch (e) {
      addToast(asApiError(e).message, 'error');
    }
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
    if (diffMs < 0) return 'Overdue';
    if (diffMs < 60_000) return 'Less than a minute';
    if (diffMs < 3_600_000) return `${Math.round(diffMs / 60_000)}m`;
    if (diffMs < 86_400_000) return `${Math.round(diffMs / 3_600_000)}h`;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function handleBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) showCreateModal = false;
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') showCreateModal = false;
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

  onMount(loadData);
</script>

<svelte:head>
  <title>Schedules - Cognis</title>
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
{:else}
  <div class="mx-auto max-w-6xl space-y-6 p-4 sm:p-6">
    <!-- Header -->
    <div class="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-2xl font-bold text-white">Schedules</h1>
        <p class="mt-1 text-sm text-slate-400">Automated task creation on cron, interval, or one-shot triggers</p>
      </div>
      <div class="flex gap-2">
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
      </div>
    </div>

    <!-- Filters -->
    <div class="flex flex-wrap gap-3">
      <Input bind:value={search} placeholder="Search schedules..." class="w-64" />
      <select
        bind:value={filterType}
        class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
      >
        <option value="">All types</option>
        <option value="cron">Cron</option>
        <option value="interval">Interval</option>
        <option value="one_shot">One-shot</option>
      </select>
      <select
        bind:value={filterEnabled}
        class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
      >
        <option value="">All states</option>
        <option value="enabled">Enabled</option>
        <option value="disabled">Disabled</option>
      </select>
    </div>

    <!-- Schedule list -->
    {#if filtered.length === 0}
      <Card class="p-12 text-center">
        <Clock class="mx-auto mb-3 h-10 w-10 text-slate-500" />
        <p class="text-slate-400">
          {schedules.length === 0 ? 'No schedules yet. Create one to automate task creation.' : 'No schedules match your filters.'}
        </p>
      </Card>
    {:else}
      <div class="space-y-3">
        {#each filtered as schedule (schedule.schedule_id)}
          {@const TypeIcon = typeIcons[schedule.schedule_type] ?? Clock}
          <Card class="group relative overflow-hidden transition-colors hover:border-slate-600">
            <button
              class="w-full p-4 text-left"
              onclick={() => goto(`/schedules/${schedule.schedule_id}`)}
            >
              <div class="flex items-start justify-between gap-4">
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <TypeIcon class="h-4 w-4 shrink-0 text-slate-400" />
                    <h3 class="truncate font-medium text-white">{schedule.name}</h3>
                    {#if !schedule.enabled}
                      <Badge class="bg-slate-700/50 text-slate-400">Disabled</Badge>
                    {/if}
                    {#if schedule.suppress_empty}
                      <Badge class="bg-indigo-500/20 text-indigo-400">Heartbeat</Badge>
                    {/if}
                    {#if schedule.consecutive_errors > 0}
                      <Badge class="bg-red-500/20 text-red-400">
                        <AlertTriangle class="mr-1 h-3 w-3" />
                        {schedule.consecutive_errors} errors
                      </Badge>
                    {/if}
                  </div>
                  <p class="mt-1 text-sm text-slate-400">
                    {schedule.human_schedule ?? schedule.cron_expr ?? `Every ${schedule.interval_seconds}s`}
                    <span class="mx-1.5 text-slate-600">·</span>
                    {agentName(schedule.agent_id)}
                  </p>
                  {#if schedule.description}
                    <p class="mt-1 truncate text-xs text-slate-500">{schedule.description}</p>
                  {/if}
                </div>

                <div class="flex shrink-0 items-center gap-3 text-right">
                  <div class="text-xs text-slate-500">
                    {#if schedule.last_run_status}
                      <span class={`inline-block rounded-full px-2 py-0.5 text-xs ${statusColors[schedule.last_run_status] ?? 'bg-slate-700/50 text-slate-400'}`}>
                        {schedule.last_run_status}
                      </span>
                    {/if}
                    {#if schedule.next_fire_at && schedule.enabled}
                      <div class="mt-1">Next: {formatNextFire(schedule.next_fire_at)}</div>
                    {/if}
                  </div>
                </div>
              </div>
            </button>

            <!-- Quick actions (visible on hover) -->
            <div class="absolute right-3 top-3 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
              <Tooltip text={schedule.enabled ? 'Disable' : 'Enable'}>
                <button
                  class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white"
                  onclick={(e: MouseEvent) => { e.stopPropagation(); toggleEnabled(schedule); }}
                >
                  {#if schedule.enabled}
                    <Pause class="h-3.5 w-3.5" />
                  {:else}
                    <Play class="h-3.5 w-3.5" />
                  {/if}
                </button>
              </Tooltip>
              <Tooltip text="Trigger now">
                <button
                  class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white"
                  onclick={(e: MouseEvent) => { e.stopPropagation(); triggerNow(schedule); }}
                >
                  <Zap class="h-3.5 w-3.5" />
                </button>
              </Tooltip>
              <Tooltip text="Delete">
                <button
                  class="rounded-lg p-1.5 text-slate-400 hover:bg-red-900/50 hover:text-red-400"
                  onclick={(e: MouseEvent) => { e.stopPropagation(); deleteSchedule(schedule); }}
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </Tooltip>
            </div>
          </Card>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<svelte:window onkeydown={handleKeydown} />

<!-- Create schedule modal -->
{#if showCreateModal}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
    onclick={handleBackdropClick}
  >
    <div class="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-3xl border border-slate-700 bg-slate-900 p-6 shadow-2xl" role="dialog" aria-modal="true" aria-label="Create schedule">
      <div class="mb-5 flex items-center justify-between">
        <h2 class="text-lg font-semibold text-white">Create schedule</h2>
        <button class="text-slate-400 hover:text-white" onclick={() => (showCreateModal = false)} aria-label="Close">&times;</button>
      </div>

      <div class="space-y-4">
        <!-- Name -->
        <div class="space-y-1">
          <label for="sched-name" class="text-xs font-medium uppercase tracking-widest text-slate-400">Name</label>
          <Input id="sched-name" bind:value={form.name} placeholder="e.g. Daily review, Email check" />
        </div>

        <!-- Description -->
        <div class="space-y-1">
          <label for="sched-desc" class="text-xs font-medium uppercase tracking-widest text-slate-400">Description</label>
          <textarea
            id="sched-desc"
            bind:value={form.description}
            class="min-h-[60px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
            placeholder="What does this schedule do?"
          ></textarea>
        </div>

        <!-- Schedule type -->
        <div class="space-y-1">
          <label class="text-xs font-medium uppercase tracking-widest text-slate-400">Schedule type</label>
          <div class="flex gap-2">
            {#each [['cron', 'Cron'], ['interval', 'Interval'], ['one_shot', 'One-shot']] as [value, label]}
              <button
                class="flex-1 rounded-xl border px-3 py-2 text-sm transition-colors {form.schedule_type === value ? 'border-blue-500 bg-blue-500/10 text-blue-400' : 'border-slate-700 bg-slate-950/80 text-slate-400 hover:border-slate-600'}"
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
            <label for="sched-cron" class="text-xs font-medium uppercase tracking-widest text-slate-400">Cron expression</label>
            <Input id="sched-cron" bind:value={form.cron_expr} placeholder="0 9 * * *" />
            <div class="flex flex-wrap gap-1.5 pt-1">
              {#each cronPresets as preset}
                <button
                  class="rounded-lg border border-slate-700 px-2 py-0.5 text-xs text-slate-400 transition-colors hover:border-slate-500 hover:text-slate-200 {form.cron_expr === preset.value ? 'border-blue-500/50 bg-blue-500/10 text-blue-400' : ''}"
                  onclick={() => (form.cron_expr = preset.value)}
                >
                  {preset.label}
                </button>
              {/each}
            </div>
          </div>
          <div class="space-y-1">
            <label for="sched-tz" class="text-xs font-medium uppercase tracking-widest text-slate-400">Timezone</label>
            <Input id="sched-tz" bind:value={form.timezone} placeholder="UTC" />
          </div>
        {:else if form.schedule_type === 'interval'}
          <div class="space-y-1">
            <label class="text-xs font-medium uppercase tracking-widest text-slate-400">Interval</label>
            <div class="flex flex-wrap gap-1.5">
              {#each intervalPresets as preset}
                <button
                  class="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-400 transition-colors hover:border-slate-500 hover:text-slate-200 {form.interval_seconds === preset.value ? 'border-blue-500/50 bg-blue-500/10 text-blue-400' : ''}"
                  onclick={() => (form.interval_seconds = preset.value)}
                >
                  {preset.label}
                </button>
              {/each}
            </div>
            <Input id="sched-interval" bind:value={form.interval_seconds} type="number" placeholder="Seconds" />
          </div>
        {:else}
          <div class="space-y-1">
            <label for="sched-at" class="text-xs font-medium uppercase tracking-widest text-slate-400">Run at</label>
            <Input id="sched-at" bind:value={form.one_shot_at} type="datetime-local" />
          </div>
        {/if}

        <!-- Agent + Workflow -->
        <div class="grid grid-cols-2 gap-4">
          <div class="space-y-1">
            <label for="sched-agent" class="text-xs font-medium uppercase tracking-widest text-slate-400">Agent</label>
            <select id="sched-agent" bind:value={form.agent_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              {#each agents as agent}
                <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
              {/each}
            </select>
          </div>
          <div class="space-y-1">
            <label for="sched-workflow" class="text-xs font-medium uppercase tracking-widest text-slate-400">Workflow</label>
            <select id="sched-workflow" bind:value={form.workflow_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Auto</option>
              {#each workflows as workflow}
                <option value={workflow.workflow_id}>{workflow.name}</option>
              {/each}
            </select>
          </div>
        </div>

        <!-- Task template -->
        <div class="space-y-1">
          <label for="sched-task-title" class="text-xs font-medium uppercase tracking-widest text-slate-400">Task title</label>
          <Input id="sched-task-title" bind:value={form.task_title} placeholder="Title for created tasks (defaults to schedule name)" />
        </div>
        <div class="space-y-1">
          <label for="sched-task-desc" class="text-xs font-medium uppercase tracking-widest text-slate-400">Task prompt</label>
          <textarea
            id="sched-task-desc"
            bind:value={form.task_description}
            class="min-h-[80px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
            placeholder="Instructions for the agent when this schedule fires"
          ></textarea>
        </div>

        <!-- Options -->
        <div class="flex items-center gap-3">
          <label class="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" bind:checked={form.suppress_empty} class="rounded border-slate-600 bg-slate-800" />
            Suppress empty results (heartbeat mode)
          </label>
        </div>
      </div>

      <div class="mt-6 flex justify-end gap-3">
        <Button variant="secondary" onclick={() => (showCreateModal = false)}>Cancel</Button>
        <Button disabled={!form.name.trim() || !form.agent_id || creating} onclick={handleCreate}>
          {creating ? 'Creating...' : 'Create schedule'}
        </Button>
      </div>
    </div>
  </div>
{/if}
