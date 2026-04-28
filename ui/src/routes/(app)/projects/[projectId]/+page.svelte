<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { api } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { Project } from '$lib/types/api';

  const projectId = $derived($page.params.projectId);
  let project = $state<Project | null>(null);
  let error = $state<string | null>(null);
  let sourceName = $state('');
  let sourcePath = $state('');
  let sourceRemote = $state('');
  let savingSource = $state(false);

  async function load(): Promise<void> {
    if (!projectId) return;
    error = null;
    try {
      project = await api.projects.detail(projectId);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to load project';
    }
  }

  async function addSource(): Promise<void> {
    if (!project || !sourceName.trim()) return;
    savingSource = true;
    try {
      await api.projects.addSource(project.project_id, {
        name: sourceName.trim(),
        local_path: sourcePath.trim() || null,
        remote_url: sourceRemote.trim() || null
      });
      sourceName = '';
      sourcePath = '';
      sourceRemote = '';
      await load();
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to add source';
    } finally {
      savingSource = false;
    }
  }

  onMount(load);
</script>

<svelte:head><title>{project?.name ?? 'Project'} · Cognis</title></svelte:head>

<section class="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6">
  {#if error}<p class="rounded-xl border border-red-500/30 bg-red-950/30 p-3 text-sm text-red-200">{error}</p>{/if}

  {#if !project}
    <p class="text-sm text-slate-400">Loading project…</p>
  {:else}
    <div class="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
      <div>
        <a class="text-sm text-sky-300 hover:text-sky-200" href="/projects">← Projects</a>
        <h1 class="mt-2 text-3xl font-semibold text-white">{project.name}</h1>
        <p class="mt-2 max-w-3xl text-sm text-slate-400">{project.description || 'No description'}</p>
      </div>
      {#if project.is_readonly_for_caller}<span class="rounded-full border border-amber-500/30 px-3 py-1 text-sm text-amber-200">Shared read-only</span>{/if}
    </div>

    <div class="grid gap-4 lg:grid-cols-[2fr_1fr]">
      <Card class="p-4">
        <h2 class="text-lg font-semibold text-white">Sources</h2>
        <div class="mt-4 grid gap-3">
          {#each project.sources as source}
            <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
              <p class="font-medium text-white">{source.name}</p>
              <p class="mt-1 break-all text-sm text-slate-400">{source.local_path || source.remote_url || 'No path or remote configured'}</p>
              {#if source.credential_ref}<p class="mt-1 text-xs text-slate-500">Credential clue: {source.credential_ref}</p>{/if}
            </div>
          {/each}
          {#if project.sources.length === 0}<p class="text-sm text-slate-400">No source hints configured.</p>{/if}
        </div>

        {#if !project.is_readonly_for_caller}
          <form class="mt-5 grid gap-3 md:grid-cols-3" onsubmit={(event) => { event.preventDefault(); void addSource(); }}>
            <Input bind:value={sourceName} placeholder="Source name" />
            <Input bind:value={sourcePath} placeholder="Local path hint" />
            <Input bind:value={sourceRemote} placeholder="Remote URL" />
            <div class="md:col-span-3"><Button type="submit" disabled={savingSource || !sourceName.trim()}>{savingSource ? 'Adding…' : 'Add Source'}</Button></div>
          </form>
        {/if}
      </Card>

      <Card class="p-4">
        <h2 class="text-lg font-semibold text-white">Workflows</h2>
        {#if project.workflow_ids.length === 0}
          <p class="mt-3 text-sm text-slate-400">No project-bound workflows yet. Generic workflows remain available.</p>
        {:else}
          <ul class="mt-3 space-y-2 text-sm text-slate-300">
            {#each project.workflow_ids as workflowId}<li class="rounded-lg bg-slate-950/60 px-3 py-2 font-mono">{workflowId}</li>{/each}
          </ul>
        {/if}
      </Card>
    </div>

    <Card class="p-4">
      <h2 class="text-lg font-semibold text-white">Instructions</h2>
      <p class="mt-3 whitespace-pre-wrap text-sm text-slate-300">{project.instructions || 'No Cognis-side project instructions configured.'}</p>
    </Card>
  {/if}
</section>
