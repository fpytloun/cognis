<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { api } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { Project } from '$lib/types/api';

  let projects = $state<Project[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let name = $state('');
  let description = $state('');
  let creating = $state(false);

  async function load(): Promise<void> {
    loading = true;
    error = null;
    try {
      projects = await api.projects.list();
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to load projects';
    } finally {
      loading = false;
    }
  }

  async function createProject(): Promise<void> {
    if (!name.trim()) return;
    creating = true;
    error = null;
    try {
      const project = await api.projects.create({ name: name.trim(), description: description.trim() || null });
      name = '';
      description = '';
      await goto(`/projects/${project.project_id}`);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to create project';
    } finally {
      creating = false;
    }
  }

  onMount(load);
</script>

<svelte:head><title>Projects · Cognis</title></svelte:head>

<section class="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6">
  <div>
    <p class="text-sm uppercase tracking-[0.2em] text-sky-300">Workspace</p>
    <h1 class="mt-2 text-3xl font-semibold text-white">Projects</h1>
    <p class="mt-2 max-w-2xl text-sm text-slate-400">Group conversations, tasks, schedules, source repositories, and preferred workflows under a durable project context.</p>
  </div>

  {#if error}<p class="rounded-xl border border-red-500/30 bg-red-950/30 p-3 text-sm text-red-200">{error}</p>{/if}

  <Card class="p-4">
    <form class="grid gap-3 md:grid-cols-[1fr_2fr_auto]" onsubmit={(event) => { event.preventDefault(); void createProject(); }}>
      <Input bind:value={name} placeholder="Project name" aria-label="Project name" />
      <Input bind:value={description} placeholder="Description" aria-label="Project description" />
      <Button type="submit" disabled={creating || !name.trim()}>{creating ? 'Creating…' : 'Create Project'}</Button>
    </form>
  </Card>

  {#if loading}
    <p class="text-sm text-slate-400">Loading projects…</p>
  {:else if projects.length === 0}
    <Card class="p-8 text-center text-slate-400">No projects yet. Create one to bind workflows and source hints.</Card>
  {:else}
    <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {#each projects as project}
        <a class="rounded-2xl border border-slate-800 bg-slate-900/70 p-4 transition hover:border-sky-500/60 hover:bg-slate-900" href={`/projects/${project.project_id}`}>
          <div class="flex items-start gap-3">
            <div class="flex h-12 w-12 items-center justify-center overflow-hidden rounded-xl bg-slate-800 text-lg font-semibold text-sky-200">
              {#if project.avatar_url}<img class="h-full w-full object-cover" src={project.avatar_url} alt="" />{:else}{project.name.slice(0, 1).toUpperCase()}{/if}
            </div>
            <div class="min-w-0">
              <h2 class="truncate text-lg font-semibold text-white">{project.name}</h2>
              <p class="mt-1 line-clamp-2 text-sm text-slate-400">{project.description || 'No description'}</p>
            </div>
          </div>
          <div class="mt-4 flex gap-2 text-xs text-slate-500">
            <span>{project.sources.length} sources</span>
            <span>{project.workflow_ids.length} workflows</span>
            {#if project.is_shared_with_me}<span class="text-amber-300">shared</span>{/if}
          </div>
        </a>
      {/each}
    </div>
  {/if}
</section>
