<script lang="ts">
  import Download from 'lucide-svelte/icons/download';
  import FileText from 'lucide-svelte/icons/file-text';
  import GitBranch from 'lucide-svelte/icons/git-branch';
  import Layers from 'lucide-svelte/icons/layers';
  import Wrench from 'lucide-svelte/icons/wrench';

  import { api } from '$lib/api/client';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import { addToast } from '$lib/stores/toasts';
  import type { Skill, SkillVersion } from '$lib/types/api';

  let {
    open,
    skill,
    onClose,
    allowRestore = false,
    onRestored = () => {}
  } = $props<{
    open: boolean;
    skill: Skill | null;
    onClose: () => void;
    allowRestore?: boolean;
    onRestored?: () => void | Promise<void>;
  }>();

  let versions = $state<SkillVersion[]>([]);
  let loadingVersions = $state(false);
  let restoringVersionId = $state<string | null>(null);
  let decompositionPreview = $state<Record<string, unknown>[] | null>(null);
  let decompositionRationale = $state('');
  let decompositionSourceHash = $state<string | null>(null);
  let decompositionLoading = $state(false);
  let decompositionSaving = $state(false);

  $effect(() => {
    if (!open || !skill) {
      versions = [];
      decompositionPreview = null;
      decompositionRationale = '';
      decompositionSourceHash = null;
      return;
    }
    loadingVersions = true;
    api.skills.versions(skill.skill_id)
      .then((items) => {
        versions = items;
      })
      .catch((error) => {
        addToast(error instanceof Error ? error.message : 'Failed to load versions', 'error');
        versions = [];
      })
      .finally(() => {
        loadingVersions = false;
      });
  });

  function formatJson(value: unknown): string {
    return JSON.stringify(value ?? {}, null, 2);
  }

  async function downloadExport(format: 'skill_md' | 'cognis_yaml' | 'cognis_package') {
    if (!skill) return;
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
      addToast(error instanceof Error ? error.message : 'Failed to export skill', 'error');
    }
  }

  async function restoreVersion(versionId: string) {
    if (!skill) return;
    restoringVersionId = versionId;
    try {
      await api.skills.restoreVersion(skill.skill_id, versionId);
      await onRestored();
      addToast('Skill version restored.', 'success');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to restore version', 'error');
    } finally {
      restoringVersionId = null;
    }
  }

  async function previewDecomposition() {
    if (!skill) return;
    decompositionLoading = true;
    try {
      const preview = await api.skills.decomposePreview(skill.skill_id);
      decompositionPreview = preview.steps;
      decompositionRationale = preview.rationale;
      decompositionSourceHash = preview.source_hash;
      addToast('Skill decomposition preview generated.', 'success');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to decompose skill', 'error');
    } finally {
      decompositionLoading = false;
    }
  }

  async function saveDecomposition() {
    if (!skill || !decompositionPreview) return;
    decompositionSaving = true;
    try {
      await api.skills.update(skill.skill_id, {
        steps: decompositionPreview,
        decomposition_source_hash: decompositionSourceHash ?? undefined
      });
      await onRestored();
      addToast('Skill decomposition saved.', 'success');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to save decomposition', 'error');
    } finally {
      decompositionSaving = false;
    }
  }
</script>

