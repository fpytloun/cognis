<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import SessionPolicyEditor from '$lib/components/SessionPolicyEditor.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import { api } from '$lib/api/client';
  import { policyFromText } from '$lib/session-policy';
  import { buildWorkflowSourceOptions, decodeWorkflowSourceValue } from '$lib/workflow-sources';
  import type { Agent, Conversation, Project, Skill, Workflow } from '$lib/types/api';

  let {
    agents,
    workflows,
    projects,
    skills,
    conversations,
    creating = false,
    onclose,
    oncreate
  } = $props<{
    agents: Agent[];
    workflows: Workflow[];
    projects: Project[];
    skills: Skill[];
    conversations: Conversation[];
    creating?: boolean;
    onclose: () => void;
    oncreate: (form: {
      title: string;
      description: string;
      agent_id: string;
      workflow_id: string | null;
      project_id: string | null;
      skill_id: string | null;
      priority: number;
      expected_output: string | null;
      delivery_mode: string;
      delivery_target: string | null;
      completion_mode_family: 'default' | 'direct';
      allow_silent_completion: boolean;
      interaction_mode_override: 'none' | 'explicit_gates' | 'step_requests' | null;
      session_policy: ReturnType<typeof policyFromText>;
      status: string;
    }) => void;
  }>();

  const primaryAgents = $derived(agents.filter((a: Agent) => a.agent_type === 'primary'));
  const defaultAgentId = $derived(
    primaryAgents.find((a: Agent) => a.status === 'active')?.agent_id ?? primaryAgents[0]?.agent_id ?? ''
  );
  const selectedAgent = $derived(
    primaryAgents.find((agent: Agent) => agent.agent_id === form.agent_id) ?? null
  );
  let projectWorkflows = $state<Workflow[]>([]);
  let workflowLoadKey = 0;
  let lastAutoProjectWorkflow = $state('');
  const workflowSourceOptions = $derived(buildWorkflowSourceOptions(projectWorkflows, skills, selectedAgent));

  let form = $state({
    title: '',
    description: '',
    agent_id: '',
    workflow_source: '',
    project_id: '',
    expected_output: '',
    priority: 0,
    delivery_mode: 'preferred_channel',
    delivery_target: '',
    completion_mode_family: 'default' as 'default' | 'direct',
    allow_silent_completion: false,
    interaction_mode_override: '' as '' | 'none' | 'explicit_gates' | 'step_requests',
    allow_policy_text: '',
    deny_policy_text: ''
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
      project_id: form.project_id || null,
      skill_id: workflowSource.skill_id,
      priority: Number(form.priority),
      delivery_mode: form.delivery_mode,
      delivery_target: form.delivery_mode === 'specific_conversation' ? form.delivery_target : null,
      completion_mode_family: form.completion_mode_family,
      allow_silent_completion: form.allow_silent_completion,
      interaction_mode_override: form.interaction_mode_override || null,
      session_policy: policyFromText(form.allow_policy_text, form.deny_policy_text),
      status: 'draft'
    });
  }

  async function loadWorkflowsForProject(projectId: string): Promise<void> {
    const key = ++workflowLoadKey;
    try {
      const next = await api.workflows.listAll({ project_id: projectId || null });
      if (key !== workflowLoadKey) return;
      projectWorkflows = next;
      const selectedProject = projects.find((project: Project) => project.project_id === projectId);
      if (form.workflow_source && form.workflow_source === lastAutoProjectWorkflow) {
        form.workflow_source = '';
        lastAutoProjectWorkflow = '';
      }
      if (selectedProject?.default_workflow_id && !form.workflow_source) {
        lastAutoProjectWorkflow = `workflow:${selectedProject.default_workflow_id}`;
        form.workflow_source = lastAutoProjectWorkflow;
      }
      const values = new Set(buildWorkflowSourceOptions(projectWorkflows, skills, selectedAgent).map((option) => option.value));
      if (form.workflow_source && !values.has(form.workflow_source)) {
        form.workflow_source = '';
        lastAutoProjectWorkflow = '';
      }
    } catch {
      if (key === workflowLoadKey) projectWorkflows = workflows;
    }
  }

  $effect(() => {
    if (!form.agent_id && defaultAgentId) {
      form.agent_id = defaultAgentId;
    }
  });

  $effect(() => {
    projectWorkflows = workflows;
  });

  $effect(() => {
    void loadWorkflowsForProject(form.project_id);
  });
</script>
<BlockingDialog label="Create task" onClose={onclose} titleId="create-task-title">
  {#snippet header()}
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-lg font-semibold text-white" id="create-task-title">Create task</h2>
      <button class="text-slate-400 hover:text-white" onclick={onclose} aria-label="Close" type="button">&times;</button>
    </div>
  {/snippet}

  {#snippet children()}
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
            <option value="">Auto{form.project_id ? ' (project-aware)' : ''}</option>
            {#each workflowSourceOptions as option}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
        </div>
        <div class="space-y-1">
          <label for="task-project" class="text-xs font-medium uppercase tracking-widest text-slate-400">Project</label>
          <select id="task-project" bind:value={form.project_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="">None</option>
            {#each projects as project}
              <option value={project.project_id}>{project.name}</option>
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
            <option value="preferred_channel">Preferred channel</option>
            <option value="specific_conversation">Specific conversation</option>
            <option value="latest_active_for_agent">Latest active</option>
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

      <div class="space-y-1">
        <label for="task-interaction" class="text-xs font-medium uppercase tracking-widest text-slate-400">Interaction policy</label>
        <select id="task-interaction" bind:value={form.interaction_mode_override} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
          <option value="">Workflow default</option>
          <option value="step_requests">Allow planning questions</option>
          <option value="explicit_gates">Gates only</option>
          <option value="none">Fully autonomous</option>
        </select>
        <p class="text-xs text-slate-500">Workflow default allows clarification in selected planning steps. Fully autonomous disables dynamic questions.</p>
      </div>

      <SessionPolicyEditor
        bind:allowText={form.allow_policy_text}
        bind:denyText={form.deny_policy_text}
        title="Intaris session policies"
      />
    </div>

  {/snippet}

  {#snippet footer()}
    <div class="flex justify-end gap-3">
      <Button variant="secondary" onclick={onclose}>Cancel</Button>
      <Button disabled={!form.title.trim() || !form.agent_id || creating} onclick={handleSubmit}>
        {creating ? 'Creating...' : 'Create draft'}
      </Button>
    </div>
  {/snippet}
</BlockingDialog>
