<script lang="ts">
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import { untrack } from 'svelte';
  import TaskFinishInstructions from './TaskFinishInstructions.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import SessionPolicyEditor from '$lib/components/SessionPolicyEditor.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import AgentProfileSelect from '$lib/components/AgentProfileSelect.svelte';
  import { api } from '$lib/api/client';
  import { COMPLETION_MODE_OPTIONS } from '$lib/task-delivery';
  import { normalizeSelectedAgentProfileId } from '$lib/agents';
  import { DEFAULT_INTARIS_EVALUATION, policyFromText } from '$lib/session-policy';
  import { buildWorkflowSourceOptions, decodeWorkflowSourceValue } from '$lib/workflow-sources';
  import type { Agent, CompletionModeFamily, Conversation, Project, Skill, Workflow } from '$lib/types/api';

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
      agent_profile_id: string | null;
      workflow_id: string | null;
      project_id: string | null;
      skill_id: string | null;
      priority: number;
      expected_output: string | null;
      delivery_mode: string;
      delivery_target: string | null;
      completion_mode_family: CompletionModeFamily;
      allow_silent_completion: boolean;
      on_finish_instructions: string | null;
      interaction_mode_override: 'none' | 'explicit_gates' | 'step_requests' | null;
      escalation_timeout_seconds: number | null;
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
    agent_id: untrack(() => defaultAgentId),
    agent_profile_id: '',
    workflow_source: '',
    project_id: '',
    expected_output: '',
    priority: 0,
    delivery_mode: 'preferred_channel',
    delivery_target: '',
    completion_mode_family: 'auto' as CompletionModeFamily,
    allow_silent_completion: false,
    on_finish_instructions: '',
    interaction_mode_override: '' as '' | 'none' | 'explicit_gates' | 'step_requests',
    escalation_timeout_seconds: '',
    allow_policy_text: '',
    intaris_evaluation: '' as '' | 'legacy' | 'strict' | 'balanced' | 'bounded_autonomy',
    deny_policy_text: ''
  });
  let initialForm = $state(untrack(() => JSON.stringify(form)));

  function handleSubmit(): void {
    if (!form.title.trim() || !form.agent_id) return;
    const workflowSource = decodeWorkflowSourceValue(form.workflow_source);
    oncreate({
      agent_id: form.agent_id,
      agent_profile_id: form.agent_profile_id || null,
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
      on_finish_instructions: form.completion_mode_family !== 'direct' ? form.on_finish_instructions.trim() || null : null,
      interaction_mode_override: form.interaction_mode_override || null,
      escalation_timeout_seconds: form.escalation_timeout_seconds
        ? Number(form.escalation_timeout_seconds)
        : null,
      session_policy: policyFromText(form.allow_policy_text, form.deny_policy_text, form.intaris_evaluation),
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
      const pristine = JSON.stringify(form) === initialForm;
      form.agent_id = defaultAgentId;
      if (pristine) initialForm = JSON.stringify(form);
    }
  });

  $effect(() => {
    const normalized = normalizeSelectedAgentProfileId(selectedAgent, form.agent_profile_id);
    if (normalized !== form.agent_profile_id) {
      const pristine = JSON.stringify(form) === initialForm;
      form.agent_profile_id = normalized;
      if (pristine) initialForm = JSON.stringify(form);
    }
  });

  $effect(() => {
    projectWorkflows = workflows;
  });

  $effect(() => {
    void loadWorkflowsForProject(form.project_id);
  });
