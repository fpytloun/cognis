<script lang="ts">
import { beforeNavigate } from '$app/navigation';
import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import ArrowDown from 'lucide-svelte/icons/arrow-down';
import ArrowUp from 'lucide-svelte/icons/arrow-up';
import MoreVertical from 'lucide-svelte/icons/more-vertical';

  import { api, asApiError } from '$lib/api/client';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { loadSkillWorkflowDraft, skillToWorkflowDraft } from '$lib/skills';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import WorkflowDiagram from '$lib/components/workflows/WorkflowDiagram.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { blockNavigationIfDirty, installBeforeUnloadGuard } from '$lib/navigation/unsaved';
  import {
    createEmptyStep,
    createEmptyWorkflowForm,
    exportWorkflowYaml,
    formStateToSystemWorkflowOverridePayload,
    formStateToWorkflowPayload,
    importWorkflowYamlWithProfiles,
    STEP_PROFILE_CAPABILITIES,
    buildStepProfileMap,
    workflowThinkingEfforts,
    validateWorkflowForm,
    workflowToFormState,
    type WorkflowFormState
  } from '$lib/workflows';
  import type { Agent, StepProfileDefinition, ToolDefinitionSummary, Workflow } from '$lib/types/api';

  let loading = true;
  let saving = false;
  let error = '';
  let importText = '';
  let workflows: Workflow[] = [];
  let secondaryAgents: Agent[] = [];
  let stepProfiles: StepProfileDefinition[] = [];
  let stepProfileMap: Record<string, StepProfileDefinition> = {};
  let availableTools: ToolDefinitionSummary[] = [];
  let availableToolCategories: string[] = [];
  let stepProfileOptions: Array<{ id: string; label: string }> = [{ id: '', label: 'Unrestricted' }];
  let selectedWorkflow: Workflow | null = null;
  let form: WorkflowFormState = createEmptyWorkflowForm();
  let dragIndex = -1;
  let initialSnapshot = JSON.stringify(form);
  let mobileWorkflowActionsOpen = false;
  let showEphemeral = false;

  function canEditSystemWorkflowField(field: 'stepReasoning' | 'stepMaxAttempts'): boolean {
    if (!selectedWorkflow?.is_system) return true;
    const editable = new Set(selectedWorkflow.editable_fields ?? []);
    if (field === 'stepReasoning') return editable.has('steps.*.reasoning_effort');
    if (field === 'stepMaxAttempts') return editable.has('steps.*.completion.max_attempts');
    return false;
  }

  function canEditSystemProfileField(): boolean {
    if (!selectedWorkflow?.is_system) return true;
    const editable = new Set(selectedWorkflow.editable_fields ?? []);
    return (
      editable.has('steps.*.step_profile_id') ||
      editable.has('steps.*.step_profile_mode') ||
      editable.has('steps.*.step_profile')
    );
  }

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
      [workflows, secondaryAgents, availableTools, stepProfiles] = await Promise.all([
        api.workflows.listAll({ include_disabled: true, include_ephemeral: showEphemeral }),
        api.agents.listAll({ agent_type: 'secondary' }),
        api.tools.list(),
        api.workflows.stepProfiles()
      ]);
      stepProfileMap = buildStepProfileMap(stepProfiles);
      stepProfileOptions = [
        { id: '', label: 'Unrestricted' },
        ...stepProfiles.map((profile) => ({ id: profile.profile_id, label: profile.name }))
      ];
      availableToolCategories = [...new Set(availableTools.map((tool) => tool.category).filter(Boolean))].sort();
      const nextSelected = selectedId ? workflows.find((workflow) => workflow.workflow_id === selectedId) : selectedWorkflow ? workflows.find((workflow) => workflow.workflow_id === selectedWorkflow?.workflow_id) : workflows[0];
      if (nextSelected) {
        selectedWorkflow = nextSelected;
        form = workflowToFormState(nextSelected, stepProfileMap);
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
      const nextForm = workflowToFormState(workflow, stepProfileMap);
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

  async function loadDraftFromQuery(): Promise<void> {
    const workflowId = $page.url.searchParams.get('draftFrom');
    const skillId = $page.url.searchParams.get('draftFromSkill');
    if (!workflowId && !skillId) return;
    if (workflowId) {
      try {
        const source = await api.workflows.detail(workflowId);
        const nextForm = workflowToFormState(source, stepProfileMap);
        selectedWorkflow = null;
        form = {
          ...nextForm,
          workflowId: '',
          lifecycle: 'persistent',
          name: source.name.endsWith(' Copy') ? source.name : `${source.name} Copy`,
          lineage: {
            ...(source.lineage ?? {}),
            base_workflow_id: source.workflow_id,
            composition_source: 'promoted'
          }
        };
        error = '';
        initialSnapshot = JSON.stringify(form);
        addToast('Workflow draft loaded from task history.', 'success');
      } catch (caughtError) {
        error = asApiError(caughtError).message;
        addToast(error, 'error', 4_000, 'Unable to load workflow draft');
      }
      return;
    }

    try {
      const stored = skillId ? loadSkillWorkflowDraft(skillId) : null;
      const nextForm = stored
        ? stored.form
        : skillId
          ? skillToWorkflowDraft(await api.skills.get(skillId), undefined, stepProfileMap)
          : null;
      if (!nextForm) {
        return;
      }
      selectedWorkflow = null;
      form = { ...nextForm, workflowId: '', lifecycle: 'persistent' };
      error = '';
      initialSnapshot = JSON.stringify(form);
      addToast('Workflow draft loaded from skill decomposition.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to load workflow draft');
    }
  }

  function toggleShowEphemeral(): void {
    showEphemeral = !showEphemeral;
    void loadWorkflows(selectedWorkflow?.workflow_id);
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

  async function resetWorkflowOverrides(): Promise<void> {
    if (!selectedWorkflow?.is_system) return;
    try {
      await api.workflows.resetOverrides(selectedWorkflow.workflow_id);
      await loadWorkflows(selectedWorkflow.workflow_id);
      addToast('Workflow overrides reset.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to reset workflow overrides');
    }
  }

  async function toggleWorkflowDisabled(): Promise<void> {
    if (!selectedWorkflow?.is_system) return;
    try {
      if (selectedWorkflow.disabled) {
        await api.workflows.enable(selectedWorkflow.workflow_id);
      } else {
        await api.workflows.disable(selectedWorkflow.workflow_id);
      }
      await loadWorkflows(selectedWorkflow.workflow_id);
      addToast(selectedWorkflow.disabled ? 'Workflow enabled.' : 'Workflow disabled.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update workflow state');
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
      if (selectedWorkflow?.is_system) {
        const updated = await api.workflows.update(
          selectedWorkflow.workflow_id,
          formStateToSystemWorkflowOverridePayload(form)
        );
        await loadWorkflows(updated.workflow_id);
      } else if (selectedWorkflow?.lifecycle === 'ephemeral') {
        error = 'Ephemeral workflows are historical artifacts. Promote or duplicate them into a persistent workflow instead.';
        return;
      } else if (selectedWorkflow) {
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
    if (!selectedWorkflow || selectedWorkflow.is_system || selectedWorkflow.lifecycle === 'ephemeral') {
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

  /**
   * Explicit reorder via up/down buttons. Works on any input type (touch,
   * keyboard, mouse) whereas HTML5 `draggable` is a no-op on iOS Safari and
   * awkward on Android Chrome. Desktop users can still use the native drag
   * handles; mobile users get the arrow buttons.
   */
  function moveStepBy(index: number, delta: -1 | 1): void {
    if (selectedWorkflow?.is_system) return;
    const target = index + delta;
    if (target < 0 || target >= form.steps.length) return;
    const steps = [...form.steps];
    const [moved] = steps.splice(index, 1);
    steps.splice(target, 0, moved);
    form.steps = steps;
  }

  function touchWorkflowSteps(): void {
    form.steps = [...form.steps];
  }

  function toggleStepProfileCapability(index: number, category: string, capability: string): void {
    const step = form.steps[index];
    const matrix = [...step.stepProfileMatrix];
    const existing = matrix.find((row) => row.category === category);
    if (!existing) {
      matrix.push({ category, capabilities: [capability] });
    } else if (existing.capabilities.includes(capability)) {
      existing.capabilities = existing.capabilities.filter((item) => item !== capability);
      if (existing.capabilities.length === 0) {
        step.stepProfileMatrix = matrix.filter((row) => row.category !== category);
        touchWorkflowSteps();
        return;
      }
    } else {
      existing.capabilities = [...existing.capabilities, capability];
    }
    step.stepProfileMatrix = matrix;
    touchWorkflowSteps();
  }

  function addProfileCategory(index: number, category: string): void {
    const step = form.steps[index];
    if (step.stepProfileMatrix.some((row) => row.category === category)) return;
    step.stepProfileMatrix = [...step.stepProfileMatrix, { category, capabilities: ['read'] }].sort((a, b) => a.category.localeCompare(b.category));
    touchWorkflowSteps();
  }

  function removeProfileCategory(index: number, category: string): void {
    const step = form.steps[index];
    step.stepProfileMatrix = step.stepProfileMatrix.filter((row) => row.category !== category);
    touchWorkflowSteps();
  }

  function remainingProfileCategories(index: number): string[] {
    const used = new Set(form.steps[index]?.stepProfileMatrix.map((row) => row.category) ?? []);
    return availableToolCategories.filter((category) => !used.has(category));
  }

  function handleAddProfileCategory(index: number, event: Event): void {
    const target = event.currentTarget;
    if (!(target instanceof HTMLSelectElement)) return;
    const category = target.value;
    if (!category) return;
    addProfileCategory(index, category);
    target.value = '';
  }

  function applyStepProfilePreset(index: number, profileId: string): void {
    const step = form.steps[index];
    const preset = stepProfileMap[profileId];
    const baseMatrix = Object.entries(preset?.config.matrix ?? {})
      .map(([category, capabilities]) => ({ category, capabilities: [...capabilities] }))
      .sort((a, b) => a.category.localeCompare(b.category));
    const baseAllowToolSearch = preset?.config.allow_tool_search !== false;
    const baseIncludeText = Array.isArray(preset?.config.tool_overrides?.include)
      ? preset.config.tool_overrides.include.join(', ')
      : '';
    const baseExcludeText = Array.isArray(preset?.config.tool_overrides?.exclude)
      ? preset.config.tool_overrides.exclude.join(', ')
      : '';
    const baseMode = preset?.mode === 'hard' ? 'hard' : 'soft';
    step.stepProfileId = profileId;
    step.stepProfileBaseMatrix = baseMatrix;
    step.stepProfileMatrix = baseMatrix.map((row) => ({ category: row.category, capabilities: [...row.capabilities] }));
    step.stepProfileBaseAllowToolSearch = baseAllowToolSearch;
    step.stepProfileAllowToolSearch = baseAllowToolSearch;
    step.stepProfileBaseMode = baseMode;
    step.stepProfileMode = baseMode;
    step.stepProfileBaseIncludeText = baseIncludeText;
    step.stepProfileBaseExcludeText = baseExcludeText;
    step.stepProfileIncludeText = baseIncludeText;
    step.stepProfileExcludeText = baseExcludeText;
    touchWorkflowSteps();
  }

  function stepProfileHasCustomizations(index: number): boolean {
    const step = form.steps[index];
    if (!step) return false;
    return (
      JSON.stringify(step.stepProfileMatrix) !== JSON.stringify(step.stepProfileBaseMatrix) ||
      step.stepProfileAllowToolSearch !== step.stepProfileBaseAllowToolSearch ||
      step.stepProfileMode !== step.stepProfileBaseMode ||
      step.stepProfileIncludeText.trim() !== step.stepProfileBaseIncludeText.trim() ||
      step.stepProfileExcludeText.trim() !== step.stepProfileBaseExcludeText.trim()
    );
  }

  function resetStepProfile(index: number): void {
    const step = form.steps[index];
    step.stepProfileMatrix = step.stepProfileBaseMatrix.map((row) => ({ category: row.category, capabilities: [...row.capabilities] }));
    step.stepProfileAllowToolSearch = step.stepProfileBaseAllowToolSearch;
    step.stepProfileMode = step.stepProfileBaseMode;
    step.stepProfileIncludeText = step.stepProfileBaseIncludeText;
    step.stepProfileExcludeText = step.stepProfileBaseExcludeText;
    touchWorkflowSteps();
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
      form = importWorkflowYamlWithProfiles(importText, stepProfileMap);
      selectedWorkflow = null;
      error = '';
      addToast('Workflow imported into the editor.', 'success');
      initialSnapshot = JSON.stringify(form);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to import workflow');
    }
  }

  /** Collect step names preceding a given index for loop-back dropdowns */
  function previousStepNames(index: number): string[] {
    return form.steps.slice(0, index).map((s) => s.name).filter(Boolean);
  }

  onMount(() => {
    const cleanup = installBeforeUnloadGuard(isDirty);
    void loadWorkflows().then(() => loadDraftFromQuery());
    return cleanup;
  });
</script>

<svelte:head>
  <title>Workflows · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading workflows" description="Fetching system templates and user-editable workflow definitions." />
{:else}
  <!-- Extra bottom padding on mobile reserves room for the sticky action bar
       (Save + Actions) so the last step editor card isn't hidden behind it. -->
  <section class="space-y-5 overflow-x-hidden pb-24 lg:pb-0">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="min-w-0">
        <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Workflow registry</p>
        <h1 class="mt-1 text-2xl font-semibold text-white">Workflows</h1>
      </div>
      <!-- Desktop action bar. Mobile gets a fixed sticky bar at the bottom
           of the viewport so Save is always one tap away even at the end of
           a long step editor. -->
      <div class="hidden lg:flex flex-wrap gap-2">
        <Button variant="secondary" onclick={newWorkflow}>New workflow</Button>
        <Button variant="secondary" onclick={duplicateSelectedWorkflow} disabled={!selectedWorkflow}>Duplicate</Button>
        <Button variant="secondary" onclick={downloadCurrentWorkflow}>Export YAML</Button>
        <Button variant="danger" onclick={deleteSelectedWorkflow} disabled={!selectedWorkflow || selectedWorkflow.is_system || selectedWorkflow.lifecycle === 'ephemeral'}>Delete</Button>
        <Button onclick={saveWorkflow} disabled={saving || (selectedWorkflow?.is_system && (selectedWorkflow.editable_fields?.length ?? 0) === 0) || selectedWorkflow?.lifecycle === 'ephemeral'}>{saving ? 'Saving…' : selectedWorkflow?.is_system ? 'Save overrides' : 'Save workflow'}</Button>
      </div>
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <!-- Two-column layout at lg+. Below lg the workflow list stacks above
         the editor for a usable single-column mobile flow. -->
    <div class="grid gap-5 lg:grid-cols-[320px_minmax(0,1fr)]">
      <aside class="space-y-5">
        <Card class="p-4">
          <label class="flex items-center justify-between gap-3 text-sm text-slate-200">
            <span>Show ephemeral workflows</span>
            <input checked={showEphemeral} class="h-4 w-4 rounded border-slate-600 bg-slate-950" type="checkbox" onchange={toggleShowEphemeral} />
          </label>
          <p class="mt-2 text-xs text-slate-500">Debug view for archived and historical composed workflows.</p>
        </Card>
        <Card class="p-4">
          <div class="space-y-2">
            {#each workflows as workflow}
              <button class={`w-full rounded-2xl border px-4 py-3 text-left transition ${workflow.workflow_id === selectedWorkflow?.workflow_id ? 'border-sky-400/40 bg-sky-500/10 text-white' : 'border-slate-800 bg-slate-950/70 text-slate-200 hover:border-slate-700'}`} onclick={() => selectWorkflow(workflow)}>
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <span class="min-w-0 flex-1 truncate font-medium">{workflow.name}</span>
                  <span class="shrink-0 rounded-full border border-slate-700 px-2.5 py-1 text-[11px] uppercase tracking-[0.2em] text-slate-400">{workflow.disabled ? 'disabled' : workflow.lifecycle === 'ephemeral' ? 'ephemeral' : workflow.is_system ? 'system' : 'user'}</span>
                </div>
                <p class="mt-2 break-all text-xs leading-5 text-slate-400">{workflow.workflow_id}</p>
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
                <p class="mt-1 text-sky-100/80">This bundled workflow is immutable. You can tune selected step runtime fields here or duplicate it for full customization.</p>
              </div>
              <div class="flex flex-wrap gap-2">
                <Button variant="secondary" onclick={duplicateSelectedWorkflow}>Duplicate to edit</Button>
                <Button variant="secondary" onclick={resetWorkflowOverrides} disabled={!selectedWorkflow.has_overrides}>Reset overrides</Button>
                {#if selectedWorkflow.disableable}
                  <Button variant="secondary" onclick={toggleWorkflowDisabled}>{selectedWorkflow.disabled ? 'Enable' : 'Disable'}</Button>
                {/if}
              </div>
            </div>
            {#if selectedWorkflow.override_warnings.length > 0}
              <p class="mt-3 text-xs text-sky-50/90">{selectedWorkflow.override_warnings.join(' ')}</p>
            {/if}
          </Card>
        {:else if selectedWorkflow?.lifecycle === 'ephemeral'}
          <Card class="border border-violet-500/30 bg-violet-500/10 p-4 text-sm text-violet-100">
            <p class="font-medium">Ephemeral workflow</p>
            <p class="mt-1 text-violet-100/80">This workflow is a historical composed artifact. It is read-only. Promote it into a new persistent workflow to edit or reuse it.</p>
          </Card>
        {/if}

        <!-- Pipeline diagram (first thing the user sees) -->
        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Pipeline diagram</p>
          <div class="mt-3">
            <WorkflowDiagram steps={form.steps} interactionMode={form.interactionMode} />
          </div>
        </Card>

        <!-- Workflow metadata -->
        <Card class="p-5">
          <p class="mb-3 text-xs uppercase tracking-[0.25em] text-slate-400">Metadata</p>
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
            <span class="inline-flex items-center gap-2">
              Selection criteria
              <Tooltip text="Natural language description of when this workflow should be auto-selected by the classifier. Used by the Decision Engine to match incoming tasks to workflows.">
                <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
              </Tooltip>
            </span>
            <textarea bind:value={form.criteria} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}></textarea>
          </label>
        </Card>

        <!-- Workflow defaults -->
        <Card class="p-5">
          <p class="mb-3 text-xs uppercase tracking-[0.25em] text-slate-400">Workflow defaults</p>
          <div class="grid gap-4 md:grid-cols-3">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span class="inline-flex items-center gap-2">
                Interaction mode
                <Tooltip text="Controls when the workflow can pause for human input. 'Autonomous' never pauses. 'Gates only' pauses at defined gate steps. 'Steps can ask' also allows run steps to request input mid-execution.">
                  <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                </Tooltip>
              </span>
              <select bind:value={form.interactionMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                <option value="none">Autonomous</option>
                <option value="explicit_gates">Gates only</option>
                <option value="step_requests">Steps can ask</option>
              </select>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span class="inline-flex items-center gap-2">
                Default max attempts
                <Tooltip text="How many times a step can retry after evaluation rejection before triggering the 'on exhausted' action. Applies to all steps unless overridden per step.">
                  <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                </Tooltip>
              </span>
              <Input bind:value={form.defaultMaxAttempts} disabled={!!selectedWorkflow?.is_system} type="number" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span class="inline-flex items-center gap-2">
                On exhausted
                <Tooltip text="What happens when a step exhausts all retry attempts. 'Continue anyway' advances to the next step. 'Fail task' marks the entire task as failed. 'Ask human' pauses and notifies the user for a decision.">
                  <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                </Tooltip>
              </span>
              <select bind:value={form.defaultOnExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                <option value="continue">Continue anyway</option>
                <option value="fail">Fail task</option>
                <option value="gate">Ask human</option>
              </select>
            </label>
          </div>
          <label class="mt-4 flex items-center gap-3 text-sm text-slate-200">
            <input bind:checked={form.defaultEvaluate} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!!selectedWorkflow?.is_system} type="checkbox" />
            <span class="inline-flex items-center gap-2">
              Evaluate steps by default
              <Tooltip text="When enabled, an evaluator LLM checks whether each step's objective was met before advancing to the next step. Disable for simple or fire-and-forget steps.">
                <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
              </Tooltip>
            </span>
          </label>
          <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
            <span class="inline-flex items-center gap-2">
              Completion notification behavior
              <Tooltip text="Default delivery sends results through the normal conversation flow. Direct channel delivery sends the final result straight to the resolved target channel. Allow silent completion lets the agent finish without notifying when nothing user-actionable happened.">
                <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
              </Tooltip>
            </span>
            <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
              <select bind:value={form.defaultCompletionModeFamily} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                <option value="default">Default delivery</option>
                <option value="direct">Direct channel delivery</option>
              </select>
              <label class="inline-flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm text-slate-200">
                <input bind:checked={form.defaultAllowSilentCompletion} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!!selectedWorkflow?.is_system} type="checkbox" />
                <span>Allow silent completion</span>
              </label>
            </div>
          </label>
        </Card>

        <!-- Step editor -->
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
                <!-- Step header: name, type, step number badge, reorder
                     buttons. The up/down buttons are the touch-friendly
                     counterpart to HTML5 `draggable` (which is a no-op on
                     iOS Safari and flaky on Android Chrome). -->
                <div class="mb-3 flex flex-wrap items-center gap-3">
                  <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-800 text-[11px] font-medium text-slate-400">{index + 1}</span>
                  <span class="min-w-0 flex-1 break-words text-sm font-medium text-slate-100">{step.name || `Step ${index + 1}`}</span>
                  <div class="flex shrink-0 items-center gap-1">
                    <button
                      type="button"
                      aria-label="Move step up"
                      class="inline-flex h-10 w-10 items-center justify-center rounded-xl text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-40 md:h-8 md:w-8"
                      disabled={!!selectedWorkflow?.is_system || index === 0}
                      onclick={() => moveStepBy(index, -1)}
                    >
                      <ArrowUp class="h-4 w-4" />
                    </button>
                    <button
                      type="button"
                      aria-label="Move step down"
                      class="inline-flex h-10 w-10 items-center justify-center rounded-xl text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-40 md:h-8 md:w-8"
                      disabled={!!selectedWorkflow?.is_system || index === form.steps.length - 1}
                      onclick={() => moveStepBy(index, 1)}
                    >
                      <ArrowDown class="h-4 w-4" />
                    </button>
                  </div>
                  <span class="shrink-0 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-widest {step.type === 'gate' ? 'border-amber-600/40 text-amber-400' : 'border-slate-700 text-slate-400'}">{step.type === 'gate' ? 'Gate' : 'Run'}</span>
                  {#if step.agentOverride && step.type === 'run'}
                    <span class="break-all rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] text-sky-300">{step.agentOverride}</span>
                  {/if}
                </div>

                <div class="grid gap-4 md:grid-cols-2">
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Name</span>
                    <Input bind:value={step.name} disabled={!!selectedWorkflow?.is_system} />
                  </label>
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Type</span>
                    <select bind:value={step.type} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                      <option value="run">Run (agent executes)</option>
                      <option value="gate">Gate (pause for approval)</option>
                    </select>
                  </label>
                </div>

                {#if step.type === 'run'}
                  <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                    <span class="inline-flex items-center gap-2">
                      Agent override
                      <Tooltip text="Run this step with a different agent instead of the task's primary agent. Useful for specialized steps like code review or architecture review.">
                        <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                      </Tooltip>
                    </span>
                    <select bind:value={step.agentOverride} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                      <option value="">Default (task agent)</option>
                      {#each secondaryAgents as agent}
                        <option value={agent.agent_id}>
                          {agent.name}{agent.is_system ? ' (system)' : ''}
                        </option>
                      {/each}
                    </select>
                  </label>
                  <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                    <span>Thinking effort</span>
                    <select bind:value={step.reasoningEffort} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!canEditSystemWorkflowField('stepReasoning')}>
                      <option value="">Default</option>
                      {#each workflowThinkingEfforts() as value}
                        <option value={value}>{value === 'xhigh' ? 'XHigh' : value.charAt(0).toUpperCase() + value.slice(1)}</option>
                      {/each}
                    </select>
                  </label>

                  <details class="mt-4" open={!!step.stepProfileId || step.stepProfileMatrix.length > 0 || !!step.stepProfileIncludeText || !!step.stepProfileExcludeText}>
                    <summary class="cursor-pointer text-sm font-medium text-slate-300 hover:text-slate-100">
                      Tool profile
                      <Tooltip text="Profiles narrow the tool surface for this step. Soft mode changes default exposure only. Hard mode also restricts which tools search can discover.">
                        <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                      </Tooltip>
                    </summary>
                    <div class="mt-3 space-y-4">
                      <div class="grid gap-4 md:grid-cols-3">
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>Preset</span>
                          <select bind:value={step.stepProfileId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!canEditSystemProfileField()} onchange={(event) => applyStepProfilePreset(index, (event.currentTarget as HTMLSelectElement).value)}>
                            {#each stepProfileOptions as option}
                              <option value={option.id}>{option.label}</option>
                            {/each}
                          </select>
                        </label>
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>Mode</span>
                          <select bind:value={step.stepProfileMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!canEditSystemProfileField()}>
                            <option value="soft">Soft</option>
                            <option value="hard">Hard</option>
                          </select>
                        </label>
                        <label class="flex items-center gap-3 rounded-xl border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm text-slate-200">
                          <input bind:checked={step.stepProfileAllowToolSearch} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!canEditSystemProfileField()} type="checkbox" />
                          <span>Allow tool search</span>
                        </label>
                      </div>

                      <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                        <div class="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <p class="text-sm font-medium text-slate-200">Capability matrix</p>
                            <p class="mt-1 text-xs text-slate-400">Rows are tool groups. Columns decide which capabilities are exposed or allowed for this step.</p>
                          </div>
                          <div class="flex flex-wrap items-center gap-2">
                            {#if stepProfileHasCustomizations(index)}
                              <button type="button" class="rounded-xl border border-slate-700 px-3 py-2 text-xs font-medium text-slate-200 hover:border-slate-500 hover:text-white" disabled={!canEditSystemProfileField()} onclick={() => resetStepProfile(index)}>
                                {step.stepProfileId ? 'Reset to preset' : 'Clear custom profile'}
                              </button>
                            {/if}
                            {#if remainingProfileCategories(index).length > 0}
                              <select class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!canEditSystemProfileField()} onchange={(event) => handleAddProfileCategory(index, event)}>
                                <option value="">Add group…</option>
                                {#each remainingProfileCategories(index) as category}
                                  <option value={category}>{category}</option>
                                {/each}
                              </select>
                            {/if}
                          </div>
                        </div>
                        <div class="mt-3 overflow-x-auto">
                          <table class="min-w-full border-separate border-spacing-y-2 text-sm text-slate-200">
                            <thead>
                              <tr class="text-left text-xs uppercase tracking-[0.2em] text-slate-500">
                                <th class="px-3 py-2">Group</th>
                                {#each STEP_PROFILE_CAPABILITIES as capability}
                                  <th class="px-3 py-2">{capability}</th>
                                {/each}
                                <th class="px-3 py-2"></th>
                              </tr>
                            </thead>
                            <tbody>
                              {#each step.stepProfileMatrix as row}
                                <tr class="rounded-xl border border-slate-800 bg-slate-950/70">
                                  <td class="px-3 py-2 font-medium">{row.category}</td>
                                  {#each STEP_PROFILE_CAPABILITIES as capability}
                                    <td class="px-3 py-2">
                                      <input
                                        checked={row.capabilities.includes(capability)}
                                        class="h-4 w-4 rounded border-slate-600 bg-slate-950"
                                        disabled={!canEditSystemProfileField()}
                                        type="checkbox"
                                        onchange={() => toggleStepProfileCapability(index, row.category, capability)}
                                      />
                                    </td>
                                  {/each}
                                  <td class="px-3 py-2 text-right">
                                    <button type="button" class="text-xs text-slate-400 hover:text-rose-300" onclick={() => removeProfileCategory(index, row.category)} disabled={!canEditSystemProfileField()}>Remove</button>
                                  </td>
                                </tr>
                              {/each}
                            </tbody>
                          </table>
                        </div>
                      </div>

                      <div class="grid gap-4 md:grid-cols-2">
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>Explicit include</span>
                          <Input bind:value={step.stepProfileIncludeText} disabled={!canEditSystemProfileField()} placeholder="tool_name, mcp_server__tool" />
                        </label>
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>Explicit exclude</span>
                          <Input bind:value={step.stepProfileExcludeText} disabled={!canEditSystemProfileField()} placeholder="tool_name, mcp_server__tool" />
                        </label>
                      </div>
                    </div>
                  </details>
                {/if}

                <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                  <span>Prompt</span>
                  <textarea bind:value={step.prompt} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}></textarea>
                </label>

                <!-- Input configuration -->
                <div class="mt-4 grid gap-4 md:grid-cols-2">
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span class="inline-flex items-center gap-2">
                      Input from previous steps
                      <Tooltip text="What context from previous steps flows into this step. 'Step output' passes the completion summary (recommended). 'Summary' generates an LLM summary. 'Full history' passes the entire session (expensive, rarely needed). 'None' starts with fresh context.">
                        <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                      </Tooltip>
                    </span>
                    <select bind:value={step.inputMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                      <option value="auto">Auto (server default)</option>
                      <option value="null">None (fresh context)</option>
                      <option value="last">Step output (recommended)</option>
                      <option value="summary">Summary (LLM-generated)</option>
                      <option value="full">Full history (expensive)</option>
                    </select>
                  </label>
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span class="inline-flex items-center gap-2">
                      Source steps
                      <Tooltip text="Comma-separated names of steps to pull input from. Leave empty to use the immediately preceding step. Only applies when input mode is not 'None'.">
                        <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                      </Tooltip>
                    </span>
                    <Input bind:value={step.inputText} disabled={!!selectedWorkflow?.is_system || step.inputMode === 'null'} placeholder={step.inputMode === 'full' ? 'plan' : 'plan, review'} />
                  </label>
                </div>

                <!-- Completion configuration -->
                {#if step.type === 'run'}
                  <div class="mt-4 grid gap-4 md:grid-cols-2">
                    <label class="space-y-2 text-sm font-medium text-slate-200">
                      <span class="inline-flex items-center gap-2">
                        Max attempts
                        <Tooltip text="How many times this step can retry after evaluation rejection before triggering the 'on exhausted' action.">
                          <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                        </Tooltip>
                      </span>
                      <Input bind:value={step.maxAttempts} disabled={!canEditSystemWorkflowField('stepMaxAttempts')} type="number" />
                    </label>
                    <label class="space-y-2 text-sm font-medium text-slate-200">
                      <span class="inline-flex items-center gap-2">
                        On exhausted
                        <Tooltip text="What happens when this step exhausts all retry attempts.">
                          <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                        </Tooltip>
                      </span>
                      <select bind:value={step.onExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                        <option value="continue">Continue anyway</option>
                        <option value="fail">Fail task</option>
                        <option value="gate">Ask human</option>
                      </select>
                    </label>
                  </div>

                  <div class="mt-4 flex flex-wrap gap-x-8 gap-y-2">
                    <label class="flex items-center gap-3 text-sm text-slate-200">
                      <input bind:checked={step.evaluate} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!!selectedWorkflow?.is_system} type="checkbox" />
                      <span class="inline-flex items-center gap-2">
                        Evaluate completion
                        <Tooltip text="When enabled, an evaluator LLM checks if the step objective was met before advancing. Rejected steps are sent back for revision.">
                          <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                        </Tooltip>
                      </span>
                    </label>
                    <label class="flex items-center gap-3 text-sm text-slate-200">
                      <input bind:checked={step.requireDeliverable} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!!selectedWorkflow?.is_system} type="checkbox" />
                      <span class="inline-flex items-center gap-2">
                        Require deliverable
                        <Tooltip text="When enabled, the agent must call write_deliverable before step_complete. Deliverables become the canonical artifact for evaluation, UI, and final workflow output.">
                          <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                        </Tooltip>
                      </span>
                    </label>
                    {#if form.interactionMode === 'step_requests'}
                      <label class="flex items-center gap-3 text-sm text-slate-200">
                        <input bind:checked={step.allowQuestions} class="h-4 w-4 rounded border-slate-600 bg-slate-950" disabled={!!selectedWorkflow?.is_system} type="checkbox" />
                        <span class="inline-flex items-center gap-2">
                          Allow questions
                          <Tooltip text="Let the agent ask clarifying questions mid-step. The workflow pauses until the user responds. Only available when interaction mode is 'Steps can ask'.">
                            <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                          </Tooltip>
                        </span>
                      </label>
                    {/if}
                  </div>
                {/if}

                <!-- Gate configuration -->
                {#if step.type === 'gate'}
                  <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                    <span>Gate message</span>
                    <textarea bind:value={step.gateMessage} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}></textarea>
                  </label>
                  <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
                    <span class="inline-flex items-center gap-2">
                      Gate options
                      <Tooltip text="One option per line in 'Label|action' format. Actions: 'continue' advances, 'revise(step_name)' loops back. Example: Approve|continue">
                        <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                      </Tooltip>
                    </span>
                    <textarea bind:value={step.gateOptionsText} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system} placeholder="Approve|continue&#10;Request changes|revise(plan)"></textarea>
                  </label>
                {/if}

                <!-- Evaluator retry loop -->
                {#if step.evaluate || step.evaluatorRejectTarget}
                  <details class="mt-4" open={!!step.evaluatorRejectTarget}>
                    <summary class="cursor-pointer text-sm font-medium text-slate-300 hover:text-slate-100">
                      Evaluator retry loop
                      <Tooltip text="Configure what happens when the evaluator says the step output is incomplete. Without a target, the agent retries in place within the same step.">
                        <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                      </Tooltip>
                    </summary>
                    <div class="mt-3 grid gap-4 md:grid-cols-3">
                      <label class="space-y-2 text-sm font-medium text-slate-200">
                        <span>Evaluator reject target</span>
                        <select bind:value={step.evaluatorRejectTarget} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                          <option value="">None (retry in place)</option>
                          {#each previousStepNames(index) as prevName}
                            <option value={prevName}>{prevName}</option>
                          {/each}
                        </select>
                      </label>
                      <label class="space-y-2 text-sm font-medium text-slate-200">
                        <span>Max evaluator loops</span>
                        <Input bind:value={step.evaluatorRejectMaxLoops} disabled={!!selectedWorkflow?.is_system || !step.evaluatorRejectTarget} type="number" />
                      </label>
                      <label class="space-y-2 text-sm font-medium text-slate-200">
                        <span>On evaluator loops exhausted</span>
                        <select bind:value={step.evaluatorRejectOnExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system || !step.evaluatorRejectTarget}>
                          <option value="continue">Continue anyway</option>
                          <option value="fail">Fail task</option>
                          <option value="gate">Ask human</option>
                        </select>
                      </label>
                    </div>
                  </details>
                {/if}

                <!-- Outcome routing -->
                {#if step.type === 'run'}
                  <details class="mt-4" open={step.outcomeSuccessAction !== 'none' || step.outcomeRejectedAction !== 'none' || step.outcomeFailedAction !== 'none'}>
                    <summary class="cursor-pointer text-sm font-medium text-slate-300 hover:text-slate-100">
                      Outcome routing
                      <Tooltip text="Configure what happens after a valid step completion reports a business outcome. This is separate from evaluator retries: an approved review can still report outcome.status='rejected' and send work back.">
                        <button type="button" aria-label="Help" class="inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5">?</button>
                      </Tooltip>
                    </summary>
                    <div class="mt-3 space-y-4">
                      <div class="grid gap-4 md:grid-cols-4">
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>On success</span>
                          <select bind:value={step.outcomeSuccessAction} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                            <option value="none">Default continue</option>
                            <option value="continue">Continue explicitly</option>
                            <option value="fail">Fail task</option>
                            <option value="gate">Ask human</option>
                            <option value="cancel">Cancel task</option>
                            <option value="revise">Loop back to step</option>
                          </select>
                        </label>
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>On success: target</span>
                          <select bind:value={step.outcomeSuccessTarget} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system || step.outcomeSuccessAction !== 'revise'}>
                            <option value="">None</option>
                            {#each previousStepNames(index) as prevName}
                              <option value={prevName}>{prevName}</option>
                            {/each}
                          </select>
                        </label>
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>Success max loops</span>
                          <Input bind:value={step.outcomeSuccessMaxLoops} disabled={!!selectedWorkflow?.is_system || step.outcomeSuccessAction !== 'revise' || !step.outcomeSuccessTarget} type="number" />
                        </label>
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>On success loops exhausted</span>
                          <select bind:value={step.outcomeSuccessOnExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system || step.outcomeSuccessAction !== 'revise' || !step.outcomeSuccessTarget}>
                            <option value="continue">Continue anyway</option>
                            <option value="fail">Fail task</option>
                            <option value="gate">Ask human</option>
                          </select>
                        </label>
                      </div>
                      <div class="grid gap-4 md:grid-cols-4">
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>On rejected</span>
                          <select bind:value={step.outcomeRejectedAction} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                            <option value="none">No special route</option>
                            <option value="fail">Fail task</option>
                            <option value="gate">Ask human</option>
                            <option value="continue">Continue anyway</option>
                            <option value="cancel">Cancel task</option>
                            <option value="revise">Loop back to step</option>
                          </select>
                        </label>
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>On rejected: target</span>
                          <select bind:value={step.outcomeRejectedTarget} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system || step.outcomeRejectedAction !== 'revise'}>
                            <option value="">None</option>
                            {#each previousStepNames(index) as prevName}
                              <option value={prevName}>{prevName}</option>
                            {/each}
                          </select>
                        </label>
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>Rejected max loops</span>
                          <Input bind:value={step.outcomeRejectedMaxLoops} disabled={!!selectedWorkflow?.is_system || step.outcomeRejectedAction !== 'revise' || !step.outcomeRejectedTarget} type="number" />
                        </label>
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>On rejected loops exhausted</span>
                          <select bind:value={step.outcomeRejectedOnExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system || step.outcomeRejectedAction !== 'revise' || !step.outcomeRejectedTarget}>
                            <option value="continue">Continue anyway</option>
                            <option value="fail">Fail task</option>
                            <option value="gate">Ask human</option>
                          </select>
                        </label>
                      </div>
                      <div class="grid gap-4 md:grid-cols-4">
                        <label class="space-y-2 text-sm font-medium text-slate-200">
                          <span>On failed</span>
                          <select bind:value={step.outcomeFailedAction} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                            <option value="none">Default fail</option>
                            <option value="fail">Fail task</option>
                            <option value="gate">Ask human</option>
                            <option value="continue">Continue anyway</option>
                            <option value="cancel">Cancel task</option>
                            <option value="revise">Loop back to step</option>
                          </select>
                        </label>
                        {#if step.outcomeFailedAction === 'revise'}
                          <label class="space-y-2 text-sm font-medium text-slate-200">
                            <span>Failed target</span>
                            <select bind:value={step.outcomeFailedTarget} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system}>
                              <option value="">Select earlier step</option>
                              {#each previousStepNames(index) as prevName}
                                <option value={prevName}>{prevName}</option>
                              {/each}
                            </select>
                          </label>
                          <label class="space-y-2 text-sm font-medium text-slate-200">
                            <span>Failed max loops</span>
                            <Input bind:value={step.outcomeFailedMaxLoops} disabled={!!selectedWorkflow?.is_system || !step.outcomeFailedTarget} type="number" />
                          </label>
                          <label class="space-y-2 text-sm font-medium text-slate-200">
                            <span>On failed loops exhausted</span>
                            <select bind:value={step.outcomeFailedOnExhausted} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!selectedWorkflow?.is_system || !step.outcomeFailedTarget}>
                              <option value="continue">Continue anyway</option>
                              <option value="fail">Fail task</option>
                              <option value="gate">Ask human</option>
                            </select>
                          </label>
                        {/if}
                      </div>
                    </div>
                  </details>
                {/if}

                <div class="mt-4 flex justify-end">
                  <Button size="sm" variant="danger" onclick={() => removeStep(index)} disabled={!!selectedWorkflow?.is_system}>Remove step</Button>
                </div>
              </article>
            {/each}
          </div>
        </Card>
      </div>
    </div>

    <!-- Mobile-only sticky action bar. Anchored above the bottom tab bar
         via safe-area. Primary action is Save; secondary actions collapse
         behind an overflow menu in a Sheet. -->
    <div
      class="fixed inset-x-0 z-30 border-t border-slate-800/80 bg-slate-950/95 px-3 py-2 backdrop-blur lg:hidden"
      style="bottom: calc(env(safe-area-inset-bottom, 0px) + 56px); padding-bottom: 6px;"
    >
      <div class="flex items-center gap-2">
        <Button
          variant="secondary"
          size="sm"
          onclick={() => (mobileWorkflowActionsOpen = true)}
          aria-label="More workflow actions"
        >
          <MoreVertical class="h-4 w-4" />
          <span class="ml-1">Actions</span>
        </Button>
        <Button
          class="flex-1 justify-center"
          onclick={saveWorkflow}
          disabled={saving || (!!selectedWorkflow?.is_system && (selectedWorkflow.editable_fields?.length ?? 0) === 0) || selectedWorkflow?.lifecycle === 'ephemeral'}
        >
          {saving ? 'Saving…' : selectedWorkflow?.is_system ? 'Save overrides' : 'Save workflow'}
        </Button>
      </div>
    </div>

    <Sheet open={mobileWorkflowActionsOpen} onClose={() => (mobileWorkflowActionsOpen = false)} side="bottom" label="Workflow actions">
      <div class="space-y-2">
        <Button class="w-full justify-center" variant="secondary" onclick={() => { mobileWorkflowActionsOpen = false; void newWorkflow(); }}>New workflow</Button>
        <Button class="w-full justify-center" variant="secondary" onclick={() => { mobileWorkflowActionsOpen = false; void duplicateSelectedWorkflow(); }} disabled={!selectedWorkflow}>Duplicate</Button>
        <Button class="w-full justify-center" variant="secondary" onclick={() => { mobileWorkflowActionsOpen = false; downloadCurrentWorkflow(); }}>Export YAML</Button>
        <Button class="w-full justify-center" variant="danger" onclick={() => { mobileWorkflowActionsOpen = false; void deleteSelectedWorkflow(); }} disabled={!selectedWorkflow || selectedWorkflow.is_system || selectedWorkflow.lifecycle === 'ephemeral'}>Delete</Button>
      </div>
    </Sheet>
  </section>
{/if}
