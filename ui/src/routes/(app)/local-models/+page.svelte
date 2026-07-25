<script lang="ts">
  import { onMount } from 'svelte';
  import Box from 'lucide-svelte/icons/box';
  import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
  import Database from 'lucide-svelte/icons/database';
  import DownloadCloud from 'lucide-svelte/icons/download-cloud';
  import Search from 'lucide-svelte/icons/search';
  import Server from 'lucide-svelte/icons/server';
  import { ApiError, api, asApiError } from '$lib/api/client';
  import CapacityPlanner from '$lib/components/local-models/CapacityPlanner.svelte';
  import CatalogModelCard from '$lib/components/local-models/CatalogModelCard.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { formatBytes } from '$lib/executors';
  import {
    fitMetadata,
    formatContext,
    deploymentPayload,
    managedHostDisabledReason,
    matchedExecutors,
    requiresCapacityOverride
  } from '$lib/local-models';
  import { auth } from '$lib/stores/auth';
  import type {
    ExecutorConfig,
    LLMProvider,
    LocalModelCatalogItem,
    LocalModelCatalogResponse,
    LocalModelCatalogSource,
    LocalModelDeployment,
    LocalModelFitPlan,
    LocalModelOperation,
    LocalModelProviderRecommendation,
    LocalModelSelector,
    LocalModelTargetStatus
  } from '$lib/types/api';

  type Tab = 'catalog' | 'deployments' | 'installed' | 'operations';
  type CatalogFilters = {
    parameterRange: string;
    downloadSizeRange: string;
    quantization: string;
    minContext: string;
    includeUnknown: boolean;
  };
  const tabs: { id: Tab; label: string; icon: typeof Box }[] = [
    { id: 'catalog', label: 'Catalog', icon: Search },
    { id: 'deployments', label: 'Deployments', icon: Box },
    { id: 'installed', label: 'Installed & targets', icon: Server },
    { id: 'operations', label: 'Operations', icon: DownloadCloud }
  ];

  let activeTab = $state<Tab>('catalog');
  let loading = $state(true);
  let searching = $state(false);
  let planning = $state(false);
  let creating = $state(false);
  let error = $state<string | null>(null);
  let notice = $state<string | null>(null);
  let runtimeViewsAvailable = $state(true);
  let hiddenExecutorCount = $state(0);

  let catalog = $state<LocalModelCatalogResponse | null>(null);
  let deployments = $state<LocalModelDeployment[]>([]);
  let executors = $state<ExecutorConfig[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let targets = $state<Record<string, LocalModelTargetStatus[]>>({});
  let operations = $state<Record<string, LocalModelOperation[]>>({});

  let searchQuery = $state('');
  let catalogSource = $state<LocalModelCatalogSource>('ollama');
  let parameterRange = $state('');
  let downloadSizeRange = $state('');
  let quantizationFilter = $state('');
  let minContext = $state('');
  let includeUnknown = $state(true);
  let appliedSearchQuery = $state('');
  let appliedCatalogFilters = $state<CatalogFilters>({
    parameterRange: '',
    downloadSizeRange: '',
    quantization: '',
    minContext: '',
    includeUnknown: true
  });
  let directRef = $state('');
  let selectedModel = $state<LocalModelCatalogItem | null>(null);
  let selectedRef = $state('');
  let contextTokens = $state(32768);
  let plan = $state<LocalModelFitPlan | null>(null);
  let targetMode = $state<'ids' | 'labels'>('ids');
  let selectedExecutorIds = $state<string[]>([]);
  let labelKey = $state('');
  let labelValue = $state('');
  let providerId = $state('');
  let providerLocked = $state(false);
  let providerRecommendation = $state<LocalModelProviderRecommendation | null>(null);
  let recommendingProvider = $state(false);
  let repairRecommendations = $state<Record<string, LocalModelProviderRecommendation>>({});
  let repairProviderIds = $state<Record<string, string>>({});
  const AUTO_PROVIDER = '__auto_create__';
  let overrideAcknowledged = $state(false);
  let planRequestSequence = 0;
  let providerRequestSequence = 0;
  let catalogRequestSequence = 0;
  const detailRequests = new Map<string, Promise<LocalModelCatalogItem>>();

  const selector = $derived<LocalModelSelector>(
    targetMode === 'ids'
      ? { executor_ids: selectedExecutorIds }
      : {
          match_labels:
            labelKey.trim() && labelValue.trim()
              ? { [labelKey.trim()]: labelValue.trim() }
              : {}
        }
  );
  const selectedProvider = $derived(
    providers.find((provider) => provider.provider_id === providerId) ?? null
  );
  const selectedCandidate = $derived(
    providerRecommendation?.candidates.find((candidate) => candidate.provider_id === providerId) ??
      null
  );
  const providerExecutors = $derived.by(() => {
    if (providerId === AUTO_PROVIDER) return executors.filter((executor) => !executor.shared);
    if (!selectedProvider) return [];
    const exactId =
      typeof selectedProvider.config?.executor_id === 'string'
        ? selectedProvider.config.executor_id
        : '';
    const labels =
      selectedProvider.config?.executor_labels &&
      typeof selectedProvider.config.executor_labels === 'object'
        ? (selectedProvider.config.executor_labels as Record<string, string>)
        : {};
    return executors.filter(
      (executor) =>
        executor.executor_id === exactId ||
        (Object.keys(labels).length > 0 &&
          Object.entries(labels).every(([key, value]) => executor.labels[key] === value))
    );
  });
  const matched = $derived(matchedExecutors(providerExecutors, selector));
  const overrideRequired = $derived(requiresCapacityOverride(plan));
  const autoSelectorUnsupported = $derived(
    providerId === AUTO_PROVIDER && targetMode === 'ids' && selectedExecutorIds.length > 1
  );
  const canCreateDeployment = $derived($auth.user?.role !== 'viewer');
  const ollamaProviders = $derived(
    providers.filter((provider) => String(provider.config?.preset ?? '').toLowerCase() === 'ollama')
  );
  const allOperations = $derived(
    deployments.flatMap((deployment) =>
      (operations[deployment.deployment_id] ?? []).map((operation) => ({
        deployment,
        operation
      }))
    )
  );

  function message(errorValue: unknown, fallback: string): string {
    const apiError = asApiError(errorValue);
    return apiError.message || fallback;
  }

  function invalidatePlan(): void {
    planRequestSequence += 1;
    plan = null;
    overrideAcknowledged = false;
    planning = false;
  }

  function canManageDeployment(deployment: LocalModelDeployment): boolean {
    if ($auth.user?.role === 'viewer') return false;
    if (deployment.shared) return $auth.user?.role === 'admin';
    return deployment.owner_email === $auth.user?.email;
  }

  function repairRecommendation(deploymentId: string): LocalModelProviderRecommendation | null {
    return repairRecommendations[deploymentId] ?? null;
  }

  function repairSupportsAutoCreate(deployment: LocalModelDeployment): boolean {
    return (deployment.selector.executor_ids?.length ?? 0) <= 1;
  }

  async function load(): Promise<void> {
    loading = true;
    error = null;
    try {
      const [catalogResult, deploymentResult, executorResult, providerResult] = await Promise.all([
        api.localModels.catalog({ source: 'ollama', limit: 20 }),
        api.localModels.deployments(),
        api.executor.list(),
        api.llmProviders.list()
      ]);
      catalog = catalogResult;
      deployments = deploymentResult;
      const visibleExecutors = executorResult.filter(
        (executor) =>
          executor.owner_email === $auth.user?.email ||
          ($auth.user?.role === 'admin' && executor.shared)
      );
      executors = visibleExecutors;
      hiddenExecutorCount = executorResult.length - visibleExecutors.length;
      providers = providerResult.items;
      const queryProviderId = new URL(window.location.href).searchParams.get('provider');
      if (queryProviderId && ollamaProviders.some((provider) => provider.provider_id === queryProviderId)) {
        providerId = queryProviderId;
        providerLocked = true;
      }
      if (new URL(window.location.href).searchParams.get('deployment')) {
        activeTab = 'deployments';
      }
      try {
        await loadRuntimeViews();
      } catch {
        runtimeViewsAvailable = false;
      }
    } catch (err) {
      error = message(err, 'Failed to load local models');
    } finally {
      loading = false;
    }
  }

  async function loadRuntimeViews(): Promise<void> {
    const nextTargets: Record<string, LocalModelTargetStatus[]> = {};
    const nextOperations: Record<string, LocalModelOperation[]> = {};
    runtimeViewsAvailable = true;
    await Promise.all(
      deployments.map(async (deployment) => {
        try {
          const [deploymentTargets, deploymentOperations] = await Promise.all([
            api.localModels.targets(deployment.deployment_id),
            api.localModels.operations(deployment.deployment_id)
          ]);
          nextTargets[deployment.deployment_id] = deploymentTargets;
          nextOperations[deployment.deployment_id] = deploymentOperations;
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) {
            runtimeViewsAvailable = false;
            nextTargets[deployment.deployment_id] = [];
            nextOperations[deployment.deployment_id] = [];
            return;
          }
          throw err;
        }
      })
    );
    targets = nextTargets;
    operations = nextOperations;
  }

  async function searchCatalog(cursor?: string): Promise<void> {
    const requestSequence = ++catalogRequestSequence;
    const filters: CatalogFilters = cursor
      ? appliedCatalogFilters
      : {
          parameterRange,
          downloadSizeRange,
          quantization: quantizationFilter.trim().toUpperCase(),
          minContext,
          includeUnknown
        };
    const query = cursor ? appliedSearchQuery : searchQuery.trim();
    searching = true;
    error = null;
    try {
      const result = await api.localModels.catalog({
        source: catalogSource,
        query,
        cursor,
        limit: 20,
        parameterRange: filters.parameterRange || undefined,
        downloadSizeRange: filters.downloadSizeRange || undefined,
        quantization: filters.quantization || undefined,
        minContext: filters.minContext ? Number(filters.minContext) : undefined,
        includeUnknown: filters.includeUnknown
      });
      if (requestSequence !== catalogRequestSequence) return;
      appliedSearchQuery = query;
      appliedCatalogFilters = filters;
      catalog =
        cursor && catalog
          ? { ...result, items: [...catalog.items, ...result.items] }
          : result;
    } catch (err) {
      if (requestSequence !== catalogRequestSequence) return;
      error = message(err, 'Catalog search failed');
    } finally {
      if (requestSequence === catalogRequestSequence) {
        searching = false;
      }
    }
  }

  function changeCatalogSource(event: Event): void {
    catalogSource = (event.currentTarget as HTMLSelectElement).value as LocalModelCatalogSource;
    catalog = null;
    void searchCatalog();
  }

  function repositoryId(model: LocalModelCatalogItem): string | null {
    return model.catalog_id.startsWith('huggingface:')
      ? model.catalog_id.slice('huggingface:'.length)
      : null;
  }

  function matchesAppliedFilters(model: LocalModelCatalogItem): boolean {
    const filters = appliedCatalogFilters;
    const numericRange = (
      value: number | null,
      range: string,
      bounds: Record<string, [number | null, number | null]>
    ): boolean => {
      if (value == null) return filters.includeUnknown;
      const [lower, upper] = bounds[range] ?? [null, null];
      return (lower == null || value > lower) && (upper == null || value <= upper);
    };
    if (
      filters.parameterRange &&
      !numericRange(model.parameter_count, filters.parameterRange, {
        le4b: [null, 4e9],
        '4b_8b': [4e9, 8e9],
        '8b_14b': [8e9, 14e9],
        '14b_32b': [14e9, 32e9],
        '32b_70b': [32e9, 70e9],
        '70b_plus': [70e9, null]
      })
    ) {
      return false;
    }
    const quantization = filters.quantization;
    const quantizations = model.quantizations.filter(
      (item) => !quantization || item.name === quantization
    );
    if (quantization && quantizations.length === 0) return false;
    if (filters.downloadSizeRange) {
      const sizes = quantizations.flatMap((item) =>
        item.size_bytes == null ? [] : [item.size_bytes]
      );
      if (sizes.length === 0 && !filters.includeUnknown) return false;
      if (
        sizes.length > 0 &&
        !sizes.some((size) =>
          numericRange(size, filters.downloadSizeRange, {
            le4gib: [null, 4 * 1024 ** 3],
            '4gib_8gib': [4 * 1024 ** 3, 8 * 1024 ** 3],
            '8gib_16gib': [8 * 1024 ** 3, 16 * 1024 ** 3],
            '16gib_32gib': [16 * 1024 ** 3, 32 * 1024 ** 3],
            '32gib_plus': [32 * 1024 ** 3, null]
          })
        )
      ) {
        return false;
      }
    }
    if (filters.minContext) {
      if (model.advertised_max_context == null) return filters.includeUnknown;
      if (model.advertised_max_context < Number(filters.minContext)) return false;
    }
    return true;
  }

  function loadModelDetails(model: LocalModelCatalogItem): void {
    const repo = repositoryId(model);
    if (!repo || model.metadata_status !== 'basic') return;
    const searchSequence = catalogRequestSequence;
    const expectedSha = model.revision_sha;
    const detailKey = `${repo.toLowerCase()}@${expectedSha?.toLowerCase() ?? 'latest'}`;
    let request = detailRequests.get(detailKey);
    if (!request) {
      request = api.localModels.detail(repo, expectedSha);
      detailRequests.set(detailKey, request);
      const cleanup = () => {
        if (detailRequests.get(detailKey) === request) detailRequests.delete(detailKey);
      };
      void request.then(cleanup, cleanup);
    }
    void request
      .then((detail) => {
        if (searchSequence !== catalogRequestSequence) return;
        const current = catalog?.items.find((item) => item.catalog_id === model.catalog_id);
        if (!current || current.revision_sha !== expectedSha) return;
        const remainsVisible = matchesAppliedFilters(detail);
        catalog = catalog
          ? {
              ...catalog,
              items: catalog.items
                .map((item) => (item.catalog_id === detail.catalog_id ? detail : item))
                .filter((item) => item.catalog_id !== detail.catalog_id || remainsVisible)
            }
          : catalog;
        if (selectedModel?.catalog_id === detail.catalog_id) {
          if (remainsVisible) {
            selectedModel = detail;
            const previousRef = selectedRef;
            if (!detail.quantizations.some((item) => item.requested_ref === selectedRef)) {
              selectedRef = detail.quantizations[0]?.requested_ref ?? detail.requested_ref;
            }
            if (selectedRef !== previousRef) void recommendProvider();
          } else {
            selectedModel = null;
            selectedRef = '';
            notice = 'The selected model no longer matches the active catalog filters after repository details loaded.';
          }
          invalidatePlan();
        }
      })
      .catch((err) => {
        if (searchSequence !== catalogRequestSequence) return;
        const diagnostic = message(err, 'Repository details are temporarily unavailable');
        catalog = catalog
          ? {
              ...catalog,
              items: catalog.items.map((item) =>
                item.catalog_id === model.catalog_id
                  ? {
                      ...item,
                      metadata_status: 'error' as const,
                      metadata_confidence: 'low' as const,
                      metadata_diagnostics: [diagnostic]
                    }
                  : item
              )
            }
          : catalog;
      });
  }

  async function resolveDirectReference(): Promise<void> {
    if (!directRef.trim()) return;
    searching = true;
    error = null;
    try {
      const model = await api.localModels.resolve(directRef.trim());
      selectModel(model, model.quantizations[0]?.requested_ref ?? model.requested_ref);
    } catch (err) {
      error = message(err, 'Invalid model reference');
    } finally {
      searching = false;
    }
  }

  function selectModel(model: LocalModelCatalogItem, requestedRef: string): void {
    providerRequestSequence += 1;
    selectedModel = model;
    selectedRef = requestedRef;
    providerRecommendation = null;
    recommendingProvider = true;
    if (!providerLocked) providerId = '';
    selectedExecutorIds = [];
    contextTokens = Math.min(model.advertised_max_context ?? 32768, 32768);
    invalidatePlan();
    notice = null;
    loadModelDetails(model);
    void recommendProvider();
    queueMicrotask(() => document.getElementById('deployment-planner')?.scrollIntoView?.({ behavior: 'smooth' }));
  }

  function reasonLabel(code: string): string {
    const labels: Record<string, string> = {
      model_already_configured: 'model is already configured',
      healthy_hosts: 'healthy provider hosts',
      user_owned: 'owned by your account',
      managed_local_reusable: 'reusable managed provider',
      compatible_ollama_provider: 'compatible Ollama routing'
    };
    return labels[code] ?? code.replaceAll('_', ' ');
  }

  function chooseDefaultHosts(): void {
    const selectable = providerExecutors.filter((executor) => !managedHostDisabledReason(executor));
    selectedExecutorIds = selectable.length === 1 ? [selectable[0].executor_id] : [];
    targetMode = 'ids';
    labelKey = '';
    labelValue = '';
    invalidatePlan();
  }

  async function recommendProvider(): Promise<void> {
    if (!selectedRef) return;
    const requestSequence = ++providerRequestSequence;
    const requestedRef = selectedRef;
    recommendingProvider = true;
    try {
      const recommendation = await api.localModels.recommendProvider({
        requested_ref: requestedRef
      });
      if (requestSequence !== providerRequestSequence || requestedRef !== selectedRef) return;
      providerRecommendation = recommendation;
      if (!providerLocked) {
        providerId = recommendation.recommended_provider_id ?? AUTO_PROVIDER;
      }
      chooseDefaultHosts();
    } catch (err) {
      if (requestSequence !== providerRequestSequence) return;
      error = message(err, 'Provider recommendation failed');
    } finally {
      if (requestSequence === providerRequestSequence) recommendingProvider = false;
    }
  }

  function changeProvider(event: Event): void {
    providerId = (event.currentTarget as HTMLSelectElement).value;
    chooseDefaultHosts();
  }

  function toggleExecutor(executorId: string): void {
    const executor = providerExecutors.find((item) => item.executor_id === executorId);
    if (!executor || managedHostDisabledReason(executor)) return;
    selectedExecutorIds = selectedExecutorIds.includes(executorId)
      ? selectedExecutorIds.filter((id) => id !== executorId)
      : [...selectedExecutorIds, executorId];
    invalidatePlan();
  }

  async function estimateFit(): Promise<void> {
    if (recommendingProvider) {
      error = 'Wait for the current provider recommendation before estimating capacity.';
      return;
    }
    if (!selectedModel || matched.length === 0) {
      error = 'Choose at least one matching executor before estimating capacity.';
      return;
    }
    planning = true;
    const requestSequence = ++planRequestSequence;
    error = null;
    notice = null;
    try {
      const result = await api.localModels.plan({
        model: fitMetadata(selectedModel, selectedRef),
        selector,
        ...(providerId !== AUTO_PROVIDER ? { provider_id: providerId } : {}),
        context_tokens: contextTokens
      });
      if (requestSequence !== planRequestSequence) return;
      plan = result;
      overrideAcknowledged = false;
    } catch (err) {
      if (requestSequence === planRequestSequence) {
        error = message(err, 'Capacity estimate failed');
      }
    } finally {
      if (requestSequence === planRequestSequence) planning = false;
    }
  }

  async function createDeployment(): Promise<void> {
    if (!selectedModel || !plan) {
      error = 'Run the capacity estimate before creating desired state.';
      return;
    }
    if (overrideRequired && !overrideAcknowledged) {
      error = 'Acknowledge the capacity warning to create this deployment unchanged.';
      return;
    }
    if (autoSelectorUnsupported) {
      error = 'Auto-created providers support one exact host or a reusable label selector. Choose one host or use labels.';
      return;
    }
    if (!canCreateDeployment) {
      error = 'Viewer accounts can inspect plans but cannot create desired state.';
      return;
    }
    creating = true;
    error = null;
    notice = null;
    try {
      if (!providerId) {
        error = 'Choose an Ollama provider before deploying.';
        return;
      }
      const payload = deploymentPayload(
        selectedRef,
        selector,
        providerId === AUTO_PROVIDER ? '' : providerId,
        plan,
        overrideAcknowledged
      );
      payload.shared =
        providerId !== AUTO_PROVIDER &&
        selectedProvider?.owner_email === 'system@cognis.local';
      const result =
        providerId === AUTO_PROVIDER
          ? await api.localModels.createManagedDeployment({
              requested_ref: payload.requested_ref,
              selector: payload.selector,
              capacity_override_acknowledged: payload.capacity_override_acknowledged,
              capacity_assessment_generation: payload.capacity_assessment_generation,
              force_create_provider: true
            })
          : { deployment: await api.localModels.createDeployment(payload), provider_created: false };
      const deployment = result.deployment;
      deployments = [deployment, ...deployments];
      notice = result.provider_created
        ? 'Provider and deployment created atomically. Rollout status will update below.'
        : 'Deployment created in the selected provider. Rollout status will update below.';
      activeTab = 'deployments';
      try {
        await loadRuntimeViews();
      } catch {
        runtimeViewsAvailable = false;
        notice = 'Desired state was created, but runtime target and operation status could not be refreshed.';
      }
    } catch (err) {
      error = message(err, 'Failed to create deployment');
    } finally {
      creating = false;
    }
  }

  async function prepareRepair(deployment: LocalModelDeployment): Promise<void> {
    try {
      const recommendation = await api.localModels.recommendProvider({
        requested_ref: deployment.requested_ref,
        selector: deployment.selector,
        shared: deployment.shared
      });
      repairRecommendations = {
        ...repairRecommendations,
        [deployment.deployment_id]: recommendation
      };
      repairProviderIds = {
        ...repairProviderIds,
        [deployment.deployment_id]:
          recommendation.recommended_provider_id ??
          (repairSupportsAutoCreate(deployment) ? AUTO_PROVIDER : '')
      };
    } catch (err) {
      error = message(err, 'Failed to load provider repair options');
    }
  }

  async function repairDeployment(deployment: LocalModelDeployment): Promise<void> {
    const selected = repairProviderIds[deployment.deployment_id];
    if (!selected) return;
    creating = true;
    try {
      const result =
        selected === AUTO_PROVIDER
          ? await api.localModels.attachManagedProvider(deployment.deployment_id, {
              force_create_provider: true
            })
          : {
              deployment: await api.localModels.updateDeployment(deployment.deployment_id, {
                provider_id: selected
              }),
              provider_created: false
            };
      const updated = result.deployment;
      deployments = deployments.map((item) =>
        item.deployment_id === updated.deployment_id ? updated : item
      );
      notice = result.provider_created
        ? 'A provider was created for this host selector and attached. Reconciliation can now proceed.'
        : 'Provider attached. Reconciliation can now proceed.';
      delete repairRecommendations[deployment.deployment_id];
      repairRecommendations = { ...repairRecommendations };
      await loadRuntimeViews();
    } catch (err) {
      error = message(err, 'Failed to repair provider linkage');
    } finally {
      creating = false;
    }
  }

  async function requestReconciliation(deployment: LocalModelDeployment): Promise<void> {
    if (!canManageDeployment(deployment)) {
      error = 'You can inspect this deployment but cannot request reconciliation for it.';
      return;
    }
    error = null;
    notice = null;
    try {
      const updated = await api.localModels.reconcile(deployment.deployment_id);
      deployments = deployments.map((item) =>
        item.deployment_id === updated.deployment_id ? updated : item
      );
      notice = 'Reconciliation requested. This does not claim that a runtime pull has completed.';
      try {
        await loadRuntimeViews();
      } catch {
        runtimeViewsAvailable = false;
        notice = 'Reconciliation was requested, but runtime status could not be refreshed.';
      }
    } catch (err) {
      error = message(err, 'Failed to request reconciliation');
    }
  }

  onMount(load);
