<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import { deriveGettingStartedSteps } from '$lib/getting-started';
  import { collectModelOptions, createProviderForm, presetHasBaseUrl, presetNeedsAuth, PRESET_LABELS, providerFormToPayload, type ProviderFormState, type ProviderPreset } from '$lib/providers';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ProviderStatusBadge from '$lib/components/ProviderStatusBadge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { getIntarisUiUrl, getMnemoryUiUrl } from '$lib/config';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { blockNavigationIfDirty, installBeforeUnloadGuard } from '$lib/navigation/unsaved';
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
  let showSecretModal = false;
  let secretModalName = '';
  let secretModalValue = '';
  let agents: Array<{ agent_id: string; name: string }> = [];
  let apiKeys: ApiKeyMetadata[] = [];
  let createdApiKey: ApiKeyCreateResponse | null = null;
  let newApiKeyName = '';
  let newApiKeyExpiresInDays = '';
  let initialSnapshot = '';

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

  function snapshotState(): string {
    return JSON.stringify({
      providerForm,
      routingForm,
      secretForm,
      passwordForm,
      newApiKeyName,
      newApiKeyExpiresInDays,
      settingValueText
    });
  }

  function isDirty(): boolean {
    return snapshotState() !== initialSnapshot;
  }

  beforeNavigate((navigation) => {
    if (busy) {
      return;
    }
    blockNavigationIfDirty(navigation, isDirty);
  });

  async function confirmDiscardChanges(): Promise<boolean> {
    if (!isDirty()) {
      return true;
    }
    return confirmAction({
      title: 'Discard unsaved changes?',
      message: 'Switching tabs or providers will replace the current unsaved edits.',
      confirmLabel: 'Discard changes'
    });
  }

  function syncTabFromUrl(): void {
    const url = new URL(window.location.href);
    const tab = url.searchParams.get('tab');
    if (tab && tabs.includes(tab as SettingsTab)) {
      activeTab = tab as SettingsTab;
    }
  }

  async function setActiveTab(tab: SettingsTab): Promise<void> {
    if (!(await confirmDiscardChanges())) {
      return;
    }
    activeTab = tab;
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    window.history.replaceState({}, '', url);
    initialSnapshot = snapshotState();
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

  function applySelectedProvider(provider: LLMProvider): void {
    selectedProviderId = provider.provider_id;
    providerForm = createProviderForm(provider);
    providerTestResult = provider.last_test;
  }

  async function selectProvider(provider: LLMProvider): Promise<void> {
    if (!(await confirmDiscardChanges())) {
      return;
    }
    applySelectedProvider(provider);
    initialSnapshot = snapshotState();
  }

  function clearProviderSelection(): void {
    selectedProviderId = '';
    providerForm = createProviderForm();
    providerTestResult = null;
  }

  async function resetProviderForm(): Promise<void> {
    if (!(await confirmDiscardChanges())) {
      return;
    }
    clearProviderSelection();
    initialSnapshot = snapshotState();
  }

  async function discoverModels(): Promise<void> {
    busy = true;
    error = '';
    try {
      let models: Array<{ model_id: string; name: string }>;
      if (selectedProviderId) {
        const result = await api.llmProviders.discoverModels(selectedProviderId);
        models = result.models;
      } else {
        // Preview mode: pass form values directly
        const result = await api.llmProviders.discoverModelsPreview({
          preset: providerForm.preset,
          base_url: providerForm.base_url,
          ...(providerForm.auth_mode === 'secret' && providerForm.auth_secret_name
            ? { secret_name: providerForm.auth_secret_name }
            : {}),
          ...(providerForm.auth_mode === 'env' && providerForm.auth_env_var
            ? {}
            : {})
        });
        models = result.models;
      }
      providerForm.discovered_models = models;
      addToast(`Discovered ${models.length} models.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Model discovery failed');
    } finally {
      busy = false;
    }
  }

  function openSecretModal(): void {
    secretModalName = providerForm.auth_secret_name || `${providerForm.preset}_api_key`;
    secretModalValue = '';
    showSecretModal = true;
  }

  async function saveSecretFromModal(): Promise<void> {
    if (!secretModalName.trim() || !secretModalValue.trim()) {
      error = 'Secret name and value are required.';
      return;
    }
    busy = true;
    error = '';
    try {
      await api.secrets.upsert({
        name: secretModalName,
        value: secretModalValue,
        scope: 'global',
        agent_id: null,
        description: `API key for provider ${providerForm.display_name || providerForm.provider_id}`
      });
      providerForm.auth_secret_name = secretModalName;
      secretModalValue = '';
      showSecretModal = false;
      // Refresh secrets list
      secrets = await api.secrets.list();
      addToast('Credential saved to encrypted store.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save credential');
    } finally {
      busy = false;
    }
  }

  const presetOptions: ProviderPreset[] = ['openai', 'openai_compatible', 'anthropic', 'ollama', 'custom'];

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
      [providers, diagnostics, agents] = await Promise.all([
        api.llmProviders.list().then((page) => page.items),
        api.system.diagnostics(),
        api.agents.list().then((page) => page.items.map((a) => ({ agent_id: a.agent_id, name: a.name }))),
      ]);
    } else {
      providers = [];
      diagnostics = null;
      agents = [];
    }

    if (selectedProviderId) {
      const next = providers.find((provider) => provider.provider_id === selectedProviderId);
      if (next) {
        applySelectedProvider(next);
      }
    }
    initialSnapshot = snapshotState();
  }

  async function loadSettings(): Promise<void> {
    loading = true;
    error = '';
    notice = '';
    try {
      await refreshPageState();
      syncTabFromUrl();
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function saveProvider(): Promise<void> {
    if (!providerForm.provider_id.trim() || !providerForm.display_name.trim()) {
      error = 'Provider ID and display name are required.';
      return;
    }
    if (providerForm.preset === 'custom') {
      try {
        JSON.parse(providerForm.custom_json || '{}');
      } catch {
        error = 'Provider config JSON must be valid.';
        return;
      }
    }
    busy = true;
    error = '';
    notice = '';
    try {
      const payload = providerFormToPayload(providerForm);
      if (selectedProviderId) {
        await api.llmProviders.update(selectedProviderId, payload);
      } else {
        await api.llmProviders.create(payload);
        selectedProviderId = providerForm.provider_id;
      }
      await refreshPageState();
      notice = 'Provider saved.';
      addToast('Provider saved.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save provider');
    } finally {
      busy = false;
    }
  }

  async function deleteProvider(providerId: string): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Delete provider?',
      message: 'This removes the configured provider from Cognis.',
      confirmLabel: 'Delete provider'
    });
    if (!confirmed) {
      return;
    }
    busy = true;
    error = '';
    try {
      await api.llmProviders.remove(providerId);
      clearProviderSelection();
      await refreshPageState();
      notice = 'Provider removed.';
      addToast('Provider removed.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to delete provider');
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
      addToast(notice, result.ok ? 'success' : 'warning');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to test provider');
    } finally {
      busy = false;
    }
  }

  async function saveRouting(): Promise<void> {
    try {
      JSON.parse(routingForm.extraJson || '{}');
    } catch {
      error = 'Additional task routes must be valid JSON.';
      return;
    }
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
      addToast('Model routing updated.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save routing');
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
    if (!secretForm.name.trim() || !secretForm.value.trim()) {
      error = 'Secret name and value are required.';
      return;
    }
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
      addToast('Secret saved.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save secret');
    } finally {
      busy = false;
    }
  }

  async function deleteSecret(secret: SecretMetadata): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Delete secret?',
      message: `Delete ${secret.name}? The secret value cannot be recovered after deletion.`,
      confirmLabel: 'Delete secret'
    });
    if (!confirmed) {
      return;
    }
    busy = true;
    error = '';
    try {
      await api.secrets.remove(secret.name, secret.scope, secret.agent_id);
      secrets = await api.secrets.list();
      notice = 'Secret deleted.';
      addToast('Secret deleted.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to delete secret');
    } finally {
      busy = false;
    }
  }

  function selectSetting(setting: Setting): void {
    selectedSettingKey = setting.key;
    settingValueText = JSON.stringify(setting.value, null, 2);
    initialSnapshot = snapshotState();
  }

  async function saveSetting(): Promise<void> {
    try {
      JSON.parse(settingValueText);
    } catch {
      error = 'Setting value must be valid JSON.';
      return;
    }
    busy = true;
    error = '';
    try {
      await api.settings.update(selectedSettingKey, JSON.parse(settingValueText));
      settings = await api.settings.list();
      notice = 'Setting updated.';
      addToast('Setting updated.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save setting');
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
      addToast(error, 'error', 4_000, 'Unable to open linked UI');
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
      addToast('Password updated.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to change password');
    } finally {
      busy = false;
    }
  }

  async function createApiKey(): Promise<void> {
    if (!newApiKeyName.trim()) {
      error = 'API key name is required.';
      return;
    }
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
      addToast('API key created. Copy it now.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to create API key');
    } finally {
      busy = false;
    }
  }

  async function revokeApiKey(keyId: string): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Revoke API key?',
      message: 'The selected API key will stop working immediately.',
      confirmLabel: 'Revoke key'
    });
    if (!confirmed) {
      return;
    }
    busy = true;
    error = '';
    try {
      await api.auth.revokeApiKey(keyId);
      apiKeys = await api.auth.listApiKeys();
      notice = 'API key revoked.';
      addToast('API key revoked.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to revoke API key');
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    const cleanup = installBeforeUnloadGuard(isDirty);
    void loadSettings();
    return cleanup;
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

        <Card class="space-y-5 p-5">
          <!-- Identity -->
          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Provider ID <span class="text-rose-300">*</span></span>
              <Input bind:value={providerForm.provider_id} disabled={!!selectedProviderId} placeholder="default" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Display name <span class="text-rose-300">*</span></span>
              <Input bind:value={providerForm.display_name} placeholder="My OpenAI" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Provider type</span>
              <select bind:value={providerForm.preset} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                {#each presetOptions as preset}
                  <option value={preset}>{PRESET_LABELS[preset]}</option>
                {/each}
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
            <!-- Custom: raw JSON -->
            <label class="block space-y-2 text-sm font-medium text-slate-200">
              <span>Config JSON</span>
              <textarea bind:value={providerForm.custom_json} class="min-h-[240px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
            </label>
          {:else}
            <!-- Credentials -->
            {#if presetNeedsAuth(providerForm.preset)}
              <div class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Credentials</p>
                <div class="mt-3 grid gap-3 md:grid-cols-2">
                  <label class="space-y-2 text-sm font-medium text-slate-200">
                    <span>Auth mode</span>
                    <select bind:value={providerForm.auth_mode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                      <option value="env">Environment variable</option>
                      <option value="secret">Credential store</option>
                    </select>
                  </label>

                  {#if providerForm.auth_mode === 'env'}
                    <label class="space-y-2 text-sm font-medium text-slate-200">
                      <span>Env variable name</span>
                      <Input bind:value={providerForm.auth_env_var} placeholder="OPENAI_API_KEY" />
                      <span class="block text-xs text-slate-400">Must be set before starting Cognis.</span>
                    </label>
                  {:else}
                    <div class="space-y-2 text-sm font-medium text-slate-200">
                      <span>Credential</span>
                      <div class="flex gap-2">
                        <select bind:value={providerForm.auth_secret_name} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                          <option value="">Select credential...</option>
                          {#each secrets.filter((s) => s.scope === 'global' || s.scope === 'user') as secret}
                            <option value={secret.name}>{secret.name}{secret.description ? ` — ${secret.description}` : ''}</option>
                          {/each}
                        </select>
                        <Button size="sm" variant="secondary" onclick={openSecretModal}>New</Button>
                      </div>
                      {#if providerForm.auth_secret_name}
                        <span class="block text-xs text-slate-400">Using credential: {providerForm.auth_secret_name}</span>
                      {:else}
                        <span class="block text-xs text-amber-300">No credential selected. Create or select one.</span>
                      {/if}
                    </div>
                  {/if}
                </div>
              </div>
            {/if}

            <!-- Connection -->
            {#if presetHasBaseUrl(providerForm.preset)}
              <label class="block space-y-2 text-sm font-medium text-slate-200">
                <span>Base URL {#if providerForm.preset !== 'ollama'}<span class="text-rose-300">*</span>{/if}</span>
                <Input bind:value={providerForm.base_url} placeholder={providerForm.preset === 'ollama' ? 'http://localhost:11434' : 'https://your-provider.example.com/v1'} />
              </label>
            {/if}

            <!-- Models -->
            <div class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
              <div class="flex items-center justify-between gap-3">
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Models</p>
                <Button size="sm" variant="secondary" onclick={discoverModels} disabled={busy}>
                  Discover models
                </Button>
              </div>

              {#if providerForm.discovered_models.length > 0}
                <div class="mt-3 max-h-48 overflow-y-auto rounded-xl border border-slate-800 bg-slate-950/80 p-2">
                  <div class="space-y-1">
                    {#each providerForm.discovered_models as m}
                      <button type="button" class="w-full rounded-lg px-3 py-1.5 text-left text-xs text-slate-200 transition hover:bg-slate-800" onclick={() => (providerForm.default_model = m.model_id)}>
                        {m.model_id}
                      </button>
                    {/each}
                  </div>
                </div>
                <p class="mt-2 text-xs text-slate-400">Click a model to set it as default.</p>
              {/if}

              <div class="mt-3">
                <label class="space-y-2 text-sm font-medium text-slate-200">
                  <span>Default model <span class="text-rose-300">*</span></span>
                  <Input bind:value={providerForm.default_model} placeholder="model id (type or pick from discovered)" />
                </label>
              </div>

              <label class="mt-3 block space-y-2 text-sm font-medium text-slate-200">
                <span>Additional models (one per line, optional)</span>
                <textarea bind:value={providerForm.additional_models} class="min-h-[80px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100" placeholder="gpt-4o&#10;gpt-4o-mini"></textarea>
              </label>
            </div>
          {/if}

          <!-- Actions -->
          <div class="flex flex-wrap gap-2 border-t border-slate-800 pt-4">
            <Button onclick={saveProvider} disabled={!isAdmin || busy}>{selectedProviderId ? 'Save provider' : 'Create provider'}</Button>
            <Button variant="secondary" onclick={resetProviderForm} disabled={busy}>Reset</Button>
            {#if selectedProviderId}
              <Button variant="secondary" onclick={() => testProvider(selectedProviderId)} disabled={!isAdmin || busy}>Test provider</Button>
              <Button variant="danger" onclick={() => deleteProvider(selectedProviderId)} disabled={!isAdmin || busy}>Delete</Button>
            {/if}
          </div>

          {#if providerTestResult}
            <div class={`rounded-2xl border px-4 py-3 text-sm ${providerTestResult.ok ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100' : 'border-rose-500/30 bg-rose-500/10 text-rose-100'}`}>
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
              Encrypted secrets for tool execution sandboxes and LLM provider credentials.
            </p>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Name <span class="text-rose-300">*</span></span>
              <Input bind:value={secretForm.name} placeholder="openai_api_key" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Value <span class="text-rose-300">*</span></span>
              <Input bind:value={secretForm.value} type="password" placeholder="write-only secret" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Scope</span>
              <select bind:value={secretForm.scope} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="global">Global (all users and agents)</option>
                <option value="user">User (current user only)</option>
                <option value="agent">Agent-specific</option>
              </select>
            </label>
            {#if secretForm.scope === 'agent'}
              <label class="space-y-2 text-sm font-medium text-slate-200">
                <span>Agent</span>
                <select bind:value={secretForm.agent_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                  <option value="">Select agent...</option>
                  {#each agents as agent}
                    <option value={agent.agent_id}>{agent.name} ({agent.agent_id})</option>
                  {/each}
                </select>
              </label>
            {/if}
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Description</span>
              <Input bind:value={secretForm.description} placeholder="What this secret is for" />
            </label>
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

<!-- Secret creation modal -->
{#if showSecretModal}
  <div class="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/80 backdrop-blur" role="dialog" aria-modal="true">
    <div class="w-full max-w-md rounded-3xl border border-slate-800 bg-slate-950 p-6 shadow-card">
      <p class="text-xs uppercase tracking-[0.25em] text-slate-400">New credential</p>
      <h3 class="mt-1 text-lg font-semibold text-white">Create API key secret</h3>
      <div class="mt-4 space-y-4">
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Secret name</span>
          <Input bind:value={secretModalName} placeholder="openai_api_key" />
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>API key value</span>
          <Input bind:value={secretModalValue} type="password" placeholder="sk-..." />
        </label>
      </div>
      <div class="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onclick={() => (showSecretModal = false)}>Cancel</Button>
        <Button onclick={saveSecretFromModal} disabled={busy || !secretModalName.trim() || !secretModalValue.trim()}>Save credential</Button>
      </div>
    </div>
  </div>
{/if}
