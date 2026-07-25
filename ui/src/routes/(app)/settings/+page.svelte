<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { onMount, tick } from 'svelte';

  import type { MCPEnvVar } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import { deriveGettingStartedSteps } from '$lib/getting-started';
  import { formatMcpOAuthStatus, isMcpOAuthStatusCritical, type MCPOAuthStatus } from '$lib/mcp-oauth-status';
  import { collectModelOptions, createProviderForm, deriveProviderId, parseExecutorSelector, presetHasBaseUrl, presetNeedsAuth, PRESET_LABELS, providerExecutorTargetError, providerFormToPayload, providerFormToUpdatePayload, providerRequiresExecutorLocation, type ProviderFormState, type ProviderModelOption, type ProviderPreset } from '$lib/providers';
  import { STEP_PROFILE_CAPABILITIES, STEP_PROFILE_GROUPS } from '$lib/workflows';
  import { defaultModelEntry, type LocalModelDeployment, type LocalModelTargetStatus, type ModelEntry } from '$lib/types/api';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ProviderStatusBadge from '$lib/components/ProviderStatusBadge.svelte';
  import EnvVarEditor from '$lib/components/settings/EnvVarEditor.svelte';
  import ExecutorHealthPanel from '$lib/components/executors/ExecutorHealthPanel.svelte';
  import LocalInferenceSettings from '$lib/components/executors/LocalInferenceSettings.svelte';
  import ModelCard from '$lib/components/settings/ModelCard.svelte';
  import ModelEditModal from '$lib/components/settings/ModelEditModal.svelte';
  import ModelDiscoveryModal from '$lib/components/settings/ModelDiscoveryModal.svelte';
  import SystemSettingsEditor from '$lib/components/settings/SystemSettingsEditor.svelte';
  import WebBackendEditModal from '$lib/components/settings/WebBackendEditModal.svelte';
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
  import { loadUserPreferences, saveUserPreferences, userPreferences } from '$lib/stores/userPreferences';
  import { wsClient } from '$lib/ws/client';
  import {
    createWebBackendEditValue,
    webBackendConfigured,
    webBackendStatusLabel,
    type EditableWebBackend,
    type WebBackendEditValue
  } from '$lib/web-backends';
  import {
    disableWebPushForCurrentDevice,
    enableWebPush,
    hasEnabledWebPush,
    isStandaloneDisplay,
    isWebPushSupported,
    needsIosHomeScreenInstall,
    permissionState,
    reconcileWebPushSubscription
  } from '$lib/notifications';
  import {
    executorDegradedDetails,
    executorRuntimeBadgeStatus,
    executorRuntimeLabel,
    executorRuntimeSummary,
    providerInferenceExecutors,
    providerSelectorCapabilityWarning,
    validateStdioCommand
  } from '$lib/executors';
  import type {
    ApiKeyCreateResponse,
    ApiKeyMetadata,
    CredentialMetadata,
    ExecutorConfig,
    ExecutorRuntimeConfig,
    ExecutorTokenResponse,
    HealthResponse,
    LLMProvider,
    LLMProviderOAuthStatus,
    CodexUsage,
    CodexUsageWindow,
    ModelRouting,
    ProviderTestResult,
    PushSubscriptionStatusResponse,
    PushSubscriptionTestResponse,
    SecretMetadata,
    Setting,
    SettingsCategory,
    SystemDiagnostics,
    MCPAuthConfig,
    MCPServerConfigResponse,
    StepProfileDefinition,
    ToolDefinitionSummary,
    UserDetail,
    UserPreferences,
    UserRole,
    WebConfigStatus
  } from '$lib/types/api';

  type SettingsTab = 'providers' | 'routing' | 'secrets' | 'notifications' | 'display' | 'web' | 'tools' | 'executors' | 'users' | 'system' | 'account';
  type CredentialKind = 'token' | 'text' | 'username_password' | 'totp_seed' | 'recovery_codes' | 'browser_storage_state';
  type MCPAuthType = 'none' | 'static_headers' | 'oauth2';
  type MCPOAuthCallbackMode = 'auto' | 'controller_public' | 'executor_loopback';
  type MCPServerFormState = {
    name: string;
    transport: string;
    command: string;
    url: string;
    args: string;
    envVars: MCPEnvVar[];
    headers: MCPEnvVar[];
    timeout_seconds: number;
    description: string;
    shared: boolean;
    authType: MCPAuthType;
    oauthIssuer: string;
    oauthAuthorizationServer: string;
    oauthResource: string;
    oauthScopes: string;
    oauthClientId: string;
    oauthClientSecretRef: string;
    oauthRedirectUri: string;
    oauthCallbackMode: MCPOAuthCallbackMode;
    oauthExecutorId: string;
    oauthDynamicClientRegistration: boolean;
    oauthClientMetadataDocumentUrl: string;
    oauthAuthorizationParams: MCPEnvVar[];
  };
  type MCPOAuthStartState = {
    authorizationUrl: string;
    callbackMode?: string | null;
    oauthExecutorId?: string | null;
    oauthExecutorName?: string | null;
    instructions?: string | null;
    copied: boolean;
  };

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

  const ALL_TABS: SettingsTab[] = ['providers', 'routing', 'secrets', 'notifications', 'display', 'web', 'tools', 'executors', 'users', 'system', 'account'];
  const USER_TABS: SettingsTab[] = ['providers', 'secrets', 'notifications', 'display', 'tools', 'executors', 'account'];
  const TAB_LABELS: Record<SettingsTab, string> = {
    providers: 'providers',
    routing: 'routing',
    secrets: 'secrets',
    notifications: 'notifications',
    display: 'display',
    web: 'web search',
    tools: 'tools',
    executors: 'executors',
    users: 'users',
    system: 'system',
    account: 'account'
  };
  const ROUTING_KEYS = ['default', 'classifier', 'compaction', 'evaluator', 'speech_to_text', 'text_to_speech', 'image_generation', 'attachment_analysis', 'embedding'] as const;
  const TEXT_ROUTING_KEYS = ['default', 'classifier', 'compaction', 'evaluator'] as const;
  const SAME_SESSION_MODEL_SENTINEL = '__same_session_model__';
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
    { key: 'compaction', label: 'compaction', description: 'Context compaction summaries. Defaults to the same model as the active agent session.', supportsThinking: true },
    { key: 'evaluator', label: 'evaluator', description: 'Workflow step evaluation. Falls back to default if not set.', supportsThinking: true },
    { key: 'speech_to_text', label: 'speech_to_text', description: 'Voice-note transcription. Use models like gpt-4o-transcribe, gpt-4o-mini-transcribe, or whisper.', supportsThinking: false },
    { key: 'text_to_speech', label: 'text_to_speech', description: 'Voice synthesis for the speaker button and conversation mode. Use models like tts-1, tts-1-hd, gpt-4o-mini-tts, eleven_multilingual_v2, or a Piper-compatible HTTP server.', supportsThinking: false },
    { key: 'image_generation', label: 'image_generation', description: 'Image-capable model for avatars and tools. Must support image generation.', supportsThinking: false },
    { key: 'attachment_analysis', label: 'attachment_analysis', description: 'Fallback model for artifact_read and binary read analysis when the main chat model lacks image/PDF/file capabilities.', supportsThinking: false },
    { key: 'embedding', label: 'embedding', description: 'Embedding model route for Knowledgebase indexing and vector search. Use models like text-embedding-3-small, bge, e5, or gte.', supportsThinking: false }
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
      text_to_speech: { model: null, reasoning_effort: null },
      image_generation: { model: null, reasoning_effort: null },
      attachment_analysis: { model: null, reasoning_effort: null },
      embedding: { model: null, reasoning_effort: null }
    };
  }

  function emptyRoutingForm(): Record<RoutingKey, RoutingFormEntry> {
    return {
      default: emptyRoutingEntry(),
      classifier: emptyRoutingEntry(),
      compaction: emptyRoutingEntry(),
      evaluator: emptyRoutingEntry(),
      speech_to_text: emptyRoutingEntry(),
      text_to_speech: emptyRoutingEntry(),
      image_generation: emptyRoutingEntry(),
      attachment_analysis: emptyRoutingEntry(),
      embedding: emptyRoutingEntry()
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
  let localModelDeployments = $state<LocalModelDeployment[]>([]);
  let localModelTargets = $state<Record<string, LocalModelTargetStatus[]>>({});
  let localModelsUnavailable = $state(false);
  let modelRouting = $state<ModelRouting>(emptyModelRouting());
  let secrets = $state<SecretMetadata[]>([]);
  let credentials = $state<CredentialMetadata[]>([]);
  let health = $state<HealthResponse | null>(null);
  let diagnostics = $state<SystemDiagnostics | null>(null);
  let executorConfigs = $state<ExecutorConfig[]>([]);
  let executorTools = $state<ToolDefinitionSummary[]>([]);
  let editingExecutor = $state<ExecutorConfig | null>(null);
  let webConfig = $state<WebConfigStatus>({
    backend: 'direct',
    search_backend: 'direct',
    fetch_backend: 'direct',
    fetch_fallback_browser: true,
    browser_fetch_session_idle_seconds: 60,
    browser_fetch_wait_timeout_seconds: 30,
    browser_fetch_navigation_timeout_seconds: 60,
    browser_fetch_wait_until: 'domcontentloaded',
    browser_fetch_network_idle_after_dom_seconds: 3,
    browser_fetch_headed_fallback_enabled: true,
    tavily_configured: false,
    tavily_enabled: true,
    brave_configured: false,
    brave_enabled: true,
    searxng_url: '',
    searxng_engines: '',
    searxng_categories: '',
    searxng_language: '',
    searxng_configured: false,
    searxng_enabled: true,
    available_backends: ['direct'],
    available_search_backends: ['direct'],
    available_fetch_backends: ['direct'],
  });
  let webBackendForm = $state('direct');
  let webSearchBackendForm = $state('direct');
  let webFetchBackendForm = $state('direct');
  let webFetchFallbackBrowserForm = $state(true);
  let webBrowserFetchSessionIdleForm = $state(60);
  let webBrowserFetchWaitTimeoutForm = $state(30);
  let webBrowserFetchNavigationTimeoutForm = $state(60);
  let webBrowserFetchWaitUntilForm = $state('domcontentloaded');
  let webBrowserFetchNetworkIdleForm = $state(3);
  let webBrowserFetchHeadedFallbackForm = $state(false);
  let editingWebBackend = $state<WebBackendEditValue | null>(null);
  let showExecutorForm = $state(false);
  let executorForm = $state({ executor_id: '', name: '', executor_type: 'websocket', labels: '', status: 'active', shared: false, is_default: false });
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
  const executorLocalInferenceSaveSequences = new Map<string, number>();

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
  let mcpForm = $state<MCPServerFormState>(createEmptyMcpForm());
  let mcpOAuthStatuses = $state<Record<string, MCPOAuthStatus>>({});
  let mcpOAuthStarts = $state<Record<string, MCPOAuthStartState>>({});
  let isAdmin = $state(false);
  let tabs = $derived(isAdmin ? ALL_TABS : USER_TABS);
  let selectedProviderId = $state('');
  let systemSettingsDirty = $state(false);
  let providerForm = $state<ProviderFormState>(createProviderForm());
  let providerTestResult = $state<ProviderTestResult | null>(null);
  let providerOAuthStatus = $state<LLMProviderOAuthStatus | null>(null);
  let providerCodexUsage = $state<CodexUsage | null>(null);
  let providerCodexUsageError = $state('');
  let anthropicOAuthCallbackInput = $state('');
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
  let pushVapid = $state<{ enabled: boolean; public_key: string | null; reason: string | null } | null>(null);
  let pushStatus = $state<PushSubscriptionStatusResponse | null>(null);
  let pushTestResult = $state<PushSubscriptionTestResponse | null>(null);
  let pushPermission = $state<NotificationPermission | 'unsupported'>('unsupported');
  let pushSupported = $state(false);
  let pushStandalone = $state(false);
  let pushNeedsInstall = $state(false);
  let pushEnabledOnDevice = $state(false);
  let pushBusy = $state(false);
  let pushError = $state('');
  let initialSnapshot = $state('');

  // User management state
  let userList = $state<UserDetail[]>([]);
  let showUserCreateModal = $state(false);
  let showUserEditModal = $state(false);
  let showDisabledUsers = $state(false);
  let editingUser = $state<UserDetail | null>(null);
  let userCreateForm = $state({ email: '', name: '', password: '', confirm_password: '', role: 'user' as UserRole });
  let userEditForm = $state({ name: '', role: 'user' as UserRole, password: '', confirm_password: '' });
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
      webSearchBackendForm,
      webFetchBackendForm,
      webFetchFallbackBrowserForm
    });
  }

  function isDirty(): boolean {
    return systemSettingsDirty || snapshotState() !== initialSnapshot;
  }

  beforeNavigate((navigation) => {
    if (busy) {
      return;
    }
    blockNavigationIfDirty(navigation, isDirty);
  });

  async function confirmDiscardChanges(message = 'Switching tabs or providers will replace the current unsaved edits.'): Promise<boolean> {
    if (!isDirty()) {
      return true;
    }
    return confirmAction({
      title: 'Discard unsaved changes?',
      message,
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

  function selectedProvider(): LLMProvider | null {
    return providers.find((provider) => provider.provider_id === selectedProviderId) ?? null;
  }

  function selectedProviderDeployments(): LocalModelDeployment[] {
    return localModelDeployments.filter(
      (deployment) => deployment.provider_id === selectedProviderId
    );
  }

  function deploymentRollout(deployment: LocalModelDeployment): string {
    const targets = localModelTargets[deployment.deployment_id] ?? [];
    if (targets.length === 0) return 'Waiting for target status';
    const ready = targets.filter((target) => target.state === 'ready').length;
    const blocked = targets.filter(
      (target) => target.state === 'blocked' || target.state === 'error'
    ).length;
    return blocked > 0
      ? `${ready}/${targets.length} ready · ${blocked} blocked`
      : `${ready}/${targets.length} ready`;
  }

  function modelOptions(): ProviderModelOption[] {
    return collectModelOptions(providers);
  }

  function looksLikeTranscriptionModel(value: string): boolean {
    const normalized = value.trim().toLowerCase().replaceAll('_', '-');
    return normalized.includes('transcribe') || normalized.includes('whisper') || normalized.includes('speech-to-text');
  }

  function looksLikeTtsModel(value: string): boolean {
    const normalized = value.trim().toLowerCase().replaceAll('_', '-');
    return (
      normalized.includes('tts') ||
      normalized.includes('text-to-speech') ||
      normalized.includes('speech-1') ||
      normalized.includes('eleven') ||
      normalized.includes('elevenlabs') ||
      normalized.includes('piper')
    );
  }

  function looksLikeEmbeddingModel(value: string): boolean {
    const normalized = value.trim().toLowerCase().replaceAll('_', '-');
    return (
      normalized.includes('embedding') ||
      normalized.includes('embed-') ||
      normalized.includes('-embed') ||
      normalized.includes('e5-') ||
      normalized.includes('bge-') ||
      normalized.includes('gte-') ||
      normalized.includes('nomic-embed')
    );
  }

  function looksLikeReasoningModel(value: string): boolean {
    const normalized = value.trim().toLowerCase().replaceAll('_', '-');
    return (
      normalized.includes('claude-3-7') ||
      normalized.includes('sonnet-4') ||
      normalized.includes('opus-4') ||
      normalized.startsWith('gpt-5') ||
      normalized.startsWith('o1') ||
      normalized.startsWith('o3') ||
      normalized.startsWith('o4') ||
      normalized.includes('gemini-2.5') ||
      normalized.includes('reason') ||
      normalized.includes('think') ||
      normalized.includes('deepseek-r1') ||
      normalized.includes('qwq') ||
      normalized.includes('qwen3') ||
      normalized.includes('grok-4') ||
      normalized.includes('kimi-k2')
    );
  }

  function inferredReasoningEfforts(modelId: string, entry: ModelEntry | null): string[] {
    if (!looksLikeReasoningModel(modelId) && !looksLikeReasoningModel(entry?.display_name ?? '')) {
      return [];
    }
    const normalized = `${modelId} ${entry?.display_name ?? ''}`.trim().toLowerCase().replaceAll('_', '-');
    if (normalized.includes('claude') || normalized.includes('gemini-2.5')) {
      return ['none', 'low', 'medium', 'high', 'max'];
    }
    if (normalized.startsWith('gpt-5') || normalized.includes(' gpt-5')) {
      return ['none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
    }
    return ['none', 'low', 'medium', 'high'];
  }

  function preferredProviderIdForModel(modelId: string): string | null {
    const normalized = modelId.trim();
    if (!normalized) {
      return null;
    }
    return modelOptions().find((option) => option.value === normalized)?.providerId ?? null;
  }

  function findModelEntry(modelId: string, providerId: string | null = null): ModelEntry | null {
    const normalized = modelId.trim();
    if (!normalized) {
      return null;
    }
    if (providerId) {
      const provider = providers.find((item) => item.provider_id === providerId);
      const providerMatch = provider?.models.find((model) => model.model_id === normalized) ?? null;
      if (providerMatch) {
        return providerMatch;
      }
    }
    for (const provider of providers) {
      const match = provider.models.find((model) => model.model_id === normalized);
      if (match) {
        return match;
      }
    }
    return null;
  }

  function modelSupportsEmbedding(entry: unknown): boolean {
    if (!entry || typeof entry !== 'object') {
      return false;
    }
    const model = entry as { display_name?: string; model_id?: string; supports_embedding?: boolean };
    if ('supports_embedding' in model) {
      return model.supports_embedding === true;
    }
    return Boolean(
      looksLikeEmbeddingModel(model.model_id ?? '') ||
        looksLikeEmbeddingModel(model.display_name ?? '')
    );
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
    if (routeKey === 'compaction') {
      return SAME_SESSION_MODEL_SENTINEL;
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
    const modelId = effectiveRouteModelId(routeKey);
    if (modelId === SAME_SESSION_MODEL_SENTINEL) {
      return [];
    }
    const modelEntry = findModelEntry(modelId, preferredProviderIdForModel(modelId));
    const explicitEfforts = (modelEntry?.reasoning_efforts ?? []).filter((value) => value !== 'default');
    return explicitEfforts.length > 0 ? explicitEfforts : inferredReasoningEfforts(modelId, modelEntry);
  }

  function routeModelOptions(routeKey: RoutingKey): ProviderModelOption[] {
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
    if (routeKey === 'embedding') {
      return options.filter((option) => modelSupportsEmbedding(findModelEntry(option.value)));
    }
    if (routeKey === 'speech_to_text') {
      return options.filter((option) => {
        const entry = findModelEntry(option.value);
        return looksLikeTranscriptionModel(option.value) || looksLikeTranscriptionModel(entry?.display_name ?? '');
      });
    }
    if (routeKey === 'text_to_speech') {
      return options.filter((option) => {
        const entry = findModelEntry(option.value);
        return looksLikeTtsModel(option.value) || looksLikeTtsModel(entry?.display_name ?? '');
      });
    }
    if (routeKey === 'compaction') {
      return [
        {
          value: SAME_SESSION_MODEL_SENTINEL,
          label: 'Same model as agent session',
          providerId: '',
          preferred: true
        },
        ...options
      ];
    }
    return options;
  }

  function defaultModelOptionLabel(routeKey: RoutingKey): string {
    if (routeKey === 'embedding') {
      return 'Select embedding model';
    }
    return 'Use provider default';
  }

  function syncRouteThinkingEffort(routeKey: RoutingKey): void {
    const entry = routingForm[routeKey];
    const available = routeThinkingEffortOptions(routeKey);
    if (!available.includes(entry.reasoningEffort)) {
      routingForm[routeKey].reasoningEffort = '';
    }
    if (effectiveRouteModelId(routeKey) === SAME_SESSION_MODEL_SENTINEL) {
      routingForm[routeKey].reasoningEffort = '';
    }
  }

  function executorSelectorFor(labels: Record<string, string> | null | undefined): string {
    return Object.entries(labels || {}).map(([k, v]) => `${k}=${v}`).join(', ');
  }

  function selectedProviderExecutorError(): string | null {
    return providerExecutorTargetError(providerForm);
  }

  function localInferenceExecutorOptions(): ExecutorConfig[] {
    return providerInferenceExecutors(executorConfigs, providerForm.executor_id);
  }

  function selectedProviderExecutorWarning(): string | null {
    return providerSelectorCapabilityWarning(
      executorConfigs,
      providerForm.executor_id,
      parseExecutorSelector(providerForm.executor_selector)
    );
  }

  function providerSaveDisabledReason(): string | null {
    if (busy) return 'Provider save is already in progress.';
    if (!canManageProvider(selectedProvider())) {
      return selectedProvider()?.owner_email
        ? 'You can only edit providers owned by your account.'
        : 'Shared system providers can only be edited by an admin.';
    }
    return selectedProviderExecutorError();
  }

  function handleProviderLocationChange(): void {
    if (providerRequiresExecutorLocation(providerForm.preset)) {
      providerForm.location = 'executor';
      return;
    }
    if (providerForm.location !== 'executor') {
      providerForm.executor_id = '';
      providerForm.executor_selector = '';
    }
  }

  function handleProviderExecutorIdChange(): void {
    if (providerForm.executor_id.trim()) {
      providerForm.executor_selector = '';
    }
  }

  function routingWarnings(): string[] {
    const knownModels = new Set(modelOptions().map((item) => item.value));
    return ROUTING_KEYS.map((key) => routingForm[key].model)
      .filter(Boolean)
      .filter((model) => model !== SAME_SESSION_MODEL_SENTINEL)
      .filter((model) => !knownModels.has(model))
      .map((model) => `Model '${model}' is not present in configured providers.`);
  }

  function codexWindowLabel(window: CodexUsageWindow | null): string {
    if (!window) return 'unavailable';
    const duration = window.window_duration_mins ? `${window.window_duration_mins}m window` : 'window';
    const reset = window.resets_at ? `resets ${new Date(window.resets_at).toLocaleString()}` : 'reset unknown';
    return `${Math.round(window.used_percent)}% used, ${duration}, ${reset}`;
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
    providerOAuthStatus = null;
    providerCodexUsage = null;
    providerCodexUsageError = '';
    anthropicOAuthCallbackInput = '';
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
    providerOAuthStatus = null;
    providerCodexUsage = null;
    providerCodexUsageError = '';
    anthropicOAuthCallbackInput = '';
  }

  async function startNewProvider(): Promise<void> {
    if (!(await confirmDiscardChanges('Starting a new provider will replace the current unsaved edits.'))) {
      return;
    }
    clearProviderSelection();
    initialSnapshot = snapshotState();
    await tick();
    if (window.innerWidth < 1024) {
      providerEditorEl?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function handleProviderPresetChange(): void {
    if (providerForm.preset === 'chatgpt') {
      providerForm.auth_mode = 'oauth';
      providerForm.use_responses_api = true;
      providerForm.base_url = '';
      providerForm.location = 'controller';
      providerForm.executor_id = '';
      providerForm.executor_selector = '';
      providerForm.backend = 'litellm';
    } else if (providerForm.auth_mode === 'oauth') {
      providerForm.auth_mode = providerForm.preset === 'ollama' ? 'none' : 'env';
      providerForm.codex_transport = 'direct';
      providerForm.backend = 'litellm';
    }
  }

  function handleProviderAuthModeChange(): void {
    if (providerForm.preset === 'anthropic' && providerForm.auth_mode === 'oauth') {
      providerForm.location = 'controller';
      providerForm.executor_id = '';
      providerForm.executor_selector = '';
      providerForm.backend = 'litellm';
    }
    providerOAuthStatus = null;
    providerCodexUsage = null;
    providerCodexUsageError = '';
    anthropicOAuthCallbackInput = '';
  }

  function isAnthropicSubscriptionOAuth(): boolean {
    return providerForm.preset === 'anthropic' && providerForm.auth_mode === 'oauth';
  }

  async function resetProviderForm(): Promise<void> {
    if (!(await confirmDiscardChanges('Discarding changes will restore the current provider form.'))) {
      return;
    }
    const provider = selectedProvider();
    if (provider) {
      applySelectedProvider(provider);
    } else {
      clearProviderSelection();
    }
    initialSnapshot = snapshotState();
  }

  async function discoverModels(): Promise<void> {
    busy = true;
    error = '';
    try {
      let models: ModelEntry[];
      const useCurrentFormForDiscovery = providerForm.location === 'executor' && providerForm.preset === 'ollama';
      if (selectedProviderId && !useCurrentFormForDiscovery) {
        const result = await api.llmProviders.discoverModels(selectedProviderId);
        models = result.models;
      } else {
        const executorLabels = parseExecutorSelector(providerForm.executor_selector);
        const result = await api.llmProviders.discoverModelsPreview({
          ...(selectedProviderId ? { provider_id: selectedProviderId } : {}),
          preset: providerForm.preset,
          base_url: providerForm.base_url,
          location: providerForm.location,
          ...(providerForm.location === 'executor' && providerForm.executor_id.trim()
            ? { executor_id: providerForm.executor_id.trim() }
            : {}),
          ...(providerForm.location === 'executor' && !providerForm.executor_id.trim() && executorLabels
            ? { executor_labels: executorLabels }
            : {}),
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

  function defaultOAuthExecutorLabel(): string {
    const candidates = executorConfigs.filter((executor) => executor.executor_type !== 'in_process');
    const defaultExecutor = candidates.find((executor) => executor.is_default) ?? candidates[0];
    if (!defaultExecutor) {
      return 'Default executor';
    }
    return `Default executor (${defaultExecutor.name || defaultExecutor.executor_id})`;
  }

  function oauthBarClasses(status: MCPOAuthStatus | undefined): string {
    if (isMcpOAuthStatusCritical(status)) {
      return 'border-rose-500/40 bg-rose-500/15 text-rose-50';
    }
    if (!status) {
      return 'border-amber-500/30 bg-amber-500/10 text-amber-50';
    }
    return 'border-sky-500/20 bg-sky-500/10 text-sky-100';
  }

  function oauthMutedTextClasses(status: MCPOAuthStatus | undefined): string {
    if (isMcpOAuthStatusCritical(status)) {
      return 'text-rose-50/85';
    }
    if (!status) {
      return 'text-amber-50/85';
    }
    return 'text-sky-100/80';
  }

  function createEmptyMcpForm(): MCPServerFormState {
    return {
      name: '',
      transport: 'stdio',
      command: '',
      url: '',
      args: '',
      envVars: [],
      headers: [],
      timeout_seconds: 30,
      description: '',
      shared: false,
      authType: 'none',
      oauthIssuer: '',
      oauthAuthorizationServer: '',
      oauthResource: '',
      oauthScopes: '',
      oauthClientId: '',
      oauthClientSecretRef: '',
      oauthRedirectUri: '',
      oauthCallbackMode: 'auto',
      oauthExecutorId: '',
      oauthDynamicClientRegistration: false,
      oauthClientMetadataDocumentUrl: '',
      oauthAuthorizationParams: []
    };
  }

  function mcpAuthType(config: MCPAuthConfig | null | undefined): MCPAuthType {
    const type = config?.type;
    return type === 'static_headers' || type === 'oauth2' ? type : 'none';
  }

  function mcpFormFromServer(server: MCPServerConfigResponse): MCPServerFormState {
    const authConfig = server.auth_config;
    return {
      ...createEmptyMcpForm(),
      name: server.name,
      transport: server.transport,
      command: server.command || '',
      url: server.url || '',
      args: (server.args || []).join('\n'),
      envVars: parseMcpEntries(server.env || {}),
      headers: parseMcpEntries(server.headers || {}),
      timeout_seconds: server.timeout_seconds,
      description: server.description || '',
      shared: !!server.shared,
      authType: mcpAuthType(authConfig),
      oauthIssuer: authConfig?.issuer || '',
      oauthAuthorizationServer: authConfig?.authorization_server || '',
      oauthResource: authConfig?.resource || '',
      oauthScopes: (authConfig?.scopes || []).join(' '),
      oauthClientId: authConfig?.client_id || '',
      oauthClientSecretRef: authConfig?.client_secret_ref?.startsWith('$secret:')
        ? authConfig.client_secret_ref.slice('$secret:'.length)
        : authConfig?.client_secret_ref || '',
      oauthRedirectUri: authConfig?.redirect_uri || '',
      oauthCallbackMode: authConfig?.callback_mode || 'auto',
      oauthExecutorId: authConfig?.oauth_executor_id || '',
      oauthDynamicClientRegistration: authConfig?.dynamic_client_registration === true,
      oauthClientMetadataDocumentUrl: authConfig?.client_metadata_document_url || '',
      oauthAuthorizationParams: parseMcpEntries(authConfig?.authorization_params || {})
    };
  }

  function mcpAuthConfigFromForm(): MCPAuthConfig {
    if (mcpForm.authType !== 'oauth2') {
      return { type: mcpForm.authType };
    }
    const scopes = mcpForm.oauthScopes.split(/\s+/).map((scope) => scope.trim()).filter(Boolean);
    const clientSecretRef = mcpForm.oauthClientSecretRef.trim();
    return {
      type: 'oauth2',
      issuer: mcpForm.oauthIssuer.trim() || null,
      authorization_server: mcpForm.oauthAuthorizationServer.trim() || null,
      resource: mcpForm.oauthResource.trim() || null,
      scopes,
      client_id: mcpForm.oauthClientId.trim() || null,
      client_secret_ref: clientSecretRef
        ? (clientSecretRef.startsWith('$secret:') ? clientSecretRef : `$secret:${clientSecretRef}`)
        : null,
      redirect_uri: mcpForm.oauthRedirectUri.trim() || null,
      callback_mode: mcpForm.oauthCallbackMode,
      oauth_executor_id: mcpForm.oauthExecutorId.trim() || null,
      dynamic_client_registration: mcpForm.oauthDynamicClientRegistration,
      client_metadata_document_url: mcpForm.oauthClientMetadataDocumentUrl.trim() || null,
      authorization_params: serializeMcpEntries(mcpForm.oauthAuthorizationParams)
    };
  }

  async function refreshMcpOAuthStatus(serverId: string): Promise<void> {
    try {
      const status = await api.tools.mcpOAuthStatus(serverId);
      mcpOAuthStatuses = { ...mcpOAuthStatuses, [serverId]: status };
    } catch {
      mcpOAuthStatuses = { ...mcpOAuthStatuses, [serverId]: { connected: false, status: 'unavailable' } };
    }
  }

  async function startMcpOAuth(server: MCPServerConfigResponse): Promise<void> {
    busy = true;
    error = '';
    try {
      const started = await api.tools.startMcpOAuth(server.server_id);
      if (started.callback_mode === 'executor_loopback') {
        let copied = false;
        if (typeof navigator !== 'undefined' && navigator.clipboard) {
          try {
            await navigator.clipboard.writeText(started.authorization_url);
            copied = true;
          } catch {
            copied = false;
          }
        }
        const baseInstructions =
          started.instructions ||
          `Open the authorization URL on executor ${started.oauth_executor_name || started.oauth_executor_id || 'selected executor'}.`;
        mcpOAuthStarts = {
          ...mcpOAuthStarts,
          [server.server_id]: {
            authorizationUrl: started.authorization_url,
            callbackMode: started.callback_mode,
            oauthExecutorId: started.oauth_executor_id,
            oauthExecutorName: started.oauth_executor_name,
            instructions: baseInstructions,
            copied
          }
        };
        addToast(
          copied
            ? `${baseInstructions} Authorization URL copied to clipboard.`
            : `${baseInstructions} Authorization URL: ${started.authorization_url}`,
          'success',
          12_000,
          'MCP OAuth authorization'
        );
      } else {
        mcpOAuthStarts = {
          ...mcpOAuthStarts,
          [server.server_id]: {
            authorizationUrl: started.authorization_url,
            callbackMode: started.callback_mode,
            oauthExecutorId: started.oauth_executor_id,
            oauthExecutorName: started.oauth_executor_name,
            instructions: null,
            copied: false
          }
        };
        openUrlInNewTab(started.authorization_url);
        addToast('MCP OAuth authorization opened in a new tab.', 'success');
      }
      await refreshMcpOAuthStatus(server.server_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to start MCP OAuth');
    } finally {
      busy = false;
    }
  }

  async function disconnectMcpOAuth(server: MCPServerConfigResponse): Promise<void> {
    busy = true;
    error = '';
    try {
      await api.tools.disconnectMcpOAuth(server.server_id);
      await refreshMcpOAuthStatus(server.server_id);
      addToast('MCP OAuth disconnected.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to disconnect MCP OAuth');
    } finally {
      busy = false;
    }
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

  const presetOptions: ProviderPreset[] = ['openai', 'openai_compatible', 'anthropic', 'ollama', 'litellm_proxy', 'chatgpt'];

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
    await loadUserPreferences(auth.getSnapshot().user?.email);

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
      text_to_speech: {
        model: modelRouting.text_to_speech?.model ?? '',
        reasoningEffort: ''
      },
      image_generation: {
        model: modelRouting.image_generation.model ?? '',
        reasoningEffort: ''
      },
      attachment_analysis: {
        model: modelRouting.attachment_analysis.model ?? '',
        reasoningEffort: ''
      },
      embedding: {
        model: modelRouting.embedding?.model ?? '',
        reasoningEffort: ''
      }
    };

    if (isAdmin) {
      webConfig = await api.webConfig.status().catch(() => webConfig);
    } else {
      webConfig = {
        backend: 'direct',
        search_backend: 'direct',
        fetch_backend: 'direct',
        fetch_fallback_browser: true,
        browser_fetch_session_idle_seconds: 60,
        browser_fetch_wait_timeout_seconds: 30,
        browser_fetch_navigation_timeout_seconds: 60,
        browser_fetch_wait_until: 'domcontentloaded',
        browser_fetch_network_idle_after_dom_seconds: 3,
        browser_fetch_headed_fallback_enabled: true,
        tavily_configured: false,
        tavily_enabled: true,
        brave_configured: false,
        brave_enabled: true,
        searxng_url: '',
        searxng_engines: '',
        searxng_categories: '',
        searxng_language: '',
        searxng_configured: false,
        searxng_enabled: true,
        available_backends: ['direct'],
        available_search_backends: ['direct'],
        available_fetch_backends: ['direct'],
      };
    }
    webBackendForm = webConfig.backend;
    webSearchBackendForm = webConfig.search_backend ?? webConfig.backend;
    webFetchBackendForm = webConfig.fetch_backend ?? webConfig.backend;
    webFetchFallbackBrowserForm = webConfig.fetch_fallback_browser ?? true;
    webBrowserFetchSessionIdleForm = webConfig.browser_fetch_session_idle_seconds ?? 60;
    webBrowserFetchWaitTimeoutForm = webConfig.browser_fetch_wait_timeout_seconds ?? 30;
    webBrowserFetchNavigationTimeoutForm = webConfig.browser_fetch_navigation_timeout_seconds ?? 60;
    webBrowserFetchWaitUntilForm = webConfig.browser_fetch_wait_until ?? 'domcontentloaded';
    webBrowserFetchNetworkIdleForm = webConfig.browser_fetch_network_idle_after_dom_seconds ?? 3;
    webBrowserFetchHeadedFallbackForm = webConfig.browser_fetch_headed_fallback_enabled ?? true;

    // Initialize account name form
    accountNameForm = auth.getSnapshot().user?.name ?? '';
    accountNameDirty = false;

    await refreshPushStatus();

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
      providers = await api.llmProviders.list().then((page) => page.items).catch(() => []);
      diagnostics = null;
      stepProfileForms = [];
      userList = [];
    }

    try {
      localModelDeployments = await api.localModels.deployments();
      localModelTargets = Object.fromEntries(
        await Promise.all(
          localModelDeployments.map(async (deployment) => [
            deployment.deployment_id,
            await api.localModels.targets(deployment.deployment_id)
          ] as const)
        )
      );
      localModelsUnavailable = false;
    } catch {
      localModelDeployments = [];
      localModelTargets = {};
      localModelsUnavailable = true;
    }

    const queryProviderId = new URL(window.location.href).searchParams.get('provider');
    if (queryProviderId && providers.some((provider) => provider.provider_id === queryProviderId)) {
      selectedProviderId = queryProviderId;
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

  async function saveExecutorLocalInference(
    executorId: string,
    config: ExecutorRuntimeConfig
  ): Promise<void> {
    const sequence = (executorLocalInferenceSaveSequences.get(executorId) ?? 0) + 1;
    executorLocalInferenceSaveSequences.set(executorId, sequence);
    setExecutorSaving(executorId, true);
    try {
      const current = executorConfigs.find((executor) => executor.executor_id === executorId);
      const updated = await api.executor.update(executorId, {
        config,
        expected_config_version: current?.desired_config_version
      });
      if (executorLocalInferenceSaveSequences.get(executorId) !== sequence) return;
      executorConfigs = executorConfigs.map((executor) =>
        executor.executor_id === executorId ? updated : executor
      );
      addToast('Local inference settings saved.', 'success');
    } catch (caughtError) {
      if (executorLocalInferenceSaveSequences.get(executorId) !== sequence) return;
      const apiError = asApiError(caughtError);
      addToast(apiError.message, 'error', 4_000, 'Unable to save local inference settings');
      throw new Error(apiError.message);
    } finally {
      if (executorLocalInferenceSaveSequences.get(executorId) === sequence) {
        setExecutorSaving(executorId, false);
      }
    }
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

  function canManageProvider(provider: LLMProvider | null): boolean {
    if (!provider) return true;
    const ownerEmail = provider.owner_email ?? null;
    if (!ownerEmail) return isAdmin;
    if (ownerEmail === 'system@cognis.local') return isAdmin;
    const currentUserEmail = auth.getSnapshot().user?.email ?? null;
    return ownerEmail === currentUserEmail || isAdmin;
  }

  async function refreshPushStatus(): Promise<void> {
    pushPermission = permissionState();
    pushSupported = isWebPushSupported();
    pushStandalone = isStandaloneDisplay();
    pushNeedsInstall = needsIosHomeScreenInstall();
    pushEnabledOnDevice = hasEnabledWebPush();
    [pushVapid, pushStatus] = await Promise.all([
      api.push.vapidPublicKey().catch(() => null),
      api.push.status().catch(() => null)
    ]);
    if (pushEnabledOnDevice) {
      const reconciled = await reconcileWebPushSubscription();
      pushEnabledOnDevice = reconciled;
      if (reconciled) {
        pushStatus = await api.push.status().catch(() => pushStatus);
      }
    }
  }

  async function refreshExecutorsOnly(): Promise<void> {
    if (typeof document !== 'undefined' && document.hidden) return;
    if (savingExecutorIds.length > 0 || toolUpdateQueues.size > 0) return;
    executorConfigs = await api.executor.list().catch(() => executorConfigs);
  }

  async function enableDeviceNotifications(): Promise<void> {
    pushBusy = true;
    pushError = '';
    pushTestResult = null;
    try {
      const result = await enableWebPush();
      if (!result.ok) {
        pushError = result.message;
        addToast(result.message, 'error', 5_000, 'Unable to enable notifications');
        return;
      }
      addToast(result.message, 'success');
      await refreshPushStatus();
    } catch (caughtError) {
      pushError = asApiError(caughtError).message;
      addToast(pushError, 'error', 5_000, 'Unable to enable notifications');
    } finally {
      pushBusy = false;
    }
  }

  async function disableDeviceNotifications(): Promise<void> {
    pushBusy = true;
    pushError = '';
    pushTestResult = null;
    try {
      const removed = await disableWebPushForCurrentDevice();
      if (removed) {
        addToast('Notifications disabled on this device.', 'success');
      } else {
        pushError = 'Unable to remove the browser push subscription on this device.';
        addToast(pushError, 'error', 5_000, 'Notifications still enabled');
      }
      await refreshPushStatus();
    } catch (caughtError) {
      pushError = asApiError(caughtError).message;
      addToast(pushError, 'error', 5_000, 'Unable to disable notifications');
    } finally {
      pushBusy = false;
    }
  }

  async function sendTestNotification(): Promise<void> {
    pushBusy = true;
    pushError = '';
    pushTestResult = null;
    try {
      pushTestResult = await api.push.test();
      await refreshPushStatus();
      if (pushTestResult.sent_to > 0 && pushTestResult.errors === 0) {
        addToast('Test notification sent.', 'success');
      } else if (pushTestResult.sent_to === 0) {
        addToast('No enabled push subscriptions were found.', 'error', 5_000, 'Test notification not sent');
      } else {
        addToast('Some subscriptions failed during test delivery.', 'error', 5_000, 'Test notification partial failure');
      }
    } catch (caughtError) {
      pushError = asApiError(caughtError).message;
      addToast(pushError, 'error', 5_000, 'Unable to send test notification');
    } finally {
      pushBusy = false;
    }
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
    const executorTargetError = providerExecutorTargetError(providerForm);
    if (executorTargetError) {
      error = executorTargetError;
      return;
    }
    busy = true;
    error = '';
    notice = '';
    try {
      const payload = providerFormToPayload(providerForm);
      if (selectedProviderId) {
        await api.llmProviders.update(selectedProviderId, providerFormToUpdatePayload(providerForm));
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

  async function startChatgptOAuth(): Promise<void> {
    if (!selectedProviderId) return;
    busy = true;
    error = '';
    try {
      providerOAuthStatus = await api.llmProviders.startChatgptOAuth(selectedProviderId);
      providerCodexUsage = null;
      providerCodexUsageError = '';
      addToast('ChatGPT OAuth started. Enter the device code in your browser.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to start OAuth');
    } finally {
      busy = false;
    }
  }

  async function checkChatgptOAuth(): Promise<void> {
    if (!selectedProviderId) return;
    busy = true;
    error = '';
    try {
      providerOAuthStatus = await api.llmProviders.chatgptOAuthStatus(selectedProviderId);
      if (providerOAuthStatus.status !== 'authorized') {
        providerCodexUsage = null;
      }
      if (providerOAuthStatus.status === 'authorized') {
        addToast('ChatGPT OAuth is authorized.', 'success');
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to check OAuth');
    } finally {
      busy = false;
    }
  }

  async function refreshCodexUsage(): Promise<void> {
    if (!selectedProviderId) return;
    busy = true;
    providerCodexUsageError = '';
    try {
      providerCodexUsage = await api.llmProviders.codexUsage(selectedProviderId);
    } catch (caughtError) {
      providerCodexUsageError = asApiError(caughtError).message;
      addToast(providerCodexUsageError, 'error', 4_000, 'Unable to fetch Codex usage');
    } finally {
      busy = false;
    }
  }

  async function clearChatgptOAuth(): Promise<void> {
    if (!selectedProviderId) return;
    busy = true;
    error = '';
    try {
      await api.llmProviders.clearChatgptOAuth(selectedProviderId);
      providerOAuthStatus = null;
      providerCodexUsage = null;
      providerCodexUsageError = '';
      addToast('ChatGPT OAuth tokens removed.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to remove OAuth tokens');
    } finally {
      busy = false;
    }
  }

  async function startAnthropicOAuth(): Promise<void> {
    if (!selectedProviderId) return;
    busy = true;
    error = '';
    try {
      providerOAuthStatus = await api.llmProviders.startAnthropicOAuth(selectedProviderId);
      anthropicOAuthCallbackInput = '';
      addToast('Claude subscription OAuth started. Open the authorization URL and paste the callback below.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to start OAuth');
    } finally {
      busy = false;
    }
  }

  async function completeAnthropicOAuth(): Promise<void> {
    if (!selectedProviderId || !anthropicOAuthCallbackInput.trim()) return;
    busy = true;
    error = '';
    try {
      providerOAuthStatus = await api.llmProviders.completeAnthropicOAuth(
        selectedProviderId,
        anthropicOAuthCallbackInput.trim()
      );
      anthropicOAuthCallbackInput = '';
      addToast('Claude subscription OAuth is authorized.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to complete OAuth');
    } finally {
      busy = false;
    }
  }

  async function checkAnthropicOAuth(): Promise<void> {
    if (!selectedProviderId) return;
    busy = true;
    error = '';
    try {
      providerOAuthStatus = await api.llmProviders.anthropicOAuthStatus(selectedProviderId);
      if (providerOAuthStatus.status === 'authorized') {
        addToast('Claude subscription OAuth is authorized.', 'success');
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to check OAuth');
    } finally {
      busy = false;
    }
  }

  async function clearAnthropicOAuth(): Promise<void> {
    if (!selectedProviderId) return;
    busy = true;
    error = '';
    try {
      await api.llmProviders.clearAnthropicOAuth(selectedProviderId);
      providerOAuthStatus = null;
      anthropicOAuthCallbackInput = '';
      addToast('Claude subscription OAuth tokens removed.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to remove OAuth tokens');
    } finally {
      busy = false;
    }
  }

  async function saveRouting(): Promise<void> {
    busy = true;
    error = '';
    try {
      const compactionModel = routingForm.compaction.model || SAME_SESSION_MODEL_SENTINEL;
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
          model: compactionModel,
          reasoning_effort: compactionModel === SAME_SESSION_MODEL_SENTINEL
            ? null
            : (routingForm.compaction.reasoningEffort || null)
        },
        evaluator: {
          model: routingForm.evaluator.model || (routingForm.evaluator.reasoningEffort ? effectiveRouteModelId('evaluator') : '') || null,
          reasoning_effort: routingForm.evaluator.reasoningEffort || null
        },
        speech_to_text: {
          model: routingForm.speech_to_text.model || null,
          reasoning_effort: null
        },
        text_to_speech: {
          model: routingForm.text_to_speech.model || null,
          reasoning_effort: null
        },
        image_generation: {
          model: routingForm.image_generation.model || null,
          reasoning_effort: null
        },
        attachment_analysis: {
          model: routingForm.attachment_analysis.model || null,
          reasoning_effort: null
        },
        embedding: {
          model: routingForm.embedding.model || null,
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
      label: 'Direct',
      description: 'Zero-setup path. DuckDuckGo powers direct search; httpx + trafilatura power direct fetch and extraction.'
    },
    tavily: {
      label: 'Tavily',
      description: 'AI-optimized search and extract with answer generation, content reranking, website crawling, and deep research. When chosen for fetch, also unlocks Tavily-native web_crawl/web_map/web_research.',
      link: 'https://tavily.com'
    },
    brave: {
      label: 'Brave Search',
      description: 'Search from Brave\'s index with freshness filters, extra snippets, and country targeting. Search-only.',
      link: 'https://brave.com/search/api/'
    },
    searxng: {
      label: 'SearXNG (self-hosted)',
      description: 'Free metasearch aggregator that federates Google, Bing, DuckDuckGo, Mojeek, Qwant and others. Configure the instance URL below — cognis does not run SearXNG itself.',
      link: 'https://searxng.org/'
    },
    browser: {
      label: 'Always use headless browser',
      description: 'Patchright/Playwright headless fetch for every request. Slower, but useful for JS-heavy sites or when you explicitly want rendered pages every time.'
    }
  };

  function searchBackendDescription(backend: string): string {
    if (backend === 'direct') {
      return 'Direct search uses DuckDuckGo. Free and zero-setup, but community-maintained and occasionally less reliable than Brave, Tavily, or your own SearXNG instance.';
    }
    return WEB_BACKEND_INFO[backend]?.description ?? '';
  }

  function fetchBackendDescription(backend: string): string {
    if (backend === 'direct') {
      return webFetchFallbackBrowserForm
        ? 'Direct fetch uses httpx + trafilatura for clean extraction, then automatically retries through the executor browser on Cloudflare/JS-required failures.'
        : 'Direct fetch uses httpx + trafilatura for clean extraction. Browser fallback is disabled, so blocked or JS-required pages return the direct error.';
    }
    if (backend === 'tavily') {
      return 'Tavily extract API for every fetch. Also enables Tavily-native web_crawl, web_map, and web_research.';
    }
    return WEB_BACKEND_INFO[backend]?.description ?? '';
  }

  async function saveWebBackend(): Promise<void> {
    busy = true;
    error = '';
    try {
      const legacyBackend = webSearchBackendForm === 'tavily' && webFetchBackendForm === 'tavily'
        ? 'tavily'
        : 'direct';
      webBackendForm = legacyBackend;
      await api.settings.update('web.backend', legacyBackend);
      await api.settings.update('web.search_backend', webSearchBackendForm);
      await api.settings.update('web.fetch_backend', webFetchBackendForm);
      await api.settings.update('web.fetch_fallback_browser', webFetchFallbackBrowserForm);
      await api.settings.update('web.browser_fetch.session_idle_seconds', Number(webBrowserFetchSessionIdleForm));
      await api.settings.update('web.browser_fetch.wait_timeout_seconds', Number(webBrowserFetchWaitTimeoutForm));
      await api.settings.update('web.browser_fetch.navigation_timeout_seconds', Number(webBrowserFetchNavigationTimeoutForm));
      await api.settings.update('web.browser_fetch.wait_until', webBrowserFetchWaitUntilForm);
      await api.settings.update('web.browser_fetch.network_idle_after_dom_seconds', Number(webBrowserFetchNetworkIdleForm));
      await api.settings.update('web.browser_fetch.headed_fallback_enabled', webBrowserFetchHeadedFallbackForm);
      webConfig = await api.webConfig.status();
      webSearchBackendForm = webConfig.search_backend ?? 'direct';
      webFetchBackendForm = webConfig.fetch_backend ?? 'direct';
      webFetchFallbackBrowserForm = webConfig.fetch_fallback_browser ?? true;
      webBrowserFetchSessionIdleForm = webConfig.browser_fetch_session_idle_seconds ?? 60;
      webBrowserFetchWaitTimeoutForm = webConfig.browser_fetch_wait_timeout_seconds ?? 30;
      webBrowserFetchNavigationTimeoutForm = webConfig.browser_fetch_navigation_timeout_seconds ?? 60;
      webBrowserFetchWaitUntilForm = webConfig.browser_fetch_wait_until ?? 'domcontentloaded';
      webBrowserFetchNetworkIdleForm = webConfig.browser_fetch_network_idle_after_dom_seconds ?? 3;
      webBrowserFetchHeadedFallbackForm = webConfig.browser_fetch_headed_fallback_enabled ?? true;
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

  function openWebBackendEditor(backend: EditableWebBackend): void {
    editingWebBackend = createWebBackendEditValue(backend, webConfig);
  }

  function resetDisabledBackendSelections(backend: EditableWebBackend): void {
    if (webSearchBackendForm === backend || webConfig.search_backend === backend) {
      webSearchBackendForm = 'direct';
    }
    if (webFetchBackendForm === backend || webConfig.fetch_backend === backend) {
      webFetchBackendForm = 'direct';
    }
    if (webBackendForm === backend || webConfig.backend === backend) {
      webBackendForm = 'direct';
    }
  }

  async function refreshWebBackendAfterFailure(backend: EditableWebBackend): Promise<void> {
    try {
      webConfig = await api.webConfig.status();
      [settings, secrets] = await Promise.all([api.settings.list(), api.secrets.list()]);
      const isAvailable = webConfig.available_search_backends.includes(backend)
        || webConfig.available_fetch_backends.includes(backend);
      if (!isAvailable) {
        resetDisabledBackendSelections(backend);
      }
      if (editingWebBackend?.backend === backend) {
        editingWebBackend = createWebBackendEditValue(backend, webConfig);
      }
      initialSnapshot = snapshotState();
    } catch {
      // Preserve the original mutation error when recovery refresh also fails.
    }
  }

  async function saveWebBackendConfig(value: WebBackendEditValue): Promise<void> {
    busy = true;
    error = '';
    try {
      webConfig = await api.webConfig.updateBackend(value.backend, {
        enabled: value.enabled,
        api_key: value.apiKey.trim() || null,
        searxng_url: value.searxngUrl.trim(),
        searxng_engines: value.searxngEngines.trim(),
        searxng_categories: value.searxngCategories.trim(),
        searxng_language: value.searxngLanguage.trim()
      });
      if (!value.enabled) {
        resetDisabledBackendSelections(value.backend);
      }
      [settings, secrets] = await Promise.all([api.settings.list(), api.secrets.list()]);
      if (editingWebBackend?.backend === value.backend) {
        editingWebBackend = null;
      }
      notice = `${WEB_BACKEND_INFO[value.backend].label} updated.`;
      addToast(notice, 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      await refreshWebBackendAfterFailure(value.backend);
      addToast(error, 'error', 4_000, 'Unable to update web backend');
    } finally {
      busy = false;
    }
  }

  async function removeWebBackendConfiguration(backend: EditableWebBackend): Promise<void> {
    const label = WEB_BACKEND_INFO[backend].label;
    const confirmed = await confirmAction({
      title: `Remove ${label} configuration?`,
      message: backend === 'searxng'
        ? 'This clears the SearXNG URL and optional defaults. The values cannot be recovered.'
        : `This permanently deletes the encrypted ${label} API key.`,
      confirmLabel: backend === 'searxng' ? 'Clear configuration' : 'Remove key'
    });
    if (!confirmed) return;

    busy = true;
    error = '';
    try {
      webConfig = await api.webConfig.updateBackend(backend, {
        enabled: false,
        remove_configuration: true
      });
      resetDisabledBackendSelections(backend);
      [settings, secrets] = await Promise.all([api.settings.list(), api.secrets.list()]);
      if (editingWebBackend?.backend === backend) {
        editingWebBackend = null;
      }
      notice = `${label} configuration removed.`;
      addToast(notice, 'success');
      initialSnapshot = snapshotState();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      await refreshWebBackendAfterFailure(backend);
      addToast(error, 'error', 4_000, 'Unable to remove web backend configuration');
    } finally {
      busy = false;
    }
  }

  async function openTargetUi(target: 'intaris' | 'mnemory'): Promise<void> {
    try {
      const exchange = await api.auth.exchangeToken(target);
      openUrlInNewTab(buildLinkedServiceUrl(target, { token: exchange.token }, exchange.ui_url));
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
    userEditForm = { name: user.name ?? '', role: user.role, password: '', confirm_password: '' };
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
    if (userEditForm.password || userEditForm.confirm_password) {
      if (userEditForm.password.length < 8) {
        addToast('Password must be at least 8 characters.', 'error');
        return;
      }
      if (userEditForm.password !== userEditForm.confirm_password) {
        addToast('Passwords do not match.', 'error');
        return;
      }
    }
    busy = true;
    error = '';
    try {
      await api.users.update(editingUser.email, {
        name: userEditForm.name || undefined,
        role: userEditForm.role,
        password: userEditForm.password || undefined
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

  async function updateUserPreferences(next: UserPreferences): Promise<void> {
    busy = true;
    error = '';
    try {
      await saveUserPreferences(next);
      addToast('Preferences saved.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to save preferences');
      await loadUserPreferences();
    } finally {
      busy = false;
    }
  }

  function updateDisplayPreference<K extends keyof UserPreferences['display']>(
    key: K,
    value: UserPreferences['display'][K],
  ): Promise<void> {
    return updateUserPreferences({
      ...$userPreferences,
      display: {
        ...$userPreferences.display,
        [key]: value
      }
    });
  }

  function updateChatPreference<K extends keyof UserPreferences['chat']>(
    key: K,
    value: UserPreferences['chat'][K],
  ): Promise<void> {
    return updateUserPreferences({
      ...$userPreferences,
      chat: {
        ...$userPreferences.chat,
        [key]: value
      }
    });
  }

  onMount(() => {
    const cleanup = installBeforeUnloadGuard(isDirty);
    void loadSettings();
    const unsubscribeWs = wsClient.subscribe((event) => {
      if (event.type !== 'mcp_oauth_status_changed') {
        return;
      }
      const status = event.status as MCPOAuthStatus;
      mcpOAuthStatuses = { ...mcpOAuthStatuses, [event.server_id]: status };
      if (status.connected) {
        const nextStarts = { ...mcpOAuthStarts };
        delete nextStarts[event.server_id];
        mcpOAuthStarts = nextStarts;
      }
    });

    // Same-tab tap on Settings: reset to the default sub-tab
    // and scroll the content shell to the top. The bottom tab bar has
    // already navigated to `/settings` (bare path) so the `?tab=` query
    // is cleared; we only need to reset local state and scroll.
    const unsubTabReset = onTabReset('/settings', () => {
      activeTab = tabs[0] ?? 'account';
      clearPersistedScroll('/settings');
      const el = document.querySelector<HTMLElement>('[data-app-content="true"]');
      if (el) el.scrollTo({ top: 0, behavior: 'smooth' });
    });

    return () => {
      if (executorPollTimer) clearInterval(executorPollTimer);
      cleanup();
      unsubTabReset();
      unsubscribeWs();
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
      void refreshExecutorsOnly();
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
    <!--
      Mobile: horizontally scrollable pill strip rendered in normal flow
      (not sticky/backdrop-blur), so it sits directly under the page
      header and scrolls with the rest of the content. Sticky + blur
      previously made it read as a detached floating bar.
      Desktop: flex-wrap row.
    -->
    <div class="-mx-2 overflow-x-auto px-2 md:hidden">
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
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Providers</p>
                <h2 class="mt-1 text-lg font-semibold text-white">LLM providers</h2>
              </div>
              <Button size="sm" variant={!selectedProviderId ? 'primary' : 'secondary'} onclick={startNewProvider} disabled={busy}>
                + New provider
              </Button>
            </div>
            {#each providers as provider}
              <button class={`w-full rounded-2xl border px-4 py-3 text-left transition ${selectedProviderId === provider.provider_id ? 'border-sky-400/40 bg-sky-500/10' : 'border-slate-800 bg-slate-950/70 hover:border-slate-700'}`} onclick={() => selectProvider(provider)}>
                <div class="flex items-center justify-between gap-3">
                  <span class="font-medium text-slate-100">{provider.is_default ? '⭐ ' : ''}{provider.display_name}</span>
                  <ProviderStatusBadge status={provider.status} />
                </div>
                <p class="mt-1 text-xs text-slate-500">{provider.owner_email ? `Owned by ${provider.owner_email}` : 'Shared system provider'}</p>
                {#if provider.last_test}
                  <p class="mt-2 text-xs text-slate-400">
                    {provider.last_test.ok ? `Last test passed (${provider.last_test.model_resolved})` : provider.last_test.error_detail}
                  </p>
                {/if}
              </button>
            {:else}
              <p class="text-sm text-slate-400">No providers configured yet.</p>
            {/each}
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
              <select bind:value={providerForm.preset} onchange={handleProviderPresetChange} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                {#each presetOptions as preset}
                  <option value={preset}>{PRESET_LABELS[preset]}</option>
                {/each}
              </select>
            </label>
            {#if isAdmin}
              <label class="space-y-2 text-sm font-medium text-slate-200">
                <span>Ownership</span>
                <select bind:value={providerForm.owner_scope} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-60">
                  <option value="user">Personal provider</option>
                  <option value="system">Shared system provider</option>
                </select>
                <span class="block text-xs text-slate-400">Personal providers are owned by your account. Shared providers are visible to all users and admin-managed. Admins can change ownership when editing an existing provider.</span>
              </label>
            {:else}
              <div class="space-y-2 text-sm font-medium text-slate-200">
                <span>Ownership</span>
                <div class="rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">Personal provider</div>
              </div>
            {/if}
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Execution location</span>
              <select bind:value={providerForm.location} onchange={handleProviderLocationChange} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="controller" disabled={providerRequiresExecutorLocation(providerForm.preset)}>Controller</option>
                {#if providerForm.preset !== 'chatgpt' && !isAnthropicSubscriptionOAuth()}
                  <option value="executor">Via executor</option>
                {/if}
              </select>
              {#if providerForm.preset === 'chatgpt'}
                <span class="block text-xs text-slate-400">ChatGPT OAuth tokens are hydrated on the controller, so executor routing is disabled for this preset.</span>
              {:else if isAnthropicSubscriptionOAuth()}
                <span class="block text-xs text-slate-400">Claude subscription OAuth tokens are managed on the controller, so executor routing is disabled for this auth mode.</span>
              {/if}
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
                <span>Executor</span>
                <select bind:value={providerForm.executor_id} onchange={handleProviderExecutorIdChange} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                  <option value="">Use label selector</option>
                  {#each localInferenceExecutorOptions() as executor}
                    <option value={executor.executor_id}>{executor.name} ({executor.executor_id}){executor.local_inference_enabled ? '' : ' — local inference disabled'}</option>
                  {/each}
                </select>
                <span class="block text-xs text-slate-400">Choose a concrete executor when this provider depends on executor-local auth or runtime state.</span>
              </label>
              <label class="space-y-2 text-sm font-medium text-slate-200 block">
                <span>Executor selector (key=value, comma-separated)</span>
                <Input bind:value={providerForm.executor_selector} placeholder="location=local, tier=gpu" disabled={Boolean(providerForm.executor_id.trim())} />
              </label>
              {#if selectedProviderExecutorError()}
                <p class="text-xs text-rose-300">{selectedProviderExecutorError()}</p>
              {/if}
              {#if selectedProviderExecutorWarning()}
                <p class="rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">{selectedProviderExecutorWarning()}</p>
              {/if}
              {#if executorConfigs.length > 0}
                <div class="flex flex-wrap gap-2">
                  <span class="text-xs text-slate-400 self-center">Use labels from:</span>
                  {#each executorConfigs.filter((executor) => executor.executor_type === 'websocket' && executor.local_inference_enabled) as executor}
                    <Button size="sm" variant="secondary" onclick={() => { providerForm.executor_id = ''; providerForm.executor_selector = executorSelectorFor(executor.labels); }} disabled={Object.keys(executor.labels || {}).length === 0}>{executor.name}</Button>
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
                  <select bind:value={providerForm.auth_mode} onchange={handleProviderAuthModeChange} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                    <option value="env">Environment variable</option>
                    <option value="secret">Credential store</option>
                    {#if providerForm.preset === 'chatgpt' || providerForm.preset === 'anthropic'}
                      <option value="oauth">{providerForm.preset === 'chatgpt' ? 'OAuth device flow' : 'Claude subscription OAuth'}</option>
                    {/if}
                  </select>
                </label>

                {#if providerForm.auth_mode === 'oauth'}
                  <div class="space-y-2 text-sm text-slate-300">
                    <p class="font-medium text-slate-200">Encrypted OAuth token storage</p>
                    {#if isAnthropicSubscriptionOAuth()}
                      <div class="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100">
                        This uses Claude Pro/Max subscription authentication through Anthropic's Claude Code OAuth flow.
                        It is an unofficial integration and may be affected by Anthropic account, subscription, or API policy changes.
                        Use it only with an account where this risk is acceptable.
                      </div>
                      <p class="text-xs text-slate-400">Tokens are stored in the encrypted Cognis secrets table and used only by controller-side Anthropic Messages calls.</p>
                    {:else}
                      <p class="text-xs text-slate-400">Tokens are stored in the encrypted Cognis secrets table. LiteLLM token files are temporary per request.</p>
                    {/if}
                    {#if selectedProviderId}
                      {#if isAnthropicSubscriptionOAuth()}
                        <div class="flex flex-wrap gap-2">
                          <Button size="sm" variant="secondary" onclick={startAnthropicOAuth} disabled={busy}>Start OAuth</Button>
                          <Button size="sm" variant="secondary" onclick={checkAnthropicOAuth} disabled={busy}>Check status</Button>
                          <Button size="sm" variant="ghost" onclick={clearAnthropicOAuth} disabled={busy}>Clear tokens</Button>
                        </div>
                      {:else}
                        <div class="flex flex-wrap gap-2">
                          <Button size="sm" variant="secondary" onclick={startChatgptOAuth} disabled={busy}>Start OAuth</Button>
                          <Button size="sm" variant="secondary" onclick={checkChatgptOAuth} disabled={busy}>Check status</Button>
                          <Button size="sm" variant="ghost" onclick={clearChatgptOAuth} disabled={busy}>Clear tokens</Button>
                        </div>
                      {/if}
                      {#if providerOAuthStatus}
                        <div class="rounded-xl border border-slate-700 bg-slate-950/70 p-3 text-xs text-slate-300">
                          <p>Status: <span class="font-mono text-slate-100">{providerOAuthStatus.status}</span></p>
                          {#if isAnthropicSubscriptionOAuth() && (providerOAuthStatus.authorization_url || providerOAuthStatus.verification_url)}
                            <p class="mt-2">Open <a class="text-sky-300 underline" href={providerOAuthStatus.authorization_url ?? providerOAuthStatus.verification_url ?? '#'} target="_blank" rel="noreferrer">Claude authorization</a>, then paste the final callback URL or <span class="font-mono">code#state</span> below.</p>
                          {/if}
                          {#if providerOAuthStatus.verification_url && providerOAuthStatus.user_code}
                            <p class="mt-2">Visit <a class="text-sky-300 underline" href={providerOAuthStatus.verification_url} target="_blank" rel="noreferrer">{providerOAuthStatus.verification_url}</a></p>
                            <p class="mt-1">Code: <span class="font-mono text-sky-100">{providerOAuthStatus.user_code}</span></p>
                          {/if}
                        </div>
                      {/if}
                      {#if isAnthropicSubscriptionOAuth()}
                        <div class="space-y-2 rounded-xl border border-slate-700 bg-slate-950/70 p-3 text-xs text-slate-300">
                          <label class="block space-y-2">
                            <span class="text-slate-200">Callback URL or code#state</span>
                            <Input bind:value={anthropicOAuthCallbackInput} placeholder="https://platform.claude.com/oauth/code/callback?code=...&state=..." />
                          </label>
                          <Button size="sm" variant="secondary" onclick={completeAnthropicOAuth} disabled={busy || !anthropicOAuthCallbackInput.trim()}>Complete OAuth</Button>
                        </div>
                      {:else}
                      <div class="rounded-xl border border-slate-700 bg-slate-950/70 p-3 text-xs text-slate-300">
                        <div class="flex flex-wrap items-center justify-between gap-2">
                          <p class="font-medium text-slate-200">Codex usage and limits</p>
                          <Button size="sm" variant="secondary" onclick={refreshCodexUsage} disabled={busy}>Refresh usage</Button>
                        </div>
                        {#if providerCodexUsage}
                          <div class="mt-2 space-y-1">
                            <p>Plan: <span class="font-mono text-slate-100">{providerCodexUsage.plan_type ?? 'unknown'}</span></p>
                            <p>Primary: {codexWindowLabel(providerCodexUsage.primary)}</p>
                            <p>Secondary: {codexWindowLabel(providerCodexUsage.secondary)}</p>
                            {#if providerCodexUsage.rate_limit_reached_type}
                              <p class="text-amber-200">Limit status: {providerCodexUsage.rate_limit_reached_type}</p>
                            {/if}
                            {#if providerCodexUsage.credits}
                              <p>Credits: {providerCodexUsage.credits.unlimited ? 'unlimited' : providerCodexUsage.credits.balance ?? 'available'}</p>
                            {/if}
                            {#if providerCodexUsage.usage_url}
                              <a class="text-sky-300 underline" href={providerCodexUsage.usage_url} target="_blank" rel="noreferrer">Open ChatGPT usage dashboard</a>
                            {/if}
                            {#if providerCodexUsage.fetched_at}
                              <p class="text-slate-500">Fetched {new Date(providerCodexUsage.fetched_at).toLocaleString()}</p>
                            {/if}
                          </div>
                        {:else if providerCodexUsageError}
                          <p class="mt-2 text-rose-200">{providerCodexUsageError}</p>
                        {:else}
                          <p class="mt-2 text-slate-400">Fetches the same Codex usage windows used by the Codex client. Exact remaining messages are not exposed.</p>
                        {/if}
                      </div>
                      {/if}
                    {:else}
                      <p class="text-xs text-sky-300">Create the provider first, then start OAuth.</p>
                    {/if}
                  </div>
                {:else if providerForm.auth_mode === 'env'}
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
              <span>Base URL {#if providerForm.preset !== 'ollama' && providerForm.preset !== 'anthropic'}<span class="text-rose-300">*</span>{/if}</span>
              <Input bind:value={providerForm.base_url} placeholder={providerForm.preset === 'ollama' ? 'http://localhost:11434' : providerForm.preset === 'anthropic' ? 'https://api.anthropic.com or compatible endpoint' : 'https://your-provider.example.com/v1'} />
            </label>
          {/if}

          {#if providerForm.preset === 'ollama'}
            <div class="rounded-2xl border border-sky-500/20 bg-sky-500/10 p-4 text-xs leading-5 text-sky-100">
              <p class="font-medium text-sky-50">Ollama must already be installed and running.</p>
              <p class="mt-1 text-sky-100/85">
                Cognis only performs read-only discovery via <code>/api/tags</code> and <code>/api/show</code>, then sends requests through LiteLLM. The model context window is also sent at runtime as Ollama <code>num_ctx</code>, bounded by discovered model metadata when available.
              </p>
              <p class="mt-1 text-sky-100/70">
                <code>localhost</code> is resolved where the provider runs: the controller for controller providers, or the selected executor for executor-routed inference.
              </p>
            </div>
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

          {#if providerForm.preset === 'chatgpt'}
            <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-4 text-sm text-slate-200">
              <input
                checked={providerForm.codex_transport === 'direct'}
                onchange={(event) => {
                  providerForm.codex_transport = event.currentTarget.checked ? 'direct' : 'litellm';
                }}
                type="checkbox"
                class="mt-1 rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300"
              />
              <span class="space-y-1">
                <span class="block font-medium">Use direct Codex transport</span>
                <span class="block text-xs text-slate-400">Default. Disable only to route Responses requests through LiteLLM. OAuth still uses the encrypted Cognis token store.</span>
              </span>
            </label>
          {/if}

          {#if selectedProviderId && providerForm.preset === 'ollama'}
            <div class="rounded-2xl border border-sky-500/20 bg-sky-500/5 p-4">
              <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p class="text-xs uppercase tracking-[0.25em] text-sky-300">Managed models</p>
                  <p class="mt-2 text-xs leading-5 text-slate-400">This provider owns host scope and inference routing. Each deployment manages one model rollout within it.</p>
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  onclick={() => goto(`/local-models?provider=${encodeURIComponent(selectedProviderId)}`)}
                >
                  Open Local Models
                </Button>
              </div>
              {#if selectedProviderDeployments().length > 0}
                <div class="mt-4 space-y-2">
                  {#each selectedProviderDeployments() as deployment (deployment.deployment_id)}
                    <a
                      href={`/local-models?provider=${encodeURIComponent(selectedProviderId)}&deployment=${encodeURIComponent(deployment.deployment_id)}`}
                      class="block rounded-xl border border-slate-800 bg-slate-950/60 p-3 transition hover:border-sky-500/40"
                    >
                      <div class="flex flex-wrap items-center justify-between gap-2">
                        <span class="font-mono text-sm text-slate-100">{deployment.runtime_name}</span>
                        <span class="text-xs text-sky-200">{deploymentRollout(deployment)}</span>
                      </div>
                      <p class="mt-2 text-xs text-slate-400">
                        {deployment.runtime_name === providerForm.default_model ? 'Default model' : 'Managed model'} ·
                        {deployment.selector.executor_ids?.length
                          ? `${deployment.selector.executor_ids.length} host${deployment.selector.executor_ids.length === 1 ? '' : 's'}`
                          : `labels ${JSON.stringify(deployment.selector.match_labels ?? {})}`}
                      </p>
                    </a>
                  {/each}
                </div>
              {:else if localModelsUnavailable}
                <p class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">Managed deployment status is temporarily unavailable. Open Local Models to retry; no empty state is being inferred.</p>
              {:else}
                <p class="mt-4 text-sm text-slate-400">No managed deployments use this provider yet.</p>
              {/if}
            </div>
          {/if}

          <!-- Models -->
          <div class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
            <div class="flex items-center justify-between gap-3">
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Models</p>
              <div class="flex gap-2">
                {#if providerForm.preset === 'ollama'}
                  <Button
                    size="sm"
                    variant="secondary"
                    onclick={() => goto(selectedProviderId ? `/local-models?provider=${encodeURIComponent(selectedProviderId)}` : '/local-models')}
                  >
                    Local Models
                  </Button>
                {/if}
                <Button
                  size="sm"
                  variant="secondary"
                  onclick={discoverModels}
                  disabled={busy || (providerForm.location === 'executor' && (providerForm.preset !== 'ollama' || providerExecutorTargetError(providerForm) !== null))}
                >
                  Discover
                </Button>
              </div>
            </div>
            {#if providerForm.location === 'executor' && providerForm.preset === 'ollama'}
              <p class="mt-3 text-xs text-slate-400">Ollama discovery runs on the selected executor and uses that executor's view of the base URL, so <code>localhost:11434</code> means Ollama on the executor host.</p>
            {:else if providerForm.location === 'executor'}
              <p class="mt-3 text-xs text-slate-400">Executor-routed discovery is currently available for Ollama providers only; add other executor-routed models manually.</p>
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

          <!--
            Action row lives inside the editor card on all viewports. The
            previous mobile-only fixed bottom action bar hovered over the
            page tab nav and read as detached chrome; an inline row keeps
            the buttons next to the form they belong to.
          -->
          <div class="flex flex-wrap gap-2 border-t border-slate-800 pt-4">
            <Button onclick={saveProvider} disabled={Boolean(providerSaveDisabledReason())}>{selectedProviderId ? 'Save provider' : 'Create provider'}</Button>
            <Button variant="secondary" onclick={resetProviderForm} disabled={busy}>{selectedProviderId ? 'Discard changes' : 'Clear form'}</Button>
            {#if selectedProviderId}
              <Button variant="secondary" onclick={() => testProvider(selectedProviderId)} disabled={!canManageProvider(selectedProvider()) || busy}>Test provider</Button>
              <Button variant="secondary" onclick={setDefaultProvider} disabled={!canManageProvider(selectedProvider()) || busy}>Set as default</Button>
              <Button variant="danger" onclick={() => deleteProvider(selectedProviderId)} disabled={!canManageProvider(selectedProvider()) || busy}>Delete</Button>
            {/if}
          </div>
          {#if providerSaveDisabledReason()}
            <p class="text-xs text-amber-200">{providerSaveDisabledReason()}</p>
          {/if}

          {#if providerTestResult}
            <div class={`rounded-2xl border px-4 py-3 text-sm ${providerTestResult.ok ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100' : 'border-rose-500/30 bg-rose-500/10 text-rose-100'}`}>
              {#if providerTestResult.ok}
                <p>Resolved model: {providerTestResult.model_resolved}</p>
                <p class="mt-1">Latency: {providerTestResult.latency_ms} ms</p>
                {#if providerTestResult.executor_routed}
                  <p class="mt-1">Executor: {providerTestResult.executor_id || 'selected by labels'} · Backend: {providerTestResult.executor_backend}</p>
                {/if}
              {:else}
                <p>{providerTestResult.error_detail}</p>
              {/if}
            </div>
          {/if}
        </Card>
        </div>
      </div>
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
                  <option value="">{defaultModelOptionLabel(route.key)}</option>
                  {#each routeModelOptions(route.key) as option}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              </div>
              {#if route.supportsThinking}
                <div class="space-y-2">
                  <span>Thinking effort</span>
                  <select bind:value={routingForm[route.key].reasoningEffort} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={routeThinkingEffortOptions(route.key).length === 0 || effectiveRouteModelId(route.key) === SAME_SESSION_MODEL_SENTINEL}>
                    <option value="">Default</option>
                    {#each routeThinkingEffortOptions(route.key) as value}
                      <option value={value}>{thinkingEffortLabel(value)}</option>
                    {/each}
                  </select>
                  {#if effectiveRouteModelId(route.key) === SAME_SESSION_MODEL_SENTINEL}
                    <span class="block text-xs text-slate-500">
                      Uses the active agent session's Thinking effort.
                    </span>
                  {:else if routeThinkingEffortOptions(route.key).length === 0}
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
    {:else if activeTab === 'notifications'}
      <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="space-y-5 p-5">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Notifications</p>
            <h2 class="mt-1 text-lg font-semibold text-white">This device</h2>
            <p class="mt-2 text-sm leading-6 text-slate-400">Native PWA notifications use the browser's Web Push subscription for this device.</p>
          </div>

          <div class="space-y-2 rounded-2xl border border-slate-800 bg-slate-950/70 p-4 text-sm">
            <div class="flex items-center justify-between gap-3">
              <span class="text-slate-400">Browser support</span>
              <span class={pushSupported ? 'text-emerald-300' : 'text-rose-300'}>{pushSupported ? 'supported' : 'unsupported'}</span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-slate-400">Permission</span>
              <span class="text-slate-100">{pushPermission}</span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-slate-400">Display mode</span>
              <span class="text-slate-100">{pushStandalone ? 'installed PWA' : 'browser tab'}</span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-slate-400">Device subscription</span>
              <span class={pushEnabledOnDevice ? 'text-emerald-300' : 'text-slate-300'}>{pushEnabledOnDevice ? 'enabled' : 'not enabled'}</span>
            </div>
          </div>

          {#if pushNeedsInstall}
            <p class="rounded-2xl border border-amber-400/30 bg-amber-400/10 px-4 py-3 text-sm text-amber-100">On iPhone or iPad, add Cognis to the Home Screen and open it there before enabling notifications.</p>
          {/if}

          {#if pushError}
            <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{pushError}</p>
          {/if}

          <div class="flex flex-wrap gap-2">
            {#if pushEnabledOnDevice}
              <Button onclick={disableDeviceNotifications} disabled={pushBusy} variant="secondary">Disable on this device</Button>
            {:else}
              <Button onclick={enableDeviceNotifications} disabled={pushBusy || !pushSupported}>Enable on this device</Button>
            {/if}
            <Button onclick={refreshPushStatus} disabled={pushBusy} variant="secondary">Refresh status</Button>
          </div>
        </Card>

        <div class="space-y-5">
          <Card class="space-y-5 p-5">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Server delivery</p>
              <h2 class="mt-1 text-lg font-semibold text-white">Push service status</h2>
            </div>

            <div class="grid gap-3 md:grid-cols-3">
              <div class="rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
                <p class="text-xs uppercase tracking-[0.2em] text-slate-500">Configured</p>
                <p class={pushVapid?.enabled ? 'mt-2 text-2xl font-semibold text-emerald-300' : 'mt-2 text-2xl font-semibold text-rose-300'}>{pushVapid?.enabled ? 'yes' : 'no'}</p>
              </div>
              <div class="rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
                <p class="text-xs uppercase tracking-[0.2em] text-slate-500">Enabled subscriptions</p>
                <p class="mt-2 text-2xl font-semibold text-white">{pushStatus?.enabled_subscriptions ?? 0}</p>
              </div>
              <div class="rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
                <p class="text-xs uppercase tracking-[0.2em] text-slate-500">Last test</p>
                <p class="mt-2 text-sm text-slate-100">{pushTestResult ? `${pushTestResult.sent_to} sent, ${pushTestResult.errors} failed` : 'not run'}</p>
              </div>
            </div>

            {#if pushVapid && !pushVapid.enabled}
              <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{pushVapid.reason ?? 'Web Push is not configured on this server.'}</p>
            {/if}

            {#if pushStatus?.last_error}
              <div class="rounded-2xl border border-amber-400/30 bg-amber-400/10 px-4 py-3">
                <p class="text-sm font-medium text-amber-100">Last delivery error</p>
                <p class="mt-2 break-words font-mono text-xs leading-5 text-amber-50/90">{pushStatus.last_error}</p>
              </div>
            {/if}

            <div class="flex flex-wrap gap-2">
              <Button onclick={sendTestNotification} disabled={pushBusy || !pushVapid?.enabled || !pushStatus?.enabled_subscriptions}>Send test notification</Button>
              <Button onclick={refreshPushStatus} disabled={pushBusy} variant="secondary">Refresh</Button>
            </div>
          </Card>

          <Card class="space-y-3 p-5">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Operator note</p>
              <h2 class="mt-1 text-lg font-semibold text-white">VAPID subject</h2>
            </div>
            <p class="text-sm leading-6 text-slate-400">For reliable delivery, set <code>COGNIS_VAPID_SUBJECT</code> to a real <code>mailto:</code> or <code>https:</code> contact URI. Development defaults such as <code>mailto:admin@localhost</code> may be rejected by Apple Web Push or FCM.</p>
          </Card>
        </div>
      </div>
    {:else if activeTab === 'display'}
      <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="space-y-5 p-5">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Display</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Appearance</h2>
            <p class="mt-2 text-sm leading-6 text-slate-400">Per-user preferences for visual presentation in the web UI.</p>
          </div>

          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Theme</span>
            <select
              class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
              value={$userPreferences.display.theme}
              disabled={busy}
              onchange={(event) => void updateDisplayPreference('theme', event.currentTarget.value as UserPreferences['display']['theme'])}
            >
              <option value="system">System</option>
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </select>
            <span class="block text-xs text-slate-500">Theme selection is persisted now; full theme switching can build on this setting.</span>
          </label>

          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Language</span>
            <Input
              value={$userPreferences.display.language}
              placeholder="auto"
              disabled={busy}
              onchange={(event) => void updateDisplayPreference('language', event.currentTarget.value.trim() || 'auto')}
            />
            <span class="block text-xs text-slate-500">Use <code>auto</code> or a language tag such as <code>en</code> or <code>cs-CZ</code>.</span>
          </label>
        </Card>

        <Card class="space-y-5 p-5">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Chat timeline</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Runtime detail visibility</h2>
            <p class="mt-2 text-sm leading-6 text-slate-400">Control how much of the agent execution trace is shown in normal chat.</p>
          </div>

          <div class="space-y-3">
            <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.chat.show_thinking_blocks}
                disabled={busy}
                class="mt-1 rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                onchange={(event) => void updateChatPreference('show_thinking_blocks', event.currentTarget.checked)}
              />
              <span>
                <span class="block font-medium text-slate-100">Show thinking blocks</span>
                <span class="mt-1 block text-xs leading-5 text-slate-500">When disabled, reasoning/thinking blocks are hidden from the normal timeline but remain available in raw logs.</span>
              </span>
            </label>

            <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.chat.keep_assistant_messages_separate}
                disabled={busy}
                class="mt-1 rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                onchange={(event) => void updateChatPreference('keep_assistant_messages_separate', event.currentTarget.checked)}
              />
              <span>
                <span class="block font-medium text-slate-100">Keep assistant messages separate</span>
                <span class="mt-1 block text-xs leading-5 text-slate-500">Do not fold assistant text into tool activity groups. Tool and thinking activity stays grouped.</span>
              </span>
            </label>

            <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.chat.group_tool_calls}
                disabled={busy}
                class="mt-1 rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                onchange={(event) => void updateChatPreference('group_tool_calls', event.currentTarget.checked)}
              />
              <span>
                <span class="block font-medium text-slate-100">Group tool and thinking activity</span>
                <span class="mt-1 block text-xs leading-5 text-slate-500">Collapse consecutive tool activity and multi-block thinking under compact semantic groups.</span>
              </span>
            </label>

            <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.chat.show_internal_tool_calls}
                disabled={busy}
                class="mt-1 rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                onchange={(event) => void updateChatPreference('show_internal_tool_calls', event.currentTarget.checked)}
              />
              <span>
                <span class="block font-medium text-slate-100">Show internal helper tool calls</span>
                <span class="mt-1 block text-xs leading-5 text-slate-500">Includes low-level helper calls such as todo updates. Keep this off for a cleaner chat transcript.</span>
              </span>
            </label>
          </div>
        </Card>
      </div>
    {:else if activeTab === 'web'}
      <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
        <!-- Left: split search + fetch backend selectors -->
        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Web tools</p>
              <h2 class="mt-1 text-lg font-semibold text-white">Search and fetch backends</h2>
            </div>
            <p class="text-sm leading-6 text-slate-400">
              Search and fetch are independent. Each agent's <code>web_search</code> uses the search backend. <code>web_fetch</code> should usually stay on direct extraction so browser fallback can rescue Cloudflare-blocked or JS-required pages automatically.
            </p>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Search backend</span>
              <select bind:value={webSearchBackendForm} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="direct">Direct (DuckDuckGo)</option>
                <option value="tavily" disabled={!webConfig.available_search_backends.includes('tavily')}>{WEB_BACKEND_INFO.tavily.label}{webConfig.available_search_backends.includes('tavily') ? '' : ' (unavailable)'}</option>
                <option value="brave" disabled={!webConfig.available_search_backends.includes('brave')}>{WEB_BACKEND_INFO.brave.label}{webConfig.available_search_backends.includes('brave') ? '' : ' (unavailable)'}</option>
                <option value="searxng" disabled={!webConfig.available_search_backends.includes('searxng')}>{WEB_BACKEND_INFO.searxng.label}{webConfig.available_search_backends.includes('searxng') ? '' : ' (unavailable)'}</option>
              </select>
            </label>
            <p class="text-sm leading-6 text-slate-400">{searchBackendDescription(webSearchBackendForm)}</p>
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Fetch backend</span>
              <select bind:value={webFetchBackendForm} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="direct">Direct extraction (httpx + trafilatura)</option>
                <option value="tavily" disabled={!webConfig.available_fetch_backends.includes('tavily')}>{WEB_BACKEND_INFO.tavily.label}{webConfig.available_fetch_backends.includes('tavily') ? '' : ' (unavailable)'}</option>
                <option value="browser">{WEB_BACKEND_INFO.browser.label}</option>
              </select>
            </label>
            <p class="text-sm leading-6 text-slate-400">{fetchBackendDescription(webFetchBackendForm)}</p>
            <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-950/50 px-4 py-3 text-sm text-slate-200">
              <input bind:checked={webFetchFallbackBrowserForm} type="checkbox" class="mt-1 rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300" disabled={webFetchBackendForm === 'browser'} />
              <span class="space-y-1">
                <span class="block font-medium text-slate-100">Browser fallback</span>
                <span class="block text-xs leading-5 text-slate-400">Retry direct fetch failures through the executor browser on Cloudflare/JS-required pages. This is recommended for normal direct fetch usage and ignored when fetch backend is set to always use the browser.</span>
              </span>
            </label>
            <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-950/50 px-4 py-3 text-sm text-slate-200">
              <input bind:checked={webBrowserFetchHeadedFallbackForm} type="checkbox" class="mt-1 rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300" />
              <span class="space-y-1">
                <span class="block font-medium text-slate-100">Prefer headed browser fallback</span>
                <span class="block text-xs leading-5 text-slate-400">After direct extraction fails, use headed mode when the executor enables headed browser sessions. Headless mode is selected only on executors that do not support headed sessions.</span>
              </span>
            </label>
            <div class="grid gap-3 md:grid-cols-2">
              <label class="space-y-1 text-sm text-slate-300">
                <span class="text-xs text-slate-400">Navigation timeout (seconds)</span>
                <Input bind:value={webBrowserFetchNavigationTimeoutForm} type="number" min="5" max="300" step="5" />
              </label>
              <label class="space-y-1 text-sm text-slate-300">
                <span class="text-xs text-slate-400">Initial wait state</span>
                <select bind:value={webBrowserFetchWaitUntilForm} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                  <option value="commit">commit</option>
                  <option value="domcontentloaded">domcontentloaded</option>
                  <option value="load">load</option>
                  <option value="networkidle">networkidle</option>
                </select>
              </label>
              <label class="space-y-1 text-sm text-slate-300">
                <span class="text-xs text-slate-400">Network idle soft wait (seconds)</span>
                <Input bind:value={webBrowserFetchNetworkIdleForm} type="number" min="0" max="30" step="1" />
              </label>
              <label class="space-y-1 text-sm text-slate-300">
                <span class="text-xs text-slate-400">Fetch session idle (seconds)</span>
                <Input bind:value={webBrowserFetchSessionIdleForm} type="number" min="10" max="3600" step="10" />
              </label>
              <label class="space-y-1 text-sm text-slate-300">
                <span class="text-xs text-slate-400">Pool wait timeout (seconds)</span>
                <Input bind:value={webBrowserFetchWaitTimeoutForm} type="number" min="1" max="300" step="1" />
              </label>
            </div>
            <Button class="w-full justify-center" onclick={saveWebBackend} disabled={!isAdmin || busy}>Save</Button>
          </div>
        </Card>

        <!-- Right: backend status + key setup -->
        <Card class="p-5">
          <div class="space-y-4">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Backend status</p>

            <!-- Direct -->
            <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div>
                <p class="font-medium text-white">Direct search + fetch path</p>
                <p class="text-xs text-slate-400">DuckDuckGo for direct search, httpx + trafilatura for direct fetch. Always available, free.</p>
              </div>
              <ProviderStatusBadge status="healthy" />
            </div>

            <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div>
                <p class="font-medium text-white">Browser fallback</p>
                <p class="text-xs text-slate-400">{webFetchFallbackBrowserForm ? 'Enabled for direct fetch failures on Cloudflare/JS-required pages.' : 'Disabled. Direct fetch errors are returned without browser retry.'}</p>
              </div>
              <ProviderStatusBadge status={webFetchFallbackBrowserForm ? 'healthy' : 'degraded'} />
            </div>

            <!-- Tavily -->
            <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <p class="font-medium text-white">{WEB_BACKEND_INFO.tavily.label}</p>
                  <p class="text-xs text-slate-400">{webBackendStatusLabel('tavily', webConfig)}</p>
                </div>
                <div class="flex items-center gap-2">
                  <Button size="sm" variant="secondary" onclick={() => openWebBackendEditor('tavily')}>Edit</Button>
                  <ProviderStatusBadge status={webConfig.tavily_configured && webConfig.tavily_enabled ? 'healthy' : 'degraded'} />
                </div>
              </div>
            </div>

            <!-- Brave -->
            <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <p class="font-medium text-white">{WEB_BACKEND_INFO.brave.label}</p>
                  <p class="text-xs text-slate-400">{webBackendStatusLabel('brave', webConfig)}</p>
                </div>
                <div class="flex items-center gap-2">
                  <Button size="sm" variant="secondary" onclick={() => openWebBackendEditor('brave')}>Edit</Button>
                  <ProviderStatusBadge status={webConfig.brave_configured && webConfig.brave_enabled ? 'healthy' : 'degraded'} />
                </div>
              </div>
            </div>

            <!-- SearXNG -->
            <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <p class="font-medium text-white">{WEB_BACKEND_INFO.searxng.label}</p>
                  <p class="text-xs text-slate-400">{webBackendStatusLabel('searxng', webConfig)}</p>
                </div>
                <div class="flex items-center gap-2">
                  <Button size="sm" variant="secondary" onclick={() => openWebBackendEditor('searxng')}>Edit</Button>
                  <ProviderStatusBadge status={webConfig.searxng_configured && webConfig.searxng_enabled ? 'healthy' : 'degraded'} />
                </div>
              </div>
            </div>

            <p class="text-xs text-slate-500">
              <code>web_crawl</code> and <code>web_map</code> work with any fetch backend. Tavily enables its native crawl/map engines, while <code>web_research</code> still requires Tavily.
            </p>
          </div>

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
          <Button variant="primary" size="sm" onclick={() => { executorForm = { executor_id: '', name: '', executor_type: 'websocket', labels: '', status: 'active', shared: false, is_default: false }; editingExecutor = null; executorToken = null; showExecutorForm = true; }}>New executor</Button>
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
              <label class="flex items-center gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 px-4 py-3 text-sm text-slate-200 md:col-span-2">
                <input bind:checked={executorForm.is_default} type="checkbox" class="rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300" />
                <span>Use as default executor</span>
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
                    await api.executor.update(editingExecutor.executor_id, { name: executorForm.name, labels, status: executorForm.status, shared: executorForm.shared, is_default: executorForm.is_default });
                  } else {
                    await api.executor.create({ executor_id: executorForm.executor_id || null, name: executorForm.name, executor_type: executorForm.executor_type, labels, shared: executorForm.shared, is_default: executorForm.is_default });
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
            <div class="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div class="flex flex-wrap items-center gap-3">
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
                {#if !exec.local_inference_enabled}
                  <span class="px-2 py-0.5 rounded bg-amber-500/15 text-amber-200 text-xs">local inference disabled</span>
                {/if}
              </div>
              <div class="flex flex-wrap gap-2">
                {#if canManage}
                  <Button variant="secondary" size="sm" onclick={() => {
                    editingExecutor = exec;
                    executorForm = {
                      executor_id: exec.executor_id,
                      name: exec.name,
                      executor_type: exec.executor_type,
                      labels: Object.entries(exec.labels || {}).map(([k, v]) => `${k}=${v}`).join(', '),
                      status: exec.status,
                      shared: !!exec.shared,
                      is_default: !!exec.is_default
                    };
                    showExecutorForm = true;
                  }}>Edit</Button>
                {/if}
                {#if canManage && exec.executor_type === 'websocket'}
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
            <ExecutorHealthPanel executor={exec} />
            {#if exec.executor_type === 'websocket'}
              <LocalInferenceSettings
                executor={exec}
                editable={canManage}
                saving={isExecutorSaving(exec.executor_id)}
                onSave={(config) => saveExecutorLocalInference(exec.executor_id, config)}
              />
            {/if}
            {#if executorRuntimeSummary(exec)}
              <div class="text-xs {exec.runtime_state === 'degraded' ? 'text-sky-300' : 'text-slate-500'}">{executorRuntimeSummary(exec)}</div>
            {/if}
            {#if executorDegradedDetails(exec).length > 0}
              <div class="space-y-1 rounded-xl border border-sky-500/20 bg-sky-500/5 px-3 py-2 text-xs text-sky-100/90">
                <p class="font-medium text-sky-100">Degraded executor details</p>
                {#each executorDegradedDetails(exec) as detail}
                  <p>{detail}</p>
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
                <p class="text-sm text-emerald-100">Copy this token now. It does not expire; generate a new token to revoke older tokens.</p>
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

            <!-- Office document tools settings -->
            {@const officeConfig = ((exec.config || {}).officecli || {}) as Record<string, unknown>}
            {@const officeEnabled = officeConfig.enabled !== false}
            {@const officeAutoInstall = officeConfig.auto_install !== false}
            {@const officeVersion = String(officeConfig.version ?? 'v1.0.102')}
            {@const officeBinaryPath = String(officeConfig.binary_path ?? '')}
            {@const officeCacheDir = String(officeConfig.cache_dir ?? '')}
            {@const officeRuntime = (exec.runtime_metadata.officecli || {}) as Record<string, unknown>}
            {@const officeAvailable = officeRuntime.available === true}
            {@const officeError = typeof officeRuntime.error === 'string' ? officeRuntime.error : ''}
            <details class="group">
              <summary class="cursor-pointer text-xs uppercase tracking-wider text-slate-400 hover:text-slate-300 select-none">
                Office Documents
                <span class="ml-1 text-slate-500">{officeEnabled ? (officeAvailable ? '(available)' : '(enabled)') : '(disabled)'}</span>
              </summary>
              <div class="mt-3 space-y-3 pl-1">
                <div class="flex flex-wrap gap-4">
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                    <input type="checkbox" checked={officeEnabled}
                      class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), officecli: { ...officeConfig, enabled: checked } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                        addToast(`Office document tools ${checked ? 'enabled' : 'disabled'}.`, 'success');
                      }}
                    />
                    Enabled
                  </label>
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                    <input type="checkbox" checked={officeAutoInstall} disabled={!officeEnabled}
                      class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), officecli: { ...officeConfig, auto_install: checked } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                        addToast(`OfficeCLI auto-install ${checked ? 'enabled' : 'disabled'}.`, 'success');
                      }}
                    />
                    Auto-install certified OfficeCLI
                  </label>
                </div>
                <div class="grid gap-3 md:grid-cols-3">
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Certified version</span>
                    <Input value={officeVersion} disabled={!officeEnabled}
                      onchange={async (e) => {
                        const cfg = { ...(exec.config || {}), officecli: { ...officeConfig, version: e.currentTarget.value.trim() || 'v1.0.102' } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    <span class="block text-xs text-slate-500">Only certified Cognis versions expose office tools.</span>
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Binary path override</span>
                    <Input value={officeBinaryPath} disabled={!officeEnabled}
                      placeholder="/usr/local/bin/officecli"
                      onchange={async (e) => {
                        const next: Record<string, unknown> = { ...officeConfig };
                        const value = e.currentTarget.value.trim();
                        if (value) next.binary_path = value;
                        else delete next.binary_path;
                        const cfg = { ...(exec.config || {}), officecli: next };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Cache directory</span>
                    <Input value={officeCacheDir} disabled={!officeEnabled}
                      placeholder="default: $COGNIS_DATA_DIR/cache/officecli"
                      onchange={async (e) => {
                        const next: Record<string, unknown> = { ...officeConfig };
                        const value = e.currentTarget.value.trim();
                        if (value) next.cache_dir = value;
                        else delete next.cache_dir;
                        const cfg = { ...(exec.config || {}), officecli: next };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                </div>
                <div class="text-xs text-slate-500">
                  Runtime: {officeAvailable ? `available ${String(officeRuntime.version ?? '')} via ${String(officeRuntime.installed_from ?? 'unknown')}` : `unavailable${officeError ? `: ${officeError}` : ''}`}
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
            {@const browserRuntime = String(browserConfig.runtime ?? 'playwright')}
            {@const browserChannel = String(browserConfig.channel ?? '')}
            {@const browserStealthEnabledRaw = browserConfig.stealth_enabled}
            {@const browserStealthEnabled = browserStealthEnabledRaw === undefined
              ? browserRuntime !== 'patchright'
              : browserStealthEnabledRaw !== false}
            {@const browserRealisticUserAgent = browserConfig.realistic_user_agent !== false}
            {@const browserDefaultTimezoneId = String(browserConfig.default_timezone_id ?? 'UTC')}
            {@const browserDefaultAcceptLanguage = String(browserConfig.default_accept_language ?? 'en-US,en;q=0.9')}
            {@const browserStealthEvasionsRaw = browserConfig.stealth_evasions}
            {@const browserStealthEvasions = Array.isArray(browserStealthEvasionsRaw)
              ? browserStealthEvasionsRaw.map((entry) => String(entry)).join(', ')
              : typeof browserStealthEvasionsRaw === 'string'
                ? browserStealthEvasionsRaw
                : ''}
            {@const browserAutoConsent = String(browserConfig.auto_consent ?? (browserStealthEnabled ? 'accept' : 'off'))}
            {@const browserAutoConsentDelayMs = Number(browserConfig.auto_consent_delay_ms ?? 800)}
            {@const browserAutoConsentDisabledRaw = browserConfig.auto_consent_disabled_domains}
            {@const browserAutoConsentDisabled = Array.isArray(browserAutoConsentDisabledRaw)
              ? browserAutoConsentDisabledRaw.map((entry) => String(entry)).join(', ')
              : typeof browserAutoConsentDisabledRaw === 'string'
                ? browserAutoConsentDisabledRaw
                : ''}
            {@const browserHumanizeInputRaw = browserConfig.humanize_input}
            {@const browserHumanizeInput = browserHumanizeInputRaw === undefined
              ? browserStealthEnabled
              : browserHumanizeInputRaw !== false}
            {@const browserHumanizeIntensity = String(browserConfig.humanize_intensity ?? 'low')}
            {@const browserFingerprintHardeningRaw = browserConfig.fingerprint_hardening}
            {@const browserFingerprintHardening = browserFingerprintHardeningRaw === undefined
              ? browserStealthEnabled
              : browserFingerprintHardeningRaw !== false}
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
                    <span class="text-xs text-slate-400">Runtime</span>
                    <select class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                      value={browserRuntime}
                      disabled={!browserEnabled}
                      onchange={async (e) => {
                        const value = e.currentTarget.value as 'playwright' | 'patchright';
                        const next: Record<string, unknown> = { ...browserConfig, runtime: value };
                        const cfg = { ...(exec.config || {}), browser: next };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                        addToast(`Browser runtime set to ${value}.`, 'success');
                      }}>
                      <option value="playwright">Playwright (default)</option>
                      <option value="patchright">Patchright (anti-detect)</option>
                    </select>
                    <span class="block text-xs text-slate-500">
                      Patchright reduces CDP detection but is not a Cloudflare/Turnstile silver bullet. Leave channel empty for bundled Chromium, or set chrome only after installing system Chrome yourself.
                    </span>
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Browser engine</span>
                    <select class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                      value={String(browserConfig.engine ?? 'chromium')}
                      disabled={!browserEnabled}
                      onchange={async (e) => {
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, engine: e.currentTarget.value } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}>
                      <option value="chromium">Chromium</option>
                      <option value="firefox">Firefox</option>
                      <option value="webkit">WebKit</option>
                    </select>
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Channel</span>
                    <Input value={browserChannel} disabled={!browserEnabled}
                      placeholder={browserRuntime === 'patchright' ? 'chrome (recommended)' : 'leave empty for bundled'}
                      onchange={async (e) => {
                        const value = e.currentTarget.value.trim();
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, channel: value || undefined } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    <span class="block text-xs text-slate-500">Use chrome, msedge, chrome-beta, etc. only when that system browser is already installed. Leave empty for bundled Chromium.</span>
                  </label>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Max sessions</span>
                    <Input value={Number(browserConfig.max_sessions ?? 8)} disabled={!browserEnabled}
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
                    <Input value={Number(browserConfig.idle_timeout_seconds ?? 1800)} disabled={!browserEnabled}
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
                <div class="rounded-xl border border-slate-700/60 bg-slate-900/40 p-3 space-y-3">
                  <div class="text-xs uppercase tracking-wider text-slate-400">
                    Stealth defaults
                    <span class="ml-1 text-slate-500">{browserStealthEnabled ? '(enabled)' : '(disabled)'}</span>
                  </div>
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                    <input type="checkbox" checked={browserStealthEnabled} disabled={!browserEnabled}
                      class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, stealth_enabled: checked } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                        addToast(`Browser stealth ${checked ? 'enabled' : 'disabled'}.`, 'success');
                      }}
                    />
                    Enable playwright-stealth evasions for new contexts
                  </label>
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                    <input type="checkbox" checked={browserRealisticUserAgent} disabled={!browserEnabled || !browserStealthEnabled}
                      class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, realistic_user_agent: checked } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    Send a realistic Chrome desktop user agent (avoids HeadlessChrome leak)
                  </label>
                  <div class="grid gap-3 md:grid-cols-2">
                    <label class="space-y-1 text-sm text-slate-300">
                      <span class="text-xs text-slate-400">Default timezone (when not overridden)</span>
                      <Input value={browserDefaultTimezoneId} disabled={!browserEnabled || !browserStealthEnabled}
                        placeholder="UTC"
                        onchange={async (e) => {
                          const value = e.currentTarget.value.trim();
                          const cfg = { ...(exec.config || {}), browser: { ...browserConfig, default_timezone_id: value || undefined } };
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                        }}
                      />
                    </label>
                    <label class="space-y-1 text-sm text-slate-300">
                      <span class="text-xs text-slate-400">Accept-Language header</span>
                      <Input value={browserDefaultAcceptLanguage} disabled={!browserEnabled || !browserStealthEnabled}
                        placeholder="en-US,en;q=0.9"
                        onchange={async (e) => {
                          const value = e.currentTarget.value.trim();
                          const cfg = { ...(exec.config || {}), browser: { ...browserConfig, default_accept_language: value || 'en-US,en;q=0.9' } };
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                        }}
                      />
                    </label>
                  </div>
                  <label class="space-y-1 text-sm text-slate-300">
                    <span class="text-xs text-slate-400">Disable specific evasions (comma-separated)</span>
                    <Input value={browserStealthEvasions} disabled={!browserEnabled || !browserStealthEnabled}
                      placeholder="navigator_languages, webgl_vendor, audio_context, battery_api, viewport_jitter"
                      onchange={async (e) => {
                        const items = e.currentTarget.value.split(',').map((entry) => entry.trim()).filter(Boolean);
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, stealth_evasions: items.length ? items : undefined } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    <span class="block text-xs text-slate-500">
                      Names from playwright_stealth.Stealth (e.g. <code>navigator_webdriver</code>, <code>webgl_vendor</code>) plus cognis fingerprint scripts (<code>audio_context</code>, <code>battery_api</code>, <code>viewport_jitter</code>). Use to skip an evasion that breaks a specific site.
                    </span>
                  </label>
                </div>
                <div class="rounded-xl border border-slate-700/60 bg-slate-900/40 p-3 space-y-3">
                  <div class="text-xs uppercase tracking-wider text-slate-400">
                    Behaviour
                  </div>
                  <div class="grid gap-3 md:grid-cols-3">
                    <label class="space-y-1 text-sm text-slate-300">
                      <span class="text-xs text-slate-400">Cookie consent</span>
                      <select class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                        value={browserAutoConsent}
                        disabled={!browserEnabled}
                        onchange={async (e) => {
                          const value = e.currentTarget.value as 'accept' | 'reject' | 'off';
                          const cfg = { ...(exec.config || {}), browser: { ...browserConfig, auto_consent: value } };
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                          addToast(`Cookie consent set to ${value}.`, 'success');
                        }}>
                        <option value="accept">Accept all (faster page render)</option>
                        <option value="reject">Reject all (privacy-first)</option>
                        <option value="off">Off</option>
                      </select>
                      <span class="block text-xs text-slate-500">Auto-clicks the chosen action on common cookie banners (OneTrust, Cookiebot, Quantcast, Sourcepoint, Didomi, ...).</span>
                    </label>
                    <label class="space-y-1 text-sm text-slate-300">
                      <span class="text-xs text-slate-400">Banner detection delay (ms)</span>
                      <Input value={browserAutoConsentDelayMs} disabled={!browserEnabled || browserAutoConsent === 'off'}
                        type="number" min="0" max="15000" step="50"
                        onchange={async (e) => {
                          const val = parseInt(e.currentTarget.value, 10);
                          if (isNaN(val)) return;
                          const cfg = { ...(exec.config || {}), browser: { ...browserConfig, auto_consent_delay_ms: val } };
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                        }}
                      />
                    </label>
                    <label class="space-y-1 text-sm text-slate-300">
                      <span class="text-xs text-slate-400">Disable on these hosts (comma-separated)</span>
                      <Input value={browserAutoConsentDisabled} disabled={!browserEnabled || browserAutoConsent === 'off'}
                        placeholder="example.com, foo.bar"
                        onchange={async (e) => {
                          const items = e.currentTarget.value.split(',').map((entry) => entry.trim()).filter(Boolean);
                          const cfg = { ...(exec.config || {}), browser: { ...browserConfig, auto_consent_disabled_domains: items.length ? items : undefined } };
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                        }}
                      />
                    </label>
                  </div>
                  <div class="grid gap-3 md:grid-cols-2">
                    <label class="flex items-center gap-2 text-sm text-slate-300">
                      <input type="checkbox" checked={browserHumanizeInput} disabled={!browserEnabled}
                        class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                        onchange={async (e) => {
                          const checked = e.currentTarget.checked;
                          const cfg = { ...(exec.config || {}), browser: { ...browserConfig, humanize_input: checked } };
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                        }}
                      />
                      Humanize click and type (Bezier mouse paths + jittered key intervals)
                    </label>
                    <label class="space-y-1 text-sm text-slate-300">
                      <span class="text-xs text-slate-400">Humanize intensity</span>
                      <select class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                        value={browserHumanizeIntensity}
                        disabled={!browserEnabled || !browserHumanizeInput}
                        onchange={async (e) => {
                          const value = e.currentTarget.value as 'off' | 'low' | 'medium' | 'high';
                          const cfg = { ...(exec.config || {}), browser: { ...browserConfig, humanize_intensity: value } };
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                        }}>
                        <option value="off">off (passthrough)</option>
                        <option value="low">low (~150 ms overhead)</option>
                        <option value="medium">medium (~300 ms overhead)</option>
                        <option value="high">high (~500 ms overhead)</option>
                      </select>
                    </label>
                  </div>
                  <label class="flex items-center gap-2 text-sm text-slate-300">
                    <input type="checkbox" checked={browserFingerprintHardening} disabled={!browserEnabled}
                      class="rounded border-slate-600 bg-slate-800 text-emerald-500 focus:ring-emerald-500/30 disabled:opacity-40"
                      onchange={async (e) => {
                        const checked = e.currentTarget.checked;
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, fingerprint_hardening: checked } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    Inject AudioContext / Battery / viewport-jitter fingerprint hardening
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
                  <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-4">
                    <div class="flex items-center justify-between gap-3">
                      <p class="font-medium text-white">Allow in-process executors</p>
                      <ProviderStatusBadge status={settingBool('executors.allow_in_process', true) ? 'healthy' : 'degraded'} />
                    </div>
                    <p class="mt-2 text-sm text-slate-400">Disable to prevent controller-local tool execution in production.</p>
                  </div>
                  <div class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-4">
                    <div class="flex items-center justify-between gap-3">
                      <p class="font-medium text-white">Allow subprocess executors</p>
                      <ProviderStatusBadge status={settingBool('executors.allow_subprocess', true) ? 'healthy' : 'degraded'} />
                    </div>
                    <p class="mt-2 text-sm text-slate-400">Disable to require persistent websocket executors instead of local child processes.</p>
                  </div>
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

            </Card>
          </div>

        {/if}

        {#if isAdmin}
          <SystemSettingsEditor
            {settings}
            disabled={busy}
            onsettingschange={(nextSettings) => { settings = nextSettings; }}
            ondirtychange={(dirty) => { systemSettingsDirty = dirty; }}
          />
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
            mcpForm = createEmptyMcpForm();
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
            {#if mcpForm.transport !== 'stdio'}
              <div class="space-y-4 rounded-2xl border border-slate-800 bg-slate-950/40 p-4">
                <div class="grid gap-4 md:grid-cols-2">
                  <label class="space-y-1 text-sm text-slate-200">
                    <span>Authentication</span>
                    <select bind:value={mcpForm.authType} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                      <option value="none">None</option>
                      <option value="static_headers">Static headers</option>
                      <option value="oauth2">OAuth 2.1 / MCP authorization</option>
                    </select>
                  </label>
                  <div class="text-xs text-slate-500">
                    {#if mcpForm.authType === 'oauth2'}
                      Cognis will open a browser authorization URL when this MCP server needs access and store OAuth tokens encrypted per user.
                    {:else if mcpForm.authType === 'static_headers'}
                      Use HTTP headers above for manually managed tokens.
                    {:else}
                      No MCP authentication metadata will be configured.
                    {/if}
                  </div>
                </div>
                {#if mcpForm.authType === 'oauth2'}
                  <div class="grid gap-4 md:grid-cols-2">
                    <label class="space-y-1 text-sm text-slate-200">
                      <span>Resource</span>
                      <Input bind:value={mcpForm.oauthResource} placeholder="Defaults to MCP server URL" />
                    </label>
                    <label class="space-y-1 text-sm text-slate-200">
                      <span>Scopes</span>
                      <Input bind:value={mcpForm.oauthScopes} placeholder="Optional space separated scopes" />
                    </label>
                  </div>
                  <details class="rounded-2xl border border-slate-800 bg-slate-900/40 p-4">
                    <summary class="cursor-pointer text-sm font-semibold text-slate-200">
                      Advanced OAuth settings
                    </summary>
                    <div class="mt-4 grid gap-4 md:grid-cols-2">
                      <label class="space-y-1 text-sm text-slate-200">
                        <span>Issuer</span>
                        <Input bind:value={mcpForm.oauthIssuer} placeholder="Discovered automatically when empty" />
                      </label>
                      <label class="space-y-1 text-sm text-slate-200">
                        <span>Authorization server</span>
                        <Input bind:value={mcpForm.oauthAuthorizationServer} placeholder="Optional explicit issuer URL" />
                      </label>
                      <label class="space-y-1 text-sm text-slate-200">
                        <span>Client ID</span>
                        <Input bind:value={mcpForm.oauthClientId} placeholder="Optional static client ID" />
                      </label>
                      <label class="space-y-1 text-sm text-slate-200">
                        <span>Client secret reference</span>
                        <Input bind:value={mcpForm.oauthClientSecretRef} placeholder="secret name or $secret:name" />
                      </label>
                      <label class="space-y-1 text-sm text-slate-200">
                        <span>Redirect URI</span>
                        <Input bind:value={mcpForm.oauthRedirectUri} placeholder="Optional override" />
                      </label>
                      <label class="space-y-1 text-sm text-slate-200">
                        <span>Callback mode</span>
                        <select
                          bind:value={mcpForm.oauthCallbackMode}
                          class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
                        >
                          <option value="auto">Auto</option>
                          <option value="controller_public">Controller public callback</option>
                          <option value="executor_loopback">Executor loopback callback</option>
                        </select>
                        <span class="block text-xs text-slate-500">
                          Executor loopback starts a temporary 127.0.0.1 callback listener on the selected executor.
                        </span>
                      </label>
                      <label class="space-y-1 text-sm text-slate-200">
                        <span>OAuth executor</span>
                        <select bind:value={mcpForm.oauthExecutorId} disabled={mcpForm.oauthCallbackMode !== 'executor_loopback' && mcpForm.oauthCallbackMode !== 'auto'} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-60">
                          <option value="">{defaultOAuthExecutorLabel()}</option>
                          {#each executorConfigs.filter((executor) => executor.executor_type !== 'in_process') as executor}
                            <option value={executor.executor_id}>{executor.name || executor.executor_id}</option>
                          {/each}
                        </select>
                        <span class="block text-xs text-slate-500">
                          Configure this when a provider only accepts loopback redirect URIs.
                        </span>
                      </label>
                      <label class="space-y-1 text-sm text-slate-200">
                        <span>Client metadata document URL</span>
                        <Input bind:value={mcpForm.oauthClientMetadataDocumentUrl} placeholder="Optional" />
                      </label>
                    </div>
                    <div class="mt-4">
                      <EnvVarEditor
                        envVars={mcpForm.oauthAuthorizationParams}
                        {secrets}
                        title="Extra authorization parameters"
                        emptyMessage="No extra authorization parameters configured."
                        addLabel="Add parameter"
                        keyPlaceholder="audience"
                        valuePlaceholder="value"
                        onChange={(next) => {
                          mcpForm.oauthAuthorizationParams = next;
                        }}
                        onCreateSecret={openMcpSecretModal}
                      />
                    </div>
                  </details>
                {/if}
              </div>
            {/if}
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
                      auth_config: mcpForm.transport !== 'stdio' ? mcpAuthConfigFromForm() : { type: 'none' },
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
                      auth_config: mcpForm.transport !== 'stdio' ? mcpAuthConfigFromForm() : { type: 'none' },
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
                  mcpForm = mcpFormFromServer(srv);
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
            {#if srv.auth_config?.type === 'oauth2'}
              {@const oauthStatus = mcpOAuthStatuses[srv.server_id]}
              {@const oauthStart = mcpOAuthStarts[srv.server_id]}
              <div class={`rounded-2xl border px-4 py-3 text-sm ${oauthBarClasses(oauthStatus)}`}>
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div class="min-w-0 flex-1">
                    <p class="font-medium">OAuth authorization</p>
                    <p class={`text-xs ${oauthMutedTextClasses(oauthStatus)}`}>
                      {formatMcpOAuthStatus(oauthStatus)}
                    </p>
                    {#if oauthStart}
                      <div class="mt-3 space-y-2">
                        {#if oauthStart.instructions}
                          <p class={`text-xs ${oauthMutedTextClasses(oauthStatus)}`}>{oauthStart.instructions}</p>
                        {/if}
                        <div class="rounded-xl border border-white/10 bg-slate-950/70 p-3">
                          <p class="mb-1 text-[11px] uppercase tracking-[0.18em] text-slate-400">
                            Authorization URL{oauthStart.copied ? ' · copied to clipboard' : ''}
                          </p>
                          <p class="break-all font-mono text-xs text-slate-100">{oauthStart.authorizationUrl}</p>
                        </div>
                      </div>
                    {/if}
                  </div>
                  <div class="flex gap-2">
                    <Button variant="secondary" size="sm" onclick={() => refreshMcpOAuthStatus(srv.server_id)}>Check</Button>
                    <Button variant="primary" size="sm" onclick={() => startMcpOAuth(srv)}>Authorize</Button>
                    {#if oauthStatus?.connected || oauthStatus?.authorized}
                      <Button variant="secondary" size="sm" onclick={() => disconnectMcpOAuth(srv)}>Disconnect</Button>
                    {/if}
                  </div>
                </div>
              </div>
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

{#if editingWebBackend}
  <WebBackendEditModal
    backendValue={editingWebBackend}
    configured={webBackendConfigured(editingWebBackend.backend, webConfig)}
    {busy}
    onclose={() => (editingWebBackend = null)}
    onsave={(value) => void saveWebBackendConfig(value)}
    onremove={() => void removeWebBackendConfiguration(editingWebBackend!.backend)}
  />
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
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>New password</span>
            <Input bind:value={userEditForm.password} type="password" autocomplete="new-password" placeholder="Leave blank to keep current" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Confirm new password</span>
            <Input bind:value={userEditForm.confirm_password} type="password" autocomplete="new-password" placeholder="Repeat new password" />
          </label>
        </div>
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
