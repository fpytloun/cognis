<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import ListTodo from 'lucide-svelte/icons/list-todo';
  import { api } from '$lib/api/client';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { taskBoardProjectUrl } from '$lib/tasks';
  import type { Project } from '$lib/types/api';

  let projects = $state<Project[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let name = $state('');
  let description = $state('');
  let creating = $state(false);
  let profileProject = $state<Project | null>(null);
  let lightboxProject = $state<Project | null>(null);

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
        <div class="group rounded-2xl border border-slate-800 bg-slate-900/70 p-4 transition hover:border-sky-500/60 hover:bg-slate-900">
          <div class="flex items-start gap-3">
            <button type="button" class="shrink-0 cursor-pointer" onclick={() => { profileProject = project; }} aria-label={`Show ${project.name} profile`}>
              <AgentAvatar name={project.name} avatarUrl={project.avatar_url} class="h-12 w-12 rounded-xl" />
            </button>
            <div class="min-w-0 flex-1">
              <div class="flex min-w-0 items-start justify-between gap-2">
                <a class="min-w-0 break-words text-lg font-semibold text-white hover:text-sky-200 lg:truncate lg:group-hover:whitespace-normal lg:group-focus-within:whitespace-normal" href={`/projects/${project.project_id}`} title={project.name}>{project.name}</a>
                <a
                  class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-700 bg-slate-950/70 text-slate-400 transition hover:border-sky-500/60 hover:text-sky-200"
                  href={taskBoardProjectUrl(project.project_id)}
                  aria-label={`Open task board filtered for ${project.name}`}
                  title="Open filtered task board"
                >
                  <ListTodo class="h-4 w-4" aria-hidden="true" />
                </a>
              </div>
              <p class="mt-1 line-clamp-2 text-sm text-slate-400">{project.description || 'No description'}</p>
            </div>
          </div>
          <div class="mt-4 flex gap-2 text-xs text-slate-500">
            <span>{project.sources.length} sources</span>
            <span>{project.workflow_ids.length} workflows</span>
            {#if project.is_shared_with_me}<span class="text-amber-300">shared</span>{/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</section>

{#if profileProject}
  <button class="fixed inset-0 z-40 cursor-default bg-slate-950/30" type="button" onclick={() => { profileProject = null; }} aria-label="Close project profile"></button>
  <div class="fixed left-1/2 top-1/2 z-50 w-[min(92vw,24rem)] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-2xl" role="dialog" aria-modal="true" aria-label={`${profileProject.name} profile`}>
    <div class="flex items-start gap-4">
      <button type="button" class="shrink-0 cursor-pointer disabled:cursor-default" onclick={() => { if (profileProject?.avatar_url) lightboxProject = profileProject; }} aria-label="View avatar full size" disabled={!profileProject.avatar_url}>
        <AgentAvatar name={profileProject.name} avatarUrl={profileProject.avatar_url} class="h-20 w-20" />
      </button>
      <div class="min-w-0">
        <p class="break-words text-lg font-semibold text-slate-100" title={profileProject.name}>{profileProject.name}</p>
        <p class="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">Project</p>
      </div>
    </div>
    <p class="mt-4 whitespace-pre-wrap text-sm leading-6 text-slate-300">{profileProject.description || 'No project description yet.'}</p>
    <div class="mt-5 flex justify-end gap-2">
      <Button variant="secondary" onclick={() => { profileProject = null; }}>Close</Button>
      <Button onclick={() => goto(`/projects/${profileProject?.project_id}`)}>Open project</Button>
    </div>
  </div>
{/if}

{#if lightboxProject?.avatar_url}
  <ImageLightbox src={lightboxProject.avatar_url} alt={`${lightboxProject.name} avatar`} onClose={() => { lightboxProject = null; }} />
{/if}
