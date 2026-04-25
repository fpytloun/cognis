<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
import { onMount, tick } from 'svelte';

  import type { MCPEnvVar } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import { deriveGettingStartedSteps } from '$lib/getting-started';
  import { collectModelOptions, createProviderForm, deriveProviderId, presetHasBaseUrl, presetNeedsAuth, PRESET_LABELS, providerFormToPayload, type ProviderFormState, type ProviderPreset } from '$lib/providers';
  import { STEP_PROFILE_CAPABILITIES, STEP_PROFILE_GROUPS } from '$lib/workflows';
  import { defaultModelEntry, type ModelEntry } from '$lib/types/api';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ProviderStatusBadge from '$lib/components/ProviderStatusBadge.svelte';
  import EnvVarEditor from '$lib/components/settings/EnvVarEditor.svelte';
  import ModelCard from '$lib/components/settings/ModelCard.svelte';
  import ModelEditModal from '$lib/components/settings/ModelEditModal.svelte';
  import ModelDiscoveryModal from '$lib/components/settings/ModelDiscoveryModal.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import { buildLinkedServiceUrl, openUrlInNewTab } from '$lib/config';
  import { clearPersistedScroll } from '$lib/actions/scrollPersist';
  import { confirmAction } from '$lib/stores/confirm';
  import { onTabReset } from '$lib/stores/tabReset';
  import { addToast } from '$lib/stores/toasts';
  import { blockNavigationIfDirty, installBeforeUnloadGuard } from '$lib/navigation/unsaved';
  import { thinkingEffortLabel } from '$lib/thinking';
  import { auth } from '$lib/stores/auth';
  import {
    executorMcpFailureDetails,
    executorObservedNote,
    executorRuntimeBadgeStatus,
    executorRuntimeLabel,
    executorRuntimeSummary,
    validateStdioCommand
  } from '$lib/executors';
  import type {
    ApiKeyCreateResponse,
    ApiKeyMetadata,
    CredentialMetadata,
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
    StepProfileDefinition,
    ToolDefinitionSummary,
    UserDetail,
    UserRole,
    WebConfigStatus
  } from '$lib/types/api';

  type SettingsTab = 'providers' | 'routing' | 'secrets' | 'web' | 'tools' | 'executors' | 'users' | 'system' | 'account';

  type CredentialKind = 'token' | 'text' | 'username_password' | 'totp_seed' | 'recovery_codes' | 'browser_storage_state';

  const CREDENTIAL_PAYLOAD_TEMPLATES: Record<CredentialKind, string> = {
    token: '{\n  "token": ""\n}',
    text: '{\n  "value": ""\n}',
    username_password: '{\n  "username": "",\n  "password": ""\n}',
    totp_seed: '{\n  "issuer": "",\n  "account_name": "",\n  "secret": ""\n}',
    recovery_codes: '{\n  "codes": []\n}',
    browser_storage_state: '{\n  "storage_state": {\n    "cookies": [],\n    "origins": []\n  }\n}'
  };

  const CREDENTIAL_METADATA_HINTS: Record<CredentialKind, string> = {
    token: 'Optional metadata can capture safe context such as provider, origin, or tags.',
    text: 'Use metadata for safe labels like provider, origin, or intended purpose.',
    username_password: 'Store safe metadata like origin, login_url, or provider. Username and password belong in payload.',
    totp_seed: 'Metadata can describe the site or login flow. Keep the actual seed in payload.secret.',
    recovery_codes: 'Metadata can describe issuer or origin. Put recovery codes in payload.codes.',
    browser_storage_state: 'Set metadata.origin to the bound site origin when entering this manually, for example https://www.rohlik.cz.'
  };

  const ALL_TABS: SettingsTab[] = ['providers', 'routing', 'secrets', 'web', 'tools', 'executors', 'users', 'system', 'account'];
  const USER_TABS: SettingsTab[] = ['secrets', 'tools', 'executors', 'account'];
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
  const ROUTING_KEYS = ['default', 'classifier', 'compaction', 'evaluator', 'speech_to_text', 'image_generation', 'attachment_analysis'] as const;
  const TEXT_ROUTING_KEYS = ['default', 'classifier', 'compaction', 'evaluator'] as const;
  type RoutingKey = (typeof ROUTING_KEYS)[number];
  type RoutingFormEntry = { model: string; reasoningEffort: string };
  const ROUTING_METADATA: Array<{
    key: RoutingKey;
    label: string;
    description: string;
    supportsThinking: boolean;
  }> = [
    { key: 'default', label: 'default', description: 'Main chat and task execution.', supportsThinking: true },
    { key: 'classifier', label: 'classifier', description: 'Decision engine / fast model.', supportsThinking: true },
    { key: 'compaction', label: 'compaction', description: 'Context compaction summaries.', supportsThinking: true },
    { key: 'evaluator', label: 'evaluator', description: 'Workflow step evaluation. Falls back to default if not set.', supportsThinking: true },
    { key: 'speech_to_text', label: 'speech_to_text', description: 'Voice-note transcription. Use models like gpt-4o-transcribe, gpt-4o-mini-transcribe, or whisper.', supportsThinking: false },
    { key: 'image_generation', label: 'image_generation', description: 'Image-capable model for avatars and tools. Must support image generation.', supportsThinking: false },
    { key: 'attachment_analysis', label: 'attachment_analysis', description: 'Fallback model for artifact_read and binary read analysis when the main chat model lacks image/PDF/file capabilities.', supportsThinking: false }
  ];

  function emptyRoutingEntry(): RoutingFormEntry {
    return { model: '', reasoningEffort: '' };
  }

  function emptyModelRouting(): ModelRouting {
    return {
      default: { model: null, reasoning_effort: null },
      classifier: { model: null, reasoning_effort: null },
      compaction: { model: null, reasoning_effort: null },
      evaluator: { model: null, reasoning_effort: null },
      speech_to_text: { model: null, reasoning_effort: null },
      image_generation: { model: null, reasoning_effort: null },
      attachment_analysis: { model: null, reasoning_effort: null }
    };
  }

  function emptyRoutingForm(): Record<RoutingKey, RoutingFormEntry> {
    return {
      default: emptyRoutingEntry(),
      classifier: emptyRoutingEntry(),
      compaction: emptyRoutingEntry(),
      evaluator: emptyRoutingEntry(),
      speech_to_text: emptyRoutingEntry(),
      image_generation: emptyRoutingEntry(),
      attachment_analysis: emptyRoutingEntry()
    };
  }

  let activeTab = $state<SettingsTab>('providers');
  let settingsPanelAnchor = $state<HTMLDivElement | null>(null);
  let mobileTabListEl = $state<HTMLDivElement | null>(null);
  let providerEditorEl = $state<HTMLElement | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let savingExecutorIds = $state<string[]>([]);
  let error = $state('');
  let notice = $state('');
  let settings = $state<SettingsCategory[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let modelRouting = $state<ModelRouting>(emptyModelRouting());
  let secrets = $state<SecretMetadata[]>([]);
  let credentials = $state<CredentialMetadata[]>([]);
  let health = $state<HealthResponse | null>(null);
  let diagnostics = $state<SystemDiagnostics | null>(null);
  let executorConfigs = $state<ExecutorConfig[]>([]);
  let executorTools = $state<ToolDefinitionSummary[]>([]);
  let editingExecutor = $state<ExecutorConfig | null>(null);
  let webConfig = $state<WebConfigStatus>({ backend: 'direct', tavily_configured: false, brave_configured: false, available_backends: ['direct'] });
  let webBackendForm = $state('direct');
  let webKeySetup = $state<{ backend: string; value: string } | null>(null);
  let showExecutorForm = $state(false);
  let executorForm = $state({ executor_id: '', name: '', executor_type: 'websocket', labels: '', status: 'active', shared: false });
  let executorToken = $state<ExecutorTokenResponse | null>(null);
  // Mobile tool-picker sheet: holds the executor id whose tool list should be
  // open, plus the current search query inside that picker. Setting both to
  // null/empty closes the sheet.
  let toolPickerExecutorId = $state<string | null>(null);
  let toolPickerQuery = $state('');
  // Serialize executor tool updates so rapid taps don't race each other on
  // a stale `exec.enabled_tools` snapshot. We accumulate the latest tool
  // list per executor and flush it via a single in-flight promise chain.
  const toolUpdateQueues = new Map<string, { pending: string[]; inFlight: Promise<void> | null }>();

  async function queueExecutorToolUpdate(executorId: string, nextTools: string[]): Promise<void> {
    const current = toolUpdateQueues.get(executorId) ?? { pending: nextTools, inFlight: null };
    current.pending = nextTools;
    toolUpdateQueues.set(executorId, current);
    if (current.inFlight) return;

    const run = async (): Promise<void> => {
      while (true) {
        const entry = toolUpdateQueues.get(executorId);
        if (!entry) return;
        const toFlush = entry.pending;
        try {
          await api.executor.update(executorId, { enabled_tools: toFlush });
        } catch (err) {
          addToast(asApiError(err).message, 'error', 4_000, 'Unable to update tools');
          toolUpdateQueues.delete(executorId);
          await refreshPageState();
          return;
        }
        // If nothing new arrived while the request was in flight, we're done.
        const after = toolUpdateQueues.get(executorId);
        if (!after || after.pending === toFlush) {
          toolUpdateQueues.delete(executorId);
          await refreshPageState();
          return;
        }
      }
    };
    current.inFlight = run();
    await current.inFlight;
  }
  let mcpServerConfigs = $state<MCPServerConfigResponse[]>([]);
  type StepProfileMatrixRow = { category: string; capabilities: string[] };
  type StepProfileFormState = {
    profile_id: string;
    name: string;
    mode: 'soft' | 'hard';
    has_override: boolean;
    is_custom: boolean;
    allowToolSearch: boolean;
    matrix: StepProfileMatrixRow[];
    includeText: string;
    excludeText: string;
  };
  let stepProfileForms = $state<StepProfileFormState[]>([]);
  let savingStepProfileIds = $state<string[]>([]);
  let openStepProfileIds = $state<string[]>([]);
  let creatingStepProfile = $state(false);
  let newStepProfileForm = $state({ profile_id: '', name: '', mode: 'soft' as 'soft' | 'hard' });
  let showMcpForm = $state(false);
  let editingMcpServer = $state<MCPServerConfigResponse | null>(null);
  let mcpForm = $state({ name: '', transport: 'stdio', command: '', url: '', args: '', envVars: [] as MCPEnvVar[], headers: [] as MCPEnvVar[], timeout_seconds: 30, description: '', shared: false });
  let isAdmin = $state(false);
  let tabs = $derived(isAdmin ? ALL_TABS : USER_TABS);
  let selectedProviderId = $state('');
  let selectedSettingKey = $state('');
  let settingValueText = $state('');
  let providerForm = $state<ProviderFormState>(createProviderForm());
  let providerTestResult = $state<ProviderTestResult | null>(null);
  let showModelDiscovery = $state(false);
  let editingModel = $state<ModelEntry | null>(null);
  let addModelId = $state('');
  let showAdvancedSettings = $state(false);
  let showSecretModal = $state(false);
  let secretModalTarget = $state<'provider' | 'mcp'>('provider');
  let mcpSecretTargetKey = $state('');
  let secretModalName = $state('');
  let secretModalValue = $state('');
  let agents = $state<Array<{ agent_id: string; name: string; is_system?: boolean }>>([]);
  let apiKeys = $state<ApiKeyMetadata[]>([]);
  let createdApiKey = $state<ApiKeyCreateResponse | null>(null);
  let newApiKeyName = $state('');
  let newApiKeyExpiresInDays = $state('');
  let initialSnapshot = $state('');

  // User management state
  let userList = $state<UserDetail[]>([]);
  let showUserCreateModal = $state(false);
  let showUserEditModal = $state(false);
  let showDisabledUsers = $state(false);
  let editingUser = $state<UserDetail | null>(null);
  let userCreateForm = $state({ email: '', name: '', password: '', confirm_password: '', role: 'user' as UserRole });
  let userEditForm = $state({ name: '', role: 'user' as UserRole });
  let accountNameForm = $state('');
  let accountNameDirty = $state(false);
  let executorPollTimer: ReturnType<typeof setInterval> | null = null;

  let routingForm = $state<Record<RoutingKey, RoutingFormEntry>>(emptyRoutingForm());

  let secretForm = $state({
    name: '',
    value: '',
    scope: 'user',
    agent_id: '',
    description: ''
  });

  let credentialForm = $state({
    credential_id: '',
    kind: 'token',
    label: '',
    payload_json: CREDENTIAL_PAYLOAD_TEMPLATES.token,
    metadata_json: '{}',
    scope: 'user',
    agent_id: '',
    description: '',
    expires_at: ''
  });

  let passwordForm = $state({
    current_password: '',
    new_password: '',
    confirm_password: ''
  });

  function credentialPayloadTemplate(kind: string): string {
    return CREDENTIAL_PAYLOAD_TEMPLATES[(kind as CredentialKind)] ?? '{}';
  }

  function credentialMetadataHint(kind: string): string {
    return CREDENTIAL_METADATA_HINTS[(kind as CredentialKind)] ?? 'Metadata is optional and should contain only safe, non-secret context.';
  }

  function updateCredentialKind(nextKind: string): void {
    const previousTemplate = credentialPayloadTemplate(credentialForm.kind);
    const trimmedPayload = credentialForm.payload_json.trim();
    const shouldReplacePayload = !trimmedPayload || trimmedPayload === previousTemplate.trim();
    credentialForm.kind = nextKind;
    if (shouldReplacePayload) {
      credentialForm.payload_json = credentialPayloadTemplate(nextKind);
    }
  }

  function snapshotState(): string {
    return JSON.stringify({
      providerForm,
      routingForm,
      stepProfileForms,
      secretForm,
      credentialForm,
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
    await tick();
    settingsPanelAnchor?.scrollIntoView({ block: 'start' });
    const activeTabButton = mobileTabListEl?.querySelector<HTMLElement>(`[data-settings-tab="${tab}"]`);
    activeTabButton?.scrollIntoView({ inline: 'center', block: 'nearest' });
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

  function looksLikeTranscriptionModel(value: string): boolean {
    const normalized = value.trim().toLowerCase().replaceAll('_', '-');
    return normalized.includes('transcribe') || normalized.includes('whisper') || normalized.includes('speech-to-text');
  }

  function findModelEntry(modelId: string): ModelEntry | null {
    const normalized = modelId.trim();
    if (!normalized) {
      return null;
    }
    for (const provider of providers) {
      const match = provider.models.find((model) => model.model_id === normalized);
      if (match) {
        return match;
      }
    }
    return null;
  }

  function defaultProviderModelId(): string {
    const defaultProvider =
      providers.find((provider) => provider.is_default) ??
      providers.find((provider) => provider.provider_id === 'default') ??
      null;
    const defaultModel = typeof defaultProvider?.config?.default_model === 'string'
      ? defaultProvider.config.default_model
      : '';
    return defaultModel.trim();
  }

  function effectiveRouteModelId(routeKey: RoutingKey): string {
    const explicitModel = routingForm[routeKey].model.trim();
    if (explicitModel) {
      return explicitModel;
    }
    if (routeKey !== 'default') {
      const inheritedDefaultRouteModel = routingForm.default.model.trim();
      if (inheritedDefaultRouteModel) {
        return inheritedDefaultRouteModel;
      }
    }
    return defaultProviderModelId();
  }

  function routeThinkingEffortOptions(routeKey: RoutingKey): string[] {
    if (!TEXT_ROUTING_KEYS.includes(routeKey as (typeof TEXT_ROUTING_KEYS)[number])) {
      return [];
    }
    const modelEntry = findModelEntry(effectiveRouteModelId(routeKey));
    return (modelEntry?.reasoning_efforts ?? []).filter((value) => value !== 'default');
  }

  function routeModelOptions(routeKey: RoutingKey): Array<{ value: string; label: string; providerId: string }> {
    const options = modelOptions();
    if (routeKey === 'image_generation') {
      return options.filter((option) => findModelEntry(option.value)?.supports_image_generation);
    }
    if (routeKey === 'attachment_analysis') {
      return options.filter((option) => {
        const entry = findModelEntry(option.value);
        return Boolean(
          entry?.supports_vision ||
          entry?.supports_pdf_input ||
          entry?.supports_audio_input ||
          entry?.supports_file_input
        );
      });
    }
    if (routeKey === 'speech_to_text') {
      return options.filter((option) => {
        const entry = findModelEntry(option.value);
        return looksLikeTranscriptionModel(option.value) || looksLikeTranscriptionModel(entry?.display_name ?? '');
      });
    }
    return options;
  }

  function syncRouteThinkingEffort(routeKey: RoutingKey): void {
    const entry = routingForm[routeKey];
    const available = routeThinkingEffortOptions(routeKey);
    if (!available.includes(entry.reasoningEffort)) {
      routingForm[routeKey].reasoningEffort = '';
    }
  }

  function executorSelectorFor(labels: Record<string, string> | null | undefined): string {
    return Object.entries(labels || {}).map(([k, v]) => `${k}=${v}`).join(', ');
  }

  function routingWarnings(): string[] {
    const knownModels = new Set(modelOptions().map((item) => item.value));
    return ROUTING_KEYS.map((key) => routingForm[key].model)
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
    await tick();
    if (window.innerWidth < 1024) {
      providerEditorEl?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
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
      let models: ModelEntry[];
      if (selectedProviderId) {
        const result = await api.llmProviders.discoverModels(selectedProviderId);
        models = result.models;
      } else {
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
      showModelDiscovery = true;
      addToast(`Discovered ${models.length} models.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Model discovery failed');
    } finally {
      busy = false;
    }
  }

  function handleAddDiscoveredModels(selected: ModelEntry[]): void {
    const existingIds = new Set(providerForm.models.map((m) => m.model_id));
    const newModels = selected.filter((m) => !existingIds.has(m.model_id));
    providerForm.models = [...providerForm.models, ...newModels];
    // Auto-set default model if none is set
    if (!providerForm.default_model && providerForm.models.length > 0) {
      providerForm.default_model = providerForm.models[0].model_id;
    }
    showModelDiscovery = false;
  }

  function handleRemoveModel(modelId: string): void {
    providerForm.models = providerForm.models.filter((m) => m.model_id !== modelId);
    if (providerForm.default_model === modelId) {
      providerForm.default_model = providerForm.models[0]?.model_id ?? '';
    }
  }

  function handleSaveModelEdit(updated: ModelEntry): void {
    providerForm.models = providerForm.models.map((m) =>
      m.model_id === updated.model_id ? updated : m
    );
    editingModel = null;
  }

  async function handleAddManualModel(): Promise<void> {
    const mid = addModelId.trim();
    if (!mid) return;
    if (providerForm.models.some((m) => m.model_id === mid)) {
      addToast('Model already configured.', 'error');
      return;
    }
    busy = true;
    try {
      let raw: ModelEntry | undefined;
      if (selectedProviderId) {
        const result = await api.llmProviders.enrichModels(selectedProviderId, [mid]);
        raw = result.models[0];
      } else {
        const result = await api.llmProviders.enrichModelsPreview({
          preset: providerForm.preset,
          base_url: providerForm.base_url,
          model_ids: [mid],
          ...(providerForm.auth_mode === 'secret' && providerForm.auth_secret_name
            ? { secret_name: providerForm.auth_secret_name }
            : {}),
          ...(providerForm.auth_mode === 'env' && providerForm.auth_env_var
            ? { env_var: providerForm.auth_env_var }
            : {})
        });
        raw = result.models[0];
      }
      // Guard against error/sparse responses — fill missing fields with defaults
      const rawObj = (raw ?? {}) as unknown as Record<string, unknown>;
      const enriched: ModelEntry = {
        ...defaultModelEntry(mid),
        ...('error' in rawObj ? {} : rawObj),
        model_id: mid
      };
      providerForm.models = [...providerForm.models, enriched];
      if (!providerForm.default_model) {
        providerForm.default_model = mid;
      }
      addModelId = '';
      addToast(`Added model: ${mid}`, 'success');
    } catch {
      // Fallback: add with defaults if enrichment fails
      providerForm.models = [...providerForm.models, defaultModelEntry(mid)];
      if (!providerForm.default_model) {
        providerForm.default_model = mid;
      }
      addModelId = '';
      addToast(`Added model with default properties: ${mid}`, 'success');
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

  function parseMcpEntries(values: Record<string, string>): MCPEnvVar[] {
    return Object.entries(values).map(([key, value]) => ({
      key,
      value: value.startsWith('$secret:') ? value.slice('$secret:'.length) : value,
      type: value.startsWith('$secret:') ? 'secret' : 'literal'
    }));
  }

  function serializeMcpEntries(entries: MCPEnvVar[]): Record<string, string> {
    return Object.fromEntries(
      entries
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
        scope: secretModalTarget === 'provider' ? 'system' : 'user',
        agent_id: null,
        description: secretModalTarget === 'provider'
          ? `API key for provider ${providerForm.display_name || providerForm.provider_id}`
          : `MCP config secret for ${mcpSecretTargetKey || 'entry'}`
      });
      if (secretModalTarget === 'provider') {
        providerForm.auth_secret_name = secretModalName;
      } else {
        const targetEntries = mcpForm.transport === 'stdio' ? mcpForm.envVars : mcpForm.headers;
        const nextEntries: MCPEnvVar[] = targetEntries.map((entry) =>
          entry.key === mcpSecretTargetKey ? { ...entry, type: 'secret', value: secretModalName } : entry
        );
        if (mcpForm.transport === 'stdio') {
          mcpForm.envVars = nextEntries;
        } else {
          mcpForm.headers = nextEntries;
        }
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

  const presetOptions: ProviderPreset[] = ['openai', 'openai_compatible', 'anthropic', 'ollama', 'litellm_proxy'];

  function toStepProfileForm(profile: StepProfileDefinition): StepProfileFormState {
    const matrix = Object.entries(profile.config.matrix || {})
      .map(([category, capabilities]) => ({ category, capabilities: [...capabilities].sort() }))
      .sort((a, b) => a.category.localeCompare(b.category));
    const include = Array.isArray(profile.config.tool_overrides?.include)
      ? profile.config.tool_overrides?.include ?? []
      : [];
    const exclude = Array.isArray(profile.config.tool_overrides?.exclude)
      ? profile.config.tool_overrides?.exclude ?? []
      : [];
    return {
      profile_id: profile.profile_id,
      name: profile.name,
      mode: profile.mode === 'hard' ? 'hard' : 'soft',
      has_override: profile.has_override === true,
      is_custom: profile.is_custom === true,
      allowToolSearch: profile.config.allow_tool_search !== false,
      matrix,
      includeText: include.join(', '),
      excludeText: exclude.join(', ')
    };
  }

  function availableStepProfileCategories(profile: StepProfileFormState): string[] {
    return [...STEP_PROFILE_GROUPS]
      .filter((category) => !profile.matrix.some((row) => row.category === category))
      .sort();
  }

  function toggleStepProfileOpen(profileId: string): void {
    openStepProfileIds = openStepProfileIds.includes(profileId)
      ? openStepProfileIds.filter((value) => value !== profileId)
      : [...openStepProfileIds, profileId];
  }

  function updateStepProfileForm(
    profileId: string,
    updater: (profile: StepProfileFormState) => StepProfileFormState
  ): void {
    stepProfileForms = stepProfileForms.map((profile) =>
      profile.profile_id === profileId ? updater(profile) : profile
    );
  }

  function toggleSettingsStepProfileCapability(profileId: string, category: string, capability: string): void {
    updateStepProfileForm(profileId, (profile) => {
      const matrix = profile.matrix.map((row) => ({ category: row.category, capabilities: [...row.capabilities] }));
      const existing = matrix.find((row) => row.category === category);
      if (!existing) {
        matrix.push({ category, capabilities: [capability] });
      } else if (existing.capabilities.includes(capability)) {
        existing.capabilities = existing.capabilities.filter((item) => item !== capability);
        if (existing.capabilities.length === 0) {
          return { ...profile, matrix: matrix.filter((row) => row.category !== category) };
        }
      } else {
        existing.capabilities = [...existing.capabilities, capability].sort();
      }
      return { ...profile, matrix: matrix.sort((a, b) => a.category.localeCompare(b.category)) };
    });
  }

  function addSettingsStepProfileCategory(profileId: string, category: string): void {
    updateStepProfileForm(profileId, (profile) => {
      if (profile.matrix.some((row) => row.category === category)) return profile;
      return {
        ...profile,
        matrix: [...profile.matrix, { category, capabilities: ['read'] }].sort((a, b) => a.category.localeCompare(b.category))
      };
    });
  }

  function removeSettingsStepProfileCategory(profileId: string, category: string): void {
    updateStepProfileForm(profileId, (profile) => ({
      ...profile,
      matrix: profile.matrix.filter((row) => row.category !== category)
    }));
  }

  function serializeStepProfileForm(profile: StepProfileFormState): Record<string, unknown> {
    const matrix = Object.fromEntries(
      profile.matrix
        .map((row) => [row.category.trim(), [...new Set(row.capabilities.map((item) => item.trim()).filter(Boolean))].sort()])
        .filter(([category, capabilities]) => category && capabilities.length > 0)
    );
    const include = profile.includeText.split(',').map((item) => item.trim()).filter(Boolean);
    const exclude = profile.excludeText.split(',').map((item) => item.trim()).filter(Boolean);
    return {
      name: profile.name.trim(),
      mode: profile.mode,
      config: {
        matrix,
        tool_overrides: {
          include,
          exclude
        },
        allow_tool_search: profile.allowToolSearch
      }
    };
  }

  async function saveStepProfile(profileId: string): Promise<void> {
    const profile = stepProfileForms.find((item) => item.profile_id === profileId);
    if (!profile) return;
    savingStepProfileIds = [...new Set([...savingStepProfileIds, profileId])];
    error = '';
    try {
      await api.settings.updateStepProfile(profileId, serializeStepProfileForm(profile));
      await refreshPageState();
      addToast('Step profile saved.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save step profile');
    } finally {
      savingStepProfileIds = savingStepProfileIds.filter((value) => value !== profileId);
    }
  }

  async function resetStepProfilePreset(profileId: string): Promise<void> {
    savingStepProfileIds = [...new Set([...savingStepProfileIds, profileId])];
    error = '';
    try {
      await api.settings.resetStepProfile(profileId);
      await refreshPageState();
      addToast('Step profile reset to default.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to reset step profile');
    } finally {
      savingStepProfileIds = savingStepProfileIds.filter((value) => value !== profileId);
    }
  }

  function isStepProfileSaving(profileId: string): boolean {
    return savingStepProfileIds.includes(profileId);
  }

  async function createStepProfile(): Promise<void> {
    savingStepProfileIds = [...new Set([...savingStepProfileIds, '__new__'])];
    error = '';
    try {
      await api.settings.createStepProfile({
        profile_id: newStepProfileForm.profile_id.trim(),
        name: newStepProfileForm.name.trim() || newStepProfileForm.profile_id.trim(),
        mode: newStepProfileForm.mode,
        config: {
          matrix: {},
          tool_overrides: { include: [], exclude: [] },
          allow_tool_search: true
        }
      });
      creatingStepProfile = false;
      newStepProfileForm = { profile_id: '', name: '', mode: 'soft' };
      await refreshPageState();
      addToast('Step profile created.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to create step profile');
    } finally {
      savingStepProfileIds = savingStepProfileIds.filter((value) => value !== '__new__');
    }
  }

  async function refreshPageState(): Promise<void> {
    isAdmin = auth.getSnapshot().user?.role === 'admin';
    if (!isAdmin && !USER_TABS.includes(activeTab)) {
      activeTab = 'account';
    }
    [secrets, credentials, health, apiKeys, agents, executorConfigs, executorTools] = await Promise.all([
      api.secrets.list(),
      api.credentials.list().catch(() => []),
      api.system.health(),
      api.auth.listApiKeys(),
      api.agents.list().then((page) => page.items.map((a) => ({ agent_id: a.agent_id, name: a.name, is_system: a.is_system }))),
      api.executor.list().catch(() => []),
      api.tools.executorTools().catch(() => [])
    ]);

    if (isAdmin) {
      [settings, modelRouting] = await Promise.all([
        api.settings.list(),
        api.modelRouting.get(),
      ]);
    } else {
      settings = [];
      modelRouting = emptyModelRouting();
    }

    routingForm = {
      default: {
        model: modelRouting.default.model ?? '',
        reasoningEffort: modelRouting.default.reasoning_effort ?? ''
      },
      classifier: {
        model: modelRouting.classifier.model ?? '',
        reasoningEffort: modelRouting.classifier.reasoning_effort ?? ''
      },
      compaction: {
        model: modelRouting.compaction.model ?? '',
        reasoningEffort: modelRouting.compaction.reasoning_effort ?? ''
      },
      evaluator: {
        model: modelRouting.evaluator.model ?? '',
        reasoningEffort: modelRouting.evaluator.reasoning_effort ?? ''
      },
      speech_to_text: {
        model: modelRouting.speech_to_text.model ?? '',
        reasoningEffort: ''
      },
      image_generation: {
        model: modelRouting.image_generation.model ?? '',
        reasoningEffort: ''
      },
      attachment_analysis: {
        model: modelRouting.attachment_analysis.model ?? '',
        reasoningEffort: ''
      }
    };

    if (isAdmin) {
      webConfig = await api.webConfig.status().catch(() => webConfig);
    } else {
      webConfig = { backend: 'direct', tavily_configured: false, brave_configured: false, available_backends: ['direct'] };
    }
    webBackendForm = webConfig.backend;

    // Initialize account name form
    accountNameForm = auth.getSnapshot().user?.name ?? '';
    accountNameDirty = false;

    mcpServerConfigs = await api.tools.listMcpServerConfigs().catch(() => []);

    if (isAdmin) {
      const profileDefs = await api.settings.stepProfiles().catch(() => []);
      [providers, diagnostics] = await Promise.all([
        api.llmProviders.list().then((page) => page.items),
        api.system.diagnostics(),
      ]);
      stepProfileForms = profileDefs.map(toStepProfileForm);
      await loadUsers();
    } else {
      providers = [];
      diagnostics = null;
      stepProfileForms = [];
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

  function setExecutorSaving(executorId: string, saving: boolean): void {
    if (saving) {
      if (!savingExecutorIds.includes(executorId)) {
        savingExecutorIds = [...savingExecutorIds, executorId];
      }
      return;
    }
    savingExecutorIds = savingExecutorIds.filter((value) => value !== executorId);
  }

  function isExecutorSaving(executorId: string): boolean {
    return savingExecutorIds.includes(executorId);
  }

  function canManageExecutor(exec: ExecutorConfig): boolean {
    const currentUserEmail = auth.getSnapshot().user?.email ?? null;
    const localExecutor = exec.executor_type === 'in_process' || exec.executor_type === 'subprocess';
    if (exec.shared || localExecutor) {
      return isAdmin;
    }
    return !exec.owner_email || exec.owner_email === currentUserEmail;
  }

  function canManageMcpServer(server: MCPServerConfigResponse): boolean {
    const currentUserEmail = auth.getSnapshot().user?.email ?? null;
    if (server.shared) {
      return isAdmin;
    }
    return !server.owner_email || server.owner_email === currentUserEmail;
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
    busy = true;
    error = '';
    try {
      modelRouting = await api.modelRouting.update({
        default: {
          model: routingForm.default.model || (routingForm.default.reasoningEffort ? effectiveRouteModelId('default') : '') || null,
          reasoning_effort: routingForm.default.reasoningEffort || null
        },
        classifier: {
          model: routingForm.classifier.model || (routingForm.classifier.reasoningEffort ? effectiveRouteModelId('classifier') : '') || null,
          reasoning_effort: routingForm.classifier.reasoningEffort || null
        },
        compaction: {
          model: routingForm.compaction.model || (routingForm.compaction.reasoningEffort ? effectiveRouteModelId('compaction') : '') || null,
          reasoning_effort: routingForm.compaction.reasoningEffort || null
        },
        evaluator: {
          model: routingForm.evaluator.model || (routingForm.evaluator.reasoningEffort ? effectiveRouteModelId('evaluator') : '') || null,
          reasoning_effort: routingForm.evaluator.reasoningEffort || null
        },
        speech_to_text: {
          model: routingForm.speech_to_text.model || null,
          reasoning_effort: null
        },
        image_generation: {
          model: routingForm.image_generation.model || null,
          reasoning_effort: null
        },
        attachment_analysis: {
          model: routingForm.attachment_analysis.model || null,
          reasoning_effort: null
        }
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

  async function saveCredential(): Promise<void> {
    if (!credentialForm.credential_id.trim() || !credentialForm.label.trim()) {
      error = 'Credential ID and label are required.';
      return;
    }
    busy = true;
    error = '';
    try {
      const payload = JSON.parse(credentialForm.payload_json || '{}');
      const metadata = JSON.parse(credentialForm.metadata_json || '{}');
      if (credentialForm.kind === 'browser_storage_state') {
        const origin = typeof metadata.origin === 'string' ? metadata.origin.trim() : '';
        const storageState = typeof payload.storage_state === 'object' && payload.storage_state !== null
          ? payload.storage_state
          : null;
        if (!storageState) {
          error = 'Browser auth state credentials require payload.storage_state with cookies/origins data.';
          return;
        }
        if (!origin) {
          error = 'Browser auth state credentials require metadata.origin, for example https://www.rohlik.cz.';
          return;
        }
        try {
          const parsed = new URL(origin);
          if (!parsed.protocol || !parsed.host) {
            error = 'metadata.origin must be a full origin URL such as https://www.rohlik.cz.';
            return;
          }
        } catch {
          error = 'metadata.origin must be a valid origin URL such as https://www.rohlik.cz.';
          return;
        }
      }
      await api.credentials.upsert({
        credential_id: credentialForm.credential_id,
        kind: credentialForm.kind,
        label: credentialForm.label,
        payload,
        metadata,
        scope: credentialForm.scope,
        agent_id: credentialForm.scope === 'agent' ? credentialForm.agent_id || null : null,
        description: credentialForm.description || null,
        expires_at: credentialForm.expires_at ? new Date(credentialForm.expires_at).toISOString() : null,
      });
      credentialForm = {
        ...credentialForm,
        credential_id: '',
        label: '',
        payload_json: credentialPayloadTemplate(credentialForm.kind),
        metadata_json: '{}',
        agent_id: '',
        description: '',
        expires_at: ''
      };
      credentials = await api.credentials.list();
      addToast('Credential saved.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save credential');
    } finally {
      busy = false;
    }
  }

  async function deleteCredential(credential: CredentialMetadata): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Delete credential?',
      message: `Delete ${credential.label}? Stored credential material cannot be recovered after deletion.`,
      confirmLabel: 'Delete credential'
    });
    if (!confirmed) return;
    busy = true;
    error = '';
    try {
      await api.credentials.remove(credential.credential_id);
      credentials = await api.credentials.list();
      addToast('Credential deleted.', 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to delete credential');
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
      await api.secrets.upsert({ name: secretName, value: webKeySetup.value, scope: 'system', description: `API key for ${WEB_BACKEND_INFO[webKeySetup.backend]?.label ?? webKeySetup.backend}` });
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
      openUrlInNewTab(buildLinkedServiceUrl(target, { token: exchange.token }));
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

    // Same-tab tap on Settings: reset to the default sub-tab (providers)
    // and scroll the content shell to the top. The bottom tab bar has
    // already navigated to `/settings` (bare path) so the `?tab=` query
    // is cleared; we only need to reset local state and scroll.
    const unsubTabReset = onTabReset('/settings', () => {
      activeTab = 'providers';
      clearPersistedScroll('/settings');
      const el = document.querySelector<HTMLElement>('[data-app-content="true"]');
      if (el) el.scrollTo({ top: 0, behavior: 'smooth' });
    });

    return () => {
      if (executorPollTimer) clearInterval(executorPollTimer);
      cleanup();
      unsubTabReset();
    };
  });

  $effect(() => {
    void activeTab;
    void mobileTabListEl;
    const activeTabButton = mobileTabListEl?.querySelector<HTMLElement>(`[data-settings-tab="${activeTab}"]`);
    activeTabButton?.scrollIntoView({ inline: 'center', block: 'nearest' });
  });

  $effect(() => {
    if (executorPollTimer) {
      clearInterval(executorPollTimer);
      executorPollTimer = null;
    }
    if (activeTab !== 'executors') return;
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
    <div bind:this={settingsPanelAnchor}></div>
    <!-- Mobile: horizontally scrollable pill strip. Desktop: flex-wrap.
         Previously 9 buttons wrapped to 3 lines on phones. -->
    <div class="sticky top-0 z-10 -mx-2 overflow-x-auto border-b border-slate-800/80 bg-slate-950/95 px-2 py-1 backdrop-blur sm:mx-0 sm:px-0 md:hidden">
      <div bind:this={mobileTabListEl} class="flex gap-2 pb-1" role="tablist" aria-label="Settings sections">
        {#each tabs as tab}
          <Button
            data-settings-tab={tab}
            class="shrink-0 snap-start"
            variant={activeTab === tab ? 'primary' : 'secondary'}
            size="sm"
            onclick={() => setActiveTab(tab)}
            aria-selected={activeTab === tab}
            role="tab"
          >{TAB_LABELS[tab]}</Button>
        {/each}
      </div>
    </div>
    <div class="hidden md:flex flex-wrap gap-2" role="tablist" aria-label="Settings sections">
      {#each tabs as tab}
        <Button variant={activeTab === tab ? 'primary' : 'secondary'} onclick={() => setActiveTab(tab)} role="tab" aria-selected={activeTab === tab}>{TAB_LABELS[tab]}</Button>
      {/each}
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}
    {#if notice}
      <p class="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{notice}</p>
    {/if}

    {#if activeTab === 'providers'}
      <div class="grid gap-5 lg:grid-cols-[340px_minmax(0,1fr)]">
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
                <button class={`w-full rounded-2xl border px-4 py-3 text-left transition ${selectedProviderId === provider.provider_id ? 'border-sky-400/40 bg-sky-500/10' : 'border-slate-800 bg-slate-950/70 hover:border-slate-700'}`} onclick={() => selectProvider(provider)}>
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

        <div bind:this={providerEditorEl}>
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
                      <span class="block text-xs text-sky-300">No credential selected. Create or select one.</span>
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

          {#if ['openai', 'openai_compatible', 'litellm_proxy'].includes(providerForm.preset)}
            <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-4 text-sm text-slate-200">
              <input bind:checked={providerForm.use_responses_api} type="checkbox" class="mt-1 rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300" />
              <span class="space-y-1">
                <span class="block font-medium">Use OpenAI Responses transport when supported</span>
                <span class="block text-xs text-slate-400">Recommended for `gpt-5*` models. Disable this if your provider or LiteLLM proxy behaves better on the legacy chat-completions path.</span>
              </span>
            </label>
          {/if}

          <!-- Models -->
          <div class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
            <div class="flex items-center justify-between gap-3">
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Models</p>
              <div class="flex gap-2">
                <Button size="sm" variant="secondary" onclick={discoverModels} disabled={busy || providerForm.location === 'executor'}>
                  Discover
                </Button>
              </div>
            </div>
            {#if providerForm.location === 'executor'}
              <p class="mt-3 text-xs text-slate-400">Model discovery runs from the controller. For executor-routed providers, add models manually.</p>
            {/if}

            <!-- Default model selection -->
            {#if providerForm.models.length > 0}
              <div class="mt-3">
                <label class="space-y-2 text-sm font-medium text-slate-200">
                  <span>Default model <span class="text-rose-300">*</span></span>
                  <select bind:value={providerForm.default_model} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    {#each providerForm.models as m}
                      <option value={m.model_id}>{m.model_id}</option>
                    {/each}
                  </select>
                </label>
              </div>
            {/if}

            <!-- Model cards -->
            <div class="mt-3 space-y-2">
              {#each providerForm.models as model (model.model_id)}
                <ModelCard
                  {model}
                  isDefault={model.model_id === providerForm.default_model}
                  onedit={() => (editingModel = { ...model })}
                  onremove={() => handleRemoveModel(model.model_id)}
                />
              {/each}
            </div>

            {#if providerForm.models.length === 0}
              <p class="mt-3 text-sm text-slate-400">No models configured. Click Discover to find available models, or add one manually below.</p>
            {/if}

            <!-- Manual add -->
            <div class="mt-3 flex gap-2">
              <Input bind:value={addModelId} placeholder="Add model by ID..." onkeydown={(e: KeyboardEvent) => e.key === 'Enter' && handleAddManualModel()} />
              <Button size="sm" variant="secondary" onclick={handleAddManualModel} disabled={busy || !addModelId.trim()}>Add</Button>
            </div>
          </div>

          <!-- Advanced settings -->
          {#if providerForm.advanced_settings.length > 0 || showAdvancedSettings}
            <div class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
              <button type="button" class="flex w-full items-center justify-between text-xs uppercase tracking-[0.25em] text-slate-400" onclick={() => (showAdvancedSettings = !showAdvancedSettings)}>
                <span>Advanced settings</span>
                <span class="text-slate-500">{showAdvancedSettings ? '−' : '+'}</span>
              </button>
              {#if showAdvancedSettings}
                <div class="mt-3 space-y-2">
                  {#each providerForm.advanced_settings as setting, i}
                    <div class="flex gap-2">
                      <Input bind:value={providerForm.advanced_settings[i].key} placeholder="key" />
                      <Input bind:value={providerForm.advanced_settings[i].value} placeholder="value" />
                      <Button size="sm" variant="ghost" onclick={() => (providerForm.advanced_settings = providerForm.advanced_settings.filter((_, idx) => idx !== i))}>x</Button>
                    </div>
                  {/each}
                  <Button size="sm" variant="secondary" onclick={() => (providerForm.advanced_settings = [...providerForm.advanced_settings, { key: '', value: '' }])}>+ Add setting</Button>
                </div>
                <p class="mt-2 text-xs text-slate-400">Additional key-value pairs merged into the provider config. Use for provider-specific litellm kwargs.</p>
              {/if}
            </div>
          {:else}
            <button type="button" class="text-xs text-slate-500 hover:text-slate-300 transition" onclick={() => (showAdvancedSettings = true)}>
              + Advanced settings
            </button>
          {/if}

          <!-- Actions -->
          <div class="flex flex-wrap gap-2 border-t border-slate-800 pt-4 pb-20 md:pb-0">
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
      </div>
      {#if isAdmin}
        <div
          class="fixed inset-x-0 z-30 border-t border-slate-800/80 bg-slate-950/95 px-3 py-2 backdrop-blur md:hidden"
          style="bottom: var(--app-shell-bottom-offset, 0px);"
        >
          <div class="flex items-center gap-2">
            <Button class="flex-1 justify-center" onclick={saveProvider} disabled={!isAdmin || busy}>
              {selectedProviderId ? 'Save provider' : 'Create provider'}
            </Button>
            <Button variant="secondary" onclick={resetProviderForm} disabled={busy}>Reset</Button>
          </div>
        </div>
      {/if}
    {:else if activeTab === 'routing'}
      <Card class="p-5">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Model routing</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Task-type routing</h2>
          </div>
        </div>

        <div class="mt-4 grid gap-4 md:grid-cols-2">
          {#each ROUTING_METADATA as route}
            <div class="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/50 p-4 text-sm font-medium text-slate-200">
              <div class="space-y-2">
                <span>{route.label}</span>
                <select bind:value={routingForm[route.key].model} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" onchange={() => syncRouteThinkingEffort(route.key)}>
                  <option value="">Use provider default</option>
                  {#each routeModelOptions(route.key) as option}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              </div>
              {#if route.supportsThinking}
                <div class="space-y-2">
                  <span>Thinking effort</span>
                  <select bind:value={routingForm[route.key].reasoningEffort} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={routeThinkingEffortOptions(route.key).length === 0}>
                    <option value="">Default</option>
                    {#each routeThinkingEffortOptions(route.key) as value}
                      <option value={value}>{thinkingEffortLabel(value)}</option>
                    {/each}
                  </select>
                  {#if routeThinkingEffortOptions(route.key).length === 0}
                    <span class="block text-xs text-slate-500">
                      Select or resolve a model first to choose a Thinking effort.
                    </span>
                  {:else}
                    <span class="block text-xs text-slate-500">Uses the model default when unset.</span>
                  {/if}
                </div>
              {/if}
              <span class="block text-xs text-slate-400">{route.description}</span>
            </div>
          {/each}
        </div>

        {#if routingWarnings().length > 0}
          <div class="mt-4 rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            {#each routingWarnings() as warning}
              <p>{warning}</p>
            {/each}
          </div>
        {/if}

        <div class="mt-5 flex justify-end">
          <Button onclick={saveRouting} disabled={!isAdmin || busy}>Save routing</Button>
        </div>
      </Card>
    {:else if activeTab === 'secrets'}
      <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
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
                {#if isAdmin}
                  <option value="system">System (shared infrastructure)</option>
                {/if}
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
      <div class="mt-5 grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="p-5">
          <div class="space-y-4">
            <p class="text-sm leading-6 text-slate-400">
              Structured credentials for agents and browser automation. Payload and metadata are stored separately from LLM context.
            </p>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Credential ID <span class="text-rose-300">*</span></span>
              <Input bind:value={credentialForm.credential_id} placeholder="github_work" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Kind</span>
              <select bind:value={credentialForm.kind} onchange={(event) => updateCredentialKind((event.currentTarget as HTMLSelectElement).value)} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="token">Token</option>
                <option value="text">Text</option>
                <option value="username_password">Username/password</option>
                <option value="totp_seed">TOTP seed</option>
                <option value="recovery_codes">Recovery codes</option>
                <option value="browser_storage_state">Browser auth state</option>
              </select>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Label <span class="text-rose-300">*</span></span>
              <Input bind:value={credentialForm.label} placeholder="GitHub work login" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Payload (JSON)</span>
              <textarea bind:value={credentialForm.payload_json} class="min-h-[140px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
              <span class="block text-xs text-slate-400">Expected payload template for <code>{credentialForm.kind}</code>. For login forms use <code>username</code>/<code>password</code>; for saved browser reuse use <code>browser_storage_state</code>.</span>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Metadata (JSON)</span>
              <textarea bind:value={credentialForm.metadata_json} class="min-h-[120px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100"></textarea>
              <span class="block text-xs text-slate-400">{credentialMetadataHint(credentialForm.kind)}</span>
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Scope</span>
              <select bind:value={credentialForm.scope} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="user">User</option>
                <option value="agent">Agent-specific</option>
              </select>
            </label>
            {#if credentialForm.scope === 'agent'}
              <label class="space-y-2 text-sm font-medium text-slate-200">
                <span>Agent</span>
                <select bind:value={credentialForm.agent_id} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                  <option value="">Select agent...</option>
                  {#each agents.filter((a) => !a.is_system) as agent}
                    <option value={agent.agent_id}>{agent.name} ({agent.agent_id})</option>
                  {/each}
                </select>
              </label>
            {/if}
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Expires at</span>
              <Input bind:value={credentialForm.expires_at} type="datetime-local" />
            </label>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Description</span>
              <Input bind:value={credentialForm.description} placeholder="What this credential is for" />
            </label>
            <Button class="w-full justify-center" onclick={saveCredential} disabled={busy}>Save credential</Button>
          </div>
        </Card>
        <Card class="p-5">
          <div class="space-y-3">
            {#each credentials as credential}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                <div>
                  <p class="font-medium text-white">{credential.label}</p>
                  <p class="text-xs text-slate-400">{credential.credential_id} · {credential.kind} · {credential.status}</p>
                </div>
                <Button size="sm" variant="danger" onclick={() => deleteCredential(credential)} disabled={busy}>Delete</Button>
              </div>
            {/each}
          </div>
        </Card>
      </div>
    {:else if activeTab === 'web'}
      <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
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
                  Get your key at <a href={WEB_BACKEND_INFO[webKeySetup.backend].link} target="_blank" rel="noopener noreferrer" class="text-sky-400 underline">{WEB_BACKEND_INFO[webKeySetup.backend].link}</a>
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
          <Button variant="primary" size="sm" onclick={() => { executorForm = { executor_id: '', name: '', executor_type: 'websocket', labels: '', status: 'active', shared: false }; editingExecutor = null; executorToken = null; showExecutorForm = true; }}>New executor</Button>
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
                  {#if isAdmin}
                    <option value="subprocess">subprocess</option>
                    <option value="in_process">in_process</option>
                  {/if}
                </select>
              </label>
              <label class="space-y-1 text-sm text-slate-200">
                <span>Labels (key=value, comma-separated)</span>
                <Input bind:value={executorForm.labels} placeholder="tier=standard, gpu=false" />
              </label>
              {#if isAdmin}
                <label class="flex items-center gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 px-4 py-3 text-sm text-slate-200 md:col-span-2">
                  <input bind:checked={executorForm.shared} type="checkbox" class="rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300" />
                  <span>Shared executor available to all users</span>
                </label>
              {/if}
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
                    await api.executor.update(editingExecutor.executor_id, { name: executorForm.name, labels, status: executorForm.status, shared: executorForm.shared });
                  } else {
                    await api.executor.create({ executor_id: executorForm.executor_id || null, name: executorForm.name, executor_type: executorForm.executor_type, labels, shared: executorForm.shared });
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
          {@const canManage = canManageExecutor(exec)}
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
                  <span class="px-2 py-0.5 bg-sky-500/20 text-sky-300 text-xs rounded">default</span>
                {/if}
                {#if exec.shared}
                  <span class="px-2 py-0.5 bg-cyan-500/20 text-cyan-300 text-xs rounded">shared</span>
                {/if}
              </div>
              <div class="flex gap-2">
                {#if canManage}
                  <Button variant="secondary" size="sm" onclick={() => {
                    editingExecutor = exec;
                    executorForm = {
                      executor_id: exec.executor_id,
                      name: exec.name,
                      executor_type: exec.executor_type,
                      labels: Object.entries(exec.labels || {}).map(([k, v]) => `${k}=${v}`).join(', '),
                      status: exec.status,
                      shared: !!exec.shared
                    };
                    showExecutorForm = true;
                  }}>Edit</Button>
                {/if}
                {#if canManage && exec.executor_type !== 'in_process'}
                  <Button variant="secondary" size="sm" onclick={async () => {
                    try {
                      executorToken = await api.executor.generateToken(exec.executor_id);
                      addToast('Executor token generated. Copy it now.', 'success');
                    } catch (e) {
                      error = asApiError(e).message;
                    }
                  }}>Generate token</Button>
                {/if}
                {#if canManage && !exec.is_default}
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
            {#if executorRuntimeSummary(exec)}
              <div class="text-xs {exec.runtime_state === 'degraded' ? 'text-sky-300' : 'text-slate-500'}">{executorRuntimeSummary(exec)}</div>
            {/if}
            {#if executorMcpFailureDetails(exec).length > 0}
              <div class="space-y-1 rounded-xl border border-sky-500/20 bg-sky-500/5 px-3 py-2 text-xs text-sky-100/90">
                {#each executorMcpFailureDetails(exec) as failure}
                  <p>{failure}</p>
                {/each}
              </div>
            {/if}
            {#if exec.desired_config_version !== exec.applied_config_version}
              <div class="text-xs text-sky-300">
                config pending: desired v{exec.desired_config_version}, applied v{exec.applied_config_version}
              </div>
            {/if}

            {#if canManage && executorToken && executorToken.executor_id === exec.executor_id}
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

            {#if canManage}
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

            <!-- Individual tool toggles.
                 Mobile: "Configure individual tools" button opens a Sheet
                 with a search field + scrollable list so the 30-40 tiny
                 chips don't overwhelm small viewports.
                 Desktop: grid as before. -->
            <div>
              <div class="flex items-center justify-between gap-3">
                <span class="text-xs uppercase tracking-wider text-slate-400">Individual tools</span>
                <Button
                  size="sm"
                  variant="secondary"
                  class="md:hidden"
                  onclick={() => { toolPickerExecutorId = exec.executor_id; toolPickerQuery = ''; }}
                >
                  Configure ({(exec.enabled_tools || []).filter((t: string) => t !== '*').length})
                </Button>
              </div>
              <div class="mt-2 hidden grid-cols-2 md:grid md:grid-cols-3 lg:grid-cols-4 gap-1.5">
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
                      <span class="text-sky-400 ml-0.5" title="Non-bypassable">!</span>
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

            <!-- Browser automation settings -->
            {@const browserConfig = ((exec.config || {}).browser || {}) as Record<string, unknown>}
            {@const browserEnabled = browserConfig.enabled !== false}
            {@const browserAutoInstall = browserConfig.auto_install === true}
            {@const browserPersistentProfilesEnabled = browserConfig.persistent_profiles_enabled !== false}
            {@const browserRealisticLaunch = browserConfig.realistic_launch !== false}
            {@const browserXvfbAuto = browserConfig.xvfb_auto !== false}
            <details class="group">
              <summary class="cursor-pointer text-xs uppercase tracking-wider text-slate-400 hover:text-slate-300 select-none">
                Browser Automation
                <span class="ml-1 text-slate-500">{browserEnabled ? '(enabled)' : '(disabled)'}</span>
              </summary>
              <div class="mt-3 space-y-3 pl-1">
                <div class="flex flex-wrap gap-4">
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                    <input type="checkbox" checked={browserEnabled}
                      class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, enabled: checked } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                        addToast(`Browser automation ${checked ? 'enabled' : 'disabled'}.`, 'success');
                      }}
                    />
                    Enabled
                  </label>
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                    <input type="checkbox" checked={browserAutoInstall} disabled={!browserEnabled}
                      class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, auto_install: checked } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                        addToast(`Browser auto-install ${checked ? 'enabled' : 'disabled'}.`, 'success');
                      }}
                    />
                    Auto-install Playwright browser
                  </label>
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                    <input type="checkbox" checked={browserPersistentProfilesEnabled} disabled={!browserEnabled}
                      class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, persistent_profiles_enabled: checked } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    Enable persistent local profiles
                  </label>
                </div>
                <div class="grid gap-3 md:grid-cols-3">
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Browser engine</span>
                    <select class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                      value={String(browserConfig.engine ?? 'chromium')}
                      onchange={async (e) => {
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, engine: e.currentTarget.value } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}>
                      <option value="chromium">Chromium</option>
                    </select>
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Max sessions</span>
                    <Input value={Number(browserConfig.max_sessions ?? 4)} disabled={!browserEnabled}
                      type="number" min="1" max="16" step="1"
                      onchange={async (e) => {
                        const val = parseInt(e.currentTarget.value, 10);
                        if (isNaN(val)) return;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, max_sessions: val } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Idle timeout (seconds)</span>
                    <Input value={Number(browserConfig.idle_timeout_seconds ?? 600)} disabled={!browserEnabled}
                      type="number" min="60" max="3600" step="60"
                      onchange={async (e) => {
                        const val = parseInt(e.currentTarget.value, 10);
                        if (isNaN(val)) return;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, idle_timeout_seconds: val } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Default profile mode</span>
                    <select class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                      value={String(browserConfig.profile_mode_default ?? 'persistent_local')}
                      onchange={async (e) => {
                        const value = e.currentTarget.value as 'ephemeral' | 'persistent_local';
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, profile_mode_default: value } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}>
                      <option value="persistent_local">Persistent local</option>
                      <option value="ephemeral">Ephemeral</option>
                    </select>
                  </label>
                </div>
                <div class="grid gap-3 md:grid-cols-3">
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Profile base dir</span>
                    <Input value={String(browserConfig.profile_base_dir ?? '')}
                      placeholder="~/.cognis/browser-profiles"
                      onchange={async (e) => {
                        const value = e.currentTarget.value.trim();
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, profile_base_dir: value || undefined } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Locale</span>
                    <Input value={String(browserConfig.locale ?? 'en-US')}
                      onchange={async (e) => {
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, locale: e.currentTarget.value || 'en-US' } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Timezone</span>
                    <Input value={String(browserConfig.timezone_id ?? '')}
                      placeholder="America/New_York"
                      onchange={async (e) => {
                        const value = e.currentTarget.value.trim();
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, timezone_id: value || undefined } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                </div>
                <div class="grid gap-3 md:grid-cols-3">
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Viewport width</span>
                    <Input value={Number(browserConfig.viewport_width ?? 1365)} disabled={!browserEnabled}
                      type="number" min="800" max="3840" step="1"
                      onchange={async (e) => {
                        const val = parseInt(e.currentTarget.value, 10);
                        if (isNaN(val)) return;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, viewport_width: val } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Viewport height</span>
                    <Input value={Number(browserConfig.viewport_height ?? 900)} disabled={!browserEnabled}
                      type="number" min="600" max="2160" step="1"
                      onchange={async (e) => {
                        const val = parseInt(e.currentTarget.value, 10);
                        if (isNaN(val)) return;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, viewport_height: val } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                </div>
                <label class="flex items-center gap-2 text-sm text-slate-300">
                  <input type="checkbox" checked={browserConfig.headed_allowed === true} disabled={!browserEnabled}
                    class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                    onchange={async (e) => {
                      const checked = e.currentTarget.checked;
                      const cfg = { ...(exec.config || {}), browser: { ...browserConfig, headed_allowed: checked } };
                      await api.executor.update(exec.executor_id, { config: cfg });
                      await refreshPageState();
                    }}
                  />
                  Allow headed mode on this executor
                </label>
                <label class="flex items-center gap-2 text-sm text-slate-300">
                  <input type="checkbox" checked={browserXvfbAuto} disabled={!browserEnabled}
                    class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                    onchange={async (e) => {
                      const checked = e.currentTarget.checked;
                      const cfg = { ...(exec.config || {}), browser: { ...browserConfig, xvfb_auto: checked } };
                      await api.executor.update(exec.executor_id, { config: cfg });
                      await refreshPageState();
                    }}
                  />
                  Auto-start Xvfb for headed Linux browser launches without DISPLAY
                </label>
                <label class="flex items-center gap-2 text-sm text-slate-300">
                  <input type="checkbox" checked={browserRealisticLaunch} disabled={!browserEnabled}
                    class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                    onchange={async (e) => {
                      const checked = e.currentTarget.checked;
                      const cfg = { ...(exec.config || {}), browser: { ...browserConfig, realistic_launch: checked } };
                      await api.executor.update(exec.executor_id, { config: cfg });
                      await refreshPageState();
                    }}
                  />
                  Use realistic launch defaults (persistent profile friendly desktop viewport and reduced automation signals)
                </label>
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
                      disabled={isExecutorSaving(exec.executor_id)}
                      onclick={async () => {
                        const ids = [...assignedIds];
                        if (assigned) {
                          ids.splice(ids.indexOf(srv.server_id), 1);
                        } else {
                          ids.push(srv.server_id);
                        }
                        const cfg = { ...(exec.config || {}), mcp_server_ids: ids };
                        setExecutorSaving(exec.executor_id, true);
                        try {
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                          addToast(
                            `${assigned ? 'Removed' : 'Assigned'} MCP server ${srv.name}.`,
                            'success'
                          );
                        } catch (caughtError) {
                          error = asApiError(caughtError).message;
                        } finally {
                          setExecutorSaving(exec.executor_id, false);
                        }
                      }}
                    >
                      {srv.name}
                      <span class="ml-1 text-xs opacity-60">{srv.transport}</span>
                    </button>
                  {/each}
                </div>
              </details>
            {/if}
            {:else}
              <div class="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm text-slate-400">
                This executor is available for use, but you do not have permission to change its configuration.
              </div>
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

          <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
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
        {#if isAdmin}
          <Card class="p-5 space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Step Profiles</p>
              <h2 class="mt-1 text-lg font-semibold text-white">Preset management</h2>
              <p class="mt-2 text-sm text-slate-400">
                These presets are used by workflows and direct chat tool exposure. Edits here change the runtime defaults globally.
              </p>
            </div>

            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="text-sm text-slate-400">Seeded profiles can be overridden and reset. Custom profiles can be created for new workflow shapes.</p>
              <Button variant="secondary" size="sm" onclick={() => { creatingStepProfile = !creatingStepProfile; }}>New preset</Button>
            </div>

            {#if creatingStepProfile}
              <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4 space-y-4">
                <div class="grid gap-4 md:grid-cols-3">
                  <label class="space-y-1 text-sm text-slate-200">
                    <span>ID</span>
                    <Input bind:value={newStepProfileForm.profile_id} placeholder="custom:office-lite" />
                  </label>
                  <label class="space-y-1 text-sm text-slate-200">
                    <span>Name</span>
                    <Input bind:value={newStepProfileForm.name} placeholder="Office Lite" />
                  </label>
                  <label class="space-y-1 text-sm text-slate-200">
                    <span>Mode</span>
                    <select class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" bind:value={newStepProfileForm.mode}>
                      <option value="soft">Soft</option>
                      <option value="hard">Hard</option>
                    </select>
                  </label>
                </div>
                <div class="flex items-center gap-2">
                  <Button variant="primary" size="sm" disabled={isStepProfileSaving('__new__')} onclick={createStepProfile}>Create preset</Button>
                  <Button variant="ghost" size="sm" onclick={() => { creatingStepProfile = false; newStepProfileForm = { profile_id: '', name: '', mode: 'soft' }; }}>Cancel</Button>
                </div>
              </div>
            {/if}

            <div class="space-y-4">
              {#each stepProfileForms as profile}
                <details
                  class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4"
                  open={openStepProfileIds.includes(profile.profile_id)}
                  ontoggle={(event) => {
                    const target = event.currentTarget as HTMLDetailsElement;
                    const isOpen = target.open;
                    openStepProfileIds = isOpen
                      ? [...new Set([...openStepProfileIds, profile.profile_id])]
                      : openStepProfileIds.filter((value) => value !== profile.profile_id);
                  }}
                >
                  <summary class="cursor-pointer list-none">
                    <div class="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div class="flex flex-wrap items-center gap-2">
                          <p class="font-medium text-white">{profile.name}</p>
                          <span class="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-xs text-slate-300">{profile.mode}</span>
                          <span class="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-xs text-slate-300">{profile.matrix.length} groups</span>
                          {#if profile.is_custom}
                            <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-xs text-sky-300">custom</span>
                          {:else if profile.has_override}
                            <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-xs text-sky-300">customized</span>
                          {/if}
                        </div>
                        <p class="mt-1 text-xs text-slate-500">{profile.profile_id}</p>
                      </div>
                    </div>
                  </summary>

                  <div class="mt-4 space-y-4">
                    <div class="flex flex-wrap gap-2 justify-end">
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={isStepProfileSaving(profile.profile_id)}
                        onclick={() => resetStepProfilePreset(profile.profile_id)}
                      >{profile.is_custom ? 'Delete preset' : 'Reset to default'}</Button>
                      <Button
                        size="sm"
                        variant="primary"
                        disabled={isStepProfileSaving(profile.profile_id)}
                        onclick={() => saveStepProfile(profile.profile_id)}
                      >Save preset</Button>
                    </div>

                    <div class="grid gap-4 md:grid-cols-3">
                    <label class="space-y-1 text-sm text-slate-200">
                      <span>Name</span>
                      <Input
                        value={profile.name}
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, name: event.currentTarget.value }))}
                      />
                    </label>
                    <label class="space-y-1 text-sm text-slate-200">
                      <span>Mode</span>
                      <select
                        class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                        value={profile.mode}
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, mode: (event.currentTarget.value === 'hard' ? 'hard' : 'soft') }))}
                      >
                        <option value="soft">Soft</option>
                        <option value="hard">Hard</option>
                      </select>
                    </label>
                    <label class="flex items-center gap-3 rounded-xl border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm text-slate-200">
                      <input
                        type="checkbox"
                        checked={profile.allowToolSearch}
                        class="h-4 w-4 rounded border-slate-600 bg-slate-950"
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, allowToolSearch: event.currentTarget.checked }))}
                      />
                      <span>Allow tool search</span>
                    </label>
                    </div>

                    <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                    <div class="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p class="text-sm font-medium text-slate-200">Capability matrix</p>
                        <p class="mt-1 text-xs text-slate-400">Rows are tool groups. Columns define the capabilities this preset exposes or allows.</p>
                      </div>
                      {#if availableStepProfileCategories(profile).length > 0}
                        <select
                          class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                          onchange={(event) => {
                            const target = event.currentTarget as HTMLSelectElement;
                            const category = target.value;
                            if (!category) return;
                            addSettingsStepProfileCategory(profile.profile_id, category);
                            target.value = '';
                          }}
                        >
                          <option value="">Add group…</option>
                          {#each availableStepProfileCategories(profile) as category}
                            <option value={category}>{category}</option>
                          {/each}
                        </select>
                      {/if}
                    </div>
                    <div class="mt-3 overflow-x-auto">
                      <table class="min-w-full border-separate border-spacing-y-2 text-sm text-slate-200">
                        <thead>
                          <tr class="text-left text-xs uppercase tracking-[0.2em] text-slate-500">
                            <th class="px-3 py-2">Group</th>
                            {#each STEP_PROFILE_CAPABILITIES as capability}
                              <th class="px-3 py-2">{capability}</th>
                            {/each}
                            <th class="px-3 py-2"></th>
                          </tr>
                        </thead>
                        <tbody>
                          {#each profile.matrix as row}
                            <tr class="rounded-xl border border-slate-800 bg-slate-950/70">
                              <td class="px-3 py-2 font-medium">{row.category}</td>
                              {#each STEP_PROFILE_CAPABILITIES as capability}
                                <td class="px-3 py-2">
                                  <input
                                    type="checkbox"
                                    checked={row.capabilities.includes(capability)}
                                    class="h-4 w-4 rounded border-slate-600 bg-slate-950"
                                    onchange={() => toggleSettingsStepProfileCapability(profile.profile_id, row.category, capability)}
                                  />
                                </td>
                              {/each}
                              <td class="px-3 py-2 text-right">
                                <button type="button" class="text-xs text-slate-400 hover:text-rose-300" onclick={() => removeSettingsStepProfileCategory(profile.profile_id, row.category)}>Remove</button>
                              </td>
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                    </div>
                    </div>

                    <div class="grid gap-4 md:grid-cols-2">
                    <label class="space-y-1 text-sm text-slate-200">
                      <span>Explicit include</span>
                      <Input
                        value={profile.includeText}
                        placeholder="tool_name, mcp:server:tool"
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, includeText: event.currentTarget.value }))}
                      />
                    </label>
                    <label class="space-y-1 text-sm text-slate-200">
                      <span>Explicit exclude</span>
                      <Input
                        value={profile.excludeText}
                        placeholder="tool_name, mcp:server:tool"
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, excludeText: event.currentTarget.value }))}
                      />
                    </label>
                    </div>
                  </div>
                </details>
              {/each}
            </div>
          </Card>
        {/if}

        <div class="flex items-center justify-between">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Tools</p>
            <h2 class="mt-1 text-lg font-semibold text-white">MCP Servers</h2>
            <p class="mt-2 text-sm text-slate-400">
              {isAdmin ? 'Configure private or shared MCP servers, then assign them to executors.' : 'Configure your private MCP servers, then assign them to your executors.'} Agents inherit MCP tools from their executor.
            </p>
          </div>
          <Button variant="primary" size="sm" onclick={() => {
            mcpForm = { name: '', transport: 'stdio', command: '', url: '', args: '', envVars: [], headers: [], timeout_seconds: 30, description: '', shared: false };
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
                  <Input bind:value={mcpForm.command} placeholder="e.g. npx" />
                  {#if validateStdioCommand(mcpForm.command)}
                    <p class="text-xs text-sky-300">{validateStdioCommand(mcpForm.command)}</p>
                  {:else}
                    <p class="text-xs text-slate-500">Use only the executable name or absolute path here. Put flags and package names into Arguments.</p>
                  {/if}
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
              {#if isAdmin}
                <label class="flex items-center gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 px-4 py-3 text-sm text-slate-200 md:col-span-2">
                  <input bind:checked={mcpForm.shared} type="checkbox" class="rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300" />
                  <span>Shared MCP server available to all users</span>
                </label>
              {/if}
            </div>
            {#if mcpForm.transport === 'stdio'}
              <label class="space-y-1 text-sm text-slate-200">
                <span>Arguments (one per line)</span>
                <textarea bind:value={mcpForm.args} class="min-h-[60px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 font-mono" placeholder="-y&#10;@doist/todoist-ai"></textarea>
              </label>
            {/if}
            <div class="space-y-1 text-sm text-slate-200">
              <EnvVarEditor
                envVars={mcpForm.transport === 'stdio' ? mcpForm.envVars : mcpForm.headers}
                {secrets}
                title={mcpForm.transport === 'stdio' ? 'Environment variables' : 'HTTP headers'}
                emptyMessage={mcpForm.transport === 'stdio' ? 'No environment variables configured.' : 'No HTTP headers configured.'}
                addLabel={mcpForm.transport === 'stdio' ? 'Add variable' : 'Add header'}
                keyPlaceholder={mcpForm.transport === 'stdio' ? 'GITHUB_TOKEN' : 'Authorization'}
                valuePlaceholder={mcpForm.transport === 'stdio' ? 'your-value' : 'Bearer token'}
                onChange={(next) => {
                  if (mcpForm.transport === 'stdio') {
                    mcpForm.envVars = next;
                  } else {
                    mcpForm.headers = next;
                  }
                }}
                onCreateSecret={openMcpSecretModal}
              />
            </div>
            <label class="space-y-1 text-sm text-slate-200">
              <span>Description</span>
              <Input bind:value={mcpForm.description} placeholder="Optional description" />
            </label>
            <div class="flex gap-2 justify-end">
              <Button variant="secondary" size="sm" onclick={() => showMcpForm = false}>Cancel</Button>
              <Button variant="primary" size="sm" disabled={!mcpForm.name.trim() || (mcpForm.transport === 'stdio' && !!validateStdioCommand(mcpForm.command))} onclick={async () => {
                const args = mcpForm.args.split('\n').flatMap(s => s.trim().split(/\s+/)).filter(Boolean);
                const env = serializeMcpEntries(mcpForm.envVars);
                const headers = serializeMcpEntries(mcpForm.headers);
                try {
                  if (editingMcpServer) {
                    await api.tools.updateMcpServer(editingMcpServer.server_id, {
                      name: mcpForm.name,
                      transport: mcpForm.transport,
                      command: mcpForm.transport === 'stdio' ? mcpForm.command : null,
                      url: mcpForm.transport !== 'stdio' ? mcpForm.url : null,
                      args: mcpForm.transport === 'stdio' ? args : [],
                      env: mcpForm.transport === 'stdio' ? env : {},
                      headers: mcpForm.transport !== 'stdio' ? headers : {},
                      timeout_seconds: mcpForm.timeout_seconds,
                      description: mcpForm.description || null,
                      shared: isAdmin ? mcpForm.shared : false,
                    });
                  } else {
                    await api.tools.createMcpServer({
                      name: mcpForm.name,
                      transport: mcpForm.transport,
                      command: mcpForm.transport === 'stdio' ? mcpForm.command : undefined,
                      url: mcpForm.transport !== 'stdio' ? mcpForm.url : undefined,
                      args: mcpForm.transport === 'stdio' ? args : [],
                      env: mcpForm.transport === 'stdio' ? env : {},
                      headers: mcpForm.transport !== 'stdio' ? headers : {},
                      timeout_seconds: mcpForm.timeout_seconds,
                      description: mcpForm.description || undefined,
                      shared: isAdmin ? mcpForm.shared : false,
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
          {@const canManageMcp = canManageMcpServer(srv)}
          <Card class="p-5 space-y-3">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-3">
                <h3 class="text-lg font-medium text-white">{srv.name}</h3>
                <span class="px-2 py-0.5 bg-zinc-700 text-zinc-300 text-xs font-mono rounded">{srv.transport}</span>
                <span class="px-2 py-0.5 rounded text-xs {srv.status === 'active' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-zinc-700 text-zinc-400'}">{srv.status}</span>
                {#if srv.shared}
                  <span class="px-2 py-0.5 bg-cyan-500/20 text-cyan-300 text-xs rounded">shared</span>
                {/if}
              </div>
              <div class="flex gap-2">
                {#if canManageMcp}
                <Button variant="secondary" size="sm" onclick={() => {
                  editingMcpServer = srv;
                  mcpForm = {
                    name: srv.name,
                    transport: srv.transport,
                    command: srv.command || '',
                    url: srv.url || '',
                    args: (srv.args || []).join('\n'),
                    envVars: parseMcpEntries(srv.env || {}),
                    headers: parseMcpEntries(srv.headers || {}),
                    timeout_seconds: srv.timeout_seconds,
                    description: srv.description || '',
                    shared: !!srv.shared,
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
                {/if}
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
            {#if srv.invalid_reason}
              <p class="text-sm text-sky-300">{srv.invalid_reason}</p>
            {/if}
            <div class="text-xs text-slate-500 font-mono">ID: {srv.server_id}</div>
            {#if !canManageMcp}
              <p class="text-xs text-slate-500">This shared MCP server is available to assign to your executors, but only an administrator can edit it.</p>
            {/if}
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
      <div class="grid gap-5 lg:grid-cols-[minmax(0,1fr)_420px]">
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

<!-- Mobile searchable tool picker for executor tools.
     Opens when the "Configure (N)" button under Individual tools is tapped.
     Re-uses the same per-tool toggle logic as the desktop grid. -->
{#if toolPickerExecutorId}
  {@const exec = executorConfigs.find((e) => e.executor_id === toolPickerExecutorId)}
  {#if exec && canManageExecutor(exec)}
    {@const query = toolPickerQuery.trim().toLowerCase()}
    {@const filteredTools = query
      ? executorTools.filter((t) => t.name.toLowerCase().includes(query) || (t.description ?? '').toLowerCase().includes(query) || t.category.toLowerCase().includes(query))
      : executorTools}
    <Sheet open={true} onClose={() => (toolPickerExecutorId = null)} side="bottom" label={`Configure tools for ${exec.name}`}>
      {#snippet header()}
        <div class="space-y-3">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Executor</p>
            <h3 class="mt-1 text-lg font-semibold text-white">{exec.name}</h3>
          </div>
          <Input type="search" bind:value={toolPickerQuery} placeholder="Search tools by name, description, or group…" aria-label="Filter tools" />
        </div>
      {/snippet}
      <div class="space-y-1.5">
        {#each filteredTools as tool}
          {@const enabledByGroup = (exec.enabled_tool_groups || []).includes(tool.category)}
          {@const enabledByName = (exec.enabled_tools || []).includes(tool.name) || (exec.enabled_tools || []).includes('*')}
          {@const enabled = enabledByGroup || enabledByName}
          <button
            type="button"
            class="w-full rounded-xl border px-3 py-3 text-left transition-colors {enabled ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-100' : 'bg-slate-900 border-slate-700 text-slate-300 hover:border-slate-600'}"
            onclick={() => {
              if (enabledByGroup) return;
              // Compute the target list from the latest state: pending queue
              // if one is in flight, otherwise the current executor snapshot.
              // This prevents rapid taps from clobbering each other.
              const pending = toolUpdateQueues.get(exec.executor_id)?.pending;
              const base = pending ?? (exec.enabled_tools || []);
              const tools = [...base].filter((t: string) => t !== '*');
              const idx = tools.indexOf(tool.name);
              if (idx >= 0) {
                tools.splice(idx, 1);
              } else {
                tools.push(tool.name);
              }
              void queueExecutorToolUpdate(exec.executor_id, tools);
            }}
          >
            <div class="flex items-center justify-between gap-3">
              <span class="font-mono text-sm text-slate-100">{tool.name}</span>
              <span class="shrink-0 rounded-full border border-slate-700 px-2 py-0.5 text-[10px] uppercase tracking-wider text-slate-400">{tool.category}</span>
            </div>
            {#if tool.description}
              <p class="mt-1.5 text-xs text-slate-400">{tool.description}</p>
            {/if}
            <div class="mt-1.5 flex flex-wrap gap-2 text-[11px]">
              {#if enabledByGroup}
                <span class="text-sky-300">via group</span>
              {:else if enabled}
                <span class="text-emerald-300">enabled</span>
              {:else}
                <span class="text-slate-500">disabled</span>
              {/if}
              {#if tool.non_bypassable}
                <span class="text-sky-400">non-bypassable</span>
              {/if}
            </div>
          </button>
        {/each}
        {#if filteredTools.length === 0}
          <p class="py-6 text-center text-sm text-slate-500">No tools match "{toolPickerQuery}".</p>
        {/if}
      </div>
    </Sheet>
  {/if}
{/if}

<!-- Secret creation modal -->
{#if showSecretModal}
  <BlockingDialog label="Create credential secret" onClose={() => (showSecretModal = false)} titleId="secret-modal-title">
    {#snippet header()}
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">New credential</p>
          <h3 class="mt-1 text-lg font-semibold text-white" id="secret-modal-title">{secretModalTarget === 'provider' ? 'Create API key secret' : 'Create environment secret'}</h3>
        </div>
        <Button aria-label="Close credential dialog" size="icon" variant="secondary" onclick={() => (showSecretModal = false)}>&times;</Button>
      </div>
    {/snippet}

    {#snippet children()}
      <div class="space-y-4">
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Secret name</span>
          <Input bind:value={secretModalName} placeholder="openai_api_key" />
        </label>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>{secretModalTarget === 'provider' ? 'API key value' : 'Secret value'}</span>
          <Input bind:value={secretModalValue} type="password" placeholder={secretModalTarget === 'provider' ? 'sk-...' : 'secret-value'} />
        </label>
      </div>
    {/snippet}

    {#snippet footer()}
      <div class="flex justify-end gap-2">
        <Button variant="secondary" onclick={() => (showSecretModal = false)}>Cancel</Button>
        <Button onclick={saveSecretFromModal} disabled={busy || !secretModalName.trim() || !secretModalValue.trim()}>Save credential</Button>
      </div>
    {/snippet}
  </BlockingDialog>
{/if}

<!-- User create modal -->
{#if showUserCreateModal}
  <BlockingDialog label="Create user" onClose={() => (showUserCreateModal = false)} titleId="user-create-title">
    {#snippet header()}
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Administration</p>
          <h3 class="mt-1 text-lg font-semibold text-white" id="user-create-title">Create user</h3>
        </div>
        <Button aria-label="Close user creation dialog" size="icon" variant="secondary" onclick={() => (showUserCreateModal = false)}>&times;</Button>
      </div>
    {/snippet}

    {#snippet children()}
      <div class="space-y-4">
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
    {/snippet}

    {#snippet footer()}
      <div class="flex justify-end gap-2">
        <Button variant="secondary" onclick={() => (showUserCreateModal = false)}>Cancel</Button>
        <Button onclick={createUserSubmit} disabled={busy || !userCreateForm.email.trim() || userCreateForm.password.length < 8 || userCreateForm.password !== userCreateForm.confirm_password}>Create user</Button>
      </div>
    {/snippet}
  </BlockingDialog>
{/if}

<!-- User edit modal -->
{#if showUserEditModal && editingUser}
  <BlockingDialog label="Edit user" onClose={() => { showUserEditModal = false; editingUser = null; }} titleId="user-edit-title">
    {#snippet header()}
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Administration</p>
          <h3 class="mt-1 text-lg font-semibold text-white" id="user-edit-title">Edit user</h3>
          <p class="mt-1 text-sm text-slate-400">{editingUser?.email}</p>
        </div>
        <Button aria-label="Close user editor" size="icon" variant="secondary" onclick={() => { showUserEditModal = false; editingUser = null; }}>&times;</Button>
      </div>
    {/snippet}

    {#snippet children()}
      <div class="space-y-4">
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
    {/snippet}

    {#snippet footer()}
      <div class="flex justify-end gap-2">
        <Button variant="secondary" onclick={() => { showUserEditModal = false; editingUser = null; }}>Cancel</Button>
        <Button onclick={updateUserSubmit} disabled={busy}>Save changes</Button>
      </div>
    {/snippet}
  </BlockingDialog>
{/if}

<!-- Model discovery modal -->
{#if showModelDiscovery && providerForm.discovered_models.length > 0}
  <ModelDiscoveryModal
    models={providerForm.discovered_models}
    existingModelIds={providerForm.models.map((m) => m.model_id)}
    onclose={() => (showModelDiscovery = false)}
    onadd={handleAddDiscoveredModels}
  />
{/if}

<!-- Model edit modal -->
{#if editingModel}
  <ModelEditModal
    model={editingModel}
    onclose={() => (editingModel = null)}
    onsave={handleSaveModelEdit}
  />
{/if}
