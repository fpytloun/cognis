<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import type { MCPEnvVar } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import { deriveGettingStartedSteps } from '$lib/getting-started';
  import { collectModelOptions, createProviderForm, deriveProviderId, presetHasBaseUrl, presetNeedsAuth, PRESET_LABELS, providerFormToPayload, type ProviderFormState, type ProviderPreset } from '$lib/providers';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ProviderStatusBadge from '$lib/components/ProviderStatusBadge.svelte';
  import EnvVarEditor from '$lib/components/settings/EnvVarEditor.svelte';
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
    ExecutorConfig,
    ExecutorTokenResponse,
    HealthResponse,
    LLMProvider,
    ModelRouting,
    ProviderTestResult,
    SecretMetadata,
    Setting,
    SettingsCategory,
    SystemDiagnostics,
    MCPServerConfigResponse,
    ToolDefinitionSummary,
    UserDetail,
    UserRole,
    WebConfigStatus
  } from '$lib/types/api';

  type SettingsTab = 'providers' | 'routing' | 'secrets' | 'web' | 'tools' | 'executors' | 'users' | 'system' | 'account';

  const ALL_TABS: SettingsTab[] = ['providers', 'routing', 'secrets', 'web', 'tools', 'executors', 'users', 'system', 'account'];
  const TAB_LABELS: Record<SettingsTab, string> = {
    providers: 'providers',
    routing: 'routing',
    secrets: 'secrets',
    web: 'web search',
    tools: 'tools',
    executors: 'executors',
    users: 'users',
    system: 'system',
    account: 'account'
  };
  let activeTab: SettingsTab = 'providers';
  let loading = true;
  let busy = false;
  let error = '';
  let notice = '';
  let settings: SettingsCategory[] = [];
  let providers: LLMProvider[] = [];
  let modelRouting: ModelRouting = { default: null, classifier: null, compaction: null, simple_inline: null, image_generation: null, items: {} };
  let secrets: SecretMetadata[] = [];
  let health: HealthResponse | null = null;
  let diagnostics: SystemDiagnostics | null = null;
  let executorConfigs: ExecutorConfig[] = [];
  let executorTools: ToolDefinitionSummary[] = [];
  let editingExecutor: ExecutorConfig | null = null;
  let webConfig: WebConfigStatus = { backend: 'direct', tavily_configured: false, brave_configured: false, available_backends: ['direct'] };
  let webBackendForm = 'direct';
  let webKeySetup: { backend: string; value: string } | null = null;
  let showExecutorForm = false;
  let executorForm = { executor_id: '', name: '', executor_type: 'in_process', labels: '', status: 'active' };
  let executorToken: ExecutorTokenResponse | null = null;
  let mcpServerConfigs: MCPServerConfigResponse[] = [];
  let showMcpForm = false;
  let editingMcpServer: MCPServerConfigResponse | null = null;
  let mcpForm = { name: '', transport: 'stdio', command: '', url: '', args: '', envVars: [] as MCPEnvVar[], timeout_seconds: 30, description: '' };
  let isAdmin = false;
  let tabs = $derived(isAdmin ? ALL_TABS : ALL_TABS.filter((t) => t !== 'users' && t !== 'system'));
  let selectedProviderId = '';
  let selectedSettingKey = '';
  let settingValueText = '';
  let providerForm: ProviderFormState = createProviderForm();
  let providerTestResult: ProviderTestResult | null = null;
  let showSecretModal = false;
  let secretModalTarget: 'provider' | 'mcp' = 'provider';
  let mcpSecretTargetKey = '';
  let secretModalName = '';
  let secretModalValue = '';
  let agents: Array<{ agent_id: string; name: string; is_system?: boolean }> = [];
  let apiKeys: ApiKeyMetadata[] = [];
  let createdApiKey: ApiKeyCreateResponse | null = null;
  let newApiKeyName = '';
  let newApiKeyExpiresInDays = '';
  let initialSnapshot = '';

  // User management state
  let userList: UserDetail[] = [];
  let showUserCreateModal = false;
  let showUserEditModal = false;
  let showDisabledUsers = false;
  let editingUser: UserDetail | null = null;
  let userCreateForm = { email: '', name: '', password: '', confirm_password: '', role: 'user' as UserRole };
  let userEditForm = { name: '', role: 'user' as UserRole };
  let accountNameForm = '';
  let accountNameDirty = false;
  let executorPollTimer: ReturnType<typeof setInterval> | null = null;

  let routingForm = {
    default: '',
    classifier: '',
    compaction: '',
    simple_inline: '',
    image_generation: '',
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

  function settingBool(key: string, fallback = true): boolean {
    const item = groupedSettings().find((setting) => setting.key === key);
    return typeof item?.value === 'boolean' ? item.value : fallback;
  }

  async function toggleBooleanSetting(key: string, nextValue: boolean): Promise<void> {
    try {
      await api.settings.update(key, nextValue);
      await refreshPageState();
      addToast(`Updated ${key}.`, 'success');
    } catch (e) {
      error = asApiError(e).message;
    }
  }

  function selectedProvider(): LLMProvider | null {
    return providers.find((provider) => provider.provider_id === selectedProviderId) ?? null;
  }

  function modelOptions(): Array<{ value: string; label: string; providerId: string }> {
    return collectModelOptions(providers);
  }

  function executorSelectorFor(labels: Record<string, string> | null | undefined): string {
    return Object.entries(labels || {}).map(([k, v]) => `${k}=${v}`).join(', ');
  }

  function routingWarnings(): string[] {
    const knownModels = new Set(modelOptions().map((item) => item.value));
    return [routingForm.default, routingForm.classifier, routingForm.compaction, routingForm.simple_inline, routingForm.image_generation]
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
            ? { env_var: providerForm.auth_env_var }
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
    secretModalTarget = 'provider';
    secretModalName = providerForm.auth_secret_name || `${providerForm.preset}_api_key`;
    secretModalValue = '';
    showSecretModal = true;
  }

  function openMcpSecretModal(key: string): void {
    secretModalTarget = 'mcp';
    mcpSecretTargetKey = key;
    secretModalName = key ? key.toLowerCase() : 'mcp_secret';
    secretModalValue = '';
    showSecretModal = true;
  }

  function parseMcpEnvVars(env: Record<string, string>): MCPEnvVar[] {
    return Object.entries(env).map(([key, value]) => ({
      key,
      value: value.startsWith('$secret:') ? value.slice('$secret:'.length) : value,
      type: value.startsWith('$secret:') ? 'secret' : 'literal'
    }));
  }

  function serializeMcpEnvVars(envVars: MCPEnvVar[]): Record<string, string> {
    return Object.fromEntries(
      envVars
        .filter((entry) => entry.key.trim() && entry.value.trim())
        .map((entry) => [entry.key.trim(), entry.type === 'secret' ? `$secret:${entry.value.trim()}` : entry.value.trim()])
    );
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
        description: secretModalTarget === 'provider'
          ? `API key for provider ${providerForm.display_name || providerForm.provider_id}`
          : `MCP environment secret for ${mcpSecretTargetKey || 'variable'}`
      });
      if (secretModalTarget === 'provider') {
        providerForm.auth_secret_name = secretModalName;
      } else {
        mcpForm.envVars = mcpForm.envVars.map((entry) =>
          entry.key === mcpSecretTargetKey ? { ...entry, type: 'secret', value: secretModalName } : entry
        );
      }
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

  async function setDefaultProvider(): Promise<void> {
    if (!selectedProviderId) {
      return;
    }
    busy = true;
    error = '';
    try {
      await api.llmProviders.setDefault(selectedProviderId);
      await refreshPageState();
      addToast('Default provider set.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to set default');
    } finally {
      busy = false;
    }
  }

  const presetOptions: ProviderPreset[] = ['openai', 'openai_compatible', 'anthropic', 'ollama', 'litellm_proxy', 'custom'];

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
      image_generation: modelRouting.image_generation ?? '',
      extraJson: JSON.stringify(modelRouting.items, null, 2)
    };

    webConfig = await api.webConfig.status().catch(() => webConfig);
    webBackendForm = webConfig.backend;

    // Initialize account name form
    accountNameForm = auth.getSnapshot().user?.name ?? '';
    accountNameDirty = false;

    if (isAdmin) {
      [providers, diagnostics, agents, executorConfigs, executorTools, mcpServerConfigs] = await Promise.all([
        api.llmProviders.list().then((page) => page.items),
        api.system.diagnostics(),
        api.agents.list().then((page) => page.items.map((a) => ({ agent_id: a.agent_id, name: a.name, is_system: a.is_system }))),
        api.executor.list().catch(() => []),
        api.tools.executorTools().catch(() => []),
        api.tools.listMcpServerConfigs().catch(() => []),
      ]);
      await loadUsers();
    } else {
      providers = [];
      diagnostics = null;
      agents = [];
      executorConfigs = [];
      executorTools = [];
      userList = [];
    }

    if (selectedProviderId) {
      const next = providers.find((provider) => provider.provider_id === selectedProviderId);
      if (next) {
        applySelectedProvider(next);
      }
    }
    initialSnapshot = snapshotState();
  }

  function executorRuntimeBadgeStatus(executor: ExecutorConfig): 'healthy' | 'degraded' | 'unhealthy' {
    if (executor.status !== 'active') return 'degraded';
    if (executor.runtime_state === 'active') return 'healthy';
    if (executor.runtime_state === 'reconfiguring') return 'degraded';
    if (executor.runtime_state === 'blocked') return 'unhealthy';
    return 'unhealthy';
  }

  function executorRuntimeLabel(executor: ExecutorConfig): string {
    if (executor.status !== 'active') return 'disabled';
    if (executor.runtime_state === 'active') return 'connected';
    if (executor.runtime_state === 'reconfiguring') return 'reconfiguring';
    if (executor.runtime_state === 'blocked') return 'blocked';
    return 'offline';
  }

  function executorObservedNote(executor: ExecutorConfig): string | null {
    if (!executor.last_observed_at) return null;
    return `last seen ${new Date(executor.last_observed_at).toLocaleString()}`;
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
    if (!providerForm.display_name.trim()) {
      error = 'Display name is required.';
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
        const created = await api.llmProviders.create(payload);
        selectedProviderId = created.provider_id;
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
        image_generation: routingForm.image_generation || null,
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

  // -- Web config ---------------------------------------------------------------

  const WEB_BACKEND_INFO: Record<string, { label: string; description: string; link?: string }> = {
    direct: {
      label: 'Direct (DuckDuckGo)',
      description: 'Uses DuckDuckGo for search and direct HTTP for page fetching. Free, no API key needed. DuckDuckGo is community-maintained and may occasionally be unavailable.'
    },
    tavily: {
      label: 'Tavily',
      description: 'AI-optimized search with answer generation, content extraction, website crawling, and deep research. Unlocks web_crawl, web_map, and web_research tools.',
      link: 'https://tavily.com'
    },
    brave: {
      label: 'Brave Search',
      description: 'Search from Brave\'s index with freshness filters, extra snippets, and country targeting. Search only \u2014 page fetching uses direct HTTP.',
      link: 'https://brave.com/search/api/'
    }
  };

  async function saveWebBackend(): Promise<void> {
    busy = true;
    error = '';
    try {
      await api.settings.update('web.backend', webBackendForm);
      webConfig = await api.webConfig.status();
      notice = 'Web backend updated.';
      addToast('Web backend updated.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save web backend');
    } finally {
      busy = false;
    }
  }

  async function saveWebApiKey(): Promise<void> {
    if (!webKeySetup) return;
    busy = true;
    error = '';
    try {
      const secretName = webKeySetup.backend === 'tavily' ? 'tavily_api_key' : 'brave_api_key';
      await api.secrets.upsert({ name: secretName, value: webKeySetup.value, scope: 'global', description: `API key for ${WEB_BACKEND_INFO[webKeySetup.backend]?.label ?? webKeySetup.backend}` });
      secrets = await api.secrets.list();
      webConfig = await api.webConfig.status();
      webKeySetup = null;
      notice = 'API key saved.';
      addToast('API key saved.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save API key');
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

  // --- User management functions ---

  async function loadUsers(): Promise<void> {
    try {
      const page = await api.users.list(showDisabledUsers);
      userList = page.items;
    } catch {
      userList = [];
    }
  }

  function openCreateUserModal(): void {
    userCreateForm = { email: '', name: '', password: '', confirm_password: '', role: 'user' };
    showUserCreateModal = true;
  }

  function openEditUserModal(user: UserDetail): void {
    editingUser = user;
    userEditForm = { name: user.name ?? '', role: user.role };
    showUserEditModal = true;
  }

  async function createUserSubmit(): Promise<void> {
    if (userCreateForm.password !== userCreateForm.confirm_password) {
      addToast('Passwords do not match.', 'error');
      return;
    }
    busy = true;
    error = '';
    try {
      await api.users.create({
        email: userCreateForm.email,
        name: userCreateForm.name || undefined,
        password: userCreateForm.password,
        role: userCreateForm.role
      });
      showUserCreateModal = false;
      await loadUsers();
      addToast('User created.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to create user');
    } finally {
      busy = false;
    }
  }

  async function updateUserSubmit(): Promise<void> {
    if (!editingUser) return;
    busy = true;
    error = '';
    try {
      await api.users.update(editingUser.email, {
        name: userEditForm.name || undefined,
        role: userEditForm.role
      });
      showUserEditModal = false;
      editingUser = null;
      await loadUsers();
      addToast('User updated.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update user');
    } finally {
      busy = false;
    }
  }

  async function toggleUserActive(user: UserDetail): Promise<void> {
    if (user.is_active) {
      const confirmed = await confirmAction({
        title: 'Disable user?',
        message: `${user.email} will no longer be able to log in. Their data will be preserved.`,
        confirmLabel: 'Disable user',
        variant: 'danger'
      });
      if (!confirmed) return;
      busy = true;
      try {
        await api.users.disable(user.email);
        await loadUsers();
        addToast('User disabled.', 'success');
      } catch (caughtError) {
        addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to disable user');
      } finally {
        busy = false;
      }
    } else {
      busy = true;
      try {
        await api.users.enable(user.email);
        await loadUsers();
        addToast('User enabled.', 'success');
      } catch (caughtError) {
        addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to enable user');
      } finally {
        busy = false;
      }
    }
  }

  async function deleteUser(user: UserDetail): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Delete user permanently?',
      message: `This will permanently delete ${user.email} and all their data (conversations, agents, tasks). This cannot be undone.`,
      confirmLabel: 'Delete permanently',
      variant: 'danger'
    });
    if (!confirmed) return;
    busy = true;
    try {
      await api.users.remove(user.email);
      await loadUsers();
      addToast('User deleted.', 'success');
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to delete user');
    } finally {
      busy = false;
    }
  }

  async function saveAccountName(): Promise<void> {
    busy = true;
    error = '';
    try {
      const updated = await api.auth.updateProfile({ name: accountNameForm || null });
      auth.updateUser(updated);
      accountNameDirty = false;
      addToast('Name updated.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update name');
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    const cleanup = installBeforeUnloadGuard(isDirty);
    void loadSettings();
    return () => {
      if (executorPollTimer) clearInterval(executorPollTimer);
      cleanup();
    };
  });

  $effect(() => {
    if (executorPollTimer) {
      clearInterval(executorPollTimer);
      executorPollTimer = null;
    }
    if (!isAdmin || activeTab !== 'executors') return;
    executorPollTimer = setInterval(() => {
      void refreshPageState();
    }, 5000);
    return () => {
      if (executorPollTimer) {
        clearInterval(executorPollTimer);
        executorPollTimer = null;
      }
    };
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
        <Button variant={activeTab === tab ? 'primary' : 'secondary'} onclick={() => setActiveTab(tab)}>{TAB_LABELS[tab]}</Button>
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
                    <span class="font-medium text-slate-100">{provider.is_default ? '⭐ ' : ''}{provider.display_name}</span>
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
              <span>Name <span class="text-rose-300">*</span></span>
              <Input bind:value={providerForm.display_name} placeholder="My OpenAI" />
            </label>
            <div class="space-y-2 text-sm font-medium text-slate-200">
              <span>ID</span>
              {#if selectedProviderId}
                <Input value={providerForm.provider_id} disabled />
              {:else}
                <span class="block rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-400">{providerForm.provider_id || 'auto-generated from name'}</span>
              {/if}
            </div>
            {#if !selectedProviderId}
              <div class="md:col-span-2 text-xs text-slate-500">
                Provider ID will be auto-generated as <span class="font-mono text-slate-300">{deriveProviderId(providerForm.display_name) || 'unnamed'}</span>.
              </div>
            {/if}
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Provider type</span>
              <select bind:value={providerForm.preset} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                {#each presetOptions as preset}
                  <option value={preset}>{PRESET_LABELS[preset]}</option>
                {/each}
              </select>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Execution location</span>
              <select bind:value={providerForm.location} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="controller">Controller</option>
                <option value="executor">Via executor</option>
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

          {#if providerForm.location === 'executor'}
            <div class="rounded-2xl border border-sky-500/20 bg-sky-500/5 p-4 space-y-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-sky-200/80">Executor routing</p>
                <p class="mt-2 text-sm text-slate-300">This provider stays configured normally, but requests are executed from a matching remote executor instead of the controller.</p>
              </div>
              <label class="space-y-2 text-sm font-medium text-slate-200 block">
                <span>Executor selector (key=value, comma-separated)</span>
                <Input bind:value={providerForm.executor_selector} placeholder="location=local, tier=gpu" />
              </label>
              {#if executorConfigs.length > 0}
                <div class="flex flex-wrap gap-2">
                  <span class="text-xs text-slate-400 self-center">Use labels from:</span>
                  {#each executorConfigs.filter((executor) => executor.executor_type !== 'in_process') as executor}
                    <Button size="sm" variant="secondary" onclick={() => (providerForm.executor_selector = executorSelectorFor(executor.labels))}>{executor.name}</Button>
                  {/each}
                </div>
              {/if}
            </div>
          {/if}

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
                <Button size="sm" variant="secondary" onclick={discoverModels} disabled={busy || providerForm.location === 'executor'}>
                  Discover models
                </Button>
              </div>
              {#if providerForm.location === 'executor'}
                <p class="mt-3 text-xs text-slate-400">Model discovery runs from the controller. For executor-routed providers, enter models manually.</p>
              {/if}

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
              <Button variant="secondary" onclick={setDefaultProvider} disabled={!isAdmin || busy}>Set as default</Button>
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
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>image_generation</span>
            <input
              bind:value={routingForm.image_generation}
              class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500"
              placeholder="e.g. gpt-image-1, dall-e-3, gemini-2.0-flash-image-generation"
            />
            <span class="block text-xs text-slate-400">Image-capable model for avatars and tools. Must support image generation.</span>
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
                  {#each agents.filter((a) => !a.is_system) as agent}
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
    {:else if activeTab === 'web'}
      <div class="grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
        <!-- Left: backend selector -->
        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Web search</p>
              <h2 class="mt-1 text-lg font-semibold text-white">Default backend</h2>
            </div>
            <p class="text-sm leading-6 text-slate-400">
              Configure how agents search and fetch web content. The default backend is used unless the agent overrides it per-call.
            </p>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Backend</span>
              <select bind:value={webBackendForm} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="direct">{WEB_BACKEND_INFO.direct.label}</option>
                <option value="tavily" disabled={!webConfig.tavily_configured}>{WEB_BACKEND_INFO.tavily.label}{webConfig.tavily_configured ? '' : ' (not configured)'}</option>
                <option value="brave" disabled={!webConfig.brave_configured}>{WEB_BACKEND_INFO.brave.label}{webConfig.brave_configured ? '' : ' (not configured)'}</option>
              </select>
            </label>
            <p class="text-sm leading-6 text-slate-400">{WEB_BACKEND_INFO[webBackendForm]?.description ?? ''}</p>
            <Button class="w-full justify-center" onclick={saveWebBackend} disabled={!isAdmin || busy || webBackendForm === webConfig.backend}>Save backend</Button>
          </div>
        </Card>

        <!-- Right: backend status + key setup -->
        <Card class="p-5">
          <div class="space-y-4">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Backend status</p>

            <!-- Direct -->
            <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div>
                <p class="font-medium text-white">{WEB_BACKEND_INFO.direct.label}</p>
                <p class="text-xs text-slate-400">Always available, free</p>
              </div>
              <ProviderStatusBadge status="healthy" />
            </div>

            <!-- Tavily -->
            <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <p class="font-medium text-white">{WEB_BACKEND_INFO.tavily.label}</p>
                  <p class="text-xs text-slate-400">{webConfig.tavily_configured ? 'API key configured' : 'Not configured'}</p>
                </div>
                <div class="flex items-center gap-2">
                  {#if !webConfig.tavily_configured}
                    <Button size="sm" variant="secondary" onclick={() => { webKeySetup = { backend: 'tavily', value: '' }; }}>Setup</Button>
                  {/if}
                  <ProviderStatusBadge status={webConfig.tavily_configured ? 'healthy' : 'degraded'} />
                </div>
              </div>
            </div>

            <!-- Brave -->
            <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <p class="font-medium text-white">{WEB_BACKEND_INFO.brave.label}</p>
                  <p class="text-xs text-slate-400">{webConfig.brave_configured ? 'API key configured' : 'Not configured'}</p>
                </div>
                <div class="flex items-center gap-2">
                  {#if !webConfig.brave_configured}
                    <Button size="sm" variant="secondary" onclick={() => { webKeySetup = { backend: 'brave', value: '' }; }}>Setup</Button>
                  {/if}
                  <ProviderStatusBadge status={webConfig.brave_configured ? 'healthy' : 'degraded'} />
                </div>
              </div>
            </div>

            <p class="text-xs text-slate-500">
              Tavily-only tools (web_crawl, web_map, web_research) require a Tavily API key.
            </p>
          </div>

          <!-- Inline API key setup -->
          {#if webKeySetup}
            <div class="mt-4 space-y-3 border-t border-slate-800 pt-4">
              <p class="text-sm font-medium text-white">Configure {WEB_BACKEND_INFO[webKeySetup.backend]?.label ?? webKeySetup.backend}</p>
              <label class="space-y-2 text-sm font-medium text-slate-200">
                <span>API Key</span>
                <Input bind:value={webKeySetup.value} type="password" placeholder="Enter API key" />
              </label>
              {#if WEB_BACKEND_INFO[webKeySetup.backend]?.link}
                <p class="text-xs text-slate-400">
                  Get your key at <a href={WEB_BACKEND_INFO[webKeySetup.backend].link} target="_blank" rel="noopener noreferrer" class="text-blue-400 underline">{WEB_BACKEND_INFO[webKeySetup.backend].link}</a>
                </p>
              {/if}
              <div class="flex gap-2">
                <Button onclick={saveWebApiKey} disabled={busy || !webKeySetup.value}>Save key</Button>
                <Button variant="secondary" onclick={() => { webKeySetup = null; }}>Cancel</Button>
              </div>
            </div>
          {/if}
        </Card>
      </div>
    {:else if activeTab === 'executors'}
      <div class="space-y-5">
        <div class="flex items-center justify-between">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Executors</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Tool Execution</h2>
            <p class="mt-2 text-sm text-slate-400">
              Executors handle tool execution. Enable tools on each executor to make them available to agents.
            </p>
          </div>
          <Button variant="primary" size="sm" onclick={() => { executorForm = { executor_id: '', name: '', executor_type: 'websocket', labels: '', status: 'active' }; editingExecutor = null; executorToken = null; showExecutorForm = true; }}>New executor</Button>
        </div>

        {#if showExecutorForm}
          <Card class="p-5 space-y-4">
            <h3 class="text-lg font-medium text-white">{editingExecutor ? 'Edit Executor' : 'New Executor'}</h3>
            <div class="grid gap-4 md:grid-cols-2">
              <label class="space-y-1 text-sm text-slate-200">
                <span>Executor ID</span>
                <Input bind:value={executorForm.executor_id} placeholder="auto-generated if empty" disabled={!!editingExecutor} />
              </label>
              <label class="space-y-1 text-sm text-slate-200">
                <span>Name</span>
                <Input bind:value={executorForm.name} placeholder="e.g. Local Developer" />
              </label>
              <label class="space-y-1 text-sm text-slate-200">
                <span>Type</span>
                <select bind:value={executorForm.executor_type} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!!editingExecutor}>
                  <option value="websocket">websocket</option>
                  <option value="subprocess">subprocess</option>
                  <option value="in_process">in_process</option>
                </select>
              </label>
              <label class="space-y-1 text-sm text-slate-200">
                <span>Labels (key=value, comma-separated)</span>
                <Input bind:value={executorForm.labels} placeholder="tier=standard, gpu=false" />
              </label>
              {#if editingExecutor}
                <label class="space-y-1 text-sm text-slate-200">
                  <span>Status</span>
                  <select bind:value={executorForm.status} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    <option value="active">active</option>
                    <option value="disabled">disabled</option>
                  </select>
                </label>
              {/if}
            </div>
            <div class="flex gap-2 justify-end">
              <Button variant="secondary" size="sm" onclick={() => showExecutorForm = false}>Cancel</Button>
              <Button variant="primary" size="sm" disabled={!executorForm.name.trim()} onclick={async () => {
                const labels = Object.fromEntries(
                  executorForm.labels.split(',').map(s => s.trim()).filter(Boolean).map(s => {
                    const [k, ...v] = s.split('=');
                    return [k.trim(), v.join('=').trim()];
                  })
                );
                try {
                  if (editingExecutor) {
                    await api.executor.update(editingExecutor.executor_id, { name: executorForm.name, labels, status: executorForm.status });
                  } else {
                    await api.executor.create({ executor_id: executorForm.executor_id || null, name: executorForm.name, executor_type: executorForm.executor_type, labels });
                  }
                  showExecutorForm = false;
                  await refreshPageState();
                  addToast(editingExecutor ? 'Executor updated.' : 'Executor created.', 'success');
                } catch (e) { error = asApiError(e).message; }
              }}>{editingExecutor ? 'Update' : 'Create'}</Button>
            </div>
          </Card>
        {/if}

        {#each executorConfigs as exec}
          {@const toolGroups = [...new Set(executorTools.map(t => t.category))].sort()}
          <Card class="p-5 space-y-4">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-3">
                <h3 class="text-lg font-medium text-white">{exec.name}</h3>
                <span class="px-2 py-0.5 bg-zinc-700 text-zinc-300 text-xs font-mono rounded">{exec.executor_type}</span>
                <span class="px-2 py-0.5 rounded text-xs {exec.status === 'active' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-zinc-700 text-zinc-400'}">{exec.status}</span>
                <span class="inline-flex items-center gap-2 rounded px-2 py-0.5 text-xs">
                  <ProviderStatusBadge status={executorRuntimeBadgeStatus(exec)} />
                  <span class="text-slate-300">{executorRuntimeLabel(exec)}</span>
                </span>
                {#if exec.is_default}
                  <span class="px-2 py-0.5 bg-blue-500/20 text-blue-300 text-xs rounded">default</span>
                {/if}
              </div>
              <div class="flex gap-2">
                <Button variant="secondary" size="sm" onclick={() => {
                  editingExecutor = exec;
                  executorForm = {
                    executor_id: exec.executor_id,
                    name: exec.name,
                    executor_type: exec.executor_type,
                    labels: Object.entries(exec.labels || {}).map(([k, v]) => `${k}=${v}`).join(', '),
                    status: exec.status
                  };
                  showExecutorForm = true;
                }}>Edit</Button>
                {#if exec.executor_type !== 'in_process'}
                  <Button variant="secondary" size="sm" onclick={async () => {
                    try {
                      executorToken = await api.executor.generateToken(exec.executor_id);
                      addToast('Executor token generated. Copy it now.', 'success');
                    } catch (e) {
                      error = asApiError(e).message;
                    }
                  }}>Generate token</Button>
                {/if}
                {#if !exec.is_default}
                  <Button variant="danger" size="sm" onclick={async () => {
                    const confirmed = await confirmAction({ title: 'Delete executor', message: `Delete "${exec.name}"? This cannot be undone.` });
                    if (confirmed) {
                      await api.executor.delete(exec.executor_id);
                      await refreshPageState();
                      addToast('Executor deleted.', 'success');
                    }
                  }}>Delete</Button>
                {/if}
              </div>
            </div>

            {#if Object.keys(exec.labels || {}).length > 0}
              <div class="flex flex-wrap gap-1.5">
                {#each Object.entries(exec.labels || {}) as [k, v]}
                  <span class="px-2 py-0.5 bg-zinc-800 text-zinc-300 text-xs font-mono rounded border border-zinc-700">{k}={v}</span>
                {/each}
              </div>
            {/if}

            <div class="text-xs text-slate-500 font-mono">ID: {exec.executor_id}</div>
            {#if executorObservedNote(exec)}
              <div class="text-xs text-slate-500">{executorObservedNote(exec)}</div>
            {/if}
            {#if exec.desired_config_version !== exec.applied_config_version}
              <div class="text-xs text-amber-300">
                config pending: desired v{exec.desired_config_version}, applied v{exec.applied_config_version}
              </div>
            {/if}

            {#if executorToken && executorToken.executor_id === exec.executor_id}
              {@const execCommand = `cognis executor run --controller-url ${window.location.origin.replace('http', 'ws')}/api/executor/ws --token ${executorToken.token}`}
              <div class="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-4 space-y-3">
                <p class="text-sm text-emerald-100">Copy this token now. It is not stored in the UI.</p>
                <textarea readonly class="min-h-[72px] w-full rounded-2xl border border-emerald-500/20 bg-slate-950/80 px-4 py-3 font-mono text-xs text-slate-100">{executorToken.token}</textarea>
                <p class="text-xs text-slate-400 mt-2">Run this command on the remote machine:</p>
                <pre class="w-full rounded-2xl border border-emerald-500/20 bg-slate-950/80 px-4 py-3 font-mono text-xs text-slate-200 whitespace-pre-wrap break-all">{execCommand}</pre>
                <div class="flex flex-wrap gap-2">
                  <Button size="sm" variant="secondary" onclick={() => copyToClipboard(executorToken?.token ?? '')}>Copy token</Button>
                  <Button size="sm" variant="secondary" onclick={() => copyToClipboard(execCommand)}>Copy command</Button>
                </div>
              </div>
            {/if}

            <!-- Quick presets -->
            <div class="flex flex-wrap gap-2">
              <span class="text-xs text-slate-400 self-center">Presets:</span>
              <Button variant="secondary" size="sm" onclick={async () => {
                const readOnly = executorTools.filter(t => t.read_only).map(t => t.name);
                await api.executor.update(exec.executor_id, { enabled_tools: readOnly, enabled_tool_groups: [] });
                await refreshPageState();
                addToast('Enabled read-only tools.', 'success');
              }}>Read-only tools</Button>
              <Button variant="secondary" size="sm" onclick={async () => {
                await api.executor.update(exec.executor_id, { enabled_tools: ['*'], enabled_tool_groups: [] });
                await refreshPageState();
                addToast('Enabled all tools.', 'success');
              }}>All tools</Button>
              <Button variant="secondary" size="sm" onclick={async () => {
                await api.executor.update(exec.executor_id, { enabled_tools: [], enabled_tool_groups: [] });
                await refreshPageState();
                addToast('Disabled all tools.', 'success');
              }}>None</Button>
            </div>

            <!-- Tool group toggles -->
            <div>
              <span class="text-xs uppercase tracking-wider text-slate-400">Tool groups</span>
              <div class="mt-2 flex flex-wrap gap-2">
                {#each toolGroups as group}
                  {@const enabled = (exec.enabled_tool_groups || []).includes(group) || (exec.enabled_tools || []).includes('*')}
                  {@const groupToolCount = executorTools.filter(t => t.category === group).length}
                  <button
                    class="px-3 py-1.5 rounded-lg text-sm border transition-colors {enabled ? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-300' : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-600'}"
                    onclick={async () => {
                      const groups = [...(exec.enabled_tool_groups || [])];
                      if (enabled && groups.includes(group)) {
                        groups.splice(groups.indexOf(group), 1);
                      } else if (!enabled) {
                        groups.push(group);
                      }
                      const tools = (exec.enabled_tools || []).filter((t: string) => t !== '*');
                      await api.executor.update(exec.executor_id, { enabled_tool_groups: groups, enabled_tools: tools });
                      await refreshPageState();
                    }}
                  >
                    {group} ({groupToolCount})
                  </button>
                {/each}
              </div>
            </div>

            <!-- Individual tool toggles -->
            <div>
              <span class="text-xs uppercase tracking-wider text-slate-400">Individual tools</span>
              <div class="mt-2 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-1.5">
                {#each executorTools as tool}
                  {@const enabledByGroup = (exec.enabled_tool_groups || []).includes(tool.category)}
                  {@const enabledByName = (exec.enabled_tools || []).includes(tool.name) || (exec.enabled_tools || []).includes('*')}
                  {@const enabled = enabledByGroup || enabledByName}
                  <button
                    class="px-2.5 py-1.5 rounded text-xs text-left border transition-colors {enabled ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-200' : 'bg-slate-900 border-slate-700 text-slate-500 hover:border-slate-600'}"
                    title="{tool.description} ({tool.category}){enabledByGroup ? ' — enabled via group' : ''}"
                    onclick={async () => {
                      if (enabledByGroup) return;
                      const tools = [...(exec.enabled_tools || [])].filter((t: string) => t !== '*');
                      if (enabledByName) {
                        const idx = tools.indexOf(tool.name);
                        if (idx >= 0) tools.splice(idx, 1);
                      } else {
                        tools.push(tool.name);
                      }
                      await api.executor.update(exec.executor_id, { enabled_tools: tools });
                      await refreshPageState();
                    }}
                  >
                    <span class="font-mono">{tool.name}</span>
                    {#if tool.non_bypassable}
                      <span class="text-amber-400 ml-0.5" title="Non-bypassable">!</span>
                    {/if}
                  </button>
                {/each}
              </div>
            </div>

            <div class="text-xs text-slate-500">
              {(exec.enabled_tools || []).includes('*')
                ? 'All tools enabled'
                : `${(exec.enabled_tools || []).length} individual + ${(exec.enabled_tool_groups || []).length} group(s) enabled`}
            </div>

            <!-- LSP Diagnostics settings -->
            {@const lspConfig = exec.config || {}}
            {@const lspEnabled = lspConfig.lsp_enabled !== false}
            {@const lspAutoInstall = lspConfig.lsp_auto_install !== false}
            <details class="group">
              <summary class="cursor-pointer text-xs uppercase tracking-wider text-slate-400 hover:text-slate-300 select-none">
                LSP Diagnostics
                <span class="ml-1 text-slate-500">{lspEnabled ? '(enabled)' : '(disabled)'}</span>
              </summary>
              <div class="mt-3 space-y-3 pl-1">
                <div class="flex flex-wrap gap-4">
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                      <input type="checkbox" checked={lspEnabled}
                        class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), lsp_enabled: checked };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                        addToast(`LSP ${checked ? 'enabled' : 'disabled'}.`, 'success');
                      }}
                    />
                    Enabled
                  </label>
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                      <input type="checkbox" checked={lspAutoInstall} disabled={!lspEnabled}
                        class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), lsp_auto_install: checked };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                        addToast(`Auto-install ${checked ? 'enabled' : 'disabled'}.`, 'success');
                      }}
                    />
                    Auto-install servers
                  </label>
                </div>
                <div class="grid gap-3 md:grid-cols-3">
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Diagnostics timeout (ms)</span>
                    <Input value={Number(lspConfig.lsp_diagnostics_timeout_ms ?? 10000)} disabled={!lspEnabled}
                      type="number" min="1000" max="60000" step="1000"
                      onchange={async (e) => {
                        const val = parseInt(e.currentTarget.value, 10);
                        if (isNaN(val)) return;
                        const cfg = { ...(exec.config || {}), lsp_diagnostics_timeout_ms: val };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Idle timeout (seconds)</span>
                    <Input value={Number(lspConfig.lsp_idle_timeout_seconds ?? 600)} disabled={!lspEnabled}
                      type="number" min="60" max="3600" step="60"
                      onchange={async (e) => {
                        const val = parseInt(e.currentTarget.value, 10);
                        if (isNaN(val)) return;
                        const cfg = { ...(exec.config || {}), lsp_idle_timeout_seconds: val };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Max concurrent servers</span>
                    <Input value={Number(lspConfig.lsp_max_concurrent_servers ?? 8)} disabled={!lspEnabled}
                      type="number" min="1" max="32" step="1"
                      onchange={async (e) => {
                        const val = parseInt(e.currentTarget.value, 10);
                        if (isNaN(val)) return;
                        const cfg = { ...(exec.config || {}), lsp_max_concurrent_servers: val };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                </div>
              </div>
            </details>

            <!-- Signal direct-mode settings -->
            {@const signalConfig = ((exec.config || {}).signal || {}) as Record<string, unknown>}
            {@const signalDirectEnabled = signalConfig.direct_enabled === true}
            {@const signalCommand = typeof signalConfig.command === 'string' ? signalConfig.command : ''}
            <details class="group">
              <summary class="cursor-pointer text-xs uppercase tracking-wider text-slate-400 hover:text-slate-300 select-none">
                Signal Direct Mode
                <span class="ml-1 text-slate-500">{signalDirectEnabled ? '(enabled)' : '(disabled)'}</span>
              </summary>
              <div class="mt-3 space-y-3 pl-1">
                <label class="flex items-center gap-2 text-sm text-slate-300">
                  <input
                    type="checkbox"
                    checked={signalDirectEnabled}
                    class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30"
                    onchange={async (e) => {
                      const checked = e.currentTarget.checked;
                      const cfg = {
                        ...(exec.config || {}),
                        signal: {
                          ...signalConfig,
                          direct_enabled: checked,
                        },
                      };
                      await api.executor.update(exec.executor_id, { config: cfg });
                      await refreshPageState();
                      addToast(`Signal direct mode ${checked ? 'enabled' : 'disabled'}.`, 'success');
                    }}
                  />
                  Enable Signal direct JSON-RPC on this executor
                </label>

                <label class="space-y-1 text-sm text-slate-300">
                  <span class="text-xs text-slate-400">signal-cli command or absolute path</span>
                  <Input
                    value={signalCommand}
                    placeholder="signal-cli or /opt/homebrew/bin/signal-cli"
                    onchange={async (e) => {
                      const value = e.currentTarget.value.trim();
                      const cfg = {
                        ...(exec.config || {}),
                        signal: {
                          ...signalConfig,
                          command: value || 'signal-cli',
                        },
                      };
                      await api.executor.update(exec.executor_id, { config: cfg });
                      await refreshPageState();
                      addToast('Signal command updated.', 'success');
                    }}
                  />
                  <p class="text-xs text-slate-500">
                    Use this when <code>signal-cli</code> is not on the default PATH for the executor process.
                  </p>
                </label>
              </div>
            </details>

            <!-- MCP Server Assignment -->
            {#if mcpServerConfigs.length > 0}
              {@const assignedIds = ((exec.config || {}).mcp_server_ids || []) as string[]}
              <details class="group">
                <summary class="cursor-pointer text-xs uppercase tracking-wider text-slate-400 hover:text-slate-300 select-none">
                  MCP Servers
                  <span class="ml-1 text-slate-500">({assignedIds.length} assigned)</span>
                </summary>
                <div class="mt-2 flex flex-wrap gap-2">
                  {#each mcpServerConfigs as srv}
                    {@const assigned = assignedIds.includes(srv.server_id)}
                    <button
                      class="px-3 py-1.5 rounded-lg text-sm border transition-colors {assigned ? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-300' : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-600'}"
                      title="{srv.description || srv.name} ({srv.transport})"
                      onclick={async () => {
                        const ids = [...assignedIds];
                        if (assigned) {
                          ids.splice(ids.indexOf(srv.server_id), 1);
                        } else {
                          ids.push(srv.server_id);
                        }
                        const cfg = { ...(exec.config || {}), mcp_server_ids: ids };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    >
                      {srv.name}
                      <span class="ml-1 text-xs opacity-60">{srv.transport}</span>
                    </button>
                  {/each}
                </div>
              </details>
            {/if}
          </Card>
        {/each}

        {#if executorConfigs.length === 0}
          <Card class="p-5 text-center text-slate-400">
            <p>No executors configured. A default executor will be created on next restart.</p>
          </Card>
        {/if}
      </div>
    {:else if activeTab === 'system'}
      <div class="space-y-5">
        {#if diagnostics}
          {#if isAdmin}
            <Card class="p-5">
              <div class="space-y-3">
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Production executor policy</p>
                <h2 class="text-lg font-semibold text-white">Local executor modes</h2>
                <p class="text-sm text-slate-400">For multi-user production deployments, disable local executor modes and rely on websocket executors only.</p>
                <div class="grid gap-3 md:grid-cols-2">
                  <button class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-4 text-left" onclick={() => toggleBooleanSetting('executors.allow_in_process', !settingBool('executors.allow_in_process', true))}>
                    <div class="flex items-center justify-between gap-3">
                      <p class="font-medium text-white">Allow in-process executors</p>
                      <ProviderStatusBadge status={settingBool('executors.allow_in_process', true) ? 'healthy' : 'degraded'} />
                    </div>
                    <p class="mt-2 text-sm text-slate-400">Disable to prevent controller-local tool execution in production.</p>
                  </button>
                  <button class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-4 text-left" onclick={() => toggleBooleanSetting('executors.allow_subprocess', !settingBool('executors.allow_subprocess', true))}>
                    <div class="flex items-center justify-between gap-3">
                      <p class="font-medium text-white">Allow subprocess executors</p>
                      <ProviderStatusBadge status={settingBool('executors.allow_subprocess', true) ? 'healthy' : 'degraded'} />
                    </div>
                    <p class="mt-2 text-sm text-slate-400">Disable to require persistent websocket executors instead of local child processes.</p>
                  </button>
                </div>
              </div>
            </Card>
          {/if}

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
    {:else if activeTab === 'tools'}
      <div class="space-y-5">
        <div class="flex items-center justify-between">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Tools</p>
            <h2 class="mt-1 text-lg font-semibold text-white">MCP Servers</h2>
            <p class="mt-2 text-sm text-slate-400">
              Configure MCP servers globally, then assign them to executors. Agents inherit MCP tools from their executor.
            </p>
          </div>
          <Button variant="primary" size="sm" onclick={() => {
            mcpForm = { name: '', transport: 'stdio', command: '', url: '', args: '', envVars: [], timeout_seconds: 30, description: '' };
            editingMcpServer = null;
            showMcpForm = true;
          }}>New MCP server</Button>
        </div>

        {#if showMcpForm}
          <Card class="p-5 space-y-4">
            <h3 class="text-lg font-medium text-white">{editingMcpServer ? 'Edit MCP Server' : 'New MCP Server'}</h3>
            <div class="grid gap-4 md:grid-cols-2">
              <label class="space-y-1 text-sm text-slate-200">
                <span>Name</span>
                <Input bind:value={mcpForm.name} placeholder="e.g. github-api" />
              </label>
              <label class="space-y-1 text-sm text-slate-200">
                <span>Transport</span>
                <select bind:value={mcpForm.transport} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                  <option value="stdio">stdio</option>
                  <option value="sse">sse</option>
                  <option value="streamable_http">streamable_http</option>
                </select>
              </label>
              {#if mcpForm.transport === 'stdio'}
                <label class="space-y-1 text-sm text-slate-200">
                  <span>Command</span>
                  <Input bind:value={mcpForm.command} placeholder="e.g. npx -y @modelcontextprotocol/server-github" />
                </label>
              {:else}
                <label class="space-y-1 text-sm text-slate-200">
                  <span>URL</span>
                  <Input bind:value={mcpForm.url} placeholder="e.g. http://localhost:3000/sse" />
                </label>
              {/if}
              <label class="space-y-1 text-sm text-slate-200">
                <span>Timeout (seconds)</span>
                <Input bind:value={mcpForm.timeout_seconds} type="number" />
              </label>
            </div>
            {#if mcpForm.transport === 'stdio'}
              <label class="space-y-1 text-sm text-slate-200">
                <span>Arguments (one per line)</span>
                <textarea bind:value={mcpForm.args} class="min-h-[60px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 font-mono" placeholder="--port&#10;3000"></textarea>
              </label>
            {/if}
            <div class="space-y-1 text-sm text-slate-200">
              <span>Environment variables</span>
              <EnvVarEditor envVars={mcpForm.envVars} {secrets} onChange={(next) => (mcpForm.envVars = next)} onCreateSecret={openMcpSecretModal} />
            </div>
            <label class="space-y-1 text-sm text-slate-200">
              <span>Description</span>
              <Input bind:value={mcpForm.description} placeholder="Optional description" />
            </label>
            <div class="flex gap-2 justify-end">
              <Button variant="secondary" size="sm" onclick={() => showMcpForm = false}>Cancel</Button>
              <Button variant="primary" size="sm" disabled={!mcpForm.name.trim()} onclick={async () => {
                const args = mcpForm.args.split('\n').map(s => s.trim()).filter(Boolean);
                const env = serializeMcpEnvVars(mcpForm.envVars);
                try {
                  if (editingMcpServer) {
                    await api.tools.updateMcpServer(editingMcpServer.server_id, {
                      name: mcpForm.name,
                      transport: mcpForm.transport,
                      command: mcpForm.transport === 'stdio' ? mcpForm.command : null,
                      url: mcpForm.transport !== 'stdio' ? mcpForm.url : null,
                      args,
                      env,
                      timeout_seconds: mcpForm.timeout_seconds,
                      description: mcpForm.description || null,
                    });
                  } else {
                    await api.tools.createMcpServer({
                      name: mcpForm.name,
                      transport: mcpForm.transport,
                      command: mcpForm.transport === 'stdio' ? mcpForm.command : undefined,
                      url: mcpForm.transport !== 'stdio' ? mcpForm.url : undefined,
                      args,
                      env,
                      timeout_seconds: mcpForm.timeout_seconds,
                      description: mcpForm.description || undefined,
                    });
                  }
                  showMcpForm = false;
                  await refreshPageState();
                  addToast(editingMcpServer ? 'MCP server updated.' : 'MCP server created.', 'success');
                } catch (e) { error = asApiError(e).message; }
              }}>{editingMcpServer ? 'Update' : 'Create'}</Button>
            </div>
          </Card>
        {/if}

        {#each mcpServerConfigs as srv}
          <Card class="p-5 space-y-3">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-3">
                <h3 class="text-lg font-medium text-white">{srv.name}</h3>
                <span class="px-2 py-0.5 bg-zinc-700 text-zinc-300 text-xs font-mono rounded">{srv.transport}</span>
                <span class="px-2 py-0.5 rounded text-xs {srv.status === 'active' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-zinc-700 text-zinc-400'}">{srv.status}</span>
              </div>
              <div class="flex gap-2">
                <Button variant="secondary" size="sm" onclick={() => {
                  editingMcpServer = srv;
                  mcpForm = {
                    name: srv.name,
                    transport: srv.transport,
                    command: srv.command || '',
                    url: srv.url || '',
                    args: (srv.args || []).join('\n'),
                    envVars: parseMcpEnvVars(srv.env || {}),
                    timeout_seconds: srv.timeout_seconds,
                    description: srv.description || '',
                  };
                  showMcpForm = true;
                }}>Edit</Button>
                <Button variant="danger" size="sm" onclick={async () => {
                  const confirmed = await confirmAction({ title: 'Delete MCP server', message: `Delete "${srv.name}"? This cannot be undone.` });
                  if (confirmed) {
                    try {
                      await api.tools.deleteMcpServer(srv.server_id);
                      await refreshPageState();
                      addToast('MCP server deleted.', 'success');
                    } catch (e) { error = asApiError(e).message; }
                  }
                }}>Delete</Button>
              </div>
            </div>
            {#if srv.transport === 'stdio' && srv.command}
              <p class="text-xs text-slate-400 font-mono">{srv.command} {(srv.args || []).join(' ')}</p>
            {:else if srv.url}
              <p class="text-xs text-slate-400 font-mono">{srv.url}</p>
            {/if}
            {#if srv.description}
              <p class="text-sm text-slate-400">{srv.description}</p>
            {/if}
            <div class="text-xs text-slate-500 font-mono">ID: {srv.server_id}</div>
          </Card>
        {/each}

        {#if mcpServerConfigs.length === 0 && !showMcpForm}
          <Card class="p-5 text-center text-slate-400">
            <p>No MCP servers configured. Create one to make MCP tools available to executors.</p>
          </Card>
        {/if}

        <!-- Native Tools Reference -->
        {#if executorTools.length > 0}
          {@const nativeGroups = [...new Set(executorTools.map(t => t.category))].sort()}
          <Card class="p-5 space-y-3">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Native Tools Reference</p>
            <p class="text-sm text-slate-400">Built-in tools available on executors. Enable them per-executor in the Executors tab.</p>
            {#each nativeGroups as group}
              <div>
                <span class="text-xs font-medium text-slate-300">{group}</span>
                <div class="mt-1 flex flex-wrap gap-1.5">
                  {#each executorTools.filter(t => t.category === group) as tool}
                    <span class="px-2 py-0.5 bg-slate-800 text-slate-300 text-xs font-mono rounded border border-slate-700" title={tool.description}>{tool.name}</span>
                  {/each}
                </div>
              </div>
            {/each}
          </Card>
        {/if}
      </div>
    {:else if activeTab === 'users'}
      <div class="space-y-5">
        <Card class="p-5">
          <div class="space-y-4">
            <div class="flex items-center justify-between gap-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Administration</p>
                <h2 class="mt-1 text-lg font-semibold text-white">User management</h2>
              </div>
              <div class="flex items-center gap-3">
                <label class="flex items-center gap-2 text-sm text-slate-300">
                  <input type="checkbox" bind:checked={showDisabledUsers}
                    class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30"
                    onchange={() => void loadUsers()}
                  />
                  Show disabled
                </label>
                <Button onclick={openCreateUserModal}>Create user</Button>
              </div>
            </div>

            {#each userList as user}
              {@const isSelf = user.email === auth.getSnapshot().user?.email}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 {!user.is_active ? 'opacity-60' : ''}">
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <p class="font-medium text-white truncate">{user.email}</p>
                    {#if !user.is_active}
                      <span class="rounded-full border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 text-xs text-rose-300">disabled</span>
                    {/if}
                    {#if isSelf}
                      <span class="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-300">you</span>
                    {/if}
                  </div>
                  <p class="text-sm text-slate-400">{user.name ?? ''}</p>
                  <div class="mt-1 flex flex-wrap gap-3 text-xs text-slate-500">
                    <span>Role: <span class="text-slate-300">{user.role}</span></span>
                    <span>Last login: {user.last_login_at ? new Date(user.last_login_at).toLocaleDateString() : 'never'}</span>
                    <span>Created: {user.created_at ? new Date(user.created_at).toLocaleDateString() : ''}</span>
                    {#if user.disabled_by}
                      <span>Disabled by: {user.disabled_by}</span>
                    {/if}
                  </div>
                </div>
                <div class="flex items-center gap-2">
                  <Button size="sm" variant="secondary" onclick={() => openEditUserModal(user)} disabled={busy}>Edit</Button>
                  {#if !isSelf}
                    <Button size="sm" variant={user.is_active ? 'danger' : 'secondary'} onclick={() => toggleUserActive(user)} disabled={busy}>
                      {user.is_active ? 'Disable' : 'Enable'}
                    </Button>
                    <Button size="sm" variant="danger" onclick={() => deleteUser(user)} disabled={busy}>Delete</Button>
                  {/if}
                </div>
              </div>
            {:else}
              <p class="text-sm text-slate-400">No users found.</p>
            {/each}
          </div>
        </Card>
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
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Display name</span>
              <Input bind:value={accountNameForm} oninput={() => (accountNameDirty = true)} placeholder="Your name" />
            </label>
            {#if accountNameDirty}
              <Button onclick={saveAccountName} disabled={busy}>Save name</Button>
            {/if}
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
      <h3 class="mt-1 text-lg font-semibold text-white">{secretModalTarget === 'provider' ? 'Create API key secret' : 'Create environment secret'}</h3>
      <div class="mt-4 space-y-4">
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Secret name</span>
          <Input bind:value={secretModalName} placeholder="openai_api_key" />
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>{secretModalTarget === 'provider' ? 'API key value' : 'Secret value'}</span>
          <Input bind:value={secretModalValue} type="password" placeholder={secretModalTarget === 'provider' ? 'sk-...' : 'secret-value'} />
        </label>
      </div>
      <div class="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onclick={() => (showSecretModal = false)}>Cancel</Button>
        <Button onclick={saveSecretFromModal} disabled={busy || !secretModalName.trim() || !secretModalValue.trim()}>Save credential</Button>
      </div>
    </div>
  </div>
{/if}

<!-- User create modal -->
{#if showUserCreateModal}
  <div class="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/80 backdrop-blur" role="dialog" aria-modal="true">
    <div class="w-full max-w-md rounded-3xl border border-slate-800 bg-slate-950 p-6 shadow-card">
      <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Administration</p>
      <h3 class="mt-1 text-lg font-semibold text-white">Create user</h3>
      <div class="mt-4 space-y-4">
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Email</span>
          <Input bind:value={userCreateForm.email} type="email" placeholder="user@example.com" />
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Name (optional)</span>
          <Input bind:value={userCreateForm.name} placeholder="Display name" />
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Role</span>
          <select bind:value={userCreateForm.role} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="user">user</option>
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
            <option value="service">service</option>
          </select>
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Password</span>
          <Input bind:value={userCreateForm.password} type="password" />
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Confirm password</span>
          <Input bind:value={userCreateForm.confirm_password} type="password" />
        </label>
      </div>
      <div class="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onclick={() => (showUserCreateModal = false)}>Cancel</Button>
        <Button onclick={createUserSubmit} disabled={busy || !userCreateForm.email.trim() || userCreateForm.password.length < 8 || userCreateForm.password !== userCreateForm.confirm_password}>Create user</Button>
      </div>
    </div>
  </div>
{/if}

<!-- User edit modal -->
{#if showUserEditModal && editingUser}
  <div class="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/80 backdrop-blur" role="dialog" aria-modal="true">
    <div class="w-full max-w-md rounded-3xl border border-slate-800 bg-slate-950 p-6 shadow-card">
      <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Administration</p>
      <h3 class="mt-1 text-lg font-semibold text-white">Edit user</h3>
      <p class="mt-1 text-sm text-slate-400">{editingUser.email}</p>
      <div class="mt-4 space-y-4">
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Name</span>
          <Input bind:value={userEditForm.name} placeholder="Display name" />
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Role</span>
          <select bind:value={userEditForm.role} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
            <option value="user">user</option>
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
            <option value="service">service</option>
          </select>
        </label>
      </div>
      <div class="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onclick={() => { showUserEditModal = false; editingUser = null; }}>Cancel</Button>
        <Button onclick={updateUserSubmit} disabled={busy}>Save changes</Button>
      </div>
    </div>
  </div>
{/if}