</script>

<svelte:head><title>Local Models · Cognis</title></svelte:head>

<section class="mx-auto flex w-full max-w-7xl flex-col gap-6 p-4 pb-24 sm:p-6">
  <header class="relative overflow-hidden rounded-3xl border border-sky-500/20 bg-gradient-to-br from-slate-900 via-slate-900 to-sky-950/60 p-6 sm:p-8">
    <div class="relative z-10 max-w-3xl">
      <p class="text-xs font-semibold uppercase tracking-[0.25em] text-sky-300">Private inference</p>
      <h1 class="mt-3 text-3xl font-semibold text-white sm:text-4xl">Local Models</h1>
      <p class="mt-3 max-w-2xl text-sm leading-6 text-slate-300">
        Choose a model, deploy it through one Ollama provider, and track rollout. The provider owns hosts and routing; a deployment manages one model within that provider. Cognis never swaps quantization or silently reduces context.
      </p>
    </div>
    <Database class="absolute -bottom-10 -right-8 h-48 w-48 text-sky-400/5" aria-hidden="true" />
  </header>

  <Card class="grid gap-4 p-4 sm:grid-cols-3">
    <div class="flex gap-3">
      <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-sky-500/15 text-sm font-bold text-sky-200">1</span>
      <div><h2 class="text-sm font-medium text-white">Choose an artifact</h2><p class="mt-1 text-xs text-slate-400">Curated Ollama or public Hugging Face GGUF.</p></div>
    </div>
    <div class="flex gap-3">
      <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-violet-500/15 text-sm font-bold text-violet-200">2</span>
      <div><h2 class="text-sm font-medium text-white">Check capacity</h2><p class="mt-1 text-xs text-slate-400">Current and static memory, per executor.</p></div>
    </div>
    <div class="flex gap-3">
      <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-500/15 text-sm font-bold text-emerald-200">3</span>
      <div><h2 class="text-sm font-medium text-white">Create desired state</h2><p class="mt-1 text-xs text-slate-400">Track targets and operations without fake success.</p></div>
    </div>
  </Card>

  <nav class="flex gap-1 overflow-x-auto rounded-2xl border border-slate-800 bg-slate-900/70 p-1" aria-label="Local model sections">
    {#each tabs as tab}
      <button
        type="button"
        class={`flex min-h-11 shrink-0 items-center gap-2 rounded-xl px-4 text-sm font-medium transition ${activeTab === tab.id ? 'bg-sky-500 text-slate-950' : 'text-slate-300 hover:bg-slate-800 hover:text-white'}`}
        aria-current={activeTab === tab.id ? 'page' : undefined}
        onclick={() => (activeTab = tab.id)}
      >
        <tab.icon class="h-4 w-4" aria-hidden="true" />{tab.label}
      </button>
    {/each}
  </nav>

  {#if error}
    <div class="rounded-2xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-100" role="alert">{error}</div>
  {/if}
  {#if notice}
    <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 p-4 text-sm text-sky-100" role="status">{notice}</div>
  {/if}

  {#if loading}
    <Card class="p-8 text-center text-slate-400">Loading local model workspace…</Card>
  {:else if activeTab === 'catalog'}
    <div class="space-y-6">
      <Card class="p-4">
        <form class="grid gap-3 md:grid-cols-[10rem_1fr_auto]" onsubmit={(event) => { event.preventDefault(); void searchCatalog(); }}>
          <label class="text-xs font-medium text-slate-300">
            Source
            <select value={catalogSource} onchange={changeCatalogSource} class="mt-1 min-h-11 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100" aria-label="Catalog source">
              <option value="ollama">Curated Ollama</option>
              <option value="huggingface">Hugging Face GGUF</option>
              <option value="installed">Installed / live</option>
            </select>
          </label>
          <label class="text-xs font-medium text-slate-300">
            Search
            <Input bind:value={searchQuery} class="mt-1" placeholder={catalogSource === 'huggingface' ? 'Model or publisher' : 'Chat, reasoning, vision…'} aria-label="Search local model catalog" />
          </label>
          <Button type="submit" class="self-end" disabled={searching}>{searching ? 'Searching…' : 'Search'}</Button>
        </form>
        <div class="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <label class="text-xs font-medium text-slate-300">
            Parameters
            <select bind:value={parameterRange} class="mt-1 min-h-10 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100">
              <option value="">Any</option>
              <option value="le4b">≤ 4B</option><option value="4b_8b">4–8B</option>
              <option value="8b_14b">8–14B</option><option value="14b_32b">14–32B</option>
              <option value="32b_70b">32–70B</option><option value="70b_plus">70B+</option>
            </select>
          </label>
          <label class="text-xs font-medium text-slate-300">
            Selected quant size
            <select bind:value={downloadSizeRange} class="mt-1 min-h-10 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100">
              <option value="">Any</option>
              <option value="le4gib">≤ 4 GiB</option><option value="4gib_8gib">4–8 GiB</option>
              <option value="8gib_16gib">8–16 GiB</option><option value="16gib_32gib">16–32 GiB</option>
              <option value="32gib_plus">32+ GiB</option>
            </select>
          </label>
          <label class="text-xs font-medium text-slate-300">
            Quantization
            <Input bind:value={quantizationFilter} class="mt-1" placeholder="Q4_K_M" aria-label="Quantization filter" />
          </label>
          <label class="text-xs font-medium text-slate-300">
            Minimum context
            <select bind:value={minContext} class="mt-1 min-h-10 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100">
              <option value="">Any</option>
              <option value="8192">8k+</option><option value="16384">16k+</option>
              <option value="32768">32k+</option><option value="65536">64k+</option>
              <option value="131072">128k+</option>
            </select>
          </label>
          <label class="flex min-h-10 items-center gap-2 self-end rounded-xl border border-slate-700 bg-slate-950 px-3 text-xs text-slate-300">
            <input type="checkbox" bind:checked={includeUnknown} class="accent-sky-400" />
            Include unknown metadata
          </label>
        </div>
        {#each catalog?.sources ?? [] as sourceStatus}
          {#if sourceStatus.source === catalogSource && sourceStatus.detail}
            <p class={`mt-3 text-xs ${sourceStatus.available ? 'text-slate-400' : 'text-amber-200'}`}>{sourceStatus.detail}</p>
          {/if}
        {/each}
        {#if catalog?.pagination_note}
          <p class="mt-2 text-xs text-slate-500">{catalog.pagination_note}</p>
        {/if}
      </Card>

      <details class="rounded-2xl border border-slate-800 bg-slate-900/50 p-4">
        <summary class="cursor-pointer text-sm font-medium text-slate-200">Use an exact model reference</summary>
        <form class="mt-3 flex flex-col gap-2 sm:flex-row" onsubmit={(event) => { event.preventDefault(); void resolveDirectReference(); }}>
          <Input bind:value={directRef} placeholder="llama3.2:3b or hf.co/org/repo:Q4_K_M" aria-label="Exact local model reference" />
          <Button type="submit" variant="secondary" disabled={!directRef.trim() || searching}>Resolve</Button>
        </form>
      </details>

      {#if catalog?.items.length}
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {#each catalog.items as model (model.catalog_id)}
            <CatalogModelCard
              {model}
              selected={selectedModel?.catalog_id === model.catalog_id}
              onselect={selectModel}
              ondetails={loadModelDetails}
            />
          {/each}
        </div>
        {#if catalog.next_cursor}
          <div class="text-center"><Button variant="secondary" disabled={searching} onclick={() => searchCatalog(catalog?.next_cursor ?? undefined)}>Load more</Button></div>
        {/if}
      {:else}
        <Card class="p-8 text-center">
          <p class="text-sm text-slate-300">No deployable models are available from this source.</p>
          <p class="mt-1 text-xs text-slate-500">Installed inventory remains a placeholder until the runtime adapter is present.</p>
        </Card>
      {/if}

      {#if selectedModel}
        <div id="deployment-planner">
        <Card class="space-y-6 p-5 sm:p-6">
          <div class="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p class="text-xs uppercase tracking-[0.2em] text-sky-300">Deployment plan</p>
              <h2 class="mt-2 text-2xl font-semibold text-white">{selectedModel.title}</h2>
              <code class="mt-2 block break-all text-xs text-slate-400">{selectedRef}</code>
            </div>
          </div>
          <details class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
            <summary class="cursor-pointer text-sm font-medium text-slate-200">Advanced model and reproducibility details</summary>
            <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
              <div><dt class="text-slate-500">Metadata revision</dt><dd class="break-all font-mono text-slate-300">{selectedModel.revision_sha ?? 'Not reported'}</dd></div>
              <div><dt class="text-slate-500">Architecture</dt><dd class="text-slate-300">{selectedModel.architecture_name ?? 'Not reported'}</dd></div>
              <div><dt class="text-slate-500">Pipeline</dt><dd class="text-slate-300">{selectedModel.pipeline_tag ?? 'Not reported'}</dd></div>
              <div><dt class="text-slate-500">Base model</dt><dd class="text-slate-300">{selectedModel.base_models.join(', ') || 'Not reported'}</dd></div>
            </dl>
            {#if selectedModel.reference_integrity !== 'pinned'}
              <p class="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100">
                Hugging Face metadata was read at the revision above, but the pull remains <code>{selectedRef}</code>. It follows the repository quantization reference and is not pinned to that commit or an exact file list.
              </p>
            {/if}
          </details>

          <div class="grid gap-5 lg:grid-cols-2">
            <section aria-labelledby="provider-heading">
              <h3 id="provider-heading" class="text-base font-semibold text-white">1. Ollama provider</h3>
              <label class="mt-3 block text-xs font-medium text-slate-300">
                Provider and routing scope
                <select
                  value={providerId}
                  onchange={changeProvider}
                  disabled={providerLocked || recommendingProvider}
                  class="mt-1 min-h-11 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100 disabled:cursor-not-allowed disabled:opacity-70"
                  aria-label="Ollama provider"
                >
                  {#if !providerId}<option value="">Loading recommendation…</option>{/if}
                  {#if providerLocked && selectedProvider}
                    <option value={selectedProvider.provider_id}>{selectedProvider.display_name} · locked from provider settings</option>
                  {/if}
                  {#each providerRecommendation?.candidates ?? [] as candidate}
                    <option value={candidate.provider_id}>
                      {candidate.display_name}{candidate.provider_id === providerRecommendation?.recommended_provider_id ? ' · Recommended' : ''}
                    </option>
                  {/each}
                  <option value={AUTO_PROVIDER}>
                    {providerRecommendation?.candidates.length ? 'Create a separate provider for this host selector…' : 'Create a provider for this host selector'}
                  </option>
                </select>
              </label>
              {#if providerId === AUTO_PROVIDER}
                <p class="mt-3 rounded-xl border border-sky-500/30 bg-sky-500/10 p-3 text-xs leading-5 text-sky-100">
                  No existing provider will be changed. Provider creation and deployment are one atomic operation, and the provider is reused by host selector—not by model.
                </p>
              {:else if selectedCandidate}
                <p class="mt-3 text-xs leading-5 text-slate-400">
                  {providerId === providerRecommendation?.recommended_provider_id ? 'Recommended because ' : 'Selected because '}
                  {selectedCandidate.reason_codes.map(reasonLabel).join(', ')}.
                </p>
              {/if}
              {#if providerLocked}
                <p class="mt-2 text-xs text-amber-200">Provider scope is locked by the <code>provider</code> query parameter.</p>
              {/if}
              <p class="mt-3 text-xs leading-5 text-slate-400">
                Provider = host scope and inference routing. Deployment = this model's rollout, default visibility, and status inside that provider.
              </p>
            </section>

            <section aria-labelledby="target-heading">
              <h3 id="target-heading" class="text-base font-semibold text-white">2. Host subset</h3>
              <div class="mt-3 flex gap-2">
                <Button size="sm" variant={targetMode === 'ids' ? 'primary' : 'secondary'} onclick={() => { targetMode = 'ids'; invalidatePlan(); }}>Exact IDs</Button>
                <Button size="sm" variant={targetMode === 'labels' ? 'primary' : 'secondary'} onclick={() => { targetMode = 'labels'; invalidatePlan(); }}>Label selector</Button>
              </div>
              {#if targetMode === 'ids'}
                <fieldset class="mt-3 space-y-2">
                  <legend class="sr-only">Select executor IDs</legend>
                   {#each providerExecutors as executor}
                     <label class={`flex items-start gap-3 rounded-xl border border-slate-800 bg-slate-950/50 p-3 ${managedHostDisabledReason(executor) ? 'cursor-not-allowed opacity-65' : 'cursor-pointer'}`}>
                       <input type="checkbox" class="mt-1 accent-sky-400" checked={selectedExecutorIds.includes(executor.executor_id)} disabled={Boolean(managedHostDisabledReason(executor))} onchange={() => toggleExecutor(executor.executor_id)} />
                       <span>
                         <span class="block text-sm text-slate-200">{executor.name}</span>
                         <span class="block font-mono text-xs text-slate-500">{executor.executor_id}</span>
                         {#if managedHostDisabledReason(executor)}
                           <span class="mt-1 block text-xs text-amber-200">{managedHostDisabledReason(executor)} <a class="underline" href="/settings?tab=executors">Open executor settings</a></span>
                         {/if}
                       </span>
                     </label>
                   {/each}
                   {#if providerExecutors.length === 0}
                     <p class="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100">This provider currently resolves no visible hosts. Check its executor selector and executor capability settings.</p>
                   {/if}
                </fieldset>
              {:else}
                <div class="mt-3 grid grid-cols-2 gap-2">
                  <Input bind:value={labelKey} placeholder="Label key" aria-label="Executor label key" oninput={invalidatePlan} />
                  <Input bind:value={labelValue} placeholder="Label value" aria-label="Executor label value" oninput={invalidatePlan} />
                </div>
              {/if}
              <div class="mt-3 rounded-xl border border-slate-800 bg-slate-950/50 p-3">
                <p class="text-xs font-medium text-slate-300">{matched.length} matched executor{matched.length === 1 ? '' : 's'}</p>
                 <p class="mt-1 text-xs text-slate-500">{matched.map((executor) => executor.name).join(', ') || 'No selectable provider host matches yet.'}</p>
              </div>
              {#if hiddenExecutorCount > 0}
                <p class="mt-2 text-xs text-amber-200">{hiddenExecutorCount} shared or non-mutable executor{hiddenExecutorCount === 1 ? ' is' : 's are'} hidden for this private deployment.</p>
              {/if}
              {#if autoSelectorUnsupported}
                <p class="mt-2 text-xs text-amber-200">A new provider can use one exact host or a reusable label selector. Existing providers may expose a larger host scope.</p>
              {/if}
            </section>
          </div>

          <CapacityPlanner
            model={selectedModel}
            {plan}
             bind:contextTokens
             busy={planning || recommendingProvider}
            onplan={estimateFit}
            oncontextchange={invalidatePlan}
          />

          {#if plan}
            <div class="rounded-2xl border border-slate-800 bg-slate-950/50 p-4">
              {#if overrideRequired}
                <label class="flex items-start gap-3 text-sm text-rose-100">
                  <input type="checkbox" class="mt-1 accent-rose-400" bind:checked={overrideAcknowledged} />
                  <span>
                    <strong class="block">Deploy this exact artifact and context assessment anyway</strong>
                    <span class="mt-1 block text-xs text-rose-200/80">I understand that one or more machines may not load it. Cognis will persist this acknowledgement and will not change the quantization.</span>
                  </span>
                </label>
              {:else}
                <div class="flex items-center gap-2 text-sm text-emerald-200"><CheckCircle2 class="h-4 w-4" aria-hidden="true" /> Current capacity estimate is green on every matched executor.</div>
              {/if}
              <div class="mt-4 flex flex-col gap-2 sm:flex-row sm:justify-end">
                {#if selectedModel.reference_integrity !== 'pinned'}
                  <p class="mr-auto max-w-xl self-center text-xs text-amber-200">Final confirmation: this pull reference is floating and is not pinned to the displayed metadata SHA.</p>
                {/if}
                {#if plan.recommended_context_tokens && plan.recommended_context_tokens !== contextTokens}
                  <Button variant="secondary" onclick={() => { contextTokens = plan?.recommended_context_tokens ?? contextTokens; invalidatePlan(); }}>Use safe {formatContext(plan.recommended_context_tokens)}</Button>
                {/if}
                 <Button onclick={createDeployment} disabled={!canCreateDeployment || creating || autoSelectorUnsupported || (overrideRequired && !overrideAcknowledged)}>
                  {creating ? 'Creating desired state…' : overrideRequired ? 'Create anyway' : 'Create deployment'}
                </Button>
              </div>
              {#if !canCreateDeployment}<p class="mt-2 text-right text-xs text-amber-200">Viewer accounts can inspect this plan but cannot create deployments.</p>{/if}
            </div>
          {/if}
        </Card>
        </div>
      {/if}
    </div>
  {:else if activeTab === 'deployments'}
    <div class="space-y-4">
      {#if deployments.length === 0}
        <Card class="p-8 text-center"><p class="text-slate-300">No local model deployments yet.</p><Button class="mt-4" onclick={() => (activeTab = 'catalog')}>Browse catalog</Button></Card>
      {:else}
        {#each deployments as deployment}
          <Card class="p-5">
            <div class="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <div class="flex flex-wrap items-center gap-2">
                   <h2 class="font-mono text-sm font-semibold text-white">{deployment.runtime_name}</h2>
                   <span class="rounded-full border border-slate-700 px-2 py-1 text-[10px] uppercase tracking-wider text-slate-300">{deployment.desired_state}</span>
                   <span class={`rounded-full border px-2 py-1 text-[10px] ${deployment.lifecycle_state === 'needs_provider' ? 'border-amber-500/30 bg-amber-500/10 text-amber-100' : 'border-sky-500/30 bg-sky-500/10 text-sky-100'}`}>{deployment.lifecycle_state === 'needs_provider' ? 'Needs provider' : 'Managed'}</span>
                  {#if deployment.capacity_override_acknowledged}<span class="rounded-full border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[10px] text-rose-200">Capacity override</span>{/if}
                </div>
                 <p class="mt-2 text-xs text-slate-500">Generation {deployment.generation} · {Object.keys(deployment.selector.match_labels ?? {}).length ? `labels ${JSON.stringify(deployment.selector.match_labels)}` : `${deployment.selector.executor_ids?.length ?? 0} exact targets`} · Provider {deployment.provider_id ?? 'not attached'}</p>
               </div>
               {#if deployment.lifecycle_state === 'needs_provider'}
                 <Button variant="secondary" size="sm" disabled={!canManageDeployment(deployment)} onclick={() => prepareRepair(deployment)}>Repair provider</Button>
               {:else}
                 <Button variant="secondary" size="sm" disabled={!canManageDeployment(deployment)} onclick={() => requestReconciliation(deployment)}>Request reconciliation</Button>
               {/if}
             </div>
             {#if deployment.lifecycle_state === 'needs_provider'}
               <div class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
                 <p class="text-sm font-medium text-amber-100">This pre-provider deployment cannot reconcile.</p>
                 <p class="mt-1 text-xs text-amber-100/80">Attach a compatible existing Ollama provider, or explicitly create one keyed to this deployment's host selector.</p>
                 {#if repairRecommendation(deployment.deployment_id)}
                   <div class="mt-3 flex flex-col gap-2 sm:flex-row">
                     <label class="sr-only" for={`repair-provider-${deployment.deployment_id}`}>Repair provider</label>
                     <select
                       id={`repair-provider-${deployment.deployment_id}`}
                       value={repairProviderIds[deployment.deployment_id]}
                       onchange={(event) => {
                         repairProviderIds = {
                           ...repairProviderIds,
                           [deployment.deployment_id]: event.currentTarget.value
                         };
                       }}
                       class="min-h-11 flex-1 rounded-xl border border-amber-500/30 bg-slate-950 px-3 text-sm text-slate-100"
                     >
                       {#each repairRecommendation(deployment.deployment_id)?.candidates ?? [] as candidate}
                         <option value={candidate.provider_id}>{candidate.display_name}{candidate.provider_id === repairRecommendation(deployment.deployment_id)?.recommended_provider_id ? ' · Recommended' : ''}</option>
                       {/each}
                       {#if repairSupportsAutoCreate(deployment)}
                         <option value={AUTO_PROVIDER}>Create provider for this host selector…</option>
                       {/if}
                     </select>
                     <Button disabled={creating || !repairProviderIds[deployment.deployment_id]} onclick={() => repairDeployment(deployment)}>Attach and repair</Button>
                   </div>
                   {#if !repairSupportsAutoCreate(deployment) && repairRecommendation(deployment.deployment_id)?.candidates.length === 0}
                     <p class="mt-2 text-xs text-amber-100">No existing provider spans these exact hosts, and auto-created providers cannot use multiple exact IDs. Change the legacy selector to one host or a reusable label selector first.</p>
                   {/if}
                 {/if}
               </div>
             {/if}
            <div class="mt-4 grid gap-2 sm:grid-cols-3">
              {#each targets[deployment.deployment_id] ?? [] as target}
                <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
                  <p class="font-mono text-xs text-slate-300">{target.executor_id}</p>
                   <p class={`mt-1 text-sm capitalize ${target.state === 'ready' ? 'text-emerald-300' : target.state === 'error' || target.state === 'blocked' ? 'text-rose-300' : 'text-amber-200'}`}>{target.state.replaceAll('_', ' ')}</p>
                   {#if target.last_error}<p class="mt-1 text-xs text-rose-200">{target.last_error}</p>{/if}
                   {#if target.state === 'blocked'}
                     <a class="mt-2 inline-block text-xs text-sky-300 underline" href="/settings?tab=executors">Check local inference and model-management settings</a>
                   {/if}
                 </div>
              {/each}
            </div>
          </Card>
        {/each}
      {/if}
    </div>
  {:else if activeTab === 'installed'}
    <div class="space-y-4">
      {#if !runtimeViewsAvailable}
        <Card class="border-amber-500/30 p-4 text-sm text-amber-100">Runtime inventory endpoints are not available yet. Desired-state data remains usable; no installed state is being inferred.</Card>
      {/if}
      <div class="grid gap-4 lg:grid-cols-2">
        {#each executors as executor}
          <Card class="p-5">
            <div class="flex items-start justify-between gap-3">
              <div><h2 class="font-medium text-white">{executor.name}</h2><p class="mt-1 font-mono text-xs text-slate-500">{executor.executor_id}</p></div>
              <span class={`rounded-full border px-2 py-1 text-[10px] uppercase tracking-wider ${executor.resource_snapshot?.ollama?.status === 'reachable' ? 'border-emerald-500/30 text-emerald-200' : 'border-slate-700 text-slate-400'}`}>{executor.resource_snapshot?.ollama?.status ?? 'unknown'}</span>
            </div>
            <dl class="mt-4 grid grid-cols-2 gap-3 text-xs">
              <div><dt class="text-slate-500">{executor.resource_snapshot?.memory?.unified ? 'Unified memory available' : 'RAM available'}</dt><dd class="mt-1 text-slate-200">{formatBytes(executor.resource_snapshot?.memory?.available_bytes)}</dd></div>
              <div><dt class="text-slate-500">Installed models</dt><dd class="mt-1 text-slate-200">{executor.resource_snapshot?.ollama?.installed_model_count ?? 'Not reported'}</dd></div>
            </dl>
            {#if executor.resource_snapshot?.ollama?.running_models?.length}
              <div class="mt-4 flex flex-wrap gap-1">
                {#each executor.resource_snapshot.ollama.running_models as model}<span class="rounded-md bg-emerald-500/10 px-2 py-1 font-mono text-[11px] text-emerald-200">{model}</span>{/each}
              </div>
            {:else}
              <p class="mt-4 text-xs text-slate-500">No running model names reported.</p>
            {/if}
            <a class="mt-4 inline-block text-xs text-sky-300 hover:text-sky-200" href="/settings?tab=executors">Open executor health →</a>
          </Card>
        {/each}
      </div>
    </div>
  {:else}
    <div class="space-y-3">
      {#if !runtimeViewsAvailable}
        <Card class="border-amber-500/30 p-4 text-sm text-amber-100">Runtime operation APIs are not present. Cognis will not display placeholder progress or report success.</Card>
      {:else if allOperations.length === 0}
        <Card class="p-8 text-center"><p class="text-slate-300">No runtime operations have been recorded.</p><p class="mt-1 text-xs text-slate-500">Queued pulls and removals will appear here when reconciliation creates durable operation rows.</p></Card>
      {:else}
        {#each allOperations as item (item.operation.operation_id)}
          <Card class="p-4">
            <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div><p class="font-mono text-sm text-white">{item.deployment.runtime_name}</p><p class="mt-1 text-xs text-slate-500">{item.operation.executor_id} · {item.operation.action}</p></div>
              <span class="rounded-full border border-slate-700 px-3 py-1 text-xs capitalize text-slate-300">{item.operation.state.replaceAll('_', ' ')}</span>
            </div>
            <p class="mt-3 text-xs text-slate-400">{item.operation.phase ?? 'Waiting for runtime progress'} · {formatBytes(item.operation.progress_bytes)}</p>
            {#if item.operation.sanitized_error}<p class="mt-2 text-xs text-rose-200">{item.operation.sanitized_error}</p>{/if}
          </Card>
        {/each}
      {/if}
    </div>
  {/if}
</section>