<Sheet {open} {onClose} side="right" label={skill ? `${skill.name} details` : 'Skill details'} class="w-full md:w-[min(46rem,100vw)]">
  {#snippet header()}
    <div class="flex items-center justify-between gap-3">
      <div>
        <p class="text-sm font-semibold text-slate-100">{skill?.name ?? 'Skill'}</p>
        {#if skill?.description}
          <p class="mt-1 text-xs text-slate-400">{skill.description}</p>
        {/if}
      </div>
      {#if skill}
        <div class="flex flex-wrap items-center gap-2">
          <Badge>{skill.source}</Badge>
          {#if skill.is_system}
            <Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">system</Badge>
          {/if}
          {#if skill.attach_to_all_agents ?? skill.auto_load}
            <Badge class="border-blue-500/30 bg-blue-500/10 text-blue-300">attached to all agents</Badge>
          {/if}
        </div>
      {/if}
    </div>
  {/snippet}

  {#if skill}
    {@const current = skill.current_version}
    {@const savedSteps = current?.steps ?? skill.steps ?? []}
    <div class="space-y-5 text-sm">
      <div class="flex flex-wrap gap-2">
        <Button size="sm" variant="secondary" onclick={() => downloadExport('skill_md')}><Download class="mr-1 h-4 w-4" /> SKILL.md</Button>
        <Button size="sm" variant="secondary" onclick={() => downloadExport('cognis_yaml')}><Download class="mr-1 h-4 w-4" /> YAML</Button>
        <Button size="sm" variant="secondary" onclick={() => downloadExport('cognis_package')}><Download class="mr-1 h-4 w-4" /> Package</Button>
        <Button size="sm" variant="secondary" onclick={previewDecomposition} disabled={decompositionLoading}>{decompositionLoading ? 'Generating…' : 'Suggest decomposition'}</Button>
        {#if decompositionPreview && decompositionPreview.length > 0}
          <Button size="sm" variant="secondary" onclick={saveDecomposition} disabled={decompositionSaving || !!skill.is_system}>{decompositionSaving ? 'Saving…' : 'Save decomposition'}</Button>
        {/if}
      </div>

      <section class="space-y-2 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex items-center gap-2 text-slate-200">
          <FileText class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Instructions</h3>
        </div>
        <pre class="overflow-x-auto whitespace-pre-wrap rounded-xl bg-slate-900 p-3 text-xs text-slate-200">{current?.instructions ?? skill.instructions}</pre>
      </section>

      <section class="grid gap-4 lg:grid-cols-2">
        <div class="space-y-2 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
          <div class="flex items-center gap-2 text-slate-200">
            <Wrench class="h-4 w-4 text-slate-400" />
            <h3 class="font-medium">Tools</h3>
          </div>
          {#if current?.tools && current.tools.length > 0}
            <pre class="overflow-x-auto whitespace-pre-wrap rounded-xl bg-slate-900 p-3 text-xs text-slate-200">{formatJson(current.tools)}</pre>
          {:else}
            <p class="text-xs text-slate-500">No skill-defined tools.</p>
          {/if}
        </div>

        <div class="space-y-2 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
          <div class="flex items-center gap-2 text-slate-200">
            <Layers class="h-4 w-4 text-slate-400" />
            <h3 class="font-medium">Templates And Secrets</h3>
          </div>
          <p class="text-xs text-slate-400">Secret placeholders</p>
          {#if current?.secret_placeholders && current.secret_placeholders.length > 0}
            <div class="flex flex-wrap gap-1">
              {#each current.secret_placeholders as placeholder}
                <Badge>{placeholder}</Badge>
              {/each}
            </div>
          {:else}
            <p class="text-xs text-slate-500">None</p>
          {/if}
          <p class="pt-2 text-xs text-slate-400">Prompt templates</p>
          {#if current?.prompt_templates && Object.keys(current.prompt_templates).length > 0}
            <pre class="overflow-x-auto whitespace-pre-wrap rounded-xl bg-slate-900 p-3 text-xs text-slate-200">{formatJson(current.prompt_templates)}</pre>
          {:else}
            <p class="text-xs text-slate-500">No prompt templates.</p>
          {/if}
        </div>
      </section>

      <section class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex items-center gap-2 text-slate-200">
          <GitBranch class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Workflow decomposition</h3>
          {#if current?.decomposition_stale}
            <Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">stale</Badge>
          {/if}
        </div>
        {#if savedSteps.length > 0}
          <div class="space-y-2">
            {#each savedSteps as step, index}
              <div class="rounded-xl border border-slate-800 bg-slate-900/80 px-3 py-2 text-xs text-slate-200">
                <p class="font-medium text-slate-100">{index + 1}. {String(step.name ?? `step_${index + 1}`)}</p>
                <p class="mt-1 text-slate-500">{String(step.type ?? 'run')}</p>
              </div>
            {/each}
          </div>
        {:else}
          <p class="text-xs text-slate-500">No saved decomposition yet.</p>
        {/if}
        {#if decompositionPreview && decompositionPreview.length > 0}
          <div class="rounded-xl border border-sky-500/20 bg-sky-500/5 p-3 text-xs text-slate-200">
            <p class="font-medium text-sky-200">Preview</p>
            {#if decompositionRationale}
              <p class="mt-1 text-slate-400">{decompositionRationale}</p>
            {/if}
            <div class="mt-3 space-y-2">
              {#each decompositionPreview as step, index}
                <div class="rounded-xl border border-slate-800 bg-slate-900/80 px-3 py-2">
                  <p class="font-medium text-slate-100">{index + 1}. {String(step.name ?? `step_${index + 1}`)}</p>
                  <p class="mt-1 text-slate-500">{String(step.type ?? 'run')}</p>
                </div>
              {/each}
            </div>
          </div>
        {/if}
      </section>

      <section class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
        <div class="flex items-center gap-2 text-slate-200">
          <Download class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Assets</h3>
        </div>
        {#if current?.asset_manifest && current.asset_manifest.length > 0}
          <div class="space-y-2">
            {#each current.asset_manifest as asset}
              <div class="flex flex-col gap-2 rounded-xl border border-slate-800 bg-slate-900/80 p-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <p class="font-mono text-xs text-slate-100">{asset.filename}</p>
                  <p class="mt-1 text-xs text-slate-500">{asset.content_type} · {asset.size_bytes} bytes · {asset.content_hash.slice(0, 8)}</p>
                </div>
                {#if asset.url}
                  <a class="inline-flex items-center gap-1 text-xs text-blue-300 hover:text-blue-200" href={asset.url} target="_blank" rel="noreferrer">
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
          <GitBranch class="h-4 w-4 text-slate-400" />
          <h3 class="font-medium">Version History</h3>
        </div>
        {#if loadingVersions}
          <p class="text-xs text-slate-500">Loading versions...</p>
        {:else if versions.length === 0}
          <p class="text-xs text-slate-500">No versions available.</p>
        {:else}
          <div class="space-y-2">
            {#each versions as version}
              <div class="rounded-xl border border-slate-800 bg-slate-900/80 p-3">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p class="text-sm font-medium text-slate-100">v{version.version_number}</p>
                    <p class="text-xs text-slate-500">{version.version_id} · {version.content_hash.slice(0, 8)}</p>
                  </div>
                  <div class="flex flex-wrap items-center gap-2">
                    {#if version.version_id === skill.current_version_id}
                      <Badge class="border-emerald-500/30 bg-emerald-500/10 text-emerald-300">current</Badge>
                    {/if}
                    {#if allowRestore && !skill.is_system && version.version_id !== skill.current_version_id}
                      <Button size="sm" variant="secondary" disabled={restoringVersionId === version.version_id} onclick={() => restoreVersion(version.version_id)}>Restore</Button>
                    {/if}
                  </div>
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
    </div>
  {/if}
</Sheet>
