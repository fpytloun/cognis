<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import KnowledgeCard from '$lib/components/knowledge/KnowledgeCard.svelte';
  import KnowledgeFormModal from '$lib/components/knowledge/KnowledgeFormModal.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { knowledgebaseReadiness } from '$lib/knowledge/capabilities';
  import { confirmAction } from '$lib/stores/confirm';
  import { auth } from '$lib/stores/auth';
  import { addToast } from '$lib/stores/toasts';
  import type { KnowledgebaseCapabilities, KnowledgebaseDiagnostics, KnowledgebaseModel } from '$lib/types/api';
  import Library from 'lucide-svelte/icons/library';
  import Plus from 'lucide-svelte/icons/plus';
  import Search from 'lucide-svelte/icons/search';

  let loading = $state(true);
  let error = $state('');
  let capabilities = $state<KnowledgebaseCapabilities | null>(null);
  let knowledgebases = $state<KnowledgebaseModel[]>([]);
  let diagnosticsById = $state<Record<string, KnowledgebaseDiagnostics>>({});
  let search = $state('');
  let statusFilter = $state<'active' | 'archived' | 'all'>('active');
  let showFormModal = $state(false);
  let formBusy = $state(false);
  let formError = $state('');

  const filtered = $derived(
    knowledgebases
      .filter((kb) => (statusFilter === 'all' ? true : kb.status === statusFilter))
      .filter((kb) => {
        const query = search.trim().toLowerCase();
        if (!query) return true;
        return kb.name.toLowerCase().includes(query) || (kb.description ?? '').toLowerCase().includes(query);
      })
  );
  const readiness = $derived(knowledgebaseReadiness(capabilities, $auth.user?.role === 'viewer'));
  const canMutate = $derived(readiness.canMutateCrud);
  const operational = $derived(readiness.canRead);
  const owned = $derived(filtered.filter((kb) => kb.access_level === 'owner'));
  const shared = $derived(filtered.filter((kb) => kb.access_level === 'shared'));

  async function loadDiagnostics(list: KnowledgebaseModel[]): Promise<void> {
    const results = await Promise.all(
      list.filter((kb) => kb.access_level === 'owner').map(async (kb) => {
        try {
          return [kb.knowledgebase_id, await api.knowledgebases.diagnostics(kb.knowledgebase_id)] as const;
        } catch {
          return null;
        }
      })
    );
    const next: Record<string, KnowledgebaseDiagnostics> = {};
    for (const entry of results) {
      if (entry) next[entry[0]] = entry[1];
    }
    diagnosticsById = next;
  }

  async function refresh(): Promise<void> {
    loading = true;
    error = '';
    try {
      const capabilitiesResult = await api.knowledgebases.capabilities();
      capabilities = capabilitiesResult;
      if (!capabilitiesResult.enabled) {
        knowledgebases = [];
        diagnosticsById = {};
        return;
      }
      knowledgebases = await api.knowledgebases.list();
      void loadDiagnostics(knowledgebases);
    } catch (err) {
      error = asApiError(err).message;
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  function openKb(kb: KnowledgebaseModel): void {
    void goto(`/knowledge/${encodeURIComponent(kb.knowledgebase_id)}`);
  }

  async function submitCreate(values: { name: string; description: string }): Promise<void> {
    formBusy = true;
    formError = '';
    try {
      const created = await api.knowledgebases.create({
        name: values.name,
        description: values.description || null
      });
      showFormModal = false;
      addToast(`"${created.name}" created`, 'success');
      await refresh();
      openKb(created);
    } catch (err) {
      formError = asApiError(err).message;
    } finally {
      formBusy = false;
    }
  }

  async function archiveKb(kb: KnowledgebaseModel): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Archive knowledgebase',
      message: `"${kb.name}" will be hidden from active use but not deleted. You can reactivate it later.`,
      confirmLabel: 'Archive',
      variant: 'primary'
    });
    if (!confirmed) return;
    try {
      await api.knowledgebases.update(kb.knowledgebase_id, { status: 'archived' });
      addToast(`"${kb.name}" archived`, 'success');
      await refresh();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    }
  }

  async function reactivateKb(kb: KnowledgebaseModel): Promise<void> {
    try {
      await api.knowledgebases.update(kb.knowledgebase_id, { status: 'active' });
      addToast(`"${kb.name}" reactivated`, 'success');
      await refresh();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    }
  }

  async function deleteKb(kb: KnowledgebaseModel): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Delete knowledgebase',
      message: `This permanently deletes "${kb.name}" and its indexed documents. This cannot be undone.`,
      confirmLabel: 'Delete',
      variant: 'danger'
    });
    if (!confirmed) return;
    try {
      await api.knowledgebases.remove(kb.knowledgebase_id);
      addToast(`"${kb.name}" deleted`, 'success');
      await refresh();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    }
  }
</script>

<svelte:head>
  <title>Knowledge · Cognis</title>
</svelte:head>

