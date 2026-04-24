<script lang="ts">
  import { goto } from '$app/navigation';
  import Download from 'lucide-svelte/icons/download';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import FileText from 'lucide-svelte/icons/file-text';
  import GitBranch from 'lucide-svelte/icons/git-branch';
  import Info from 'lucide-svelte/icons/info';
  import Layers from 'lucide-svelte/icons/layers';
  import Pencil from 'lucide-svelte/icons/pencil';
  import Plus from 'lucide-svelte/icons/plus';
  import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
  import Save from 'lucide-svelte/icons/save';
  import Trash2 from 'lucide-svelte/icons/trash-2';
  import Upload from 'lucide-svelte/icons/upload';
  import Wrench from 'lucide-svelte/icons/wrench';
  import X from 'lucide-svelte/icons/x';

  import { api } from '$lib/api/client';
  import {
    clearSkillWorkflowDraft,
    createEmptyKeyValueEntry,
    createEmptyPromptTemplate,
    createEmptySkillForm,
    createEmptySkillParameter,
    createEmptySkillTool,
    formStateToSkillPayload,
    saveSkillWorkflowDraft,
    skillToFormState,
    skillToWorkflowDraft,
    validateSkillForm,
    type SkillFormState,
    type SkillToolFormItem
  } from '$lib/skills';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { toErrorMessage } from '$lib/utils';
  import type { Skill, SkillCreate, SkillVersion, ToolDefinitionSummary } from '$lib/types/api';

  type SkillSheetMode = 'view' | 'edit' | 'create';
  type SkillExportFormat = 'skill_md' | 'cognis_yaml' | 'cognis_package';
  const tooltipButtonClass =
    'inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 text-xs text-slate-400 hover:text-slate-200 focus-visible:border-slate-400 md:h-5 md:w-5';

  let {
    open,
    skill,
    mode = 'view',
    onClose,
    availableTools = [],
    allowManage = false,
    onSaved = async () => {},
    onDeleted = async () => {}
  } = $props<{
    open: boolean;
    skill: Skill | null;
    mode?: SkillSheetMode;
    onClose: () => void;
    availableTools?: ToolDefinitionSummary[];
    allowManage?: boolean;
    onSaved?: (skill: Skill, action: 'created' | 'updated') => void | Promise<void>;
    onDeleted?: (skillId: string) => void | Promise<void>;
  }>();

  let activeMode = $state<SkillSheetMode>('view');
  let versions = $state<SkillVersion[]>([]);
  let loadingVersions = $state(false);
  let saving = $state(false);
  let deleting = $state(false);
  let restoringVersionId = $state<string | null>(null);
  let decompositionPreview = $state<Record<string, unknown>[] | null>(null);
  let decompositionRationale = $state('');
  let decompositionSourceHash = $state<string | null>(null);
  let decompositionLoading = $state(false);
  let decompositionSaving = $state(false);
  let exportMenuOpen = $state(false);
  let linkedToolSearch = $state('');
  let secretDraft = $state('');
  let form = $state<SkillFormState>(createEmptySkillForm());
  let formSnapshot = $state('');
  let decompositionSectionEl = $state<HTMLElement | null>(null);
  let exportMenuEl = $state<HTMLDivElement | null>(null);

  const manageableSkill = $derived(
    skill !== null && !skill.is_system && (skill.source === 'db' || skill.source === 'imported')
  );
  const canEditSkill = $derived(Boolean(allowManage && manageableSkill));
  const canDeleteSkill = $derived(Boolean(allowManage && manageableSkill));
  const canResetSkill = $derived(Boolean(allowManage && skill?.is_system));
  const canExportSkill = $derived(Boolean(allowManage && skill));
  const canRestoreVersions = $derived(Boolean(allowManage && manageableSkill));
  const currentVersion = $derived(skill?.current_version ?? null);
  const savedSteps = $derived(
    ((currentVersion?.steps ?? skill?.steps ?? []) as unknown[]).filter(
      (item): item is Record<string, unknown> => typeof item === 'object' && item !== null
    )
  );
  const latestVersionId = $derived(
    versions.reduce<string | null>((current, version) => {
      if (current === null) return version.version_id;
      const currentVersion = versions.find((candidate) => candidate.version_id === current);
      if (!currentVersion) return version.version_id;
      return version.version_number > currentVersion.version_number ? version.version_id : current;
    }, null)
  );
  const isDirty = $derived(JSON.stringify(form) !== formSnapshot);
  const selectableLinkedTools = $derived(
    (availableTools ?? [])
      .filter((tool: ToolDefinitionSummary) => tool.tool_id && tool.source.type !== 'skill')
      .slice()
      .sort((left: ToolDefinitionSummary, right: ToolDefinitionSummary) => {
        const leftSource = left.source.server_name || left.source.type || '';
        const rightSource = right.source.server_name || right.source.type || '';
        const sourceCompare = leftSource.localeCompare(rightSource);
        if (sourceCompare !== 0) return sourceCompare;
        return left.name.localeCompare(right.name);
      })
  );
  const linkedToolMap = $derived(
    new Map<string, ToolDefinitionSummary>(
      selectableLinkedTools.map((tool: ToolDefinitionSummary) => [tool.tool_id as string, tool])
    )
  );
  const filteredLinkedTools = $derived(
    selectableLinkedTools.filter((tool: ToolDefinitionSummary) => {
      const query = linkedToolSearch.trim().toLowerCase();
      if (!query) return true;
      return [
        tool.name,
        tool.description,
        tool.tool_id || '',
        tool.category,
        tool.profile_group || '',
        tool.source.server_name || '',
        tool.source.type || ''
      ]
        .join(' ')
        .toLowerCase()
        .includes(query);
    })
  );

  function resetTransientState(): void {
    versions = [];
    decompositionPreview = null;
    decompositionRationale = '';
    decompositionSourceHash = null;
    linkedToolSearch = '';
    secretDraft = '';
    exportMenuOpen = false;
  }

  function linkedToolDisplay(toolId: string): string {
    const tool: ToolDefinitionSummary | undefined = linkedToolMap.get(toolId);
    if (!tool) return toolId;
    const source = tool.source.server_name || tool.source.type || 'tool';
    return `${tool.name} (${source})`;
  }

  function toggleLinkedTool(toolId: string): void {
    if (form.linkedToolIds.includes(toolId)) {
      form.linkedToolIds = form.linkedToolIds.filter((item) => item !== toolId);
      return;
    }
    form.linkedToolIds = [...form.linkedToolIds, toolId];
  }

  function setForm(nextForm: SkillFormState): void {
    form = nextForm;
    formSnapshot = JSON.stringify(nextForm);
  }

  $effect(() => {
    if (!open) {
      resetTransientState();
      return;
    }
    resetTransientState();
    if (mode === 'create') {
      activeMode = 'create';
      setForm(createEmptySkillForm());
      return;
    }
    if (skill) {
      activeMode = mode === 'edit' && canEditSkill ? 'edit' : 'view';
      setForm(skillToFormState(skill));
      loadingVersions = true;
      api.skills.versions(skill.skill_id)
        .then((items) => {
          versions = items;
        })
        .catch((error) => {
          addToast(toErrorMessage(error, 'Failed to load skill versions'), 'error');
          versions = [];
        })
        .finally(() => {
          loadingVersions = false;
        });
      return;
    }
    activeMode = 'view';
    setForm(createEmptySkillForm());
  });

  $effect(() => {
    if (!exportMenuOpen) {
      return;
    }
    if (typeof document === 'undefined') {
      return;
    }
    const handlePointerDown = (event: PointerEvent): void => {
      if (!exportMenuEl || !(event.target instanceof Node) || exportMenuEl.contains(event.target)) {
        return;
      }
      exportMenuOpen = false;
    };
    document.addEventListener('pointerdown', handlePointerDown, true);
    return () => document.removeEventListener('pointerdown', handlePointerDown, true);
  });

  function editableToolCards(): SkillToolFormItem[] {
    return form.tools;
  }

  function versionLabel(version: SkillVersion): string {
    return `v${version.version_number}`;
  }

  function formatTimestamp(value: string | null | undefined): string {
    if (!value) return 'Unknown time';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString();
  }

  function formatRelativeStepType(step: Record<string, unknown>): string {
    return typeof step.type === 'string' ? step.type : 'run';
  }

  function stepPrompt(step: Record<string, unknown>): string {
    return typeof step.prompt === 'string' ? step.prompt : '';
  }

  function stepRequiresDeliverable(step: Record<string, unknown>): boolean {
    return step.require_deliverable !== false;
  }

  function stepName(step: Record<string, unknown>, index: number): string {
    return typeof step.name === 'string' && step.name.trim() ? step.name : `step_${index + 1}`;
  }

  async function handleSheetClose(): Promise<void> {
    if ((activeMode === 'edit' || activeMode === 'create') && isDirty) {
      const confirmed = await confirmAction({
        title: 'Discard skill changes?',
        message: 'Closing now will discard your unsaved skill edits.',
        confirmLabel: 'Discard changes',
        cancelLabel: 'Keep editing'
      });
      if (!confirmed) {
        return;
      }
    }
    onClose();
  }

  async function startEditing(): Promise<void> {
    if (!skill || !canEditSkill) return;
    activeMode = 'edit';
    setForm(skillToFormState(skill));
  }

  async function cancelEditing(): Promise<void> {
    if ((activeMode === 'edit' || activeMode === 'create') && isDirty) {
      const confirmed = await confirmAction({
        title: activeMode === 'create' ? 'Discard new skill?' : 'Discard skill edits?',
        message:
          activeMode === 'create'
            ? 'This new skill has not been saved yet.'
            : 'Your unsaved skill edits will be lost.',
        confirmLabel: 'Discard changes',
        cancelLabel: 'Keep editing'
      });
      if (!confirmed) return;
    }
    if (activeMode === 'create') {
      onClose();
      return;
    }
    if (skill) {
      setForm(skillToFormState(skill));
    }
    activeMode = 'view';
  }

  async function downloadExport(format: SkillExportFormat) {
    if (!skill) return;
    exportMenuOpen = false;
    try {
      const result = await api.skills.export(skill.skill_id, format);
      if (result.warnings.length > 0) {
        addToast(result.warnings.join(' '), 'warning');
      }
      let blob: Blob;
      if (result.content_b64) {
        const bytes = Uint8Array.from(atob(result.content_b64), (char) => char.charCodeAt(0));
        blob = new Blob([bytes], { type: result.content_type || 'application/octet-stream' });
      } else {
        blob = new Blob([result.content || ''], { type: result.content_type || 'text/plain' });
      }
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = result.filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      addToast(toErrorMessage(error, 'Failed to export skill'), 'error');
    }
  }

  async function saveSkill(): Promise<void> {
    const issues = validateSkillForm(form);
    if (issues.length > 0) {
      addToast(issues[0], 'error');
      return;
    }
    saving = true;
    try {
      const payload = formStateToSkillPayload(form);
      const saved =
        activeMode === 'create' || !skill
          ? await api.skills.create(payload as SkillCreate)
          : await api.skills.update(skill.skill_id, payload);
      await onSaved(saved, activeMode === 'create' || !skill ? 'created' : 'updated');
      addToast(activeMode === 'create' || !skill ? 'Skill created.' : 'Skill updated.', 'success');
      activeMode = 'view';
    } catch (error) {
      addToast(toErrorMessage(error, 'Failed to save skill'), 'error');
    } finally {
      saving = false;
    }
  }

  async function deleteSkill(): Promise<void> {
    if (!skill || !canDeleteSkill) return;
    const confirmed = await confirmAction({
      title: 'Delete skill?',
      message: `Delete "${skill.name}"? This removes the skill and all of its versions from your library.`,
      confirmLabel: 'Delete skill',
      cancelLabel: 'Keep skill',
      variant: 'danger'
    });
    if (!confirmed) return;
    deleting = true;
    try {
      await api.skills.delete(skill.skill_id);
      await onDeleted(skill.skill_id);
      addToast(`Deleted ${skill.name}.`, 'success');
    } catch (error) {
      addToast(toErrorMessage(error, 'Failed to delete skill'), 'error');
    } finally {
      deleting = false;
    }
  }

  async function resetSkill(): Promise<void> {
    if (!skill || !canResetSkill) return;
    const confirmed = await confirmAction({
      title: 'Reset system skill?',
      message: `Reset ${skill.name} to the shipped default content? This creates a new skill version, but the current customized version stays in history.`,
      confirmLabel: 'Reset skill',
      cancelLabel: 'Keep current version',
      variant: 'danger'
    });
    if (!confirmed) return;
    try {
      const saved = await api.skills.reset(skill.skill_id);
      await onSaved(saved, 'updated');
      addToast('Skill reset to the shipped default.', 'success');
    } catch (error) {
      addToast(toErrorMessage(error, 'Failed to reset skill'), 'error');
    }
  }

  async function restoreVersion(version: SkillVersion): Promise<void> {
    if (!skill || !canRestoreVersions) return;
    const confirmed = await confirmAction({
      title: `Restore ${versionLabel(version)}?`,
      message:
        `Switch ${skill.name} to ${versionLabel(version)} from ${formatTimestamp(version.created_at)}? ` +
        'You can restore the current newest version again later from this history.',
      confirmLabel: `Restore ${versionLabel(version)}`,
      cancelLabel: 'Keep current version'
    });
    if (!confirmed) return;
    restoringVersionId = version.version_id;
    try {
      const saved = await api.skills.restoreVersion(skill.skill_id, version.version_id);
      await onSaved(saved, 'updated');
      addToast(`Restored ${versionLabel(version)}.`, 'success');
    } catch (error) {
      addToast(toErrorMessage(error, 'Failed to restore skill version'), 'error');
    } finally {
      restoringVersionId = null;
    }
  }

  async function previewDecomposition() {
    if (!skill || !allowManage) return;
    decompositionLoading = true;
    try {
      const preview = await api.skills.decomposePreview(skill.skill_id);
      decompositionPreview = preview.steps;
      decompositionRationale = preview.rationale;
      decompositionSourceHash = preview.source_hash;
      addToast('Skill decomposition preview generated.', 'success');
      requestAnimationFrame(() => {
        decompositionSectionEl?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    } catch (error) {
      addToast(toErrorMessage(error, 'Failed to decompose skill'), 'error');
    } finally {
      decompositionLoading = false;
    }
  }

  async function saveDecomposition() {
    if (!skill || !allowManage || !decompositionPreview || skill.is_system) return;
    decompositionSaving = true;
    try {
      const saved = await api.skills.update(skill.skill_id, {
        steps: decompositionPreview,
        decomposition_source_hash: decompositionSourceHash ?? undefined
      });
      decompositionPreview = null;
      decompositionRationale = '';
      decompositionSourceHash = null;
      versions = await api.skills.versions(saved.skill_id);
      await onSaved(saved, 'updated');
      addToast(
        saved.current_version
          ? `Saved decomposition as ${versionLabel(saved.current_version)}.`
          : 'Saved decomposition onto the skill.',
        'success'
      );
    } catch (error) {
      addToast(toErrorMessage(error, 'Failed to save decomposition'), 'error');
    } finally {
      decompositionSaving = false;
    }
  }

  async function openWorkflowEditor(steps: Record<string, unknown>[] | null | undefined): Promise<void> {
    if (!skill || !allowManage) return;
    if ((activeMode === 'edit' || activeMode === 'create') && isDirty) {
      const confirmed = await confirmAction({
        title: 'Leave skill editing?',
        message: 'Opening the workflow editor now will discard unsaved skill edits in this sheet.',
        confirmLabel: 'Open workflow editor',
        cancelLabel: 'Keep editing'
      });
      if (!confirmed) return;
    }
    const draft = skillToWorkflowDraft(skill, steps ?? undefined);
    const sourceHash = steps === decompositionPreview
      ? decompositionSourceHash
      : skill.current_version?.decomposition_source_hash ?? null;
    clearSkillWorkflowDraft();
    saveSkillWorkflowDraft(skill.skill_id, draft, sourceHash);
    await goto(`/workflows?draftFromSkill=${encodeURIComponent(skill.skill_id)}`);
  }

  async function uploadSkillAssets(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    const files = Array.from(target.files || []);
    if (files.length === 0) return;
    try {
      for (const file of files) {
        const uploaded = await api.artifacts.upload(file, 'skill_asset');
        form.assets = [
          ...form.assets.filter((asset) => asset.filename !== uploaded.filename),
          {
            filename: uploaded.filename,
            source_artifact_id: uploaded.artifact_id,
            content_type: uploaded.mime_type,
            size_bytes: uploaded.size_bytes
          }
        ];
      }
      addToast('Skill asset uploaded.', 'success');
    } catch (error) {
      addToast(toErrorMessage(error, 'Failed to upload skill asset'), 'error');
    } finally {
      target.value = '';
    }
  }

  function removeSkillAsset(filename: string): void {
    form.assets = form.assets.filter((asset) => asset.filename !== filename);
  }

  function addSecretPlaceholder(): void {
    const candidate = secretDraft.trim();
    if (!candidate) return;
    if (!form.secretPlaceholders.includes(candidate)) {
      form.secretPlaceholders = [...form.secretPlaceholders, candidate];
    }
    secretDraft = '';
  }

  function removeSecretPlaceholder(value: string): void {
    form.secretPlaceholders = form.secretPlaceholders.filter((item) => item !== value);
  }

  function addPromptTemplate(): void {
    form.promptTemplates = [...form.promptTemplates, createEmptyPromptTemplate()];
  }

  function removePromptTemplate(id: string): void {
    form.promptTemplates = form.promptTemplates.filter((template) => template.id !== id);
  }

  function addTool(): void {
    form.tools = [...form.tools, createEmptySkillTool()];
  }

  function removeTool(id: string): void {
    form.tools = form.tools.filter((tool) => tool.id !== id);
  }

  function addToolParameter(toolId: string): void {
    form.tools = form.tools.map((tool) =>
      tool.id === toolId
        ? {
            ...tool,
            parameters: [...tool.parameters, createEmptySkillParameter()]
          }
        : tool
    );
  }

  function removeToolParameter(toolId: string, parameterId: string): void {
    form.tools = form.tools.map((tool) =>
      tool.id === toolId
        ? {
            ...tool,
            parameters: tool.parameters.filter((parameter) => parameter.id !== parameterId)
          }
        : tool
    );
  }

  function addToolEnvEntry(toolId: string): void {
    form.tools = form.tools.map((tool) =>
      tool.id === toolId
        ? {
            ...tool,
            env: [...tool.env, createEmptyKeyValueEntry('skill_tool_env')]
          }
        : tool
    );
  }

  function removeToolEnvEntry(toolId: string, entryId: string): void {
    form.tools = form.tools.map((tool) =>
      tool.id === toolId
        ? {
            ...tool,
            env: tool.env.filter((entry) => entry.id !== entryId)
          }
        : tool
    );
  }
</script>

<Sheet
  open={open}
  onClose={() => void handleSheetClose()}
  side="right"
  label={activeMode === 'create' ? 'Create skill' : skill ? `${skill.name} skill details` : 'Skill details'}
  class="w-full md:w-[min(64rem,100vw)]"
>
  {#snippet header()}
    <div class="flex flex-col gap-3">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-sm font-semibold text-slate-100">
            {#if activeMode === 'create'}
              New Skill
            {:else}
              {skill?.name ?? 'Skill'}
            {/if}
          </p>
          <p class="mt-1 text-xs text-slate-400">
            {#if activeMode === 'create'}
              Create a reusable skill with instructions, optional tool recipes, assets, and workflow material.
            {:else if activeMode === 'edit'}
              Editing the current skill version. Saving creates a new immutable version.
            {:else}
              {skill?.description ?? 'Reusable instructions, optional tools, and optional workflow structure.'}
            {/if}
          </p>
        </div>
        {#if skill}
          <div class="flex flex-wrap items-center gap-2">
            <Badge>{skill.source}</Badge>
            {#if skill.is_system}
              <Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">system</Badge>
            {/if}
            {#if skill.attach_to_all_agents ?? skill.auto_load}
              <Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">attached to all agents</Badge>
            {/if}
            {#if savedSteps.length > 0}
              <Badge class="border-emerald-500/30 bg-emerald-500/10 text-emerald-300">workflow</Badge>
            {/if}
            {#if currentVersion?.decomposition_stale}
              <Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">decomposition stale</Badge>
            {/if}
          </div>
        {/if}
      </div>

      {#if activeMode === 'view' && skill && allowManage}
        <div class="flex flex-wrap items-center gap-2">
          {#if canEditSkill}
            <Button size="sm" variant="secondary" onclick={() => void startEditing()}>
              <Pencil class="mr-1 h-4 w-4" /> Edit skill
            </Button>
          {/if}

          {#if canExportSkill}
            <div class="relative" bind:this={exportMenuEl}>
              <Button size="sm" variant="secondary" onclick={() => (exportMenuOpen = !exportMenuOpen)}>
                <Download class="mr-1 h-4 w-4" /> Export
              </Button>
              {#if exportMenuOpen}
                <div class="absolute right-0 top-full z-10 mt-2 w-48 rounded-2xl border border-slate-800 bg-slate-950 p-1 shadow-card">
                  <button class="flex w-full items-center rounded-xl px-3 py-2 text-left text-sm text-slate-200 hover:bg-slate-900" onclick={() => void downloadExport('skill_md')}>
                    SKILL.md
                  </button>
                  <button class="flex w-full items-center rounded-xl px-3 py-2 text-left text-sm text-slate-200 hover:bg-slate-900" onclick={() => void downloadExport('cognis_yaml')}>
                    Cognis YAML
                  </button>
                  <button class="flex w-full items-center rounded-xl px-3 py-2 text-left text-sm text-slate-200 hover:bg-slate-900" onclick={() => void downloadExport('cognis_package')}>
                    Full package
                  </button>
                </div>
              {/if}
            </div>
          {/if}

          {#if canResetSkill}
            <Button size="sm" variant="secondary" onclick={() => void resetSkill()}>
              <RotateCcw class="mr-1 h-4 w-4" /> Reset to default
            </Button>
          {/if}

          {#if canDeleteSkill}
            <Button size="sm" variant="danger" disabled={deleting} onclick={() => void deleteSkill()}>
              <Trash2 class="mr-1 h-4 w-4" /> {deleting ? 'Deleting…' : 'Delete'}
            </Button>
          {/if}
        </div>
      {/if}

      {#if activeMode !== 'view'}
        <div class="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="secondary" onclick={() => void cancelEditing()}>Cancel</Button>
          <Button size="sm" variant="primary" disabled={saving} onclick={() => void saveSkill()}>
            <Save class="mr-1 h-4 w-4" /> {saving ? 'Saving…' : activeMode === 'create' ? 'Create skill' : 'Save new version'}
          </Button>
        </div>
      {/if}
    </div>
  {/snippet}

  <div class="space-y-5 text-sm">
    {#if activeMode === 'view' && skill}
      <section class="space-y-2 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex items-center gap-2 text-slate-200">
          <FileText class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Instructions</h3>
        </div>
        <pre class="overflow-x-auto whitespace-pre-wrap rounded-xl bg-slate-900 p-3 text-xs text-slate-200">{currentVersion?.instructions ?? skill.instructions}</pre>
      </section>

      <section class="grid gap-4 lg:grid-cols-2">
        <div class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
          <div class="flex items-center gap-2 text-slate-200">
            <Wrench class="h-4 w-4 text-slate-400" />
            <h3 class="font-medium">Tools</h3>
            <Tooltip placement="bottom" text="Optional executable tools bundled with this skill. Most skills only need instructions. Add tools when a skill should expose reusable commands on the executor.">
              <button aria-label="Tools help" class={tooltipButtonClass} title="Tools help" type="button"><Info class="h-4 w-4" /></button>
            </Tooltip>
          </div>
          {#if currentVersion?.tools && currentVersion.tools.length > 0}
            <div class="space-y-2">
              {#each currentVersion.tools as tool}
                <div class="rounded-xl border border-slate-800 bg-slate-900/80 p-3">
                  <div class="flex flex-wrap items-center gap-2">
                    <p class="font-medium text-slate-100">{String(tool.name ?? 'Unnamed tool')}</p>
                    {#if tool.read_only}
                      <Badge>read-only</Badge>
                    {/if}
                    {#if tool.recipe && typeof tool.recipe === 'object' && tool.recipe !== null}
                      <Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">{String((tool.recipe as Record<string, unknown>).mode ?? 'recipe')}</Badge>
                    {/if}
                  </div>
                  <p class="mt-1 text-xs text-slate-400">{String(tool.description ?? '')}</p>
                  {#if tool.parameters && typeof tool.parameters === 'object' && tool.parameters !== null && Object.keys((tool.parameters as Record<string, unknown>).properties ?? {}).length > 0}
                    <div class="mt-3 flex flex-wrap gap-2 text-xs">
                      {#each Object.entries(((tool.parameters as Record<string, unknown>).properties ?? {}) as Record<string, unknown>) as [parameterName, parameter]}
                        <span class="rounded-full border border-slate-700 bg-slate-950 px-2 py-1 text-slate-300">
                          {parameterName}
                          {#if typeof (parameter as Record<string, unknown>).type === 'string'}
                            <span class="text-slate-500"> · {(parameter as Record<string, unknown>).type}</span>
                          {/if}
                        </span>
                      {/each}
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {:else}
            <p class="text-xs text-slate-500">No executable tools bundled with this skill.</p>
          {/if}
        </div>

        <div class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
          <div class="flex items-center gap-2 text-slate-200">
            <Wrench class="h-4 w-4 text-slate-400" />
            <h3 class="font-medium">Linked Runtime Tools</h3>
            <Tooltip placement="bottom" text="Existing registry tools that this skill should expose when it is attached or loaded. These are not bundled with the skill package itself.">
              <button aria-label="Linked tools help" class={tooltipButtonClass} title="Linked tools help" type="button"><Info class="h-4 w-4" /></button>
            </Tooltip>
          </div>
          {#if skill.linked_tool_ids && skill.linked_tool_ids.length > 0}
            <div class="flex flex-wrap gap-2">
              {#each skill.linked_tool_ids as toolId}
                <Badge>{linkedToolDisplay(toolId)}</Badge>
              {/each}
            </div>
          {:else}
            <p class="text-xs text-slate-500">No linked runtime tools configured.</p>
          {/if}
        </div>

        <div class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
          <div class="flex items-center gap-2 text-slate-200">
            <Layers class="h-4 w-4 text-slate-400" />
            <h3 class="font-medium">Templates And Secrets</h3>
          </div>

          <div>
            <div class="flex items-center gap-2">
              <p class="text-xs font-medium uppercase tracking-[0.2em] text-slate-400">Prompt templates</p>
              <Tooltip placement="bottom" text="Named text snippets stored on the skill. These help with reusable prompts or sub-prompts without cluttering the main instructions.">
                <button aria-label="Prompt templates help" class={tooltipButtonClass} title="Prompt templates help" type="button"><Info class="h-4 w-4" /></button>
              </Tooltip>
            </div>
            {#if currentVersion?.prompt_templates && Object.keys(currentVersion.prompt_templates).length > 0}
              <div class="mt-2 space-y-2">
                {#each Object.entries(currentVersion.prompt_templates) as [key, value]}
                  <div class="rounded-xl border border-slate-800 bg-slate-900/80 p-3">
                    <p class="font-medium text-slate-100">{key}</p>
                    <pre class="mt-2 whitespace-pre-wrap text-xs text-slate-300">{String(value)}</pre>
                  </div>
                {/each}
              </div>
            {:else}
              <p class="mt-2 text-xs text-slate-500">No prompt templates saved.</p>
            {/if}
          </div>

          <div>
            <div class="flex items-center gap-2">
              <p class="text-xs font-medium uppercase tracking-[0.2em] text-slate-400">Secret placeholders</p>
              <Tooltip placement="bottom" text="Names of secrets that the executor may inject as environment variables when this skill's tool recipes run. These are placeholders only, never the secret values themselves.">
                <button aria-label="Secret placeholders help" class={tooltipButtonClass} title="Secret placeholders help" type="button"><Info class="h-4 w-4" /></button>
              </Tooltip>
            </div>
            {#if currentVersion?.secret_placeholders && currentVersion.secret_placeholders.length > 0}
              <div class="mt-2 flex flex-wrap gap-2">
                {#each currentVersion.secret_placeholders as placeholder}
                  <Badge>{placeholder}</Badge>
                {/each}
              </div>
            {:else}
              <p class="mt-2 text-xs text-slate-500">No secret placeholders declared.</p>
            {/if}
          </div>
        </div>
      </section>
    {:else}
      <section class="space-y-4 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm text-slate-200">
            <span>Name</span>
            <input bind:value={form.name} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="e.g. git-release" type="text" />
          </label>
          <label class="space-y-2 text-sm text-slate-200">
            <span>Tags</span>
            <input bind:value={form.tagsText} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="release, automation" type="text" />
          </label>
        </div>

        <label class="space-y-2 text-sm text-slate-200">
          <span>Description</span>
          <input bind:value={form.description} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="What this skill is for" type="text" />
        </label>

        <label class="space-y-2 text-sm text-slate-200">
          <span>Instructions</span>
          <textarea bind:value={form.instructions} rows="10" class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="# Skill instructions"></textarea>
        </label>

        <label class="flex items-center gap-2 text-sm text-slate-300">
          <input bind:checked={form.attachToAllAgents} class="rounded border-slate-600 bg-slate-950" type="checkbox" />
          Attach to all agents
        </label>
      </section>

      <section class="grid gap-4 xl:grid-cols-2">
        <div class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4 xl:col-span-2">
          <div class="flex items-center gap-2 text-slate-200">
            <Wrench class="h-4 w-4 text-slate-400" />
            <h3 class="font-medium">Linked Runtime Tools</h3>
            <Tooltip placement="bottom" text="Select existing builtin or MCP tools that should become available when this skill is attached or loaded. This is separate from bundled executable tools below.">
              <button aria-label="Linked runtime tools help" class={tooltipButtonClass} title="Linked runtime tools help" type="button"><Info class="h-4 w-4" /></button>
            </Tooltip>
          </div>
          <input bind:value={linkedToolSearch} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="Search existing tools by name, source, category, or id" type="text" />
          {#if form.linkedToolIds.length > 0}
            <div class="flex flex-wrap gap-2">
              {#each form.linkedToolIds as toolId}
                <span class="inline-flex items-center gap-1 rounded-full border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200">
                  {linkedToolDisplay(toolId)}
                  <button class="text-slate-500 hover:text-rose-300" onclick={() => toggleLinkedTool(toolId)} type="button"><X class="h-3.5 w-3.5" /></button>
                </span>
              {/each}
            </div>
          {:else}
            <p class="text-xs text-slate-500">No linked runtime tools selected yet.</p>
          {/if}
          <div class="max-h-72 space-y-2 overflow-y-auto rounded-xl border border-slate-800 bg-slate-950/40 p-2">
            {#if filteredLinkedTools.length > 0}
              {#each filteredLinkedTools as tool}
                <label class="flex cursor-pointer items-start justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900/80 p-3 text-sm text-slate-200">
                  <div class="min-w-0 space-y-1">
                    <div class="flex flex-wrap items-center gap-2">
                      <span class="font-medium text-slate-100">{tool.name}</span>
                      <Badge>{tool.category}</Badge>
                      {#if tool.profile_group}<Badge>{tool.profile_group}</Badge>{/if}
                      <Badge>{tool.source?.server_name || tool.source?.type || 'tool'}</Badge>
                    </div>
                    <p class="text-xs text-slate-400">{tool.description}</p>
                    {#if tool.tool_id}<p class="font-mono text-[11px] text-slate-500">{tool.tool_id}</p>{/if}
                  </div>
                  <input checked={Boolean(tool.tool_id && form.linkedToolIds.includes(tool.tool_id))} class="mt-1 rounded border-slate-600 bg-slate-950" disabled={!tool.tool_id} onchange={() => tool.tool_id && toggleLinkedTool(tool.tool_id)} type="checkbox" />
                </label>
              {/each}
            {:else}
              <p class="px-2 py-1 text-xs text-slate-500">No matching existing tools.</p>
            {/if}
          </div>
        </div>

        <div class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
          <div class="flex items-center gap-2 text-slate-200">
            <Layers class="h-4 w-4 text-slate-400" />
            <h3 class="font-medium">Prompt Templates</h3>
            <Tooltip placement="bottom" text="Optional named snippets stored with the skill. Useful for reusable prompts or canned sub-prompts.">
              <button aria-label="Prompt templates help" class={tooltipButtonClass} title="Prompt templates help" type="button"><Info class="h-4 w-4" /></button>
            </Tooltip>
          </div>
          {#if form.promptTemplates.length === 0}
            <p class="text-xs text-slate-500">No prompt templates yet.</p>
          {/if}
          {#each form.promptTemplates as template}
            <div class="space-y-2 rounded-xl border border-slate-800 bg-slate-900/80 p-3">
              <div class="flex items-center gap-2">
                <input bind:value={template.key} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="Template name" type="text" />
                <button class="text-slate-500 hover:text-rose-300" onclick={() => removePromptTemplate(template.id)} type="button"><X class="h-4 w-4" /></button>
              </div>
              <textarea bind:value={template.value} rows="4" class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="Template text"></textarea>
            </div>
          {/each}
          <Button size="sm" variant="secondary" onclick={addPromptTemplate}><Plus class="mr-1 h-4 w-4" /> Add template</Button>
        </div>

        <div class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
          <div class="flex items-center gap-2 text-slate-200">
            <FileText class="h-4 w-4 text-slate-400" />
            <h3 class="font-medium">Secret Placeholders</h3>
            <Tooltip placement="bottom" text="Names of secrets that the executor can inject as environment variables when the skill's tool recipes run. Put names like GITHUB_TOKEN here, never actual values.">
              <button aria-label="Secret placeholders help" class={tooltipButtonClass} title="Secret placeholders help" type="button"><Info class="h-4 w-4" /></button>
            </Tooltip>
          </div>
          <div class="flex gap-2">
            <input
              bind:value={secretDraft}
              class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
              onkeydown={(event) => {
                if (event.key === 'Enter' || event.key === ',') {
                  event.preventDefault();
                  addSecretPlaceholder();
                }
              }}
              placeholder="e.g. GITHUB_TOKEN"
              type="text"
            />
            <Button size="sm" variant="secondary" onclick={addSecretPlaceholder}>Add</Button>
          </div>
          {#if form.secretPlaceholders.length > 0}
            <div class="flex flex-wrap gap-2">
              {#each form.secretPlaceholders as placeholder}
                <span class="inline-flex items-center gap-1 rounded-full border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200">
                  {placeholder}
                  <button class="text-slate-500 hover:text-rose-300" onclick={() => removeSecretPlaceholder(placeholder)} type="button"><X class="h-3.5 w-3.5" /></button>
                </span>
              {/each}
            </div>
          {:else}
            <p class="text-xs text-slate-500">No placeholders declared yet.</p>
          {/if}
        </div>
      </section>

      <section class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex items-center gap-2 text-slate-200">
          <Wrench class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Skill Tools</h3>
          <Tooltip placement="bottom" text="Use skill tools only when the skill should expose reusable executor-side commands. This editor covers the common skill tool shape without making you write raw JSON.">
            <button aria-label="Skill tools help" class={tooltipButtonClass} title="Skill tools help" type="button"><Info class="h-4 w-4" /></button>
          </Tooltip>
        </div>
        {#if editableToolCards().length === 0}
          <p class="text-xs text-slate-500">No tools yet. Many skills work fine with instructions only.</p>
        {/if}
        {#each editableToolCards() as tool}
          <div class="space-y-4 rounded-2xl border border-slate-800 bg-slate-900/80 p-4">
            <div class="flex items-center justify-between gap-3">
              <p class="font-medium text-slate-100">{tool.name || 'New tool'}</p>
              <button class="text-slate-500 hover:text-rose-300" onclick={() => removeTool(tool.id)} type="button"><Trash2 class="h-4 w-4" /></button>
            </div>

            <div class="grid gap-4 md:grid-cols-2">
              <label class="space-y-2 text-sm text-slate-200">
                <span>Tool name</span>
                <input bind:value={tool.name} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="publish_release" type="text" />
              </label>
              <label class="space-y-2 text-sm text-slate-200">
                <span>Description</span>
                <input bind:value={tool.description} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="What the tool does" type="text" />
              </label>
            </div>

            <div class="grid gap-4 md:grid-cols-4">
              <label class="space-y-2 text-sm text-slate-200">
                <span>Timeout (s)</span>
                <input bind:value={tool.timeoutSeconds} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" min="1" type="number" />
              </label>
              <label class="space-y-2 text-sm text-slate-200">
                <span>Max result size</span>
                <input bind:value={tool.maxResultSize} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" min="1" type="number" />
              </label>
              <label class="flex items-center gap-2 text-sm text-slate-300 md:self-end">
                <input bind:checked={tool.readOnly} class="rounded border-slate-600 bg-slate-950" type="checkbox" />
                Read-only
              </label>
              <label class="flex items-center gap-2 text-sm text-slate-300 md:self-end">
                <input bind:checked={tool.nonBypassable} class="rounded border-slate-600 bg-slate-950" type="checkbox" />
                Always guardrail-check
              </label>
            </div>

            <div class="space-y-3 rounded-xl border border-slate-800 bg-slate-950/60 p-3">
              <div class="flex items-center justify-between gap-2">
                <div>
                  <p class="text-sm font-medium text-slate-100">Parameters</p>
                  <p class="text-xs text-slate-500">Common object-style tool inputs.</p>
                </div>
                <Button size="sm" variant="secondary" onclick={() => addToolParameter(tool.id)}><Plus class="mr-1 h-4 w-4" /> Add parameter</Button>
              </div>
              {#if tool.parameters.length === 0}
                <p class="text-xs text-slate-500">No parameters yet.</p>
              {/if}
              {#each tool.parameters as parameter}
                <div class="grid gap-3 rounded-xl border border-slate-800 bg-slate-900/80 p-3 md:grid-cols-[1.2fr,0.8fr,1.6fr,auto]">
                  <input bind:value={parameter.name} class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="Parameter name" type="text" />
                  <select bind:value={parameter.type} class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    <option value="string">string</option>
                    <option value="number">number</option>
                    <option value="integer">integer</option>
                    <option value="boolean">boolean</option>
                    <option value="array">array</option>
                    <option value="object">object</option>
                  </select>
                  <div class="space-y-2">
                    <input bind:value={parameter.description} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="Description" type="text" />
                    <input bind:value={parameter.enumText} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="Enum values (comma-separated, optional)" type="text" />
                  </div>
                  <div class="flex items-center justify-between gap-2 md:flex-col md:items-end">
                    <label class="flex items-center gap-2 text-xs text-slate-300">
                      <input bind:checked={parameter.required} class="rounded border-slate-600 bg-slate-950" type="checkbox" />
                      Required
                    </label>
                    <button class="text-slate-500 hover:text-rose-300" onclick={() => removeToolParameter(tool.id, parameter.id)} type="button"><X class="h-4 w-4" /></button>
                  </div>
                </div>
              {/each}
            </div>

            <div class="space-y-3 rounded-xl border border-slate-800 bg-slate-950/60 p-3">
              <div class="flex items-center gap-2">
                <p class="text-sm font-medium text-slate-100">Execution recipe</p>
                <Tooltip placement="bottom" text="Recipes tell the executor how to run this tool. Leave this off if you are only drafting a tool shape for later.">
                  <button aria-label="Execution recipe help" class={tooltipButtonClass} title="Execution recipe help" type="button"><Info class="h-4 w-4" /></button>
                </Tooltip>
              </div>

              <div class="grid gap-4 md:grid-cols-2">
                <label class="space-y-2 text-sm text-slate-200">
                  <span>Recipe mode</span>
                  <select bind:value={tool.recipeMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    <option value="none">No recipe</option>
                    <option value="script">Script</option>
                    <option value="command">Command</option>
                  </select>
                </label>
                <label class="space-y-2 text-sm text-slate-200">
                  <span>{tool.recipeMode === 'script' ? 'Entry script path' : 'Entry command'}</span>
                  <input bind:value={tool.entry} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder={tool.recipeMode === 'script' ? 'scripts/release.sh' : 'git'} type="text" />
                </label>
              </div>

              {#if tool.recipeMode !== 'none'}
                <div class="grid gap-4 md:grid-cols-2">
                  <label class="space-y-2 text-sm text-slate-200">
                    <span>Arguments</span>
                    <textarea bind:value={tool.argsText} rows="3" class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="One per line"></textarea>
                  </label>
                  <label class="space-y-2 text-sm text-slate-200">
                    <span>Working directory</span>
                    <input bind:value={tool.workingDir} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="scripts" type="text" />
                  </label>
                </div>

                <div class="grid gap-4 md:grid-cols-2">
                  <label class="space-y-2 text-sm text-slate-200">
                    <span>Required assets</span>
                    <textarea bind:value={tool.requiredAssetsText} rows="3" class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="One relative path per line"></textarea>
                  </label>
                  <label class="space-y-2 text-sm text-slate-200">
                    <span>Recipe secret placeholders</span>
                    <textarea bind:value={tool.secretPlaceholdersText} rows="3" class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="One secret name per line"></textarea>
                  </label>
                </div>

                <div class="space-y-3">
                  <div class="flex items-center justify-between gap-2">
                    <div>
                      <p class="text-sm font-medium text-slate-100">Recipe environment variables</p>
                      <p class="text-xs text-slate-500">Extra static environment values passed to the command.</p>
                    </div>
                    <Button size="sm" variant="secondary" onclick={() => addToolEnvEntry(tool.id)}><Plus class="mr-1 h-4 w-4" /> Add env</Button>
                  </div>
                  {#if tool.env.length === 0}
                    <p class="text-xs text-slate-500">No static env entries yet.</p>
                  {/if}
                  {#each tool.env as entry}
                    <div class="grid gap-3 rounded-xl border border-slate-800 bg-slate-900/80 p-3 md:grid-cols-[1fr,1fr,auto]">
                      <input bind:value={entry.key} class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="KEY" type="text" />
                      <input bind:value={entry.value} class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" placeholder="value" type="text" />
                      <button class="text-slate-500 hover:text-rose-300" onclick={() => removeToolEnvEntry(tool.id, entry.id)} type="button"><X class="h-4 w-4" /></button>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>
          </div>
        {/each}
        <Button size="sm" variant="secondary" onclick={addTool}><Plus class="mr-1 h-4 w-4" /> Add tool</Button>
      </section>

      <section class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div>
            <p class="text-sm font-medium text-slate-100">Assets</p>
            <p class="text-xs text-slate-500">Upload files once. Cognis versions them and stages them onto executors only when needed.</p>
          </div>
          <label class="inline-flex cursor-pointer items-center gap-2 rounded-xl border border-slate-700 px-3 py-2 text-sm text-slate-200 hover:border-slate-600">
            <Upload class="h-4 w-4" /> Add files
            <input type="file" multiple class="hidden" onchange={uploadSkillAssets} />
          </label>
        </div>
        {#if form.assets.length > 0}
          <div class="space-y-2">
            {#each form.assets as asset}
              <div class="flex flex-col gap-2 rounded-xl border border-slate-800 bg-slate-900/80 p-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <p class="font-mono text-xs text-slate-100">{asset.filename}</p>
                  <p class="mt-1 text-xs text-slate-500">{asset.content_type || 'application/octet-stream'}{#if asset.size_bytes} · {asset.size_bytes} bytes{/if}</p>
                </div>
                <button class="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-rose-300" type="button" onclick={() => removeSkillAsset(asset.filename)}>
                  <X class="h-3.5 w-3.5" /> Remove
                </button>
              </div>
            {/each}
          </div>
        {:else}
          <p class="text-xs text-slate-500">No assets attached.</p>
        {/if}
      </section>
    {/if}

    {#if skill}
      <section bind:this={decompositionSectionEl} class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex flex-wrap items-center gap-2 text-slate-200">
          <GitBranch class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Workflow Decomposition</h3>
          <Tooltip placement="bottom" text="Saved decomposition turns this skill into workflow material. The workflow composer can reuse these steps instead of guessing structure from the instructions each time.">
            <button aria-label="Workflow decomposition help" class={tooltipButtonClass} title="Workflow decomposition help" type="button"><Info class="h-4 w-4" /></button>
          </Tooltip>
          {#if currentVersion?.decomposition_stale}
            <Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">stale</Badge>
          {/if}
        </div>

        {#if allowManage}
          <div class="flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" onclick={() => void previewDecomposition()} disabled={decompositionLoading}>
              <GitBranch class="mr-1 h-4 w-4" /> {decompositionLoading ? 'Generating…' : 'Suggest decomposition'}
            </Button>
            {#if savedSteps.length > 0}
              <Button size="sm" variant="secondary" onclick={() => void openWorkflowEditor(savedSteps)}>
                <ExternalLink class="mr-1 h-4 w-4" /> Open in workflow editor
              </Button>
            {/if}
            {#if decompositionPreview && decompositionPreview.length > 0}
              {#if !skill.is_system}
                <Button size="sm" variant="secondary" onclick={() => void saveDecomposition()} disabled={decompositionSaving}>
                  <Save class="mr-1 h-4 w-4" /> {decompositionSaving ? 'Saving…' : 'Save decomposition'}
                </Button>
              {/if}
              <Button size="sm" variant="secondary" onclick={() => void openWorkflowEditor(decompositionPreview)}>
                <ExternalLink class="mr-1 h-4 w-4" /> Edit preview as workflow
              </Button>
            {/if}
          </div>
        {/if}

        <div class="grid gap-4 lg:grid-cols-2">
          <div class="space-y-2">
            <div class="flex items-center justify-between gap-2">
              <p class="text-xs font-medium uppercase tracking-[0.2em] text-slate-400">Saved on skill</p>
              {#if currentVersion?.decomposition_stale}
                <p class="text-xs text-amber-300">Refresh suggested steps before relying on them.</p>
              {/if}
            </div>
            {#if savedSteps.length > 0}
              <div class="space-y-2">
                {#each savedSteps as step, index}
                  <div class="rounded-xl border border-slate-800 bg-slate-900/80 p-3">
                    <div class="flex flex-wrap items-center gap-2">
                      <p class="font-medium text-slate-100">{index + 1}. {stepName(step, index)}</p>
                      <Badge>{formatRelativeStepType(step)}</Badge>
                      {#if stepRequiresDeliverable(step)}
                        <Badge class="border-emerald-500/30 bg-emerald-500/10 text-emerald-300">deliverable</Badge>
                      {/if}
                    </div>
                    {#if stepPrompt(step)}
                      <p class="mt-2 text-xs text-slate-400">{stepPrompt(step)}</p>
                    {/if}
                  </div>
                {/each}
              </div>
            {:else}
              <p class="text-xs text-slate-500">No decomposition saved yet.</p>
            {/if}
          </div>

          <div class="space-y-2">
            <p class="text-xs font-medium uppercase tracking-[0.2em] text-slate-400">Latest suggestion</p>
            {#if decompositionPreview && decompositionPreview.length > 0}
              <div class="rounded-2xl border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-slate-200">
                {#if decompositionRationale}
                  <p class="text-slate-300">{decompositionRationale}</p>
                {/if}
                <div class="mt-3 space-y-2">
                  {#each decompositionPreview as step, index}
                    <div class="rounded-xl border border-slate-800 bg-slate-900/80 p-3">
                      <div class="flex flex-wrap items-center gap-2">
                        <p class="font-medium text-slate-100">{index + 1}. {stepName(step, index)}</p>
                        <Badge>{formatRelativeStepType(step)}</Badge>
                        {#if stepRequiresDeliverable(step)}
                          <Badge class="border-emerald-500/30 bg-emerald-500/10 text-emerald-300">deliverable</Badge>
                        {/if}
                      </div>
                      {#if stepPrompt(step)}
                        <p class="mt-2 text-xs text-slate-400">{stepPrompt(step)}</p>
                      {/if}
                    </div>
                  {/each}
                </div>
              </div>
            {:else}
              <p class="text-xs text-slate-500">Generate a suggestion to review candidate workflow steps here.</p>
            {/if}
          </div>
        </div>
      </section>

      <section class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex items-center gap-2 text-slate-200">
          <Download class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Assets</h3>
        </div>
        {#if currentVersion?.asset_manifest && currentVersion.asset_manifest.length > 0}
          <div class="space-y-2">
            {#each currentVersion.asset_manifest as asset}
              <div class="flex flex-col gap-2 rounded-xl border border-slate-800 bg-slate-900/80 p-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <p class="font-mono text-xs text-slate-100">{asset.filename}</p>
                  <p class="mt-1 text-xs text-slate-500">{asset.content_type} · {asset.size_bytes} bytes · {asset.content_hash.slice(0, 8)}</p>
                </div>
                {#if asset.url}
                  <a class="inline-flex items-center gap-1 text-xs text-amber-300 hover:text-amber-200" href={asset.url} rel="noreferrer" target="_blank">
                    <Download class="h-3.5 w-3.5" /> Download
                  </a>
                {/if}
              </div>
            {/each}
          </div>
        {:else}
          <p class="text-xs text-slate-500">No assets attached.</p>
        {/if}
      </section>

      <section class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex items-center gap-2 text-slate-200">
          <RotateCcw class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Version History</h3>
        </div>
        {#if loadingVersions}
          <p class="text-xs text-slate-500">Loading versions…</p>
        {:else if versions.length === 0}
          <p class="text-xs text-slate-500">No versions available.</p>
        {:else}
          <div class="space-y-2">
            {#each versions as version}
              <div class="rounded-xl border border-slate-800 bg-slate-900/80 p-3">
                <div class="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div class="flex flex-wrap items-center gap-2">
                      <p class="text-sm font-medium text-slate-100">{versionLabel(version)}</p>
                      {#if version.version_id === skill.current_version_id}
                        <Badge class="border-emerald-500/30 bg-emerald-500/10 text-emerald-300">current</Badge>
                      {/if}
                      {#if version.version_id === latestVersionId}
                        <Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">latest</Badge>
                      {/if}
                    </div>
                    <p class="mt-1 text-xs text-slate-500">{formatTimestamp(version.created_at)} · {version.version_id} · {version.content_hash.slice(0, 8)}</p>
                  </div>
                  {#if canRestoreVersions && version.version_id !== skill.current_version_id}
                    <Button size="sm" variant="secondary" disabled={restoringVersionId === version.version_id} onclick={() => void restoreVersion(version)}>
                      {restoringVersionId === version.version_id ? 'Restoring…' : version.version_id === latestVersionId ? 'Restore latest' : 'Restore'}
                    </Button>
                  {/if}
                </div>
                {#if version.source_url}
                  <p class="mt-2 text-xs text-slate-400">Imported from <span class="break-all text-slate-300">{version.source_url}</span></p>
                {/if}
                {#if version.asset_manifest && version.asset_manifest.length > 0}
                  <p class="mt-1 text-xs text-slate-500">{version.asset_manifest.length} asset(s)</p>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
      </section>
    {/if}
  </div>
</Sheet>