</script>
<BlockingDialog label="Create task" onClose={onclose} titleId="create-task-title" dismissible={!creating} backdropDismissible={JSON.stringify(form) === initialForm}>
  {#snippet header()}
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-lg font-semibold text-strong" id="create-task-title">Create task</h2>
      <button class="text-muted hover:text-strong" onclick={onclose} aria-label="Close" type="button">&times;</button>
    </div>
  {/snippet}

  {#snippet children()}
    <div class="space-y-4">
      <div class="space-y-1">
        <label for="task-title" class="text-sm font-medium text-foreground">Title</label>
        <Input id="task-title" bind:value={form.title} placeholder="Task title" />
      </div>
      <div class="space-y-1">
        <label for="task-desc" class="text-sm font-medium text-foreground">Description</label>
        <textarea
          id="task-desc"
          bind:value={form.description}
          class="min-h-[100px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet"
          placeholder="Describe the work item"
        ></textarea>
      </div>

      <div class="space-y-1">
        <label for="task-expected" class="text-sm font-medium text-foreground">Expected output</label>
        <textarea
          id="task-expected"
          bind:value={form.expected_output}
          class="min-h-[60px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet"
          placeholder="Describe the expected format or content of the result (optional)"
        ></textarea>
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div class="space-y-1">
          <label for="task-agent" class="block text-sm font-medium text-foreground">Agent</label>
          <AgentSelect id="task-agent" agents={primaryAgents} value={form.agent_id} onchange={(next) => { form.agent_id = next; }} />
        </div>

        <div class="space-y-1">
          <label for="task-workflow" class="text-sm font-medium text-foreground">Workflow</label>
          <select id="task-workflow" bind:value={form.workflow_source} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
            <option value="">General task</option>
            {#each workflowSourceOptions as option}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
        </div>
        <AgentProfileSelect
          agents={primaryAgents}
          agentId={form.agent_id}
          bind:value={form.agent_profile_id}
        />
        <div class="space-y-1">
          <label for="task-project" class="text-sm font-medium text-foreground">Project</label>
          <select id="task-project" bind:value={form.project_id} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
            <option value="">None</option>
            {#each projects as project}
              <option value={project.project_id}>{project.name}</option>
            {/each}
          </select>
        </div>
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div class="space-y-1">
          <label for="task-priority" class="text-sm font-medium text-foreground">Priority</label>
          <Input id="task-priority" bind:value={form.priority} type="number" />
        </div>

        <div class="space-y-1">
          <label for="task-delivery" class="text-sm font-medium text-foreground">Delivery</label>
          <select id="task-delivery" bind:value={form.delivery_mode} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
            <option value="preferred_channel">Preferred channel</option>
            <option value="in_app_notifications">In-app notifications (direct only)</option>
            <option value="specific_conversation">Specific conversation</option>
            <option value="latest_active_for_agent">Latest active</option>
          </select>
        </div>
      </div>

      {#if form.delivery_mode === 'specific_conversation'}
        <div class="space-y-1">
          <label for="task-target" class="text-sm font-medium text-foreground">Target conversation</label>
          <select id="task-target" bind:value={form.delivery_target} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
            <option value="">Select conversation</option>
            {#each conversations as conversation}
              <option value={conversation.conversation_id}>{conversation.title ?? conversation.conversation_id}</option>
            {/each}
          </select>
        </div>
      {/if}

      <div class="space-y-1">
        <label for="task-completion-family" class="text-sm font-medium text-foreground">Completion notification behavior</label>
        <div class="grid gap-3">
          <select id="task-completion-family" bind:value={form.completion_mode_family} disabled={form.delivery_mode === 'in_app_notifications'} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
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
        <label for="task-interaction" class="text-sm font-medium text-foreground">Interaction policy</label>
        <select id="task-interaction" bind:value={form.interaction_mode_override} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
          <option value="">Workflow default</option>
          <option value="step_requests">Allow planning questions</option>
          <option value="explicit_gates">Gates only</option>
          <option value="none">Fully autonomous</option>
        </select>
        <p class="text-xs text-quiet">Workflow default allows clarification in selected planning steps. Fully autonomous disables dynamic questions.</p>
      </div>

      <label class="block space-y-1 text-sm text-foreground" for="task-escalation-timeout">
        <span>Escalation timeout (seconds)</span>
        <input
          id="task-escalation-timeout"
          type="number"
          min="1"
          max="86400"
          step="1"
          bind:value={form.escalation_timeout_seconds}
          placeholder="Inherit agent or global timeout"
          class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
        />
        <span class="block text-xs text-quiet">Pending escalations are denied when this time elapses. Fully autonomous tasks deny immediately.</span>
      </label>

      <SessionPolicyEditor
        showEvaluation
        bind:evaluation={form.intaris_evaluation}
        inheritedEvaluation={selectedAgent?.capabilities?.intaris_evaluation ?? DEFAULT_INTARIS_EVALUATION}
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
        {creating ? 'Creating…' : 'Create task'}
      </Button>
    </div>
  {/snippet}
</BlockingDialog>
