<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onDestroy, onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import KnowledgeAccessTab from '$lib/components/knowledge/tabs/KnowledgeAccessTab.svelte';
  import KnowledgeBrowseTab from '$lib/components/knowledge/tabs/KnowledgeBrowseTab.svelte';
  import KnowledgeDocumentsTab from '$lib/components/knowledge/tabs/KnowledgeDocumentsTab.svelte';
  import KnowledgeSearchTab from '$lib/components/knowledge/tabs/KnowledgeSearchTab.svelte';
  import KnowledgeSettingsTab from '$lib/components/knowledge/tabs/KnowledgeSettingsTab.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { knowledgebaseLifecycleAccess, knowledgebaseReadiness } from '$lib/knowledge/capabilities';
  import { collectAllKnowledgebaseDocuments, documentsFromArtifacts } from '$lib/knowledge/documents';
  import { statusToneClass } from '$lib/knowledge/format';
  import { resolveKnowledgeTab, type KnowledgeTab } from '$lib/knowledge/tabs';
  import { addToast } from '$lib/stores/toasts';
  import { auth } from '$lib/stores/auth';
  import type {
    KnowledgebaseCapabilities,
    KnowledgebaseDiagnostics,
    KnowledgebaseDocumentModel,
    KnowledgebaseIndexJobModel,
    KnowledgebaseModel
  } from '$lib/types/api';
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';

  type Tab = KnowledgeTab;
  const TABS: { id: Tab; label: string }[] = [
    { id: 'browse', label: 'Browse' },
    { id: 'search', label: 'Search' },
    { id: 'documents', label: 'Documents' },
    { id: 'access', label: 'Access' },
    { id: 'settings', label: 'Settings' }
  ];

  const kbId = $derived($page.params.kbId ?? '');

  let loading = $state(true);
  let error = $state('');
  let kb = $state<KnowledgebaseModel | null>(null);
  let capabilities = $state<KnowledgebaseCapabilities | null>(null);
  let jobs = $state<KnowledgebaseIndexJobModel[]>([]);
  let diagnostics = $state<KnowledgebaseDiagnostics | null>(null);
  let backendDocuments = $state<KnowledgebaseDocumentModel[]>([]);
  let activeTab = $state<Tab>('browse');
  let selectedDocumentId = $state<string | null>(null);
  let selectedDocumentFragment = $state<string | null>(null);
  let selectionRequestId = $state(0);
  let missingDocument = $state(false);
  let loadGeneration = 0;
  let loadController: AbortController | null = null;

  const documents = $derived(backendDocuments);
  const readiness = $derived(knowledgebaseReadiness(capabilities, $auth.user?.role === 'viewer'));
  const lifecycleAccess = $derived(
    knowledgebaseLifecycleAccess(kb?.status ?? 'deleted', readiness, kb?.access_level ?? 'shared')
  );
  const visibleTabs = $derived(
    TABS.filter((tab) => {
      if (tab.id === 'access') return lifecycleAccess.canManageLifecycle;
      if (tab.id === 'settings') return lifecycleAccess.canManageLifecycle;
      return true;
    })
  );

  async function load(): Promise<void> {
    loadController?.abort();
    const generation = ++loadGeneration;
    const controller = new AbortController();
    loadController = controller;
    loading = true;
    error = '';
    try {
      const capabilitiesResult = await api.knowledgebases.capabilities();
      if (generation !== loadGeneration) return;
      capabilities = capabilitiesResult;
      if (!capabilitiesResult.enabled) {
        return;
      }
      const kbResult = await api.knowledgebases.get(kbId);
      if (generation !== loadGeneration) return;
      kb = kbResult;
      const requestedTab = $page.url.searchParams.get('tab');
      const authorizedTab = resolveKnowledgeTab(
        requestedTab,
        kbResult.access_level,
        $auth.user?.role === 'viewer'
      );
      activeTab = authorizedTab;
      if (requestedTab !== authorizedTab) {
        const url = new URL(window.location.href);
        url.searchParams.set('tab', authorizedTab);
        void goto(`${url.pathname}${url.search}`, { replaceState: true, noScroll: true });
      }
      const [documentArtifacts, jobsResult, diagnosticsResult] = await Promise.all([
        collectAllKnowledgebaseDocuments(kbId, controller.signal),
        kbResult.access_level === 'owner' ? api.knowledgebases.jobs(kbId).catch(() => []) : Promise.resolve([]),
        kbResult.access_level === 'owner' ? api.knowledgebases.diagnostics(kbId).catch(() => null) : Promise.resolve(null)
      ]);
      if (generation !== loadGeneration) return;
      backendDocuments = documentsFromArtifacts(documentArtifacts);
      jobs = jobsResult;
      diagnostics = diagnosticsResult;
    } catch (err) {
      if (generation === loadGeneration && (err as { name?: string }).name !== 'AbortError') {
        error = asApiError(err).message;
      }
    } finally {
      if (generation === loadGeneration) {
        loading = false;
        loadController = null;
      }
    }
  }

  onMount(() => {
    void load();
  });

  onDestroy(() => {
    loadGeneration += 1;
    loadController?.abort();
  });

  function setActiveTab(tab: Tab): void {
    activeTab = tab;
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    void goto(`${url.pathname}${url.search}`, { replaceState: true, noScroll: true });
  }

  function handleUpdated(updated: KnowledgebaseModel): void {
    kb = updated;
  }

  function handleDeleted(): void {
    void goto('/knowledge');
  }

  function handleOpenDocument(docId: string, fragment?: string): void {
    const match = documents.find((doc) => doc.doc_id === docId);
    if (!match) {
      addToast('Document is no longer available', 'warning');
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set('tab', 'browse');
    url.searchParams.set('document', docId);
    url.hash = fragment ? encodeURIComponent(fragment) : '';
    selectedDocumentId = docId;
    selectedDocumentFragment = fragment ?? null;
    missingDocument = false;
    selectionRequestId += 1;
    activeTab = 'browse';
    void goto(`${url.pathname}${url.search}${url.hash}`, { replaceState: true, noScroll: true });
  }

  $effect(() => {
    const requested = $page.url.searchParams.get('document');
    if (!requested || documents.length === 0) return;
    if (!documents.some((doc) => doc.doc_id === requested)) {
      missingDocument = true;
      addToast('Document is no longer available', 'warning');
      return;
    }
    selectedDocumentId = requested;
    try {
      selectedDocumentFragment = $page.url.hash
        ? decodeURIComponent($page.url.hash.slice(1))
        : null;
    } catch {
      selectedDocumentFragment = null;
    }
    missingDocument = false;
    selectionRequestId += 1;
  });
</script>

<svelte:head>
  <title>{kb ? `${kb.name} · Knowledge` : 'Knowledge'} · Cognis</title>
</svelte:head>

<div class="mx-auto flex max-w-6xl flex-col gap-5 px-4 py-6 md:px-6">
  <a href="/knowledge" class="flex items-center gap-1.5 text-sm text-slate-400 hover:text-slate-200">
    <ArrowLeft class="h-4 w-4" /> Knowledge
  </a>

  {#if loading}
    <LoadingState label="Loading knowledgebase…" />
  {:else if error}
    <div class="rounded-2xl border border-rose-800/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-300" role="alert">{error}</div>
  {:else if capabilities && !capabilities.enabled}
    <div class="rounded-2xl border border-amber-800/60 bg-amber-950/40 px-4 py-3 text-sm text-amber-200" role="status">
      Knowledge is not enabled on this Cognis instance.
      {#if capabilities.notes.length > 0}
        <ul class="mt-2 list-inside list-disc">
          {#each capabilities.notes as note}<li>{note}</li>{/each}
        </ul>
      {/if}
    </div>
  {:else if kb}
    {#if kb.access_level === 'shared'}
      <div class="rounded-2xl border border-sky-800/60 bg-sky-950/35 px-4 py-3 text-sm text-sky-200" role="status" data-testid="knowledge-shared-banner">
        Shared read/query access from <strong>{kb.owner_email ?? 'another Cognis user'}</strong>. You can browse, search, Ask, and read documents; management remains with the owner.
      </div>
    {/if}
    {#if readiness.degraded}
      <div class="rounded-2xl border border-amber-800/60 bg-amber-950/40 px-4 py-3 text-sm text-amber-200" role="status">
        This knowledgebase remains browsable, but some search, Ask, or ingestion actions are unavailable.
        {#if capabilities && capabilities.notes.length > 0}
          <ul class="mt-2 list-inside list-disc">{#each capabilities.notes as note}<li>{note}</li>{/each}</ul>
        {/if}
      </div>
    {/if}
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div class="flex items-center gap-2">
          <h1 class="text-2xl font-semibold text-white">{kb.name}</h1>
          <span class={`rounded-full border px-2.5 py-0.5 text-xs font-medium capitalize ${statusToneClass(kb.status === 'archived' ? 'warning' : 'positive')}`}>
            {kb.status}
          </span>
        </div>
        {#if kb.description}
          <p class="mt-1 max-w-2xl text-sm text-slate-400">{kb.description}</p>
        {/if}
      </div>
    </div>

    <div class="flex gap-1 overflow-x-auto rounded-xl border border-slate-800/80 bg-slate-900/60 p-1 text-sm" role="tablist" aria-label="Knowledgebase sections">
      {#each visibleTabs as tab (tab.id)}
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === tab.id}
          class={`shrink-0 rounded-lg px-3.5 py-1.5 font-medium transition ${activeTab === tab.id ? 'bg-sky-500 text-slate-950' : 'text-slate-300 hover:bg-slate-800'}`}
          onclick={() => setActiveTab(tab.id)}
          data-testid={`knowledge-tab-${tab.id}`}
        >
          {tab.label}
        </button>
      {/each}
    </div>

    <div>
      {#if activeTab === 'browse'}
        <KnowledgeBrowseTab {kb} {documents} {selectedDocumentId} {selectedDocumentFragment} {selectionRequestId} {missingDocument} onOpenDocument={handleOpenDocument} />
      {:else if activeTab === 'search'}
        <KnowledgeSearchTab
          {kb}
          searchReady={readiness.canSearch}
          askReady={readiness.canAsk}
          onOpenDocument={handleOpenDocument}
        />
      {:else if activeTab === 'documents'}
        {#if capabilities}
          <KnowledgeDocumentsTab
            {kb}
            {documents}
            {jobs}
            {capabilities}
            canMutate={lifecycleAccess.canIngest}
            onRefresh={load}
          />
        {/if}
      {:else if activeTab === 'access'}
        <KnowledgeAccessTab {kb} />
      {:else if activeTab === 'settings'}
        <KnowledgeSettingsTab
          {kb}
          {diagnostics}
          canEdit={lifecycleAccess.canMutateContent}
          canManageLifecycle={lifecycleAccess.canManageLifecycle}
          onUpdated={handleUpdated}
          onDeleted={handleDeleted}
        />
      {/if}
    </div>
  {/if}
</div>
