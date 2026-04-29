<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { api } from '$lib/api/client';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import AvatarGenerateModal from '$lib/components/agents/AvatarGenerateModal.svelte';
  import { addToast } from '$lib/stores/toasts';
  import { confirmAction } from '$lib/stores/confirm';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { Project, ProjectGrant, ProjectSource, Workflow } from '$lib/types/api';

  const projectId = $derived($page.params.projectId);
  let project = $state<Project | null>(null);
  let workflows = $state<Workflow[]>([]);
  let error = $state<string | null>(null);
  let savingProject = $state(false);
  let savingSource = $state(false);
  let savingGrant = $state(false);
  let savingWorkflow = $state(false);
  let uploadingAvatar = $state(false);
  let showAvatarModal = $state(false);
  let showAvatarLightbox = $state(false);
  let editingSourceId = $state<string | null>(null);

  let projectForm = $state({ name: '', description: '', instructions: '', default_workflow_id: '' });
  let sourceForm = $state({ name: '', local_path: '', remote_url: '', default_branch: '', credential_ref: '', instructions: '' });
  let selectedWorkflowId = $state('');
  let grantForm = $state({ grantee_user_email: '', note: '' });

  const workflowNameById = $derived(new Map(workflows.map((workflow) => [workflow.workflow_id, workflow.name])));
  const attachableWorkflows = $derived(workflows.filter((workflow) => !workflow.is_system && !project?.workflow_ids.includes(workflow.workflow_id)));
  const readonly = $derived(Boolean(project?.is_readonly_for_caller));

  function resetProjectForm(next: Project): void {
    projectForm = {
      name: next.name,
      description: next.description ?? '',
      instructions: next.instructions ?? '',
      default_workflow_id: next.default_workflow_id ?? ''
    };
  }

  function resetSourceForm(source?: ProjectSource): void {
    editingSourceId = source?.source_id ?? null;
    sourceForm = {
      name: source?.name ?? '',
      local_path: source?.local_path ?? '',
      remote_url: source?.remote_url ?? '',
      default_branch: source?.default_branch ?? '',
      credential_ref: source?.credential_ref ?? '',
      instructions: source?.instructions ?? ''
    };
  }

  async function load(): Promise<void> {
    if (!projectId) return;
    error = null;
    try {
      const [nextProject, nextWorkflows] = await Promise.all([
        api.projects.detail(projectId),
        api.workflows.listAll({ project_id: projectId })
      ]);
      if (!nextProject.is_readonly_for_caller) {
        try {
          nextProject.grants = await api.projects.grants(projectId);
        } catch {
          // Shared projects may be usable without grant-management access.
        }
      }
      project = nextProject;
      workflows = nextWorkflows;
      resetProjectForm(nextProject);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to load project';
    }
  }

  async function saveProject(): Promise<void> {
    if (!project || readonly || !projectForm.name.trim()) return;
    savingProject = true;
    try {
      const previousGrants = project.grants;
      project = await api.projects.update(project.project_id, {
        name: projectForm.name.trim(),
        description: projectForm.description.trim() || null,
        instructions: projectForm.instructions.trim() || null,
        default_workflow_id: projectForm.default_workflow_id || null
      });
      project.grants = previousGrants;
      resetProjectForm(project);
      addToast('Project updated', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to update project', 'error');
    } finally {
      savingProject = false;
    }
  }

  async function updateAvatar(imageId: string | null, avatarUrl: string | null): Promise<void> {
    if (!project || readonly) return;
    try {
      const previousGrants = project.grants;
      project = await api.projects.update(project.project_id, {
        avatar_image_id: imageId,
        avatar_url: avatarUrl
      });
      project.grants = previousGrants;
      resetProjectForm(project);
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to update avatar', 'error');
      throw err;
    }
  }

  async function handleAvatarUpload(event: Event): Promise<void> {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file || !project || readonly) return;
    uploadingAvatar = true;
    try {
      const result = await api.images.upload(file);
      await updateAvatar(result.image_id, result.url);
      addToast('Project avatar uploaded', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to upload avatar', 'error');
    } finally {
      uploadingAvatar = false;
      input.value = '';
    }
  }

  async function handleGeneratedAvatar(imageId: string, avatarUrl: string): Promise<void> {
    await updateAvatar(imageId, avatarUrl);
    showAvatarModal = false;
    addToast('Project avatar updated', 'success');
  }

  async function removeAvatar(): Promise<void> {
    await updateAvatar(null, null);
    addToast('Project avatar removed', 'success');
  }

  async function saveSource(): Promise<void> {
    if (!project || readonly || !sourceForm.name.trim()) return;
    savingSource = true;
    const payload = {
      name: sourceForm.name.trim(),
      local_path: sourceForm.local_path.trim() || null,
      remote_url: sourceForm.remote_url.trim() || null,
      default_branch: sourceForm.default_branch.trim() || null,
      credential_ref: sourceForm.credential_ref.trim() || null,
      instructions: sourceForm.instructions.trim() || null
    };
    try {
      if (editingSourceId) {
        await api.projects.updateSource(project.project_id, editingSourceId, payload);
        addToast('Source updated', 'success');
      } else {
        await api.projects.addSource(project.project_id, payload);
        addToast('Source added', 'success');
      }
      resetSourceForm();
      await load();
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to save source', 'error');
    } finally {
      savingSource = false;
    }
  }

  async function deleteSource(source: ProjectSource): Promise<void> {
    if (!project || readonly) return;
    if (!(await confirmAction({ title: `Delete source "${source.name}"?`, message: 'This removes the project source hint only.', confirmLabel: 'Delete' }))) return;
    try {
      await api.projects.deleteSource(project.project_id, source.source_id);
      await load();
      addToast('Source deleted', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to delete source', 'error');
    }
  }

  async function attachWorkflow(): Promise<void> {
    if (!project || readonly || !selectedWorkflowId) return;
    savingWorkflow = true;
    try {
      project = await api.projects.attachWorkflow(project.project_id, selectedWorkflowId);
      selectedWorkflowId = '';
      await load();
      addToast('Workflow bound to project', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to bind workflow', 'error');
    } finally {
      savingWorkflow = false;
    }
  }

  async function detachWorkflow(workflowId: string): Promise<void> {
    if (!project || readonly) return;
    try {
      project = await api.projects.detachWorkflow(project.project_id, workflowId);
      await load();
      addToast('Workflow unbound', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to unbind workflow', 'error');
    }
  }

  async function createGrant(): Promise<void> {
    if (!project || readonly || !grantForm.grantee_user_email.trim()) return;
    savingGrant = true;
    try {
      await api.projects.createGrant(project.project_id, {
        grantee_type: 'user',
        grantee_user_email: grantForm.grantee_user_email.trim(),
        permission: 'use',
        note: grantForm.note.trim() || null
      });
      grantForm = { grantee_user_email: '', note: '' };
      await load();
      addToast('Project shared', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to share project', 'error');
    } finally {
      savingGrant = false;
    }
  }

  async function revokeGrant(grant: ProjectGrant): Promise<void> {
    if (!project || readonly) return;
    try {
      await api.projects.revokeGrant(project.project_id, grant.grant_id);
      await load();
      addToast('Project grant revoked', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to revoke grant', 'error');
    }
  }

  onMount(load);
</script>

<svelte:head><title>{project?.name ?? 'Project'} · Cognis</title></svelte:head>

<section class="mx-auto flex w-full max-w-7xl flex-col gap-6 p-4 sm:p-6">
  {#if error}<p class="rounded-xl border border-red-500/30 bg-red-950/30 p-3 text-sm text-red-200">{error}</p>{/if}

  {#if !project}
    <p class="text-sm text-slate-400">Loading project…</p>
  {:else}
    <div class="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
      <div class="flex items-start gap-4">
        <button type="button" class="shrink-0 cursor-pointer" onclick={() => { if (project?.avatar_url) showAvatarLightbox = true; }} aria-label="View project avatar">
          <AgentAvatar name={project.name} avatarUrl={project.avatar_url} class="h-16 w-16" />
        </button>
        <div>
          <a class="text-sm text-sky-300 hover:text-sky-200" href="/projects">← Projects</a>
          <h1 class="mt-2 text-3xl font-semibold text-white">{project.name}</h1>
          <p class="mt-2 max-w-3xl text-sm text-slate-400">{project.description || 'No description'}</p>
          <div class="mt-3 flex flex-wrap gap-2 text-xs text-slate-400">
            <span class="rounded-full border border-slate-700 px-2 py-1">{project.status}</span>
            <span class="rounded-full border border-slate-700 px-2 py-1">{project.sources.length} sources</span>
            <span class="rounded-full border border-slate-700 px-2 py-1">{project.workflow_ids.length} workflows</span>
            {#if project.is_shared_with_me}<span class="rounded-full border border-amber-500/30 px-2 py-1 text-amber-200">shared by {project.shared_by_email}</span>{/if}
          </div>
        </div>
      </div>
      {#if !readonly}
        <div class="flex flex-wrap gap-2">
          <label class="inline-flex min-h-[40px] cursor-pointer items-center justify-center rounded-xl border border-slate-700 bg-slate-900 px-4 py-2 text-sm font-medium text-slate-100 transition hover:border-slate-500 hover:bg-slate-800 md:min-h-[36px] md:py-1.5">
            <input class="hidden" type="file" accept="image/*" onchange={handleAvatarUpload} disabled={uploadingAvatar} />
            {uploadingAvatar ? 'Uploading…' : 'Upload avatar'}
          </label>
          <Button variant="secondary" onclick={() => { showAvatarModal = true; }}>Generate avatar</Button>
          {#if project.avatar_image_id || project.avatar_url}<Button variant="secondary" onclick={removeAvatar}>Remove avatar</Button>{/if}
        </div>
      {/if}
    </div>

    <div class="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(360px,1fr)]">
      <div class="space-y-4">
        <Card class="p-4">
          <h2 class="text-lg font-semibold text-white">Project Settings</h2>
          <div class="mt-4 grid gap-3 md:grid-cols-2">
            <Input bind:value={projectForm.name} disabled={readonly} placeholder="Project name" />
            <select bind:value={projectForm.default_workflow_id} disabled={readonly} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-60">
              <option value="">No default workflow</option>
              {#each workflows as workflow}<option value={workflow.workflow_id}>{workflow.name}</option>{/each}
            </select>
            <Input bind:value={projectForm.description} disabled={readonly} placeholder="Description" />
            <textarea bind:value={projectForm.instructions} disabled={readonly} class="min-h-28 rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-60 md:col-span-2" placeholder="Project instructions for agents"></textarea>
          </div>
          {#if !readonly}<div class="mt-4"><Button onclick={saveProject} disabled={savingProject || !projectForm.name.trim()}>{savingProject ? 'Saving…' : 'Save project'}</Button></div>{/if}
        </Card>

        <Card class="p-4">
          <h2 class="text-lg font-semibold text-white">Sources</h2>
          <div class="mt-4 grid gap-3">
            {#each project.sources as source}
              <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
                <div class="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p class="font-medium text-white">{source.name}</p>
                    <p class="mt-1 break-all text-sm text-slate-400">{source.local_path || source.remote_url || 'No path or remote configured'}</p>
                    {#if source.default_branch}<p class="mt-1 text-xs text-slate-500">Branch: {source.default_branch}</p>{/if}
                    {#if source.credential_ref}<p class="mt-1 text-xs text-slate-500">Credential clue: {source.credential_ref}</p>{/if}
                    {#if source.instructions}<p class="mt-2 whitespace-pre-wrap text-sm text-slate-300">{source.instructions}</p>{/if}
                  </div>
                  {#if !readonly}<div class="flex gap-2"><Button size="sm" variant="secondary" onclick={() => resetSourceForm(source)}>Edit</Button><Button size="sm" variant="danger" onclick={() => deleteSource(source)}>Delete</Button></div>{/if}
                </div>
              </div>
            {/each}
            {#if project.sources.length === 0}<p class="text-sm text-slate-400">No source hints configured.</p>{/if}
          </div>
          {#if !readonly}
            <form class="mt-5 grid gap-3 md:grid-cols-2" onsubmit={(event) => { event.preventDefault(); void saveSource(); }}>
              <Input bind:value={sourceForm.name} placeholder="Source name" />
              <Input bind:value={sourceForm.local_path} placeholder="Local path hint" />
              <Input bind:value={sourceForm.remote_url} placeholder="Remote URL" />
              <Input bind:value={sourceForm.default_branch} placeholder="Default branch" />
              <Input bind:value={sourceForm.credential_ref} placeholder="Credential reference clue" />
              <textarea bind:value={sourceForm.instructions} class="min-h-20 rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 md:col-span-2" placeholder="Source-specific instructions"></textarea>
              <div class="flex gap-2 md:col-span-2"><Button type="submit" disabled={savingSource || !sourceForm.name.trim()}>{savingSource ? 'Saving…' : editingSourceId ? 'Update source' : 'Add source'}</Button>{#if editingSourceId}<Button type="button" variant="secondary" onclick={() => resetSourceForm()}>Cancel edit</Button>{/if}</div>
            </form>
          {/if}
        </Card>
      </div>

      <div class="space-y-4">
        <Card class="p-4">
          <h2 class="text-lg font-semibold text-white">Bound Workflows</h2>
          <div class="mt-3 space-y-2">
            {#each project.workflow_ids as workflowId}
              <div class="flex items-center justify-between gap-2 rounded-lg bg-slate-950/60 px-3 py-2 text-sm text-slate-300">
                <span>{workflowNameById.get(workflowId) ?? workflowId}</span>
                {#if !readonly}<Button size="sm" variant="secondary" onclick={() => detachWorkflow(workflowId)}>Unbind</Button>{/if}
              </div>
            {/each}
            {#if project.workflow_ids.length === 0}<p class="text-sm text-slate-400">No project-bound workflows yet. System workflows remain available.</p>{/if}
          </div>
          {#if !readonly}
            <div class="mt-4 flex gap-2">
              <select bind:value={selectedWorkflowId} class="min-w-0 flex-1 rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="">Select workflow</option>
                {#each attachableWorkflows as workflow}<option value={workflow.workflow_id}>{workflow.name}</option>{/each}
              </select>
              <Button onclick={attachWorkflow} disabled={savingWorkflow || !selectedWorkflowId}>{savingWorkflow ? 'Binding…' : 'Bind'}</Button>
            </div>
            <p class="mt-2 text-xs text-slate-500">System workflows are always available and do not need to be bound.</p>
          {/if}
        </Card>

        <Card class="p-4">
          <h2 class="text-lg font-semibold text-white">Sharing</h2>
          {#if readonly}
            <p class="mt-3 text-sm text-slate-400">You can use this shared project but cannot manage its grants.</p>
          {:else}
            <div class="mt-3 space-y-2">
              {#each project.grants.filter((grant) => !grant.revoked_at) as grant}
                <div class="flex items-center justify-between gap-2 rounded-lg bg-slate-950/60 px-3 py-2 text-sm text-slate-300">
                  <span>{grant.grantee_user_email ?? grant.grantee_group_id} · {grant.permission}</span>
                  <Button size="sm" variant="secondary" onclick={() => revokeGrant(grant)}>Revoke</Button>
                </div>
              {/each}
              {#if project.grants.filter((grant) => !grant.revoked_at).length === 0}<p class="text-sm text-slate-400">No active grants.</p>{/if}
            </div>
            <form class="mt-4 grid gap-2" onsubmit={(event) => { event.preventDefault(); void createGrant(); }}>
              <Input bind:value={grantForm.grantee_user_email} placeholder="user@example.com" />
              <Input bind:value={grantForm.note} placeholder="Note (optional)" />
              <Button type="submit" disabled={savingGrant || !grantForm.grantee_user_email.trim()}>{savingGrant ? 'Sharing…' : 'Share project'}</Button>
            </form>
          {/if}
        </Card>
      </div>
    </div>
  {/if}
</section>

{#if showAvatarModal && project}
  <AvatarGenerateModal
    name={project.name}
    description={project.description ?? ''}
    personality={{ instructions: project.instructions ?? '' }}
    onAccept={handleGeneratedAvatar}
    onClose={() => { showAvatarModal = false; }}
  />
{/if}

{#if showAvatarLightbox && project?.avatar_url}
  <ImageLightbox src={project.avatar_url} alt={`${project.name} avatar`} onClose={() => { showAvatarLightbox = false; }} />
{/if}
