<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import { deriveGettingStartedSteps } from '$lib/getting-started';
  import { collectModelOptions, createProviderForm, providerFormToPayload, type ProviderFormState } from '$lib/providers';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ProviderStatusBadge from '$lib/components/ProviderStatusBadge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { getIntarisUiUrl, getMnemoryUiUrl } from '$lib/config';
  import { auth } from '$lib/stores/auth';
  import type {
    ApiKeyCreateResponse,
    ApiKeyMetadata,
    HealthResponse,
    LLMProvider,
    ModelRouting,
    ProviderTestResult,
    SecretMetadata,
    Setting,
    SettingsCategory,
    SystemDiagnostics
  } from '$lib/types/api';

  type SettingsTab = 'providers' | 'routing' | 'secrets' | 'system' | 'account';

  const tabs: SettingsTab[] = ['providers', 'routing', 'secrets', 'system', 'account'];
  const openAiModels = ['gpt-4o', 'gpt-4o-mini', 'gpt-5.4-mini', 'gpt-5.4-nano', 'o3-mini'];
  const anthropicModels = ['claude-sonnet-4-20250514', 'claude-3-7-sonnet-latest', 'claude-3-5-haiku-latest'];

  let activeTab: SettingsTab = 'providers';
  let loading = true;
  let busy = false;
  let error = '';
  let notice = '';
  let settings: SettingsCategory[] = [];
  let providers: LLMProvider[] = [];
  let modelRouting: ModelRouting = { default: null, classifier: null, compaction: null, simple_inline: null, items: {} };
  let secrets: SecretMetadata[] = [];
  let health: HealthResponse | null = null;
  let diagnostics: SystemDiagnostics | null = null;
  let isAdmin = false;
  let selectedProviderId = '';
  let selectedSettingKey = '';
  let settingValueText = '';
  let providerForm: ProviderFormState = createProviderForm();
  let providerTestResult: ProviderTestResult | null = null;
  let apiKeys: ApiKeyMetadata[] = [];
  let createdApiKey: ApiKeyCreateResponse | null = null;
  let newApiKeyName = '';
  let newApiKeyExpiresInDays = '';

  let routingForm = {
    default: '',
    classifier: '',
    compaction: '',
    simple_inline: '',
    extraJson: '{}'
  };

  let secretForm = {
    name: '',
    value: '',
    scope: 'user',
    agent_id: '',
    description: ''
  };

  let passwordForm = {
    current_password: '',
    new_password: '',
    confirm_password: ''
  };

  function syncTabFromUrl(): void {
    const url = new URL(window.location.href);
    const tab = url.searchParams.get('tab');
    if (tab && tabs.includes(tab as SettingsTab)) {
      activeTab = tab as SettingsTab;
    }
  }

  function setActiveTab(tab: SettingsTab): void {
    activeTab = tab;
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    window.history.replaceState({}, '', url);
  }

  function groupedSettings(): Setting[] {
    return settings.flatMap((group) => group.items);
  }

  function selectedProvider(): LLMProvider | null {
    return providers.find((provider) => provider.provider_id === selectedProviderId) ?? null;
  }

  function modelOptions(): Array<{ value: string; label: string; providerId: string }> {
    return collectModelOptions(providers);
  }

  function routingWarnings(): string[] {
    const knownModels = new Set(modelOptions().map((item) => item.value));
    return [routingForm.default, routingForm.classifier, routingForm.compaction, routingForm.simple_inline]
      .filter(Boolean)
      .filter((model) => !knownModels.has(model))
      .map((model) => `Model '${model}' is not present in configured providers.`);
  }

  function diagnosticsEnvBlock(): string {
    const config = diagnostics?.config ?? {};
    return [
      `export COGNIS_DATA_DIR='${String(config.data_dir ?? '')}'`,
      `export COGNIS_HOST='${String(config.host ?? '')}'`,
      `export COGNIS_PORT='${String(config.port ?? '')}'`,
      `export COGNIS_SERVE_UI='${String(config.serve_ui ?? '')}'`,
      `export COGNIS_MNEMORY_URL='${String(config.mnemory_url ?? '')}'`,
      `export COGNIS_INTARIS_URL='${String(config.intaris_url ?? '')}'`,
      `export COGNIS_LOG_LEVEL='${String(config.log_level ?? '')}'`,
      `export COGNIS_LOG_FORMAT='${String(config.log_format ?? '')}'`,
      `export COGNIS_CORS_ORIGINS='${Array.isArray(config.cors_origins) ? config.cors_origins.join(',') : ''}'`
    ].join('\n');
  }

  async function copyToClipboard(value: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      notice = 'Copied to clipboard.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  function selectProvider(provider: LLMProvider): void {
    selectedProviderId = provider.provider_id;
    providerForm = createProviderForm(provider);
    providerTestResult = provider.last_test;
  }

  function resetProviderForm(): void {
    selectedProviderId = '';
    providerForm = createProviderForm();
    providerTestResult = null;
  }

  function prefillSecretForPreset(): void {
    const name = providerForm.preset === 'anthropic' ? 'anthropic_api_key' : 'openai_api_key';
    secretForm = { ...secretForm, name, description: `${providerForm.display_name || providerForm.provider_id} credential` };
    setActiveTab('secrets');
  }

  async function refreshPageState(): Promise<void> {
    isAdmin = auth.getSnapshot().user?.role === 'admin';
    [settings, modelRouting, secrets, health, apiKeys] = await Promise.all([
      api.settings.list(),
      api.modelRouting.get(),
      api.secrets.list(),
      api.system.health(),
      api.auth.listApiKeys()
    ]);

    routingForm = {
      default: modelRouting.default ?? '',
      classifier: modelRouting.classifier ?? '',
      compaction: modelRouting.compaction ?? '',
      simple_inline: modelRouting.simple_inline ?? '',
      extraJson: JSON.stringify(modelRouting.items, null, 2)
    };

    if (isAdmin) {
      [providers, diagnostics] = await Promise.all([api.llmProviders.list().then((page) => page.items), api.system.diagnostics()]);
    } else {
      providers = [];
      diagnostics = null;
    }

    if (selectedProviderId) {
      const next = providers.find((provider) => provider.provider_id === selectedProviderId);
      if (next) {
        selectProvider(next);
      }
    }
  }

  async function loadSettings(): Promise<void> {
    loading = true;
    error = '';
    notice = '';
    try {
      await refreshPageState();
      syncTabFromUrl();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function saveProvider(): Promise<void> {
    busy = true;
    error = '';
    notice = '';
    try {
      const payload = providerFormToPayload(providerForm);
      if (selectedProviderId) {
        await api.llmProviders.update(selectedProviderId, payload);
      } else {
        await api.llmProviders.create(payload);
      }
      await refreshPageState();
      notice = 'Provider saved.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  async function deleteProvider(providerId: string): Promise<void> {
    busy = true;
    error = '';
    try {
      await api.llmProviders.remove(providerId);
      resetProviderForm();
      await refreshPageState();
      notice = 'Provider removed.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  async function testProvider(providerId: string): Promise<void> {
    busy = true;
    error = '';
    notice = '';
    try {
      const result = await api.llmProviders.test(providerId);
      providerTestResult = result;
      await refreshPageState();
      notice = result.ok ? 'Provider test succeeded.' : 'Provider test failed.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  async function saveRouting(): Promise<void> {
    busy = true;
    error = '';
    try {
      modelRouting = await api.modelRouting.update({
        default: routingForm.default || null,
        classifier: routingForm.classifier || null,
        compaction: routingForm.compaction || null,
        simple_inline: routingForm.simple_inline || null,
        items: JSON.parse(routingForm.extraJson || '{}')
      });
      notice = 'Model routing updated.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  function copyDefaultModelToAll(): void {
    if (!routingForm.default) {
      return;
    }
    routingForm = {
      ...routingForm,
      classifier: routingForm.default,
      compaction: routingForm.default,
      simple_inline: routingForm.default
    };
  }

  async function saveSecret(): Promise<void> {
    busy = true;
    error = '';
    try {
      await api.secrets.upsert({
        name: secretForm.name,
        value: secretForm.value,
        scope: secretForm.scope,
        agent_id: secretForm.agent_id || null,
        description: secretForm.description || null
      });
      secretForm = { ...secretForm, name: '', value: '', agent_id: '', description: '' };
      secrets = await api.secrets.list();
      notice = 'Secret saved.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  async function deleteSecret(secret: SecretMetadata): Promise<void> {
    busy = true;
    error = '';
    try {
      await api.secrets.remove(secret.name, secret.scope, secret.agent_id);
      secrets = await api.secrets.list();
      notice = 'Secret deleted.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  function selectSetting(setting: Setting): void {
    selectedSettingKey = setting.key;
    settingValueText = JSON.stringify(setting.value, null, 2);
  }

  async function saveSetting(): Promise<void> {
    busy = true;
    error = '';
    try {
      await api.settings.update(selectedSettingKey, JSON.parse(settingValueText));
      settings = await api.settings.list();
      notice = 'Setting updated.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  async function openTargetUi(target: 'intaris' | 'mnemory'): Promise<void> {
    try {
      const exchange = await api.auth.exchangeToken(target);
      const baseUrl = target === 'intaris' ? getIntarisUiUrl() : getMnemoryUiUrl();
      const url = new URL(baseUrl, window.location.origin);
      url.searchParams.set('token', exchange.token);
      window.open(url.toString(), '_blank', 'noopener,noreferrer');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function changePassword(): Promise<void> {
    if (passwordForm.new_password !== passwordForm.confirm_password) {
      error = 'New password and confirmation must match.';
      return;
    }
    busy = true;
    error = '';
    try {
      await api.auth.changePassword({
        current_password: passwordForm.current_password,
        new_password: passwordForm.new_password
      });
      passwordForm = { current_password: '', new_password: '', confirm_password: '' };
      notice = 'Password updated.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  async function createApiKey(): Promise<void> {
    busy = true;
    error = '';
    try {
      createdApiKey = await api.auth.createApiKey({
        name: newApiKeyName,
        expires_in_days: newApiKeyExpiresInDays ? Number(newApiKeyExpiresInDays) : null
      });
      apiKeys = await api.auth.listApiKeys();
      newApiKeyName = '';
      newApiKeyExpiresInDays = '';
      notice = 'API key created. Copy it now — it will not be shown again.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  async function revokeApiKey(keyId: string): Promise<void> {
    busy = true;
    error = '';
    try {
      await api.auth.revokeApiKey(keyId);
      apiKeys = await api.auth.listApiKeys();
      notice = 'API key revoked.';
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void loadSettings();
  });
</script>

<svelte:head>
  <title>Settings · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading settings" description="Fetching providers, routing, secrets, diagnostics, and account data." />
{:else}
  <section class="space-y-5">
    <div class="flex flex-wrap gap-2">
      {#each tabs as tab}
        <Button variant={activeTab === tab ? 'primary' : 'secondary'} onclick={() => setActiveTab(tab)}>{tab}</Button>
      {/each}
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}
    {#if notice}
      <p class="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{notice}</p>
    {/if}

    {#if activeTab === 'providers'}
      <div class="grid gap-5 xl:grid-cols-[340px_minmax(0,1fr)]">
        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Providers</p>
              <h2 class="mt-1 text-lg font-semibold text-white">LLM providers</h2>
            </div>
            {#if !isAdmin}
              <p class="text-sm text-slate-400">Provider management is available to admin users only.</p>
            {:else}
              {#each providers as provider}
                <button class="w-full rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-left" onclick={() => selectProvider(provider)}>
                  <div class="flex items-center justify-between gap-3">
                    <span class="font-medium text-slate-100">{provider.display_name}</span>
                    <ProviderStatusBadge status={provider.status} />
                  </div>
                  {#if provider.last_test}
                    <p class="mt-2 text-xs text-slate-400">
                      {provider.last_test.ok ? `Last test passed (${provider.last_test.model_resolved})` : provider.last_test.error_detail}
                    </p>
                  {/if}
                </button>
              {/each}
            {/if}
          </div>
        </Card>

        <Card class="p-5">
          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Provider ID</span>
              <Input bind:value={providerForm.provider_id} disabled={!!selectedProviderId} placeholder="default" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Display name</span>
              <Input bind:value={providerForm.display_name} placeholder="OpenAI" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Preset</span>
              <select bind:value={providerForm.preset} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="ollama">Ollama</option>
                <option value="custom">Custom</option>
              </select>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Status</span>
              <select bind:value={providerForm.status} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="active">active</option>
                <option value="disabled">disabled</option>
              </select>
            </label>
          </div>

          {#if providerForm.preset === 'custom'}
            <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
              <span>Config JSON</span>
              <textarea bind:value={providerForm.custom_json} class="min-h-[240px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
            </label>
          {:else}
            <div class="mt-4 grid gap-4 md:grid-cols-2">
              <label class="space-y-2 text-sm font-medium text-slate-200">
                <span>Base URL {providerForm.preset === 'ollama' ? '' : '(optional)'}</span>
                <Input bind:value={providerForm.base_url} placeholder={providerForm.preset === 'ollama' ? 'http://localhost:11434' : 'https://api.openai.com/v1'} />
              </label>
              <label class="space-y-2 text-sm font-medium text-slate-200">
                <span>Default model</span>
                <Input bind:value={providerForm.default_model} list={`${providerForm.preset}-models`} placeholder="gpt-4o-mini" />
                <datalist id={`${providerForm.preset}-models`}>
                  {#each providerForm.preset === 'openai' ? openAiModels : providerForm.preset === 'anthropic' ? anthropicModels : ['ollama/llama3.2', 'ollama/qwen2.5-coder'] as model}
                    <option value={model}></option>
                  {/each}
                </datalist>
              </label>
            </div>
            <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
              <span>Additional models (one per line)</span>
              <textarea bind:value={providerForm.additional_models} class="min-h-[120px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
            </label>
            <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/50 px-4 py-4 text-sm text-slate-300">
              <p>LiteLLM reads API keys from environment variables. Use secrets only for executor-side tool sandboxes.</p>
              {#if providerForm.preset !== 'ollama'}
                <div class="mt-3 flex flex-wrap gap-2">
                  <Button type="button" size="sm" variant="secondary" onclick={prefillSecretForPreset}>Prefill matching secret</Button>
                </div>
              {/if}
            </div>
          {/if}

          <div class="mt-5 flex flex-wrap gap-2">
            <Button onclick={saveProvider} disabled={!isAdmin || busy}>{selectedProviderId ? 'Save provider' : 'Create provider'}</Button>
            <Button variant="secondary" onclick={resetProviderForm} disabled={busy}>Reset</Button>
            {#if selectedProviderId}
              <Button variant="secondary" onclick={() => testProvider(selectedProviderId)} disabled={!isAdmin || busy}>Test provider</Button>
              <Button variant="danger" onclick={() => deleteProvider(selectedProviderId)} disabled={!isAdmin || busy}>Delete</Button>
            {/if}
          </div>

          {#if providerTestResult}
            <div class={`mt-4 rounded-2xl border px-4 py-3 text-sm ${providerTestResult.ok ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100' : 'border-rose-500/30 bg-rose-500/10 text-rose-100'}`}>
              {#if providerTestResult.ok}
                <p>Resolved model: {providerTestResult.model_resolved}</p>
                <p class="mt-1">Latency: {providerTestResult.latency_ms} ms</p>
              {:else}
                <p>{providerTestResult.error_detail}</p>
              {/if}
            </div>
          {/if}
        </Card>
      </div>
    {:else if activeTab === 'routing'}
      <Card class="p-5">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Model routing</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Task-type routing</h2>
          </div>
          <Button variant="secondary" onclick={copyDefaultModelToAll}>Use default for all</Button>
        </div>

        <div class="mt-4 grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>default</span>
            <select bind:value={routingForm.default} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Use provider default</option>
              {#each modelOptions() as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
            <span class="block text-xs text-slate-400">Main chat and task execution.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>classifier</span>
            <select bind:value={routingForm.classifier} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Use provider default</option>
              {#each modelOptions() as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
            <span class="block text-xs text-slate-400">Decision engine / fast model.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>compaction</span>
            <select bind:value={routingForm.compaction} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Use provider default</option>
              {#each modelOptions() as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
            <span class="block text-xs text-slate-400">Context compaction summaries.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>simple_inline</span>
            <select bind:value={routingForm.simple_inline} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Use provider default</option>
              {#each modelOptions() as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
            <span class="block text-xs text-slate-400">Short inline responses / fast model.</span>
          </label>
        </div>

        {#if routingWarnings().length > 0}
          <div class="mt-4 rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
            {#each routingWarnings() as warning}
              <p>{warning}</p>
            {/each}
          </div>
        {/if}

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Additional task routes (JSON)</span>
          <textarea bind:value={routingForm.extraJson} class="min-h-[180px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
        </label>
        <div class="mt-5 flex justify-end">
          <Button onclick={saveRouting} disabled={!isAdmin || busy}>Save routing</Button>
        </div>
      </Card>
    {:else if activeTab === 'secrets'}
      <div class="grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="p-5">
          <div class="space-y-4">
            <p class="text-sm leading-6 text-slate-400">
              Use encrypted secrets for tool execution sandboxes. LLM provider API keys are typically read from environment variables before Cognis starts.
            </p>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Name</span><Input bind:value={secretForm.name} /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Value</span><Input bind:value={secretForm.value} type="password" placeholder="write-only secret" /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Scope</span><Input bind:value={secretForm.scope} /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Agent ID</span><Input bind:value={secretForm.agent_id} /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Description</span><Input bind:value={secretForm.description} /></label>
            <Button class="w-full justify-center" onclick={saveSecret} disabled={busy}>Save secret</Button>
          </div>
        </Card>
        <Card class="p-5">
          <div class="space-y-3">
            {#each secrets as secret}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                <div>
                  <p class="font-medium text-white">{secret.name}</p>
                  <p class="text-xs text-slate-400">{secret.scope}{secret.agent_id ? ` · ${secret.agent_id}` : ''}</p>
                </div>
                <Button size="sm" variant="danger" onclick={() => deleteSecret(secret)} disabled={busy}>Delete</Button>
              </div>
            {/each}
          </div>
        </Card>
      </div>
    {:else if activeTab === 'system'}
      <div class="space-y-5">
        {#if diagnostics}
          <Card class="p-5">
            <div class="flex items-center justify-between gap-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Getting started</p>
                <h2 class="mt-1 text-lg font-semibold text-white">Readiness checklist</h2>
              </div>
              <Button variant="secondary" onclick={() => goto('/getting-started')}>Open guide</Button>
            </div>
            <div class="mt-4 grid gap-3 md:grid-cols-2">
              {#each deriveGettingStartedSteps(diagnostics) as step}
                <a class="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-4" href={step.href}>
                  <div class="flex items-center justify-between gap-3">
                    <p class="font-medium text-white">{step.label}</p>
                    <ProviderStatusBadge status={step.done ? 'healthy' : 'degraded'} />
                  </div>
                  <p class="mt-2 text-sm text-slate-400">{step.description}</p>
                </a>
              {/each}
            </div>
          </Card>

          <div class="grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
            <Card class="p-5">
              <div class="space-y-3">
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Health</p>
                {#if health}
                  {#each Object.entries(health.providers) as [name, provider]}
                    <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                      <span class="font-medium text-white">{name}</span>
                      <ProviderStatusBadge status={provider.status} />
                    </div>
                  {/each}
                {/if}
              </div>
            </Card>

            <Card class="p-5">
              <div class="grid gap-4 md:grid-cols-2">
                <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-4">
                  <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Database</p>
                  <p class="mt-2 text-sm text-slate-200">{String(diagnostics.database.drivername ?? '')}</p>
                  <p class="mt-1 text-xs text-slate-400">Migration: {String(diagnostics.database.migration_version ?? 'unknown')}</p>
                </div>
                <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-4">
                  <p class="text-xs uppercase tracking-[0.25em] text-slate-400">JWT key fingerprint</p>
                  <p class="mt-2 text-sm text-slate-200">{diagnostics.key_fingerprint ?? 'unavailable'}</p>
                </div>
              </div>

              <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
                <div class="flex items-center justify-between gap-3">
                  <p class="font-medium text-white">Configuration summary</p>
                  <Button size="sm" variant="secondary" onclick={() => copyToClipboard(diagnosticsEnvBlock())}>Copy env block</Button>
                </div>
                <pre class="mt-3 whitespace-pre-wrap text-xs text-slate-300">{diagnosticsEnvBlock()}</pre>
              </div>

              <div class="mt-4 grid gap-3 md:grid-cols-2">
                {#each groupedSettings() as setting}
                  <button class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-left text-slate-200" onclick={() => selectSetting(setting)}>
                    <p class="font-medium">{setting.key}</p>
                    <p class="mt-1 text-xs text-slate-400">{setting.category}</p>
                  </button>
                {/each}
              </div>

              {#if selectedSettingKey}
                <div class="mt-4 space-y-3 border-t border-slate-800 pt-4">
                  <p class="text-sm font-medium text-white">Edit {selectedSettingKey}</p>
                  <textarea bind:value={settingValueText} class="min-h-[220px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
                  <Button onclick={saveSetting} disabled={!isAdmin || busy}>Save setting</Button>
                </div>
              {/if}
            </Card>
          </div>
        {/if}
      </div>
    {:else}
      <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_420px]">
        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Account</p>
              <h2 class="mt-1 text-lg font-semibold text-white">{auth.getSnapshot().user?.name ?? auth.getSnapshot().user?.email}</h2>
              <p class="text-sm text-slate-400">{auth.getSnapshot().user?.email}</p>
            </div>
            <div class="grid gap-4 md:grid-cols-2">
              <label class="space-y-2 text-sm font-medium text-slate-200"><span>Current password</span><Input bind:value={passwordForm.current_password} type="password" /></label>
              <label class="space-y-2 text-sm font-medium text-slate-200"><span>New password</span><Input bind:value={passwordForm.new_password} type="password" /></label>
              <label class="space-y-2 text-sm font-medium text-slate-200 md:col-span-2"><span>Confirm new password</span><Input bind:value={passwordForm.confirm_password} type="password" /></label>
            </div>
            <div class="flex flex-wrap gap-2">
              <Button onclick={changePassword} disabled={busy}>Change password</Button>
              <Button variant="secondary" onclick={() => openTargetUi('intaris')}>Open Intaris</Button>
              <Button variant="secondary" onclick={() => openTargetUi('mnemory')}>Open Mnemory</Button>
              <Button variant="danger" onclick={async () => { await auth.logout(); await goto('/login'); }}>Sign out</Button>
            </div>
          </div>
        </Card>

        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">API keys</p>
              <h2 class="mt-1 text-lg font-semibold text-white">Programmatic access</h2>
            </div>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Name</span><Input bind:value={newApiKeyName} placeholder="CI key" /></label>
            <label class="space-y-2 text-sm font-medium text-slate-200"><span>Expires in days (optional)</span><Input bind:value={newApiKeyExpiresInDays} type="number" min="1" /></label>
            <Button class="w-full justify-center" onclick={createApiKey} disabled={busy || !newApiKeyName}>Create API key</Button>

            {#if createdApiKey}
              <div class="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-4 text-sm text-emerald-100">
                <p class="font-medium">Copy this key now</p>
                <pre class="mt-2 whitespace-pre-wrap break-all text-xs">{createdApiKey.api_key}</pre>
                <div class="mt-3 flex justify-end">
                  <Button size="sm" variant="secondary" onclick={() => copyToClipboard(createdApiKey?.api_key ?? '')}>Copy</Button>
                </div>
              </div>
            {/if}

            {#each apiKeys as apiKey}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                <div>
                  <p class="font-medium text-white">{apiKey.name}</p>
                  <p class="text-xs text-slate-400">{apiKey.prefix}</p>
                  <p class="text-xs text-slate-500">Last used: {apiKey.last_used_at ?? 'never'}</p>
                </div>
                <Button size="sm" variant="danger" onclick={() => revokeApiKey(apiKey.key_id)} disabled={busy}>Revoke</Button>
              </div>
            {/each}
          </div>
        </Card>
      </div>
    {/if}
  </section>
{/if}