<div class="mx-auto flex max-w-5xl flex-col gap-6 px-4 py-6 md:px-6">
  <div class="flex flex-wrap items-start justify-between gap-4">
    <div>
      <h1 class="text-2xl font-semibold text-white">Knowledge</h1>
      <p class="mt-1 text-sm text-slate-400">
        Give agents grounded, cited access to your documents via hybrid search and retrieval.
      </p>
    </div>
    {#if canMutate}
      <Button onclick={() => { formError = ''; showFormModal = true; }} data-testid="knowledge-create-button">
        <Plus class="mr-1.5 h-4 w-4" /> New knowledgebase
      </Button>
    {/if}
  </div>

  {#if capabilities && (!operational || readiness.degraded)}
    <div class="rounded-2xl border border-amber-800/60 bg-amber-950/40 px-4 py-3 text-sm text-amber-200" role="status">
      {#if !capabilities.enabled}
        Knowledge is not enabled on this Cognis instance. Ask an administrator to configure a vector backend and embedding route.
      {:else}
        Knowledgebases remain available, but some retrieval or ingestion features need additional setup.
        {#if capabilities.notes.length > 0}
          <ul class="mt-2 list-inside list-disc space-y-0.5">
            {#each capabilities.notes as note}
              <li>{note}</li>
            {/each}
          </ul>
        {/if}
      {/if}
    </div>
  {/if}

  {#if loading}
    <LoadingState label="Loading knowledgebases…" />
  {:else if error}
    <div class="rounded-2xl border border-rose-800/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-300" role="alert">{error}</div>
  {:else if operational}
    <div class="flex flex-wrap items-center gap-3">
      <div class="relative flex-1 min-w-[220px]">
        <Search class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
        <Input bind:value={search} placeholder="Search knowledgebases…" class="pl-9" data-testid="knowledge-search-input" />
      </div>
      <div class="flex gap-1 rounded-xl border border-slate-800/80 bg-slate-900/60 p-1 text-sm">
        {#each [['active', 'Active'], ['archived', 'Archived'], ['all', 'All']] as [value, label] (value)}
          <button
            type="button"
            class={`rounded-lg px-3 py-1.5 font-medium transition ${statusFilter === value ? 'bg-sky-500 text-slate-950' : 'text-slate-300 hover:bg-slate-800'}`}
            onclick={() => (statusFilter = value as typeof statusFilter)}
          >
            {label}
          </button>
        {/each}
      </div>
    </div>

    {#if knowledgebases.length === 0}
      <div class="flex flex-col items-center gap-4 rounded-3xl border border-dashed border-slate-800/80 px-6 py-16 text-center">
        <span class="flex h-14 w-14 items-center justify-center rounded-2xl bg-sky-500/10 text-sky-300">
          <Library class="h-7 w-7" />
        </span>
        <div>
          <h2 class="text-lg font-semibold text-white">No knowledgebases yet</h2>
          <p class="mx-auto mt-1 max-w-sm text-sm text-slate-400">
            Create one, then add documents — files, folders, or existing artifacts — to give agents grounded, cited answers.
          </p>
        </div>
        {#if canMutate}
          <Button onclick={() => { formError = ''; showFormModal = true; }}>
            <Plus class="mr-1.5 h-4 w-4" /> Create your first knowledgebase
          </Button>
        {/if}
      </div>
    {:else if filtered.length === 0}
      <div class="rounded-3xl border border-dashed border-slate-800/80 px-6 py-12 text-center text-sm text-slate-400">
        No knowledgebases match your filters.
      </div>
    {:else}
      {#if owned.length > 0}
        <section class="flex flex-col gap-3" aria-labelledby="owned-heading">
          <h2 id="owned-heading" class="text-sm font-semibold text-slate-300">Owned by you</h2>
          <div class="grid gap-4 sm:grid-cols-2">
        {#each owned as kb (kb.knowledgebase_id)}
          <KnowledgeCard
            {kb}
            diagnostics={diagnosticsById[kb.knowledgebase_id] ?? null}
            canMutate={canMutate && kb.access_level === 'owner'}
            onOpen={openKb}
            onArchive={archiveKb}
            onReactivate={reactivateKb}
            onDelete={deleteKb}
          />
        {/each}
          </div>
        </section>
      {/if}
      {#if shared.length > 0}
        <section class="mt-6 flex flex-col gap-3" aria-labelledby="shared-heading">
          <h2 id="shared-heading" class="text-sm font-semibold text-slate-300">Shared with you</h2>
          <div class="grid gap-4 sm:grid-cols-2">
            {#each shared as kb (kb.knowledgebase_id)}
              <KnowledgeCard {kb} diagnostics={diagnosticsById[kb.knowledgebase_id] ?? null}
                canMutate={false} onOpen={openKb} onArchive={archiveKb}
                onReactivate={reactivateKb} onDelete={deleteKb} />
            {/each}
          </div>
        </section>
      {/if}
    {/if}
  {/if}
</div>

<KnowledgeFormModal
  open={showFormModal}
  busy={formBusy}
  error={formError}
  onClose={() => (showFormModal = false)}
  onSubmit={submitCreate}
/>
