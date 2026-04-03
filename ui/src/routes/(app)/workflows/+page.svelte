<script lang="ts">
  import { beforeNavigate } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { blockNavigationIfDirty, installBeforeUnloadGuard } from '$lib/navigation/unsaved';
  import {
    createEmptyStep,
    createEmptyWorkflowForm,
    exportWorkflowYaml,
    formStateToWorkflowPayload,
    importWorkflowYaml,
    validateWorkflowForm,
    workflowToFormState,
    type WorkflowFormState
  } from '$lib/workflows';
  import type { Agent, Workflow } from '$lib/types/api';

  let loading = true;
  let saving = false;
  let error = '';
  let importText = '';
  let workflows: Workflow[] = [];
  let secondaryAgents: Agent[] = [];
  let selectedWorkflow: Workflow | null = null;
  let form: WorkflowFormState = createEmptyWorkflowForm();
  let dragIndex = -1;
  let initialSnapshot = JSON.stringify(form);

  function isDirty(): boolean {
    return JSON.stringify(form) !== initialSnapshot;
  }

  beforeNavigate((navigation) => {
    if (saving) {
      return;
    }
    blockNavigationIfDirty(navigation, isDirty);
  });

  async function confirmDiscardChanges(): Promise<boolean> {
    if (!isDirty()) {
      return true;
    }
    return confirmAction({
      title: 'Discard unsaved workflow changes?',
      message: 'Switching workflows will replace the current editor contents.',
      confirmLabel: 'Discard changes'
    });
  }

  async function loadWorkflows(selectedId?: string): Promise<void> {
    loading = true;
    error = '';
    try {
      [workflows, secondaryAgents] = await Promise.all([
        api.workflows.listAll(),
        api.agents.listAll({ agent_type: 'secondary' }),
      ]);
      const nextSelected = selectedId ? workflows.find((workflow) => workflow.workflow_id === selectedId) : selectedWorkflow ? workflows.find((workflow) => workflow.workflow_id === selectedWorkflow?.workflow_id) : workflows[0];
      if (nextSelected) {
        selectedWorkflow = nextSelected;
        form = workflowToFormState(nextSelected);
      } else {
        selectedWorkflow = null;
        form = createEmptyWorkflowForm();
      }
      initialSnapshot = JSON.stringify(form);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function selectWorkflow(workflow: Workflow): Promise<void> {
    if (!(await confirmDiscardChanges())) {
      return;
    }
    try {
      const nextForm = workflowToFormState(workflow);
      selectedWorkflow = workflow;
      form = nextForm;
      error = '';
      initialSnapshot = JSON.stringify(form);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to open workflow');
    }
  }

  async function newWorkflow(): Promise<void> {
    if (!(await confirmDiscardChanges())) {
      return;
    }
    selectedWorkflow = null;
    form = createEmptyWorkflowForm();
    error = '';
    initialSnapshot = JSON.stringify(form);
  }

  async function duplicateSelectedWorkflow(): Promise<void> {
    if (!selectedWorkflow) {
      return;
    }
    try {
      const duplicated = await api.workflows.duplicate(selectedWorkflow.workflow_id);
      await loadWorkflows(duplicated.workflow_id);
      addToast('Workflow duplicated.', 'success');
      initialSnapshot = JSON.stringify(form);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to duplicate workflow');
    }
  }

  async function saveWorkflow(): Promise<void> {
    const issues = validateWorkflowForm(form);
    if (issues.length > 0) {
      error = issues.join(' ');
      return;
    }
    saving = true;
    try {
      const payload = formStateToWorkflowPayload(form);
      if (selectedWorkflow && !selectedWorkflow.is_system) {
        const updated = await api.workflows.update(selectedWorkflow.workflow_id, payload);
        await loadWorkflows(updated.workflow_id);
      } else {
        const created = await api.workflows.create(payload);
        await loadWorkflows(created.workflow_id);
      }
      addToast('Workflow saved.', 'success');
      initialSnapshot = JSON.stringify(form);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save workflow');
    } finally {
      saving = false;
    }
  }

  async function deleteSelectedWorkflow(): Promise<void> {
    if (!selectedWorkflow || selectedWorkflow.is_system) {
      return;
    }
    const confirmed = await confirmAction({
      title: 'Delete workflow?',
      message: 'This permanently removes the selected workflow definition.',
      confirmLabel: 'Delete workflow'
    });
    if (!confirmed) {
      return;
    }
    try {
      await api.workflows.remove(selectedWorkflow.workflow_id);
      await newWorkflow();
      await loadWorkflows();
      addToast('Workflow deleted.', 'success');
      initialSnapshot = JSON.stringify(form);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to delete workflow');
    }
  }

  function addStep(): void {
    form.steps = [...form.steps, createEmptyStep()];
  }

  function removeStep(index: number): void {
    form.steps = form.steps.filter((_, candidateIndex) => candidateIndex !== index);
  }

  function moveStep(targetIndex: number): void {
    if (dragIndex < 0 || dragIndex === targetIndex) {
      dragIndex = -1;
      return;
    }
    const steps = [...form.steps];
    const [moved] = steps.splice(dragIndex, 1);
    steps.splice(targetIndex, 0, moved);
    form.steps = steps;
    dragIndex = -1;
  }

  function downloadCurrentWorkflow(): void {
    const blob = new Blob([exportWorkflowYaml(form)], { type: 'text/yaml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${form.workflowId || form.name || 'workflow'}.yaml`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function importYaml(): void {
    try {
      form = importWorkflowYaml(importText);
      selectedWorkflow = null;
      error = '';
      addToast('Workflow imported into the editor.', 'success');
      initialSnapshot = JSON.stringify(form);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to import workflow');
    }
  }

  onMount(() => {
    const cleanup = installBeforeUnloadGuard(isDirty);
    void loadWorkflows();
    return cleanup;
  });
</script>

<svelte:head>
  <title>Workflows · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading workflows" description="Fetching system templates and user-editable workflow definitions." />
{:else}
  <section class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Workflow registry</p>
        <h1 class="mt-1 text-2xl font-semibold text-white">Workflows</h1>
      </div>
      <div class="flex flex-wrap gap-2">
        <Button variant="secondary" onclick={newWorkflow}>New workflow</Button>
        <Button variant="secondary" onclick={duplicateSelectedWorkflow} disabled={!selectedWorkflow}>Duplicate</Button>
        <Button variant="secondary" onclick={downloadCurrentWorkflow}>Export YAML</Button>
        <Button variant="danger" onclick={deleteSelectedWorkflow} disabled={!selectedWorkflow || selectedWorkflow.is_system}>Delete</Button>
        <Button onclick={saveWorkflow} disabled={saving || !!selectedWorkflow?.is_system}>{saving ? 'Saving…' : selectedWorkflow?.is_system ? 'Duplicate to edit' : 'Save workflow'}</Button>
      </div>
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <div class="grid gap-5 xl:grid-cols-[320px_minmax(0,1fr)]">
      <aside class="space-y-5">
        <Card class="p-4">
          <div class="space-y-2">
            {#each workflows as workflow}
              <button class={`w-full rounded-2xl border px-4 py-3 text-left transition ${workflow.workflow_id === selectedWorkflow?.workflow_id ? 'border-sky-400/40 bg-sky-500/10 text-white' : 'border-slate-800 bg-slate-950/70 text-slate-200 hover:border-slate-700'}`} onclick={() => selectWorkflow(workflow)}>
                <div class="flex items-center justify-between gap-3">
                  <span class="font-medium">{workflow.name}</span>
                  <span class="rounded-full border border-slate-700 px-2.5 py-1 text-[11px] uppercase tracking-[0.2em] text-slate-400">{workflow.is_system ? 'system' : 'user'}</span>
                </div>
                <p class="mt-2 text-xs leading-5 text-slate-400">{workflow.workflow_id}</p>
              </button>
            {/each}
          </div>
        </Card>

        <Card class="p-4">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Import YAML</p>
          <textarea bind:value={importText} class="mt-3 min-h-[180px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100 placeholder:text-slate-500"></textarea>
          <Button class="mt-3 w-full justify-center" variant="secondary" onclick={importYaml}>Import into editor</Button>
        </Card>
      </aside>

      <div class="space-y-5">
        {#if selectedWorkflow?.is_system}
          <Card class="border border-sky-500/30 bg-sky-500/10 p-4 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p class="font-medium">System workflow</p>
                <p class="mt-1 text-sky-100/80">This bundled workflow is read-only. Duplicate it to create an editable copy.</p>
              </div>
              <Button variant="secondary" onclick={duplicateSelectedWorkflow}>Duplicate to edit</Button>
            </div>
          </Card>
        {/if}

        <Card class="p-5">
          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Workflow ID</span>
              <Input bind:value={form.workflowId} disabled={!!selectedWorkflow} placeholder="wf_review_loop" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Name</span>
                <Input bind:value={form.name} disabled={!!selectedWorkflow?.is_system} />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Version</span>
                <Input bind:value={form.version} disabled={!!selectedWorkflow?.is_system} type="number" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Tags</span>
                <Input bind:value={form.tagsText} disabled={!!selectedWorkflow?.is_system} placeholder="code, review" />
            </label>
          </div>

          <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
            <span>Description</span>
            <textarea bind:value={form.description} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}></textarea>
          </label>

          <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
            <span>Selection criteria</span>
            <textarea bind:value={form.criteria} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}></textarea>
          </label>
        </Card>

        <Card class="p-5">
          <div class="grid gap-4 md:grid-cols-3">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Interaction mode</span>
                <select bind:value={form.interactionMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                <option value="none">none</option>
                <option value="explicit_gates">explicit_gates</option>
                <option value="step_requests">step_requests</option>
              </select>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Default max attempts</span>
              <Input bind:value={form.defaultMaxAttempts} disabled={!!selectedWorkflow?.is_system} type="number" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>On exhausted</span>
                <select bind:value={form.defaultOnExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                <option value="continue">continue</option>
                <option value="fail">fail</option>
                <option value="gate">gate</option>
              </select>
            </label>
          </div>
          <label class="mt-4 flex items-center gap-3 text-sm text-slate-200">
            <input bind:checked={form.defaultEvaluate} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!!selectedWorkflow?.is_system} type="checkbox" />
            <span>Evaluate run steps by default</span>
          </label>
        </Card>

        <Card class="p-5">
          <div class="flex items-center justify-between gap-3">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Step editor</p>
              <h2 class="mt-1 text-lg font-semibold text-white">Workflow steps</h2>
            </div>
            <Button size="sm" variant="secondary" onclick={addStep} disabled={!!selectedWorkflow?.is_system}>Add step</Button>
          </div>

          <div class="mt-4 space-y-4">
            {#each form.steps as step, index}
              <article class="rounded-2xl border border-slate-800 bg-slate-950/70 p-4" draggable={!selectedWorkflow?.is_system} ondragstart={() => (dragIndex = index)} ondragover={(event) => event.preventDefault()} ondrop={() => moveStep(index)}>
                <div class="grid gap-4 md:grid-cols-2">
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Name</span>
                    <Input bind:value={step.name} disabled={!!selectedWorkflow?.is_system} />
                  </label>
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Type</span>
                    <select bind:value={step.type} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                      <option value="run">run</option>
                      <option value="gate">gate</option>
                    </select>
                  </label>
                </div>

                {#if step.type === 'run'}
                  <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                    <span>Agent override</span>
                    <select bind:value={step.agentOverride} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                      <option value="">Default (task agent)</option>
                      {#each secondaryAgents as agent}
                        <option value={agent.agent_id}>
                          {agent.name}{agent.is_system ? ' (system)' : ''}
                        </option>
                      {/each}
                    </select>
                  </label>
                {/if}

                <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                  <span>Prompt</span>
                  <textarea bind:value={step.prompt} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}></textarea>
                </label>

                <div class="mt-4 grid gap-4 md:grid-cols-4">
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Input mode</span>
                    <select bind:value={step.inputMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                      <option value="auto">auto</option>
                      <option value="null">null</option>
                      <option value="last">last</option>
                      <option value="full">full</option>
                      <option value="summary">summary</option>
                    </select>
                  </label>
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Input sources</span>
                    <Input bind:value={step.inputText} disabled={!!selectedWorkflow?.is_system || step.inputMode === 'null'} placeholder={step.inputMode === 'full' ? 'plan' : 'plan, review'} />
                  </label>
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Max attempts</span>
                    <Input bind:value={step.maxAttempts} disabled={!!selectedWorkflow?.is_system} type="number" />
                  </label>
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>On exhausted</span>
                    <select bind:value={step.onExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                      <option value="continue">continue</option>
                      <option value="fail">fail</option>
                      <option value="gate">gate</option>
                    </select>
                  </label>
                </div>

                <label class="mt-4 flex items-center gap-3 text-sm text-slate-200">
                  <input bind:checked={step.allowQuestions} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!!selectedWorkflow?.is_system} type="checkbox" />
                  <span>Allow in-step questions</span>
                </label>

                <label class="mt-2 flex items-center gap-3 text-sm text-slate-200">
                  <input bind:checked={step.evaluate} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!!selectedWorkflow?.is_system} type="checkbox" />
                  <span>Evaluate completion</span>
                </label>

                {#if step.type === 'gate'}
                  <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                    <span>Gate message</span>
                    <textarea bind:value={step.gateMessage} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}></textarea>
                  </label>
                  <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                    <span>Gate options</span>
                    <textarea bind:value={step.gateOptionsText} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system} placeholder="Approve|continue\nRequest changes|revise(plan)"></textarea>
                  </label>
                {/if}

                <div class="mt-4 grid gap-4 md:grid-cols-3">
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Reject target</span>
                    <Input bind:value={step.rejectTarget} disabled={!!selectedWorkflow?.is_system} placeholder="implement" />
                  </label>
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Reject max loops</span>
                    <Input bind:value={step.rejectMaxLoops} disabled={!!selectedWorkflow?.is_system} type="number" />
                  </label>
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Reject on exhausted</span>
                    <select bind:value={step.rejectOnExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                      <option value="continue">continue</option>
                      <option value="fail">fail</option>
                      <option value="gate">gate</option>
                    </select>
                  </label>
                </div>

                <div class="mt-4 flex justify-end">
                  <Button size="sm" variant="danger" onclick={() => removeStep(index)} disabled={!!selectedWorkflow?.is_system}>Remove step</Button>
                </div>
              </article>
            {/each}
          </div>
        </Card>

        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Pipeline preview</p>
          <div class="mt-4 flex flex-wrap items-center gap-3">
            {#each form.steps as step, index}
              <div class="contents">
                <div class="rounded-2xl border {step.agentOverride ? 'border-sky-500/30 bg-sky-500/5' : 'border-slate-800 bg-slate-950/70'} px-4 py-3 text-sm text-slate-100">
                  <span class="font-medium">{step.name || `step_${index + 1}`}</span>
                  <span class="ml-2 text-xs uppercase tracking-[0.2em] text-slate-500">{step.type}</span>
                  {#if step.agentOverride}
                    <span class="ml-2 rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] text-sky-300">{step.agentOverride}</span>
                  {/if}
                </div>
                {#if index < form.steps.length - 1}
                  <span class="text-slate-500">→</span>
                {/if}
              </div>
            {/each}
          </div>
        </Card>
      </div>
    </div>
  </section>
{/if}
