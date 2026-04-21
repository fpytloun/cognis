<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { buildWorkflowSourceOptions, decodeWorkflowSourceValue } from '$lib/workflow-sources';
  import type { Agent, Conversation, Skill, Workflow } from '$lib/types/api';

  let {
    agents,
    workflows,
    skills,
    conversations,
    creating = false,
    onclose,
    oncreate
  } = $props<{
    agents: Agent[];
    workflows: Workflow[];
    skills: Skill[];
    conversations: Conversation[];
    creating?: boolean;
    onclose: () => void;
    oncreate: (form: {
      title: string;
      description: string;
      agent_id: string;
      workflow_id: string | null;
      skill_id: string | null;
      priority: number;
      expected_output: string | null;
      delivery_mode: string;
      delivery_target: string | null;
      completion_mode_family: 'default' | 'direct';
      allow_silent_completion: boolean;
      status: string;
    }) => void;
  }>();

  const primaryAgents = agents.filter((a: Agent) => a.agent_type === 'primary');
  const defaultAgentId = primaryAgents.find((a: Agent) => a.status === 'active')?.agent_id ?? primaryAgents[0]?.agent_id ?? '';
  const selectedAgent = $derived(
    primaryAgents.find((agent: Agent) => agent.agent_id === form.agent_id) ?? null
  );
  const workflowSourceOptions = $derived(buildWorkflowSourceOptions(workflows, skills, selectedAgent));

  let form = $state({
    title: '',
    description: '',
    agent_id: defaultAgentId,
    workflow_source: '',
    expected_output: '',
    priority: 0,
    delivery_mode: 'same_conversation',
    delivery_target: '',
    completion_mode_family: 'default' as 'default' | 'direct',
    allow_silent_completion: false
  });

  function handleSubmit(): void {
    if (!form.title.trim() || !form.agent_id) return;
    const workflowSource = decodeWorkflowSourceValue(form.workflow_source);
    oncreate({
      agent_id: form.agent_id,
      title: form.title,
      description: form.description,
      expected_output: form.expected_output || null,
      workflow_id: workflowSource.workflow_id,
      skill_id: workflowSource.skill_id,
      priority: Number(form.priority),
      delivery_mode: form.delivery_mode,
      delivery_target: form.delivery_mode === 'specific_conversation' ? form.delivery_target : null,
      completion_mode_family: form.completion_mode_family,
      allow_silent_completion: form.allow_silent_completion,
      status: 'draft'
    });
  }

  function handleBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) onclose();
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') onclose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
  class="app-viewport-overlay z-50 flex items-center justify-center overflow-y-auto overscroll-contain bg-black/60 px-4 py-4 backdrop-blur-sm"
  onclick={handleBackdropClick}
  role="presentation"
>
  <div class="max-h-full w-full max-w-lg overflow-y-auto rounded-3xl border border-slate-700 bg-slate-900 p-6 shadow-2xl overscroll-contain" role="dialog" aria-modal="true" aria-label="Create task">
    <div class="mb-5 flex items-center justify-between">
      <h2 class="text-lg font-semibold text-white">Create task</h2>
      <button class="text-slate-400 hover:text-white" onclick={onclose} aria-label="Close">&times;</button>
    </div>

    <div class="space-y-4">
      <div class="space-y-1">
        <label for="task-title" class="text-xs font-medium uppercase tracking-widest text-slate-400">Title</label>
        <Input id="task-title" bind:value={form.title} placeholder="Task title" />
      </div>

      <div class="space-y-1">
        <label for="task-desc" class="text-xs font-medium uppercase tracking-widest text-slate-400">Description</label>
        <textarea
          id="task-desc"
          bind:value={form.description}
          class="min-h-[100px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
          placeholder="Describe the work item"
        ></textarea>
      </div>

      <div class="space-y-1">
        <label for="task-expected" class="text-xs font-medium uppercase tracking-widest text-slate-400">Expected output</label>
        <textarea
          id="task-expected"
          bind:value={form.expected_output}
          class="min-h-[60px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
          placeholder="Describe the expected format or content of the result (optional)"
        ></textarea>
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div class="space-y-1">
          <label for="task-agent" class="text-xs font-medium uppercase tracking-widest text-slate-400">Agent</label>
          <select id="task-agent" bind:value={form.agent_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            {#each primaryAgents as agent}
              <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
            {/each}
          </select>
        </div>

        <div class="space-y-1">
          <label for="task-workflow" class="text-xs font-medium uppercase tracking-widest text-slate-400">Workflow</label>
          <select id="task-workflow" bind:value={form.workflow_source} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">Auto</option>
            {#each workflowSourceOptions as option}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
        </div>
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div class="space-y-1">
          <label for="task-priority" class="text-xs font-medium uppercase tracking-widest text-slate-400">Priority</label>
          <Input id="task-priority" bind:value={form.priority} type="number" />
        </div>

        <div class="space-y-1">
          <label for="task-delivery" class="text-xs font-medium uppercase tracking-widest text-slate-400">Delivery</label>
          <select id="task-delivery" bind:value={form.delivery_mode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="same_conversation">Same conversation</option>
            <option value="specific_conversation">Specific conversation</option>
            <option value="latest_active_for_agent">Latest active</option>
            <option value="preferred_channel">Preferred channel</option>
          </select>
        </div>
      </div>

      {#if form.delivery_mode === 'specific_conversation'}
        <div class="space-y-1">
          <label for="task-target" class="text-xs font-medium uppercase tracking-widest text-slate-400">Target conversation</label>
          <select id="task-target" bind:value={form.delivery_target} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">Select conversation</option>
            {#each conversations as conversation}
              <option value={conversation.conversation_id}>{conversation.title ?? conversation.conversation_id}</option>
            {/each}
          </select>
        </div>
      {/if}

      <div class="space-y-1">
        <label for="task-completion-family" class="text-xs font-medium uppercase tracking-widest text-slate-400">Completion notification behavior</label>
        <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
          <select id="task-completion-family" bind:value={form.completion_mode_family} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="default">Default delivery</option>
            <option value="direct">Direct channel delivery</option>
          </select>
          <label class="inline-flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm text-slate-200">
            <input type="checkbox" bind:checked={form.allow_silent_completion} class="rounded border-slate-600 bg-slate-800" />
            <span>Allow silent completion</span>
          </label>
        </div>
        <p class="text-xs text-slate-500">Default delivery uses the normal conversation flow. Direct channel delivery sends the final result straight to the resolved target channel.</p>
      </div>
    </div>

    <div class="mt-6 flex justify-end gap-3">
      <Button variant="secondary" onclick={onclose}>Cancel</Button>
      <Button disabled={!form.title.trim() || !form.agent_id || creating} onclick={handleSubmit}>
        {creating ? 'Creating...' : 'Create draft'}
      </Button>
    </div>
  </div>
</div>
