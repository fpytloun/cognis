<script lang="ts">
  import { findTarget } from '$lib/find';
  import { ALL_SETTINGS_TABS as ALL_TABS, USER_SETTINGS_TABS as USER_TABS, type SettingsTab } from '$lib/settings-navigation';
  import { beforeNavigate, goto } from '$app/navigation';
  import { onMount, tick, untrack } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';

  import type { MCPEnvVar } from '$lib/agents';
  import { api, asApiError } from '$lib/api/client';
  import { THEME_FAMILIES, type ThemeFamily } from '$lib/theme/catalog';
  import { deriveGettingStartedSteps } from '$lib/getting-started';
  import { formatMcpOAuthStatus, isMcpOAuthStatusCritical, type MCPOAuthStatus } from '$lib/mcp-oauth-status';
  import { applyProviderPresetDefaults, PROVIDER_PRESETS, collectModelOptions, createProviderForm, deriveProviderId, isChatModel, isSpeechModelForRoute, modelEntryFromMetadata, parseExecutorSelector, presetHasBaseUrl, presetNeedsAuth, PRESET_LABELS, providerExecutorTargetError, providerFormToPayload, providerFormToUpdatePayload, providerRequiresExecutorLocation, type ProviderFormState, type ProviderModelOption, type ProviderPreset } from '$lib/providers';
  import { STEP_PROFILE_CAPABILITIES, STEP_PROFILE_GROUPS } from '$lib/workflows';
  import { defaultModelEntry, type LocalModelDeployment, type LocalModelTargetStatus, type ModelEntry } from '$lib/types/api';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ProviderStatusBadge from '$lib/components/ProviderStatusBadge.svelte';
  import ProviderHealthLine from '$lib/components/ProviderHealthLine.svelte';
  import { invalidateProviderMonitor, loadProviderMonitor, providerMonitoring } from '$lib/provider-monitor';
  import EnvVarEditor from '$lib/components/settings/EnvVarEditor.svelte';
  import ExecutorHealthPanel from '$lib/components/executors/ExecutorHealthPanel.svelte';
  import ExecutorCard from '$lib/components/executors/ExecutorCard.svelte';
  import { ageExecutor, appendResourceSample, type ResourceSample } from '$lib/executor-monitor';
  import ExecutorRuntimeDiagnostics from '$lib/components/executors/ExecutorRuntimeDiagnostics.svelte';
  import ExecutorCapabilitiesPanel from '$lib/components/executors/ExecutorCapabilitiesPanel.svelte';
  import LocalInferenceSettings from '$lib/components/executors/LocalInferenceSettings.svelte';
  import ModelCard from '$lib/components/settings/ModelCard.svelte';
  import CompanionRouting from '$lib/components/settings/CompanionRouting.svelte';
  import ModelRegistryDrift from '$lib/components/settings/ModelRegistryDrift.svelte';
  import ModelEditModal from '$lib/components/settings/ModelEditModal.svelte';
  import MfaSettings from '$lib/components/settings/MfaSettings.svelte';
  import ModelDiscoveryModal from '$lib/components/settings/ModelDiscoveryModal.svelte';
  import SystemSettingsEditor from '$lib/components/settings/SystemSettingsEditor.svelte';
  import WebBackendEditModal from '$lib/components/settings/WebBackendEditModal.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import InferenceRoutes from '$lib/components/settings/InferenceRoutes.svelte';
  import VideoSettings from '$lib/components/settings/VideoSettings.svelte';
  import type { InferenceRoute } from '$lib/inference-routes';
  import OfflineDataCard from '$lib/components/OfflineDataCard.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { buildLinkedServiceUrl, openUrlInNewTab } from '$lib/config';
  import { clearPersistedScroll } from '$lib/actions/scrollPersist';
  import { confirmAction } from '$lib/stores/confirm';
  import { onTabReset } from '$lib/stores/tabReset';
  import { addToast } from '$lib/stores/toasts';
  import { blockNavigationIfDirty, installBeforeUnloadGuard } from '$lib/navigation/unsaved';
  import { thinkingEffortLabel } from '$lib/thinking';
  import { auth } from '$lib/stores/auth';
  import { loadUserPreferences, saveUserPreferences, userPreferences } from '$lib/stores/userPreferences';
  import { wsClient, wsState } from '$lib/ws/client';
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
    reconcileWebPushSubscription,
    wantsWebPush
  } from '$lib/notifications';
  import { isNativeHost } from '$lib/native/host';
  import NativeNotificationSettings from '$lib/components/settings/NativeNotificationSettings.svelte';
  import {
    executorTypeChoices,
    executorUnavailable,
    executorToolSelectionGuard,
    executorRuntimeSummary,
    localExecutorTypesUnavailable,
    providerInferenceExecutors,
    providerSelectorCapabilityWarning,
    validateStdioCommand
  } from '$lib/executors';
  import type {
    ApiKeyCreateResponse,
    ApiKeyMetadata,
    CredentialMetadata,
    ExecutorConfig,
    ExecutorStatus,
    ExecutorRuntimeConfig,
    ExecutorTokenResponse,
    HealthResponse,
    LLMProvider,
    LLMProviderOAuthStatus,
    CodexUsageAdditionalLimit,
    CodexUsageWindow,
    ProviderUsage,
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
  const ROUTING_KEYS = ['default', 'classifier', 'auxiliary_generation', 'compaction', 'evaluator', 'audio_narrative', 'speech_to_text', 'text_to_speech', 'image_generation', 'video_generation', 'video_editing', 'video_analysis', 'attachment_analysis', 'embedding'] as const;
  const TEXT_ROUTING_KEYS = ['default', 'classifier', 'auxiliary_generation', 'compaction', 'evaluator', 'audio_narrative'] as const;
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
    { key: 'classifier', label: 'classifier', description: 'Typed tool classification and skill-tool selection. Supports chat models and native System One / Jev decision models.', supportsThinking: true },
    { key: 'auxiliary_generation', label: 'auxiliary_generation', description: 'Conversation recaps, workflow composition, and skill decomposition. Requires a chat model; falls back to default when unset.', supportsThinking: true },
    { key: 'compaction', label: 'compaction', description: 'Context compaction summaries. Defaults to the same model as the active agent session.', supportsThinking: true },
    { key: 'evaluator', label: 'evaluator', description: 'Workflow step evaluation. Falls back to default if not set.', supportsThinking: true },
    { key: 'speech_to_text', label: 'speech_to_text', description: 'Voice-note transcription. Use models like gpt-4o-transcribe, gpt-4o-mini-transcribe, or whisper.', supportsThinking: false },
    { key: 'text_to_speech', label: 'Speech / voice (text_to_speech)', description: 'Default speech provider and model for messages, document narration, podcasts, and conversation mode. Documents prefer their author agent’s accessible TTS provider, model, and voice.', supportsThinking: false },
    { key: 'audio_narrative', label: 'Podcast writing (audio_narrative)', description: 'Text model for source-grounded podcast scripts and optional message adaptation. Narration does not use this model. Inherits the Lite companion model when unset. Choose podcast length in the document’s Listen panel.', supportsThinking: true },
    { key: 'image_generation', label: 'image_generation', description: 'Image-capable model for avatars and tools. Must support image generation.', supportsThinking: false },
    { key: 'video_generation', label: 'video_generation', description: 'Native video model: Google Veo/Omni or OpenRouter video API. Requires a video spending policy. No fallback to chat.', supportsThinking: false },
    { key: 'video_editing', label: 'video_editing', description: 'Optional edit/extension model; inherits video_generation when unset. Source-provider continuity is required.', supportsThinking: false },
    { key: 'video_analysis', label: 'video_analysis', description: 'Verified native video-input model, without frame extraction. Inherits attachment_analysis only if native input is enabled.', supportsThinking: false },
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
      auxiliary_generation: { model: null, reasoning_effort: null },
      compaction: { model: null, reasoning_effort: null },
      evaluator: { model: null, reasoning_effort: null },
      speech_to_text: { model: null, reasoning_effort: null },
      text_to_speech: { model: null, reasoning_effort: null },
      audio_narrative: { model: null, reasoning_effort: null },
      image_generation: { model: null, reasoning_effort: null },
      video_generation: { model: null, reasoning_effort: null },
      video_editing: { model: null, reasoning_effort: null },
      video_analysis: { model: null, reasoning_effort: null },
      attachment_analysis: { model: null, reasoning_effort: null },
      embedding: { model: null, reasoning_effort: null }
    };
  }

  function emptyRoutingForm(): Record<RoutingKey, RoutingFormEntry> {
    return {
      default: emptyRoutingEntry(),
      classifier: emptyRoutingEntry(),
      auxiliary_generation: emptyRoutingEntry(),
      compaction: emptyRoutingEntry(),
      evaluator: emptyRoutingEntry(),
      speech_to_text: emptyRoutingEntry(),
      text_to_speech: emptyRoutingEntry(),
      audio_narrative: emptyRoutingEntry(),
      image_generation: emptyRoutingEntry(),
      video_generation: emptyRoutingEntry(),
      video_editing: emptyRoutingEntry(),
      video_analysis: emptyRoutingEntry(),
      attachment_analysis: emptyRoutingEntry(),
      embedding: emptyRoutingEntry()
    };
  }

  let activeTab = $state<SettingsTab>('providers');
  let settingsPanelAnchor = $state<HTMLDivElement | null>(null);
  let mobileTabListEl = $state<HTMLDivElement | null>(null);
  let providerEditorEl = $state<HTMLElement | null>(null);
  let loading = $state(true);
  let loadFailed = $state(false);
  let busy = $state(false);
  let savingExecutorIds = $state<string[]>([]);
  let error = $state('');
  let notice = $state('');
  let settings = $state<SettingsCategory[]>([]);
  let providers = $state<LLMProvider[]>([]);
  let inferenceRoutes = $state<InferenceRoute[]>([]);
  let localModelDeployments = $state<LocalModelDeployment[]>([]);
  let localModelTargets = $state<Record<string, LocalModelTargetStatus[]>>({});
  let localModelsUnavailable = $state(false);
  let modelRouting = $state<ModelRouting>(emptyModelRouting());
  let secrets = $state<SecretMetadata[]>([]);
  let credentials = $state<CredentialMetadata[]>([]);
  let health = $state<HealthResponse | null>(null);
  let diagnostics = $state<SystemDiagnostics | null>(null);
  let executorConfigs = $state<ExecutorConfig[]>([]);
  let executorStatus = $state<ExecutorStatus | null>(null);
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
  const toolUpdateQueues = new SvelteMap<string, { pending: string[]; inFlight: Promise<void> | null }>();
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
  const providerUsage = $derived(selectedProviderId ? $providerMonitoring[selectedProviderId]?.usage ?? null : null);
  let providerUsageError = $state('');
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
  let selectedExecutorId = $state<string | null>(null);
  type ExecutorDetailTab = 'overview' | 'tools' | 'runtime' | 'inference' | 'settings';
  let executorDetailTab = $state<ExecutorDetailTab>('overview');
  const executorDetailTabs: { id: ExecutorDetailTab; label: string }[] = [
    { id: 'overview', label: 'Overview' }, { id: 'tools', label: 'Tools & MCP' },
    { id: 'runtime', label: 'Runtime' }, { id: 'inference', label: 'Inference' },
    { id: 'settings', label: 'Settings' }
  ];
  let executorHistory = $state<Record<string, ResourceSample[]>>({});
  let executorReceivedAt = $state<Record<string, number>>({});
  const observedSnapshots = new WeakSet<object>();
  let monitorNow = $state(Date.now());
  let executorSearch = $state('');
  let executorIdentityBaseline = $state('');
  let newExecutorBaseline = $state('');
  let secretModalBaseline = $state('');
  $effect(() => {
    if (!showExecutorForm) newExecutorBaseline = '';
    else if (!editingExecutor && !newExecutorBaseline) newExecutorBaseline = JSON.stringify(executorForm);
  });
  $effect(() => {
    if (!showSecretModal) secretModalBaseline = '';
    else if (!secretModalBaseline) secretModalBaseline = JSON.stringify([secretModalName, secretModalValue]);
  });
  let executorIdentitySaving = $state(false);
  let executorInferenceDirty = $state(false);
  let executorRefreshPending = $state(false);
  let executorRefreshInFlight = $state(false);
  let executorMonitorVisible = $state(true);
  const visibleExecutors = $derived(executorConfigs.filter((executor) =>
    `${executor.name} ${executor.executor_id}`.toLowerCase().includes(executorSearch.toLowerCase())
  ));

  function observedExecutor(executor: ExecutorConfig): ExecutorConfig {
    return ageExecutor(executor, (monitorNow - (executorReceivedAt[executor.executor_id] ?? monitorNow)) / 1000);
  }

  function openExecutor(executor: ExecutorConfig): void {
    selectedExecutorId = executor.executor_id;
    executorDetailTab = 'overview';
    editingExecutor = executor;
    executorForm = {
      executor_id: executor.executor_id, name: executor.name, executor_type: executor.executor_type,
      labels: Object.entries(executor.labels || {}).map(([key, value]) => `${key}=${value}`).join(', '),
      status: executor.status, shared: !!executor.shared, is_default: !!executor.is_default
    };
    executorToken = null;
    showExecutorForm = true;
    executorIdentityBaseline = JSON.stringify(executorForm);
    executorInferenceDirty = false;
  }

  async function closeExecutor(): Promise<void> {
    if (executorIdentitySaving || (selectedExecutorId && isExecutorSaving(selectedExecutorId))) return;
    if ((showExecutorForm && JSON.stringify(executorForm) !== executorIdentityBaseline) || executorInferenceDirty) {
      if (!await confirmAction({ title: 'Discard unsaved changes?', message: 'Your unsaved executor settings will be lost.' })) return;
    }
    selectedExecutorId = null;
    showExecutorForm = false;
    editingExecutor = null;
    executorToken = null;
  }

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
    switch (activeTab) {
      case 'providers': return JSON.stringify(providerForm);
      case 'routing': return JSON.stringify({ routingForm, companionRouting: modelRouting });
      case 'tools': return JSON.stringify(stepProfileForms);
      case 'secrets': return JSON.stringify({ secretForm, credentialForm });
      case 'account': return JSON.stringify({
        passwordForm, newApiKeyName, newApiKeyExpiresInDays
      });
      case 'web': return JSON.stringify({
        webSearchBackendForm, webFetchBackendForm, webFetchFallbackBrowserForm,
        webBrowserFetchSessionIdleForm, webBrowserFetchWaitTimeoutForm,
        webBrowserFetchNavigationTimeoutForm, webBrowserFetchWaitUntilForm,
        webBrowserFetchNetworkIdleForm, webBrowserFetchHeadedFallbackForm
      });
      default: return '';
    }
  }

  function isDirty(): boolean {
    if (loading || !baselineReady) return false;
    return systemSettingsDirty
      || (activeTab === 'account' && accountNameForm !== (auth.getSnapshot().user?.name ?? ''))
      || snapshotState() !== initialSnapshot;
  }

  let baselineReady = $state(false);

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
    systemSettingsDirty = false;
    await loadSettings();
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
    return collectModelOptions(providers, true);
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
    if (routeKey === 'video_generation' || routeKey === 'video_editing') {
      return options.filter((option) => {
        const entry = findModelEntry(option.value);
        return entry?.model_mode === 'video' || entry?.supports_video_generation;
      });
    }
    if (routeKey === 'video_analysis') {
      return options.filter((option) => {
        const entry = findModelEntry(option.value);
        return entry && isChatModel(entry) && entry.supports_video_input;
      });
    }
    if (routeKey === 'image_generation') {
      return options.filter((option) => findModelEntry(option.value)?.supports_image_generation);
    }
    if (routeKey === 'attachment_analysis') {
      return options.filter((option) => {
        const entry = findModelEntry(option.value);
        return isChatModel(entry ?? { model_id: option.value }) && Boolean(
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
    if (routeKey === 'speech_to_text' || routeKey === 'text_to_speech') {
      return options.filter(option => isSpeechModelForRoute(routeKey, findModelEntry(option.value), option.value));
    }
    if (routeKey === 'compaction') {
      return [
        {
          value: SAME_SESSION_MODEL_SENTINEL,
          label: 'Same model as agent session',
          providerId: '',
          preferred: true
        },
        ...options.filter((option) => isChatModel(findModelEntry(option.value) ?? { model_id: option.value }))
      ];
    }
    return options.filter((option) => {
      const entry = findModelEntry(option.value);
      return (routeKey === 'classifier' && entry?.model_mode === 'systemone') ||
        isChatModel(entry ?? { model_id: option.value });
    });
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

  function codexWindowDuration(window: CodexUsageWindow): string {
    const minutes = window.window_duration_mins;
    if (!minutes) return 'rolling window';
    if (minutes % 1440 === 0) return `${minutes / 1440}-day rolling window`;
    if (minutes % 60 === 0) return `${minutes / 60}-hour rolling window`;
    return `${minutes}-minute rolling window`;
  }

  function codexWindowReset(window: CodexUsageWindow): string {
    return window.resets_at
      ? `Resets ${new Date(window.resets_at).toLocaleString()}`
      : 'Reset time unavailable';
  }

  function codexLimitTone(usedPercent: number): string {
    if (usedPercent >= 100) return 'bg-danger';
    if (usedPercent >= 85) return 'bg-warning';
    if (usedPercent >= 65) return 'bg-warning';
    return 'bg-success';
  }

  function codexBaseLimitLabel(window: CodexUsageWindow, position: 'primary' | 'secondary'): string {
    const minutes = window.window_duration_mins ?? 0;
    if (minutes >= 7 * 24 * 60) return 'Weekly rolling limit';
    if (minutes >= 24 * 60) return 'Long-term rolling limit';
    return position === 'primary' ? 'Short-term rolling limit' : 'Secondary rolling limit';
  }

  function codexAdditionalLimitLabel(limit: CodexUsageAdditionalLimit): string {
    return limit.limit_name?.trim() || limit.limit_id?.trim() || 'Additional feature limit';
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
    providerUsageError = '';
    anthropicOAuthCallbackInput = '';
  }

  async function selectProvider(provider: LLMProvider): Promise<void> {
    if (!(await confirmDiscardChanges())) {
      return;
    }
    applySelectedProvider(provider);
    await tick();
    initialSnapshot = snapshotState();
    if (window.innerWidth < 1024) {
      providerEditorEl?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function clearProviderSelection(): void {
    selectedProviderId = '';
    providerForm = createProviderForm();
    providerTestResult = null;
    providerOAuthStatus = null;
    providerUsageError = '';
    anthropicOAuthCallbackInput = '';
  }

  async function startNewProvider(): Promise<void> {
    if (!(await confirmDiscardChanges('Starting a new provider will replace the current unsaved edits.'))) {
      return;
    }
    clearProviderSelection();
    await tick();
    initialSnapshot = snapshotState();
    if (window.innerWidth < 1024) {
      providerEditorEl?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function handleProviderPresetChange(): void {
    applyProviderPresetDefaults(providerForm);
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
    providerUsageError = '';
    anthropicOAuthCallbackInput = '';
  }

  function isAnthropicSubscriptionOAuth(): boolean {
    return providerForm.preset === 'anthropic' && providerForm.auth_mode === 'oauth';
  }

  function providerUsageSupported(): boolean {
    if (providerForm.preset === 'chatgpt') return providerForm.auth_mode === 'oauth';
    return providerForm.preset === 'anthropic';
  }

  function providerUsageTitle(): string {
    if (providerForm.preset === 'chatgpt') return 'Codex usage and limits';
    if (isAnthropicSubscriptionOAuth()) return 'Claude subscription usage and limits';
    return 'Anthropic API rate limits';
  }

  function providerUsageHint(): string {
    if (providerForm.preset === 'chatgpt') {
      return 'Fetches the same Codex usage windows used by the Codex client. Exact remaining messages are not exposed.';
    }
    if (isAnthropicSubscriptionOAuth()) {
      return 'Fetches the same 5-hour and 7-day utilization windows Claude Code shows, plus the latest rate-limit headers seen by this controller.';
    }
    return 'API-key providers have no usage endpoint. Cognis reports the per-minute rate-limit headers from the latest Anthropic response.';
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

  let registrySnapshot = $state<{ scope: string; models: ModelEntry[] } | null>(null);

  function discoveryScope(form = providerForm): string {
    return JSON.stringify([
      form.provider_id, form.preset, form.location, form.base_url, form.backend,
      form.executor_id, form.executor_selector, form.auth_mode, form.auth_env_var,
      form.auth_secret_name, form.auth_secret_value, form.advanced_settings,
      form.aws_region_name, form.aws_profile_name,
    ]);
  }

  const registryCurrent = $derived(registrySnapshot?.scope === discoveryScope());
  function registryModel(id: string): ModelEntry | null {
    return registryCurrent ? registrySnapshot?.models.find((m) => m.model_id === id) || null : null;
  }

  async function discoverModels(): Promise<void> {
    busy = true;
    error = '';
    registrySnapshot = null;
    const scope = discoveryScope();
    try {
      let models: ModelEntry[];
      const useCurrentFormForDiscovery = providerForm.location === 'executor' && providerForm.preset === 'ollama';
      if (selectedProviderId && !useCurrentFormForDiscovery) {
        const saved = selectedProvider();
        if (saved && scope !== discoveryScope(createProviderForm(saved))) {
          throw new Error('Save or discard provider connection changes before registry discovery.');
        }
        const result = await api.llmProviders.discoverModels(selectedProviderId);
        models = result.models;
      } else {
        const executorLabels = parseExecutorSelector(providerForm.executor_selector);
        const result = await api.llmProviders.discoverModelsPreview({
          ...(selectedProviderId ? { provider_id: selectedProviderId } : {}),
          preset: providerForm.preset,
          base_url: providerForm.base_url,
          ...(providerForm.preset === 'bedrock' ? {
            aws_region_name: providerForm.aws_region_name,
            aws_profile_name: providerForm.aws_profile_name
          } : {}),
          catalog_provider: providerForm.advanced_settings.find((item) => item.key === 'catalog_provider')?.value,
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
      if (scope !== discoveryScope()) return;
      registrySnapshot = { scope, models };
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
    const newModels = selected.filter((m) => !existingIds.has(m.model_id)).map(modelEntryFromMetadata);
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
          catalog_provider: providerForm.advanced_settings.find((item) => item.key === 'catalog_provider')?.value,
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
      const enriched: ModelEntry = modelEntryFromMetadata({
        ...defaultModelEntry(mid),
        ...('error' in rawObj || !raw ? { provider_metadata: { field_sources: {}, catalog_status: 'enrichment_failed' } } : rawObj),
        model_id: mid
      });
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
      return 'border-danger-border bg-danger-subtle text-danger';
    }
    if (!status) {
      return 'border-warning-border bg-warning-subtle text-warning';
    }
    return 'border-accent/20 bg-accent-subtle text-accent';
  }

  function oauthMutedTextClasses(status: MCPOAuthStatus | undefined): string {
    if (isMcpOAuthStatusCritical(status)) {
      return 'text-danger/85';
    }
    if (!status) {
      return 'text-warning/85';
    }
    return 'text-accent/80';
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

  const presetOptions = PROVIDER_PRESETS;

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
    switch (activeTab) {
      case 'providers':
        [providers, secrets, executorConfigs] = await Promise.all([
          api.llmProviders.list().then((page) => page.items),
          api.secrets.list(),
          api.executor.list()
        ]);
        // Rollout status is informational, not a prerequisite for editing providers.
        void refreshLocalModelStatus();
        break;
      case 'routing':
        providers = await api.llmProviders.list().then((page) => page.items);
        break;
      case 'secrets':
        [secrets, credentials, agents] = await Promise.all([
          api.secrets.list(),
          api.credentials.list(),
          api.agents.list().then((page) => page.items.map((a) => ({
            agent_id: a.agent_id, name: a.name, is_system: a.is_system
          })))
        ]);
        break;
      case 'executors':
        [executorConfigs, executorTools, executorStatus, mcpServerConfigs] = await Promise.all([
          api.executor.list(), api.tools.executorTools(), api.executor.status(),
          api.tools.listMcpServerConfigs()
        ]);
        break;
      case 'system':
        [health, diagnostics] = await Promise.all([api.system.health(), api.system.diagnostics()]);
        break;
      case 'tools':
        [mcpServerConfigs, secrets, executorConfigs, executorTools] = await Promise.all([
          api.tools.listMcpServerConfigs(), api.secrets.list(), api.executor.list(), api.tools.executorTools()
        ]);
        stepProfileForms = isAdmin ? (await api.settings.stepProfiles()).map(toStepProfileForm) : [];
        break;
      case 'users':
        await loadUsers();
        break;
      case 'account':
        apiKeys = await api.auth.listApiKeys();
        accountNameForm = auth.getSnapshot().user?.name ?? '';
        accountNameDirty = false;
        break;
      case 'notifications':
        await loadUserPreferences(auth.getSnapshot().user?.email);
        await refreshPushStatus();
        break;
      case 'display':
        await loadUserPreferences(auth.getSnapshot().user?.email);
        break;
      case 'web':
        secrets = await api.secrets.list();
        break;
    }

    if (isAdmin && ['system', 'tools', 'web'].includes(activeTab)) {
      settings = await api.settings.list();
    }

    if (activeTab === 'routing') {
    modelRouting = await api.modelRouting.get();
    routingForm = {
      default: {
        model: modelRouting.default.model ?? '',
        reasoningEffort: modelRouting.default.reasoning_effort ?? ''
      },
      classifier: {
        model: modelRouting.classifier.model ?? '',
        reasoningEffort: modelRouting.classifier.reasoning_effort ?? ''
      },
      auxiliary_generation: {
        model: modelRouting.auxiliary_generation.model ?? '',
        reasoningEffort: modelRouting.auxiliary_generation.reasoning_effort ?? ''
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
      audio_narrative: {
        model: modelRouting.audio_narrative?.model ?? '',
        reasoningEffort: modelRouting.audio_narrative?.reasoning_effort ?? ''
      },
      image_generation: {
        model: modelRouting.image_generation.model ?? '',
        reasoningEffort: ''
      },
      video_generation: {
        model: modelRouting.video_generation?.model ?? '', reasoningEffort: ''
      },
      video_editing: {
        model: modelRouting.video_editing?.model ?? '', reasoningEffort: ''
      },
      video_analysis: {
        model: modelRouting.video_analysis?.model ?? '', reasoningEffort: ''
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
    }

    if (activeTab === 'web') {
    webConfig = await api.webConfig.status();
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
    }

    if (activeTab === 'providers') {
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
    }
    await tick();
    initialSnapshot = snapshotState();
  }

  let localModelStatusRequest = 0;

  async function refreshLocalModelStatus(): Promise<void> {
    const request = ++localModelStatusRequest;
    try {
      const deployments = await api.localModels.deployments();
      const targets = Object.fromEntries(await Promise.all(deployments.map(async (deployment) => [
        deployment.deployment_id, await api.localModels.targets(deployment.deployment_id)
      ] as const)));
      if (request !== localModelStatusRequest) return;
      localModelDeployments = deployments;
      localModelTargets = targets;
      localModelsUnavailable = false;
    } catch {
      if (request !== localModelStatusRequest) return;
      localModelDeployments = [];
      localModelTargets = {};
      localModelsUnavailable = true;
    }
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

  function canConfigureExecutor(exec: ExecutorConfig): boolean {
    return canManageExecutor(exec) && !executorUnavailable(exec);
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
    if (isNativeHost()) return;
    pushPermission = permissionState();
    pushSupported = isWebPushSupported();
    pushStandalone = isStandaloneDisplay();
    pushNeedsInstall = needsIosHomeScreenInstall();
    pushEnabledOnDevice = hasEnabledWebPush();
    [pushVapid, pushStatus] = await Promise.all([
      api.push.vapidPublicKey().catch(() => null),
      api.push.status().catch(() => null)
    ]);
    if (wantsWebPush()) {
      const reconciled = await reconcileWebPushSubscription();
      pushEnabledOnDevice = reconciled;
      if (reconciled) {
        pushStatus = await api.push.status().catch(() => pushStatus);
      }
    }
  }

  async function refreshExecutorsOnly(): Promise<void> {
    executorRefreshPending = false;
    executorRefreshInFlight = true;
    const before = new Map(executorConfigs.map(executor => [executor.executor_id, executor]));
    try {
      const [configs, status] = await Promise.all([api.executor.list(), api.executor.status()]);
      if (savingExecutorIds.length > 0 || toolUpdateQueues.size > 0) {
        executorRefreshPending = true;
        return;
      }
      executorConfigs = configs.map(config => {
        const current = executorConfigs.find(executor => executor.executor_id === config.executor_id);
        // A resource frame received during this inventory request owns the newer sample.
        return current && current.resource_snapshot !== before.get(config.executor_id)?.resource_snapshot
          ? { ...config, resource_snapshot: current.resource_snapshot }
          : config;
      });
      executorStatus = status;
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to refresh executors');
    } finally {
      executorRefreshInFlight = false;
    }
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
    loadFailed = false;
    baselineReady = false;
    error = '';
    notice = '';
    let loaded = false;
    try {
      isAdmin = auth.getSnapshot().user?.role === 'admin';
      syncTabFromUrl();
      await refreshPageState();
      loaded = true;
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      loadFailed = true;
    } finally {
      loading = false;
      // Mount bindings and child normalization are part of initialization, not edits.
      await tick();
      initialSnapshot = snapshotState();
      baselineReady = loaded;
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
        invalidateProviderMonitor(selectedProviderId);
        await loadProviderMonitor(selectedProviderId);
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
      invalidateProviderMonitor(selectedProviderId);
      providerUsageError = '';
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
      invalidateProviderMonitor(selectedProviderId);
      await loadProviderMonitor(selectedProviderId);
      if (providerOAuthStatus.status === 'authorized') {
        addToast('ChatGPT OAuth is authorized.', 'success');
      } else if (providerOAuthStatus.status === 'reauthorization_required') {
        addToast('ChatGPT sign-in expired or was revoked. Start OAuth again.', 'error', 6_000, 'Reauthorization required');
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to check OAuth');
    } finally {
      busy = false;
    }
  }

  async function refreshProviderUsage(): Promise<void> {
    if (!selectedProviderId) return;
    busy = true;
    providerUsageError = '';
    try {
      await loadProviderMonitor(selectedProviderId, true);
    } catch (caughtError) {
      providerUsageError = asApiError(caughtError).message;
      addToast(providerUsageError, 'error', 4_000, 'Unable to fetch provider usage');
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
      invalidateProviderMonitor(selectedProviderId);
      await loadProviderMonitor(selectedProviderId);
      providerOAuthStatus = null;
      providerUsageError = '';
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
      invalidateProviderMonitor(selectedProviderId);
      await loadProviderMonitor(selectedProviderId);
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
      } else if (providerOAuthStatus.status === 'reauthorization_required') {
        addToast('Claude sign-in expired or was revoked. Start OAuth again.', 'error', 6_000, 'Reauthorization required');
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
      invalidateProviderMonitor(selectedProviderId);
      await loadProviderMonitor(selectedProviderId);
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
        ...modelRouting,
        default: {
          model: routingForm.default.model || (routingForm.default.reasoningEffort ? effectiveRouteModelId('default') : '') || null,
          reasoning_effort: routingForm.default.reasoningEffort || null
        },
        classifier: {
          model: routingForm.classifier.model || (routingForm.classifier.reasoningEffort ? effectiveRouteModelId('classifier') : '') || null,
          reasoning_effort: routingForm.classifier.reasoningEffort || null
        },
        auxiliary_generation: {
          model: routingForm.auxiliary_generation.model || (routingForm.auxiliary_generation.reasoningEffort ? effectiveRouteModelId('auxiliary_generation') : '') || null,
          reasoning_effort: routingForm.auxiliary_generation.reasoningEffort || null
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
        audio_narrative: {
          model: routingForm.audio_narrative.model || null,
          reasoning_effort: routingForm.audio_narrative.reasoningEffort || null
        },
        image_generation: {
          model: routingForm.image_generation.model || null,
          reasoning_effort: null
        },
        video_generation: {
          model: routingForm.video_generation.model || null, reasoning_effort: null
        },
        video_editing: {
          model: routingForm.video_editing.model || null, reasoning_effort: null
        },
        video_analysis: {
          model: routingForm.video_analysis.model || null, reasoning_effort: null
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
      description: 'Zero-setup path. DDGS metasearch powers direct search; httpx + trafilatura power direct fetch and extraction.'
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
      return 'Direct search uses DDGS metasearch. Free and zero-setup, but community-maintained and occasionally less reliable than Brave, Tavily, or your own SearXNG instance.';
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
      webConfig = await api.webConfig.updateDefaults({
        search_backend: webSearchBackendForm as 'direct' | 'tavily' | 'brave' | 'searxng',
        fetch_backend: webFetchBackendForm as 'direct' | 'tavily' | 'browser'
      });
      webBackendForm = webConfig.backend;
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
      if (loading) return;
      void setActiveTab(tabs[0] ?? 'account');
      clearPersistedScroll('/settings');
      const el = document.querySelector<HTMLElement>('[data-app-content="true"]');
      if (el) el.scrollTo({ top: 0, behavior: 'smooth' });
    });

    return () => {
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
    if (activeTab !== 'executors') return;
    const release = wsClient.acquireExecutorMonitor();
    const visibilityChanged = () => { executorMonitorVisible = !document.hidden; };
    visibilityChanged();
    document.addEventListener('visibilitychange', visibilityChanged);
    let connected = false;
    const stopState = wsState.subscribe((state) => {
      if (state.status === 'connected' && !connected) executorRefreshPending = true;
      connected = state.status === 'connected';
    });
    const stopEvents = wsClient.subscribe((event) => {
      if (event.type === 'executor_monitor_update') {
        const previous = executorConfigs.find((executor) => executor.executor_id === event.executor_id);
        if (previous && (
          previous.applied_config_version !== event.applied_config_version
          || (event.details_revision && previous.runtime_metadata.monitor_runtime_revision !== event.details_revision)
        )) executorRefreshPending = true;
        executorConfigs = executorConfigs.map((executor) => executor.executor_id === event.executor_id
          ? { ...executor, ...event } : executor);
      } else if (event.type === 'scope_invalidated' && event.reason === 'executor_state_changed') {
        executorRefreshPending = true;
      } else if (event.type === 'reconnected') {
        executorRefreshPending = true;
      }
    });
    // Only age labels locally. No periodic HTTP requests.
    const clock = setInterval(() => { monitorNow = Date.now(); }, 1000);
    return () => {
      release();
      stopState();
      stopEvents();
      clearInterval(clock);
      document.removeEventListener('visibilitychange', visibilityChanged);
    };
  });

  $effect(() => {
    if (activeTab !== 'executors' || !executorMonitorVisible || !executorRefreshPending
      || executorRefreshInFlight || savingExecutorIds.length > 0 || toolUpdateQueues.size > 0) return;
    untrack(() => { void refreshExecutorsOnly(); });
  });

  $effect(() => {
    for (const executor of executorConfigs) {
      const snapshot = executor.resource_snapshot;
      if (!snapshot || observedSnapshots.has(snapshot)) continue;
      observedSnapshots.add(snapshot);
      untrack(() => {
        executorHistory[executor.executor_id] = appendResourceSample(executorHistory[executor.executor_id] ?? [], snapshot);
        executorReceivedAt[executor.executor_id] = Date.now();
      });
    }
  });
</script>

<svelte:head>
  <title>Settings · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading settings" description="Fetching the selected settings section." />
{:else if loadFailed}
  <p role="alert">{error}</p>
  <Button onclick={loadSettings}>Retry settings</Button>
{:else}
  <section class="space-y-5">
    <h1 class="hidden text-2xl font-semibold text-strong md:block">Settings</h1>
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
      <p class="rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">{error}</p>
    {/if}
    {#if notice}
      <p class="rounded-2xl border border-success-border bg-success-subtle px-4 py-3 text-sm text-success">{notice}</p>
    {/if}

    {#if activeTab === 'providers'}
      <div class="grid gap-5 lg:grid-cols-[340px_minmax(0,1fr)]">
        <Card class="p-5">
          <div class="space-y-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-muted">Providers</p>
                <h2 class="mt-1 text-lg font-semibold text-strong">LLM providers</h2>
              </div>
              <Button size="sm" variant={!selectedProviderId ? 'primary' : 'secondary'} onclick={startNewProvider} disabled={busy}>
                + New provider
              </Button>
            </div>
            {#each providers as provider}
              <button class={`w-full rounded-2xl border px-4 py-3 text-left transition ${selectedProviderId === provider.provider_id ? 'border-accent/40 bg-accent-subtle' : 'border-separator bg-canvas/70 hover:border-control-border'}`} onclick={() => selectProvider(provider)}>
                <div class="flex items-center justify-between gap-3">
                  <span class="font-medium text-foreground">{provider.is_default ? '⭐ ' : ''}{provider.display_name}</span>
                  <ProviderStatusBadge status={provider.status} />
                </div>
                <p class="mt-1 text-xs text-quiet">{provider.owner_email ? `Owned by ${provider.owner_email}` : 'Shared system provider'}</p>
                {#if provider.last_test}
                  <p class="mt-2 text-xs text-muted">
                    {provider.last_test.ok ? `Last test passed (${provider.last_test.model_resolved})` : provider.last_test.error_detail}
                  </p>
                {/if}
              </button>
            {:else}
              <p class="text-sm text-muted">No providers configured yet.</p>
            {/each}
          </div>
      </Card>

        <div bind:this={providerEditorEl}>
        <Card class="space-y-5 p-5">
          <!-- Identity -->
          <div class="grid gap-4 md:grid-cols-2">
           <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Name <span class="text-danger">*</span></span>
              <Input bind:value={providerForm.display_name} placeholder="My OpenAI" />
            </label>
            <div class="space-y-2 text-sm font-medium text-foreground">
              <span>ID</span>
              {#if selectedProviderId}
                <Input value={providerForm.provider_id} disabled />
              {:else}
                <span class="block rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-muted">{providerForm.provider_id || 'auto-generated from name'}</span>
              {/if}
            </div>
            {#if !selectedProviderId}
              <div class="md:col-span-2 text-xs text-quiet">
                Provider ID will be auto-generated as <span class="font-mono text-secondary">{deriveProviderId(providerForm.display_name) || 'unnamed'}</span>.
              </div>
            {/if}
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Provider type</span>
              <select bind:value={providerForm.preset} onchange={handleProviderPresetChange} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                {#each presetOptions as preset}
                  <option value={preset}>{PRESET_LABELS[preset]}</option>
                {/each}
              </select>
            </label>
            {#if isAdmin}
              <label class="space-y-2 text-sm font-medium text-foreground">
                <span>Ownership</span>
                <select bind:value={providerForm.owner_scope} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground disabled:opacity-60">
                  <option value="user">Personal provider</option>
                  <option value="system">Shared system provider</option>
                </select>
                <span class="block text-xs text-muted">Personal providers are owned by your account. Shared providers are visible to all users and admin-managed. Admins can change ownership when editing an existing provider.</span>
              </label>
            {:else}
              <div class="space-y-2 text-sm font-medium text-foreground">
                <span>Ownership</span>
                <div class="rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">Personal provider</div>
              </div>
            {/if}
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Execution location</span>
              <select bind:value={providerForm.location} onchange={handleProviderLocationChange} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                <option value="controller" disabled={providerRequiresExecutorLocation(providerForm.preset)}>Controller</option>
                {#if providerForm.preset !== 'chatgpt' && !isAnthropicSubscriptionOAuth()}
                  <option value="executor">Via executor</option>
                {/if}
              </select>
              {#if providerForm.preset === 'chatgpt'}
                <span class="block text-xs text-muted">ChatGPT OAuth tokens are hydrated on the controller, so executor routing is disabled for this preset.</span>
              {:else if isAnthropicSubscriptionOAuth()}
                <span class="block text-xs text-muted">Claude subscription OAuth tokens are managed on the controller, so executor routing is disabled for this auth mode.</span>
              {/if}
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Status</span>
              <select bind:value={providerForm.status} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                <option value="active">active</option>
                <option value="disabled">disabled</option>
              </select>
            </label>
          </div>

          {#if providerForm.location === 'executor'}
            <div class="rounded-2xl border border-accent/20 bg-accent-subtle p-4 space-y-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-accent/80">Executor routing</p>
                <p class="mt-2 text-sm text-secondary">This provider stays configured normally, but requests are executed from a matching remote executor instead of the controller.</p>
              </div>
              <label class="space-y-2 text-sm font-medium text-foreground block">
                <span>Executor</span>
                <select bind:value={providerForm.executor_id} onchange={handleProviderExecutorIdChange} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                  <option value="">Use label selector</option>
                  {#each localInferenceExecutorOptions() as executor}
                    <option value={executor.executor_id}>{executor.name} ({executor.executor_id}){executor.local_inference_enabled ? '' : ' — local inference disabled'}</option>
                  {/each}
                </select>
                <span class="block text-xs text-muted">Choose a concrete executor when this provider depends on executor-local auth or runtime state.</span>
              </label>
              <label class="space-y-2 text-sm font-medium text-foreground block">
                <span>Executor selector (key=value, comma-separated)</span>
                <Input bind:value={providerForm.executor_selector} placeholder="location=local, tier=gpu" disabled={Boolean(providerForm.executor_id.trim())} />
              </label>
              {#if selectedProviderExecutorError()}
                <p class="text-xs text-danger">{selectedProviderExecutorError()}</p>
              {/if}
              {#if selectedProviderExecutorWarning()}
                <p class="rounded-xl border border-warning-border bg-warning-subtle px-3 py-2 text-xs text-warning">{selectedProviderExecutorWarning()}</p>
              {/if}
              {#if executorConfigs.length > 0}
                <div class="flex flex-wrap gap-2">
                  <span class="text-xs text-muted self-center">Use labels from:</span>
                  {#each executorConfigs.filter((executor) => executor.executor_type === 'websocket' && executor.local_inference_enabled) as executor}
                    <Button size="sm" variant="secondary" onclick={() => { providerForm.executor_id = ''; providerForm.executor_selector = executorSelectorFor(executor.labels); }} disabled={Object.keys(executor.labels || {}).length === 0}>{executor.name}</Button>
                  {/each}
                </div>
              {/if}
            </div>
          {/if}

          <!-- Credentials -->
          {#if providerForm.preset === 'bedrock'}
            <div class="rounded-2xl border border-separator bg-surface/60 p-4 space-y-3">
              <p class="text-xs uppercase tracking-[0.25em] text-muted">AWS IAM credentials</p>
              <p class="text-sm text-secondary">Uses the AWS credential chain on the selected controller or executor: environment, shared profiles, or an IAM workload role. S3 credentials can be reused only if their IAM policy also grants Bedrock access. No keys are stored in provider configuration.</p>
              <div class="grid gap-3 md:grid-cols-2">
                <label class="space-y-2 text-sm font-medium text-foreground">
                  <span>AWS region</span>
                  <Input bind:value={providerForm.aws_region_name} placeholder="Use runtime AWS region" />
                </label>
                <label class="space-y-2 text-sm font-medium text-foreground">
                  <span>AWS profile (optional)</span>
                  <Input bind:value={providerForm.aws_profile_name} placeholder="Use default credential chain" />
                </label>
              </div>
            </div>
          {/if}
          {#if presetNeedsAuth(providerForm.preset)}
            <div class="rounded-2xl border border-separator bg-surface/60 p-4">
              <p class="text-xs uppercase tracking-[0.25em] text-muted">Credentials</p>
              <div class="mt-3 grid gap-3 md:grid-cols-2">
                <label class="space-y-2 text-sm font-medium text-foreground">
                  <span>Auth mode</span>
                  <select bind:value={providerForm.auth_mode} onchange={handleProviderAuthModeChange} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                    <option value="env">Environment variable</option>
                    <option value="secret">Credential store</option>
                    {#if providerForm.preset === 'chatgpt' || providerForm.preset === 'anthropic'}
                      <option value="oauth">{providerForm.preset === 'chatgpt' ? 'OAuth device flow' : 'Claude subscription OAuth'}</option>
                    {/if}
                  </select>
                </label>

                {#if providerForm.auth_mode === 'oauth'}
                  <div class="space-y-2 text-sm text-secondary">
                    <p class="font-medium text-foreground">Encrypted OAuth token storage</p>
                    {#if isAnthropicSubscriptionOAuth()}
                      <div class="rounded-xl border border-warning-border bg-warning-subtle p-3 text-xs text-warning">
                        This uses Claude Pro/Max subscription authentication through Anthropic's Claude Code OAuth flow.
                        It is an unofficial integration and may be affected by Anthropic account, subscription, or API policy changes.
                        Use it only with an account where this risk is acceptable.
                      </div>
                      <p class="text-xs text-muted">Tokens are stored in the encrypted Cognis secrets table and used only by controller-side Anthropic Messages calls.</p>
                    {:else}
                      <p class="text-xs text-muted">Tokens are stored in the encrypted Cognis secrets table. LiteLLM token files are temporary per request.</p>
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
                        <div class="rounded-xl border border-control-border bg-canvas/70 p-3 text-xs text-secondary">
                          <p>Status: <span class="font-mono text-foreground">{providerOAuthStatus.status}</span></p>
                          {#if providerOAuthStatus.status === 'reauthorization_required'}
                            <p class="mt-2 text-danger" role="alert">Sign-in expired or was revoked. Requests to this provider fail until you start OAuth again.</p>
                          {/if}
                          {#if isAnthropicSubscriptionOAuth() && (providerOAuthStatus.authorization_url || providerOAuthStatus.verification_url)}
                            <p class="mt-2">Open <a class="text-accent underline" href={providerOAuthStatus.authorization_url ?? providerOAuthStatus.verification_url ?? '#'} target="_blank" rel="noreferrer">Claude authorization</a>, then paste the final callback URL or <span class="font-mono">code#state</span> below.</p>
                          {/if}
                          {#if providerOAuthStatus.verification_url && providerOAuthStatus.user_code}
                            <p class="mt-2">Visit <a class="text-accent underline" href={providerOAuthStatus.verification_url} target="_blank" rel="noreferrer">{providerOAuthStatus.verification_url}</a></p>
                            <p class="mt-1">Code: <span class="font-mono text-accent">{providerOAuthStatus.user_code}</span></p>
                          {/if}
                        </div>
                      {/if}
                      {#if isAnthropicSubscriptionOAuth()}
                        <div class="space-y-2 rounded-xl border border-control-border bg-canvas/70 p-3 text-xs text-secondary">
                          <label class="block space-y-2">
                            <span class="text-foreground">Callback URL or code#state</span>
                            <Input bind:value={anthropicOAuthCallbackInput} placeholder="https://platform.claude.com/oauth/code/callback?code=...&state=..." />
                          </label>
                          <Button size="sm" variant="secondary" onclick={completeAnthropicOAuth} disabled={busy || !anthropicOAuthCallbackInput.trim()}>Complete OAuth</Button>
                        </div>
                      {/if}
                    {:else}
                      <p class="text-xs text-accent">Create the provider first, then start OAuth.</p>
                    {/if}
                  </div>
                {:else if providerForm.auth_mode === 'env'}
                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Env variable name</span>
                    <Input bind:value={providerForm.auth_env_var} placeholder="OPENAI_API_KEY" />
                    <span class="block text-xs text-muted">Must be set before starting Cognis.</span>
                  </label>
                {:else}
                  <div class="space-y-2 text-sm font-medium text-foreground">
                    <span>Credential</span>
                    <div class="flex gap-2">
                      <select bind:value={providerForm.auth_secret_name} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                        <option value="">Select credential...</option>
                        {#each secrets.filter((s) => s.scope === 'global' || s.scope === 'user') as secret}
                          <option value={secret.name}>{secret.name}{secret.description ? ` — ${secret.description}` : ''}</option>
                        {/each}
                      </select>
                      <Button size="sm" variant="secondary" onclick={openSecretModal}>New</Button>
                    </div>
                    {#if providerForm.auth_secret_name}
                      <span class="block text-xs text-muted">Using credential: {providerForm.auth_secret_name}</span>
                    {:else}
                      <span class="block text-xs text-accent">No credential selected. Create or select one.</span>
                    {/if}
                  </div>
                {/if}
              </div>
            </div>
          {/if}

          <section class="space-y-3 border-y border-separator py-3" aria-label="Provider monitoring">
            <label class="flex items-center gap-2 text-sm text-foreground">
              <input type="checkbox" bind:checked={providerForm.monitoring.enabled} />
              Automatic health and usage checks
            </label>
            <div class="grid gap-3 sm:grid-cols-3">
              <label class="space-y-1 text-xs text-muted">Check interval (minutes)
                <input class="w-full rounded-lg border border-control-border bg-canvas px-3 py-2 text-foreground" type="number" min="1" max="10080" bind:value={providerForm.monitoring.interval_minutes} />
              </label>
              <label class="space-y-1 text-xs text-muted">Warning remaining (%)
                <input class="w-full rounded-lg border border-control-border bg-canvas px-3 py-2 text-foreground" type="number" min="0" max="100" step="any" bind:value={providerForm.monitoring.warning_percent} />
              </label>
              <label class="space-y-1 text-xs text-muted">Critical remaining (%)
                <input class="w-full rounded-lg border border-control-border bg-canvas px-3 py-2 text-foreground" type="number" min="0" max={providerForm.monitoring.warning_percent} step="any" bind:value={providerForm.monitoring.critical_percent} />
              </label>
            </div>
            <p class="text-xs text-quiet">No generation calls. Alerts stay in Cognis. With automatic checks off, only Refresh now contacts the provider.</p>
            {#if selectedProviderId}
              <ProviderHealthLine providerId={selectedProviderId} showName={false} showUsage={false} />
            {/if}
          </section>

          {#if selectedProviderId && providerUsageSupported()}
            <div class="rounded-2xl border border-separator bg-surface/60 p-4" data-testid="provider-usage-card">
              <p class="text-xs uppercase tracking-[0.25em] text-muted">Usage and limits</p>
              <div class="mt-3">
                <div class="rounded-xl border border-control-border bg-canvas/70 p-3 text-xs text-secondary">
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <p class="font-medium text-foreground">{providerUsageTitle()}</p>
                    <Button size="sm" variant="secondary" onclick={refreshProviderUsage} disabled={busy}>Refresh usage</Button>
                  </div>
                  {#if providerUsage}
                    <div class="mt-3 space-y-3">
                      <div class="flex flex-wrap items-center justify-between gap-2">
                        {#if providerUsage.plan_type}
                          <p>Plan: <span class="font-mono text-foreground">{providerUsage.plan_type}</span></p>
                        {:else}
                          <p class="text-muted">{providerUsage.source === 'anthropic_rate_limit_headers' ? 'Per-minute rate limits' : 'Rolling usage windows'}</p>
                        {/if}
                        {#if providerUsage.limit_reached}
                          <span class="rounded-full bg-danger-subtle px-2 py-0.5 text-danger">Limit reached</span>
                        {/if}
                      </div>
                      {#if providerUsage.primary}
                        {@const window = providerUsage.primary}
                        <div class="rounded-lg border border-separator bg-canvas/50 p-2.5" data-testid="codex-primary-limit">
                          <div class="flex items-center justify-between gap-3">
                            <p class="font-medium text-foreground">{codexBaseLimitLabel(window, 'primary')}</p>
                            <span class="text-foreground">{Math.round(window.used_percent)}% used</span>
                          </div>
                          <div class="mt-2 h-2 overflow-hidden rounded-full bg-raised" role="progressbar" aria-label="Codex short-term rolling limit usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow={window.used_percent}>
                            <div class={`h-full rounded-full ${codexLimitTone(window.used_percent)}`} style={`width: ${Math.min(100, Math.max(0, window.used_percent))}%`}></div>
                          </div>
                          <p class="mt-1.5 text-muted">{codexWindowDuration(window)} · {codexWindowReset(window)}</p>
                        </div>
                      {/if}
                      {#if providerUsage.secondary}
                        {@const window = providerUsage.secondary}
                        <div class="rounded-lg border border-separator bg-canvas/50 p-2.5" data-testid="codex-secondary-limit">
                          <div class="flex items-center justify-between gap-3">
                            <p class="font-medium text-foreground">{codexBaseLimitLabel(window, 'secondary')}</p>
                            <span class="text-foreground">{Math.round(window.used_percent)}% used</span>
                          </div>
                          <div class="mt-2 h-2 overflow-hidden rounded-full bg-raised" role="progressbar" aria-label="Codex secondary rolling limit usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow={window.used_percent}>
                            <div class={`h-full rounded-full ${codexLimitTone(window.used_percent)}`} style={`width: ${Math.min(100, Math.max(0, window.used_percent))}%`}></div>
                          </div>
                          <p class="mt-1.5 text-muted">{codexWindowDuration(window)} · {codexWindowReset(window)}</p>
                        </div>
                      {/if}
                      {#each providerUsage.additional_rate_limits as limit}
                        {#if limit.primary}
                          {@const window = limit.primary}
                          <div class="rounded-lg border border-separator bg-canvas/50 p-2.5">
                            <div class="flex items-center justify-between gap-3">
                              <p class="font-medium text-foreground">{codexAdditionalLimitLabel(limit)}</p>
                              <span class="text-foreground">{Math.round(window.used_percent)}% used</span>
                            </div>
                            <div class="mt-2 h-2 overflow-hidden rounded-full bg-raised" role="progressbar" aria-label={`${codexAdditionalLimitLabel(limit)} usage`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={window.used_percent}>
                              <div class={`h-full rounded-full ${codexLimitTone(window.used_percent)}`} style={`width: ${Math.min(100, Math.max(0, window.used_percent))}%`}></div>
                            </div>
                            <p class="mt-1.5 text-muted">{codexWindowDuration(window)} · {codexWindowReset(window)}</p>
                          </div>
                        {/if}
                        {#if limit.secondary}
                          {@const window = limit.secondary}
                          <div class="rounded-lg border border-separator bg-canvas/50 p-2.5">
                            <div class="flex items-center justify-between gap-3">
                              <p class="font-medium text-foreground">{codexAdditionalLimitLabel(limit)} — secondary window</p>
                              <span class="text-foreground">{Math.round(window.used_percent)}% used</span>
                            </div>
                            <div class="mt-2 h-2 overflow-hidden rounded-full bg-raised" role="progressbar" aria-label={`${codexAdditionalLimitLabel(limit)} secondary usage`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={window.used_percent}>
                              <div class={`h-full rounded-full ${codexLimitTone(window.used_percent)}`} style={`width: ${Math.min(100, Math.max(0, window.used_percent))}%`}></div>
                            </div>
                            <p class="mt-1.5 text-muted">{codexWindowDuration(window)} · {codexWindowReset(window)}</p>
                          </div>
                        {/if}
                      {/each}
                      {#if providerUsage.rate_limit_reached_type}
                        <p class="text-warning">Limit status: {providerUsage.rate_limit_reached_type}</p>
                      {/if}
                      {#if providerUsage.credits}
                        <p>Credits: {providerUsage.credits.unlimited ? 'unlimited' : providerUsage.credits.balance ?? 'available'}</p>
                      {/if}
                      {#if providerUsage.usage_url}
                        <a class="text-accent underline" href={providerUsage.usage_url} target="_blank" rel="noreferrer">Open provider usage dashboard</a>
                      {/if}
                      {#if providerUsage.fetched_at}
                        <p class="text-quiet">Fetched {new Date(providerUsage.fetched_at).toLocaleString()}</p>
                      {/if}
                      {#if providerUsage.rate_limit_headers}
                        <details class="rounded-lg border border-separator bg-canvas/50 p-2.5" data-testid="provider-usage-rate-limit-headers">
                          <summary class="cursor-pointer text-secondary">Latest rate-limit headers{providerUsage.observed_at ? ` · observed ${new Date(providerUsage.observed_at).toLocaleString()}` : ''}</summary>
                          <dl class="mt-2 grid gap-1 font-mono text-[11px] text-muted">
                            {#each Object.entries(providerUsage.rate_limit_headers) as [name, value]}
                              <div class="flex justify-between gap-3"><dt>{name}</dt><dd class="text-foreground">{value}</dd></div>
                            {/each}
                          </dl>
                        </details>
                      {/if}
                      {#if !providerUsage.ok && providerUsage.unavailable_reason === 'no_requests_observed'}
                        <p class="text-muted">No Anthropic responses observed on this controller replica yet. Rate-limit headers appear after the first request.</p>
                      {/if}
                    </div>
                  {:else if providerUsageError}
                    <p class="mt-2 text-danger">{providerUsageError}</p>
                  {:else}
                    <p class="mt-2 text-muted">{providerUsageHint()}</p>
                  {/if}
                </div>
              </div>
            </div>
          {/if}

          <!-- Connection -->
          {#if presetHasBaseUrl(providerForm.preset)}
            <label class="block space-y-2 text-sm font-medium text-foreground">
              <span>Base URL {#if providerForm.preset !== 'ollama' && providerForm.preset !== 'anthropic'}<span class="text-danger">*</span>{/if}</span>
              <Input bind:value={providerForm.base_url} placeholder={providerForm.preset === 'ollama' ? 'http://localhost:11434' : providerForm.preset === 'anthropic' ? 'https://api.anthropic.com or compatible endpoint' : 'https://your-provider.example.com/v1'} />
              {#if providerForm.preset === 'openai_compatible' || providerForm.preset === 'litellm_proxy'}
                <span class="block text-xs font-normal text-muted">Service root or URL ending in /v1; both work for discovery, checks, and inference. Keep any path prefix required by your provider.</span>
              {/if}
            </label>
          {/if}

          {#if providerForm.preset === 'ollama'}
            <div class="rounded-2xl border border-accent/20 bg-accent-subtle p-4 text-xs leading-5 text-accent">
              <p class="font-medium text-accent">Ollama must already be installed and running.</p>
              <p class="mt-1 text-accent/85">
                Cognis only performs read-only discovery via <code>/api/tags</code> and <code>/api/show</code>, then sends requests through LiteLLM. The model context window is also sent at runtime as Ollama <code>num_ctx</code>, bounded by discovered model metadata when available.
              </p>
              <p class="mt-1 text-accent/70">
                <code>localhost</code> is resolved where the provider runs: the controller for controller providers, or the selected executor for executor-routed inference.
              </p>
            </div>
          {/if}

          {#if ['openai', 'openai_compatible', 'litellm_proxy'].includes(providerForm.preset)}
            <label class="flex items-start gap-3 rounded-2xl border border-separator bg-surface/60 p-4 text-sm text-foreground">
              <input bind:checked={providerForm.use_responses_api} type="checkbox" class="mt-1 rounded border-control-border bg-canvas text-accent focus:ring-accent" />
              <span class="space-y-1">
                <span class="block font-medium">Use OpenAI Responses transport when supported</span>
                <span class="block text-xs text-muted">Recommended for `gpt-5*` models. Disable this if your provider or LiteLLM proxy behaves better on the legacy chat-completions path.</span>
              </span>
            </label>
          {/if}

          {#if providerForm.preset === 'chatgpt'}
            <label class="flex items-start gap-3 rounded-2xl border border-separator bg-surface/60 p-4 text-sm text-foreground">
              <input
                checked={providerForm.codex_transport === 'direct'}
                onchange={(event) => {
                  providerForm.codex_transport = event.currentTarget.checked ? 'direct' : 'litellm';
                }}
                type="checkbox"
                class="mt-1 rounded border-control-border bg-canvas text-accent focus:ring-accent"
              />
              <span class="space-y-1">
                <span class="block font-medium">Use direct Codex transport</span>
                <span class="block text-xs text-muted">Default. Disable only to route Responses requests through LiteLLM. OAuth still uses the encrypted Cognis token store.</span>
              </span>
            </label>
          {/if}

          {#if selectedProviderId && providerForm.preset === 'ollama'}
            <div class="rounded-2xl border border-accent/20 bg-accent-subtle p-4">
              <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p class="text-xs uppercase tracking-[0.25em] text-accent">Managed models</p>
                  <p class="mt-2 text-xs leading-5 text-muted">This provider owns host scope and inference routing. Each deployment manages one model rollout within it.</p>
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
                      class="block rounded-xl border border-separator bg-canvas/60 p-3 transition hover:border-accent/40"
                    >
                      <div class="flex flex-wrap items-center justify-between gap-2">
                        <span class="font-mono text-sm text-foreground">{deployment.runtime_name}</span>
                        <span class="text-xs text-accent">{deploymentRollout(deployment)}</span>
                      </div>
                      <p class="mt-2 text-xs text-muted">
                        {deployment.runtime_name === providerForm.default_model ? 'Default model' : 'Managed model'} ·
                        {deployment.selector.executor_ids?.length
                          ? `${deployment.selector.executor_ids.length} host${deployment.selector.executor_ids.length === 1 ? '' : 's'}`
                          : `labels ${JSON.stringify(deployment.selector.match_labels ?? {})}`}
                      </p>
                    </a>
                  {/each}
                </div>
              {:else if localModelsUnavailable}
                <p class="mt-4 rounded-xl border border-warning-border bg-warning-subtle p-3 text-sm text-warning">Managed deployment status is temporarily unavailable. Open Local Models to retry; no empty state is being inferred.</p>
              {:else}
                <p class="mt-4 text-sm text-muted">No managed deployments use this provider yet.</p>
              {/if}
            </div>
          {/if}

          <!-- Models -->
          <div class="rounded-2xl border border-separator bg-surface/60 p-4">
            <div class="flex items-center justify-between gap-3">
              <p class="text-xs uppercase tracking-[0.25em] text-muted">Models</p>
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
              <p class="mt-3 text-xs text-muted">Ollama discovery runs on the selected executor and uses that executor's view of the base URL, so <code>localhost:11434</code> means Ollama on the executor host.</p>
            {:else if providerForm.location === 'executor'}
              <p class="mt-3 text-xs text-muted">Executor-routed discovery is currently available for Ollama providers only; add other executor-routed models manually.</p>
            {/if}

            <!-- Default model selection -->
            {#if providerForm.models.length > 0}
              <div class="mt-3">
                <label class="space-y-2 text-sm font-medium text-foreground">
                  <span>Default model <span class="text-danger">*</span></span>
                  <select bind:value={providerForm.default_model} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                    {#each providerForm.models as m}
                      <option value={m.model_id}>{m.model_id}</option>
                    {/each}
                  </select>
                </label>
              </div>
            {/if}

            <!-- Model cards -->
            <p class="mt-3 text-xs text-muted">
              {#if registryCurrent}
                Registry comparison is available below. Discovery can use a bundled catalog or return partial metadata.
                Differences are not automatically errors. Resets affect only this draft until Save provider.
              {:else}
                Click Discover to compare configured models with the provider registry. No comparison is available for the current connection.
              {/if}
            </p>
            <div class="mt-3 space-y-2">
              {#each providerForm.models as model (model.model_id)}
                <ModelCard
                  {model}
                  isDefault={model.model_id === providerForm.default_model}
                  onedit={() => (editingModel = { ...model })}
                  onremove={() => handleRemoveModel(model.model_id)}
                />
                {#if registryCurrent}
                  <ModelRegistryDrift {model} registry={registryModel(model.model_id)} onreset={handleSaveModelEdit} />
                {/if}
              {/each}
            </div>

            {#if providerForm.models.length === 0}
              <p class="mt-3 text-sm text-muted">No models configured. Click Discover to find available models, or add one manually below.</p>
            {/if}

            <!-- Manual add -->
            <div class="mt-3 flex gap-2">
              <Input bind:value={addModelId} placeholder="Add model by ID..." onkeydown={(e: KeyboardEvent) => e.key === 'Enter' && handleAddManualModel()} />
              <Button size="sm" variant="secondary" onclick={handleAddManualModel} disabled={busy || !addModelId.trim()}>Add</Button>
            </div>
          </div>

          <!-- Advanced settings -->
          {#if providerForm.advanced_settings.length > 0 || showAdvancedSettings}
            <div class="rounded-2xl border border-separator bg-surface/60 p-4">
              <button type="button" class="flex w-full items-center justify-between text-xs uppercase tracking-[0.25em] text-muted" onclick={() => (showAdvancedSettings = !showAdvancedSettings)}>
                <span>Advanced settings</span>
                <span class="text-quiet">{showAdvancedSettings ? '−' : '+'}</span>
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
                <p class="mt-2 text-xs text-muted">Additional key-value pairs merged into the provider config. Use for provider-specific litellm kwargs.</p>
              {/if}
            </div>
          {:else}
            <button type="button" class="text-xs text-quiet hover:text-secondary transition" onclick={() => (showAdvancedSettings = true)}>
              + Advanced settings
            </button>
          {/if}

          <!--
            Action row lives inside the editor card on all viewports. The
            previous mobile-only fixed bottom action bar hovered over the
            page tab nav and read as detached chrome; an inline row keeps
            the buttons next to the form they belong to.
          -->
          <div class="flex flex-wrap gap-2 border-t border-separator pt-4">
            <Button onclick={saveProvider} disabled={Boolean(providerSaveDisabledReason())}>{selectedProviderId ? 'Save provider' : 'Create provider'}</Button>
            <Button variant="secondary" onclick={resetProviderForm} disabled={busy}>{selectedProviderId ? 'Discard changes' : 'Clear form'}</Button>
            {#if selectedProviderId}
              <Button variant="secondary" onclick={() => testProvider(selectedProviderId)} disabled={!canManageProvider(selectedProvider()) || busy}>Test provider</Button>
              <Button variant="secondary" onclick={setDefaultProvider} disabled={!canManageProvider(selectedProvider()) || busy}>Set as default</Button>
              <Button variant="danger" onclick={() => deleteProvider(selectedProviderId)} disabled={!canManageProvider(selectedProvider()) || busy}>Delete</Button>
            {/if}
          </div>
          {#if providerSaveDisabledReason()}
            <p class="text-xs text-warning">{providerSaveDisabledReason()}</p>
          {/if}

          {#if providerTestResult}
            <div class={`rounded-2xl border px-4 py-3 text-sm ${providerTestResult.ok ? 'border-success-border bg-success-subtle text-success' : 'border-danger-border bg-danger-subtle text-danger'}`}>
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
      <InferenceRoutes {providers} onchange={(routes) => inferenceRoutes = routes} />
      <Card class="p-5">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Model routing</p>
            <h2 id="model-routing" class="mt-1 text-lg font-semibold text-strong">Task-type routing</h2>
          </div>
        </div>

        <div class="mt-4 grid gap-4 md:grid-cols-2">
          {#each ROUTING_METADATA as route}
            <div class="space-y-3 rounded-2xl border border-separator bg-canvas/50 p-4 text-sm font-medium text-foreground">
              <div class="space-y-2">
                <span>{route.label}</span>
                <select bind:value={routingForm[route.key].model} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" onchange={() => syncRouteThinkingEffort(route.key)}>
                  <option value="">{defaultModelOptionLabel(route.key)}</option>
                  {#each inferenceRoutes as named}
                    <option value={named.reference}>{named.reference} · named route</option>
                  {/each}
                  {#each routeModelOptions(route.key) as option}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              </div>
              {#if route.supportsThinking}
                <div class="space-y-2">
                  <span>Thinking effort</span>
                  <select bind:value={routingForm[route.key].reasoningEffort} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={routeThinkingEffortOptions(route.key).length === 0 || effectiveRouteModelId(route.key) === SAME_SESSION_MODEL_SENTINEL}>
                    <option value="">Default</option>
                    {#each routeThinkingEffortOptions(route.key) as value}
                      <option value={value}>{thinkingEffortLabel(value)}</option>
                    {/each}
                  </select>
                  {#if effectiveRouteModelId(route.key) === SAME_SESSION_MODEL_SENTINEL}
                    <span class="block text-xs text-quiet">
                      Uses the active agent session's Thinking effort.
                    </span>
                  {:else if routeThinkingEffortOptions(route.key).length === 0}
                    <span class="block text-xs text-quiet">
                      Select or resolve a model first to choose a Thinking effort.
                    </span>
                  {:else}
                    <span class="block text-xs text-quiet">Uses the model default when unset.</span>
                  {/if}
                </div>
              {/if}
              <span class="block text-xs text-muted">{route.description}</span>
            </div>
          {/each}
        </div>

        {#if routingWarnings().length > 0}
          <div class="mt-4 rounded-2xl border border-accent/30 bg-accent-subtle px-4 py-3 text-sm text-accent">
            {#each routingWarnings() as warning}
              <p>{warning}</p>
            {/each}
          </div>
        {/if}

        {#if modelRouting}
          <CompanionRouting bind:routing={modelRouting} {providers} />
        {/if}
        <div class="mt-5 flex justify-end">
          <Button onclick={saveRouting} disabled={!isAdmin || busy}>Save routing</Button>
        </div>
        </Card>
    {:else if activeTab === 'secrets'}
      <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="p-5">
          <div class="space-y-4">
            <p class="text-sm leading-6 text-muted">
              Encrypted secrets for tool execution sandboxes and LLM provider credentials.
            </p>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Name <span class="text-danger">*</span></span>
              <Input bind:value={secretForm.name} placeholder="openai_api_key" />
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Value <span class="text-danger">*</span></span>
              <Input bind:value={secretForm.value} type="password" placeholder="write-only secret" />
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Scope</span>
              <select bind:value={secretForm.scope} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                {#if isAdmin}
                  <option value="system">System (shared infrastructure)</option>
                {/if}
                <option value="user">User (current user only)</option>
                <option value="agent">Agent-specific</option>
              </select>
            </label>
            {#if secretForm.scope === 'agent'}
              <label class="space-y-2 text-sm font-medium text-foreground">
                <span>Agent</span>
                <select bind:value={secretForm.agent_id} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                  <option value="">Select agent...</option>
                  {#each agents.filter((a) => !a.is_system) as agent}
                    <option value={agent.agent_id}>{agent.name} ({agent.agent_id})</option>
                  {/each}
                </select>
              </label>
            {/if}
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Description</span>
              <Input bind:value={secretForm.description} placeholder="What this secret is for" />
            </label>
            <Button class="w-full justify-center" onclick={saveSecret} disabled={busy}>Save secret</Button>
          </div>
        </Card>
        <Card class="p-5">
          <div class="space-y-3">
            {#each secrets as secret}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
                <div>
                  <p class="font-medium text-strong">{secret.name}</p>
                  <p class="text-xs text-muted">{secret.scope}{secret.agent_id ? ` · ${secret.agent_id}` : ''}</p>
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
            <p class="text-sm leading-6 text-muted">
              Structured credentials for agents and browser automation. Payload and metadata are stored separately from LLM context.
            </p>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Credential ID <span class="text-danger">*</span></span>
              <Input bind:value={credentialForm.credential_id} placeholder="github_work" />
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Kind</span>
              <select bind:value={credentialForm.kind} onchange={(event) => updateCredentialKind((event.currentTarget as HTMLSelectElement).value)} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                <option value="token">Token</option>
                <option value="text">Text</option>
                <option value="username_password">Username/password</option>
                <option value="totp_seed">TOTP seed</option>
                <option value="recovery_codes">Recovery codes</option>
                <option value="browser_storage_state">Browser auth state</option>
              </select>
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Label <span class="text-danger">*</span></span>
              <Input bind:value={credentialForm.label} placeholder="GitHub work login" />
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Payload (JSON)</span>
              <textarea bind:value={credentialForm.payload_json} class="min-h-[140px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 font-mono text-sm text-foreground"></textarea>
              <span class="block text-xs text-muted">Expected payload template for <code>{credentialForm.kind}</code>. For login forms use <code>username</code>/<code>password</code>; for saved browser reuse use <code>browser_storage_state</code>.</span>
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Metadata (JSON)</span>
              <textarea bind:value={credentialForm.metadata_json} class="min-h-[120px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 font-mono text-sm text-foreground"></textarea>
              <span class="block text-xs text-muted">{credentialMetadataHint(credentialForm.kind)}</span>
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Scope</span>
              <select bind:value={credentialForm.scope} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                <option value="user">User</option>
                <option value="agent">Agent-specific</option>
              </select>
            </label>
            {#if credentialForm.scope === 'agent'}
              <label class="space-y-2 text-sm font-medium text-foreground">
                <span>Agent</span>
                <select bind:value={credentialForm.agent_id} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                  <option value="">Select agent...</option>
                  {#each agents.filter((a) => !a.is_system) as agent}
                    <option value={agent.agent_id}>{agent.name} ({agent.agent_id})</option>
                  {/each}
                </select>
              </label>
            {/if}
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Expires at</span>
              <Input bind:value={credentialForm.expires_at} type="datetime-local" />
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Description</span>
              <Input bind:value={credentialForm.description} placeholder="What this credential is for" />
            </label>
            <Button class="w-full justify-center" onclick={saveCredential} disabled={busy}>Save credential</Button>
          </div>
        </Card>
        <Card class="p-5">
          <div class="space-y-3">
            {#each credentials as credential}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
                <div>
                  <p class="font-medium text-strong">{credential.label}</p>
                  <p class="text-xs text-muted">{credential.credential_id} · {credential.kind} · {credential.status}</p>
                </div>
                <Button size="sm" variant="danger" onclick={() => deleteCredential(credential)} disabled={busy}>Delete</Button>
              </div>
            {/each}
          </div>
        </Card>
      </div>
    {:else if activeTab === 'notifications'}
      {#if isNativeHost()}
        <NativeNotificationSettings />
      {:else}
      <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="space-y-5 p-5">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Notifications</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">This device</h2>
            <p class="mt-2 text-sm leading-6 text-muted">Native PWA notifications use the browser's Web Push subscription for this device.</p>
          </div>

          <div class="space-y-2 rounded-2xl border border-separator bg-canvas/70 p-4 text-sm">
            <div class="flex items-center justify-between gap-3">
              <span class="text-muted">Browser support</span>
              <span class={pushSupported ? 'text-success' : 'text-danger'}>{pushSupported ? 'supported' : 'unsupported'}</span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-muted">Permission</span>
              <span class="text-foreground">{pushPermission}</span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-muted">Display mode</span>
              <span class="text-foreground">{pushStandalone ? 'installed PWA' : 'browser tab'}</span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-muted">Device subscription</span>
              <span class={pushEnabledOnDevice ? 'text-success' : 'text-secondary'}>{pushEnabledOnDevice ? 'enabled' : 'not enabled'}</span>
            </div>
          </div>

          {#if pushNeedsInstall}
            <p class="rounded-2xl border border-warning-border bg-warning-subtle px-4 py-3 text-sm text-warning">On iPhone or iPad, add Cognis to the Home Screen and open it there before enabling notifications.</p>
          {/if}

          {#if pushError}
            <p class="rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">{pushError}</p>
          {/if}

          <div class="flex flex-wrap gap-2">
            {#if pushEnabledOnDevice}
              <Button onclick={disableDeviceNotifications} disabled={pushBusy} variant="secondary">Disable on this device</Button>
            {:else}
              <Button onclick={enableDeviceNotifications} disabled={pushBusy || !pushSupported}>Enable on this device</Button>
            {/if}
            <Button onclick={refreshPushStatus} disabled={pushBusy} variant="secondary">Refresh status</Button>
          </div>
          <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
            <input
              type="checkbox"
              checked={$userPreferences.notifications.include_content}
              disabled={busy}
              class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
              onchange={(event) => void updateUserPreferences({
                ...$userPreferences,
                notifications: { include_content: event.currentTarget.checked }
              })}
            />
            <span>
              <span class="block font-medium text-foreground">Show notification content</span>
              <span class="mt-1 block text-xs leading-5 text-quiet">
                Include replies, questions, results, and action context in system notifications. Disable this to show generic attention notices.
              </span>
            </span>
          </label>
        </Card>

        <div class="space-y-5">
          <OfflineDataCard />
          <Card class="space-y-5 p-5">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-muted">Server delivery</p>
              <h2 class="mt-1 text-lg font-semibold text-strong">Push service status</h2>
            </div>

            <div class="grid gap-3 md:grid-cols-3">
              <div class="rounded-2xl border border-separator bg-canvas/70 p-4">
                <p class="text-xs uppercase tracking-[0.2em] text-quiet">Configured</p>
                <p class={pushVapid?.enabled ? 'mt-2 text-2xl font-semibold text-success' : 'mt-2 text-2xl font-semibold text-danger'}>{pushVapid?.enabled ? 'yes' : 'no'}</p>
              </div>
              <div class="rounded-2xl border border-separator bg-canvas/70 p-4">
                <p class="text-xs uppercase tracking-[0.2em] text-quiet">Enabled subscriptions</p>
                <p class="mt-2 text-2xl font-semibold text-strong">{pushStatus?.enabled_subscriptions ?? 0}</p>
              </div>
              <div class="rounded-2xl border border-separator bg-canvas/70 p-4">
                <p class="text-xs uppercase tracking-[0.2em] text-quiet">Last test</p>
                <p class="mt-2 text-sm text-foreground">{pushTestResult ? `${pushTestResult.sent_to} sent, ${pushTestResult.errors} failed` : 'not run'}</p>
              </div>
            </div>

            {#if pushVapid && !pushVapid.enabled}
              <p class="rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">{pushVapid.reason ?? 'Web Push is not configured on this server.'}</p>
            {/if}

            {#if pushStatus?.last_error}
              <div class="rounded-2xl border border-warning-border bg-warning-subtle px-4 py-3">
                <p class="text-sm font-medium text-warning">Last delivery error</p>
                <p class="mt-2 break-words font-mono text-xs leading-5 text-warning/90">{pushStatus.last_error}</p>
              </div>
            {/if}

            <div class="flex flex-wrap gap-2">
              <Button onclick={sendTestNotification} disabled={pushBusy || !pushVapid?.enabled || !pushStatus?.enabled_subscriptions}>Send test notification</Button>
              <Button onclick={refreshPushStatus} disabled={pushBusy} variant="secondary">Refresh</Button>
            </div>
          </Card>

          <Card class="space-y-3 p-5">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-muted">Operator note</p>
              <h2 class="mt-1 text-lg font-semibold text-strong">VAPID subject</h2>
            </div>
            <p class="text-sm leading-6 text-muted">For reliable delivery, set <code>COGNIS_VAPID_SUBJECT</code> to a real <code>mailto:</code> or <code>https:</code> contact URI. Development defaults such as <code>mailto:admin@localhost</code> may be rejected by Apple Web Push or FCM.</p>
          </Card>
        </div>
      </div>
      {/if}
    {:else if activeTab === 'display'}
      <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
        <Card class="space-y-5 p-5">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Display</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">Appearance</h2>
            <p class="mt-2 text-sm leading-6 text-muted">Per-user preferences for visual presentation in the web UI.</p>
          </div>

          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Family</span>
            <select
              class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
              value={$userPreferences.display.theme_family}
              disabled={busy}
              onchange={(event) => void updateDisplayPreference('theme_family', event.currentTarget.value as ThemeFamily)}
            >
              {#each THEME_FAMILIES as family}
                <option value={family.id}>{family.name}</option>
              {/each}
            </select>
            <span class="block text-xs text-muted">{THEME_FAMILIES.find((family) => family.id === $userPreferences.display.theme_family)?.description}</span>
          </label>

          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Appearance</span>
            <select
              class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
              value={$userPreferences.display.theme}
              disabled={busy}
              onchange={(event) => void updateDisplayPreference('theme', event.currentTarget.value as UserPreferences['display']['theme'])}
            >
              <option value="system">System</option>
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </select>
            <span class="block text-xs text-quiet">Choose light, dark, or follow your system appearance.</span>
          </label>

          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Language</span>
            <Input
              value={$userPreferences.display.language}
              placeholder="auto"
              disabled={busy}
              onchange={(event) => void updateDisplayPreference('language', event.currentTarget.value.trim() || 'auto')}
            />
             <span class="block text-xs text-quiet">Use <code>auto</code> or a language tag such as <code>en</code> or <code>cs-CZ</code>.</span>
           </label>

           <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
             <input
               type="checkbox"
               checked={$userPreferences.display.dashboard_workspace_windows}
               disabled={busy}
               class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
               onchange={(event) => void updateDisplayPreference('dashboard_workspace_windows', event.currentTarget.checked)}
             />
             <span>
               <span class="block font-medium text-foreground">Dashboard workspace windows</span>
               <span class="mt-1 block text-xs leading-5 text-quiet">
                 Open Dashboard chats and task cockpits in non-blocking workspace windows on desktop and tablet. Phone behavior is unchanged.
              </span>
            </span>
           </label>

            {#each [
              { key: 'pinned_agents_collapsed' as const, label: 'Collapse pinned agent avatars',
                description: 'Use a compact avatar stack. Turn off to keep a movable block visible, with 2, 4, or 6 rows and horizontally scrollable columns.' },
              { key: 'pinned_agents_order_by_activity' as const, label: 'Order pinned agents by activity',
                description: 'Bring recent activity forward within attention and running-status priority. Turn off to keep stable pin order in both stack and block.' },
            ] as preference}
              <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
                <input type="checkbox" checked={$userPreferences.display[preference.key]} disabled={busy}
                  class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                  onchange={(event) => void updateDisplayPreference(preference.key, event.currentTarget.checked)} />
                <span>
                  <span class="block font-medium text-foreground">{preference.label}</span>
                  <span class="mt-1 block text-xs leading-5 text-quiet">{preference.description}</span>
                </span>
              </label>
            {/each}

            <label class="flex items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
              Project badge in conversations
              <select
                class="rounded-lg border border-control-border bg-surface px-2 py-1"
                value={$userPreferences.display.conversation_project_badge ?? 'name'} disabled={busy}
                onchange={(event) => void updateDisplayPreference('conversation_project_badge', event.currentTarget.value as 'name' | 'icon' | 'monochrome' | 'hidden')}
              >
                <option value="name">Name badge</option><option value="icon">Avatar</option><option value="monochrome">Monochrome icon</option><option value="hidden">Hidden</option>
              </select>
            </label>
            <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.display.conversation_sidebar_sections}
               disabled={busy}
               class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
               onchange={(event) => void updateDisplayPreference('conversation_sidebar_sections', event.currentTarget.checked)}
             />
             <span>
               <span class="block font-medium text-foreground">Conversation sidebar sections</span>
               <span class="mt-1 block text-xs leading-5 text-quiet">
                 Separate ongoing, waiting, and historical conversations so active work and required input stay visible.
               </span>
             </span>
           </label>

           <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
             <input
               type="checkbox"
               checked={$userPreferences.display.conversation_sidebar_dense}
               disabled={busy}
               class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
               onchange={(event) => void updateDisplayPreference('conversation_sidebar_dense', event.currentTarget.checked)}
             />
             <span>
               <span class="block font-medium text-foreground">Dense conversation sidebar</span>
               <span class="mt-1 block text-xs leading-5 text-quiet">
                 Use compact single-line conversation rows. Long titles remain horizontally scrollable.
               </span>
             </span>
           </label>
         </Card>

        <Card class="space-y-5 p-5">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Chat timeline</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">Runtime detail visibility</h2>
            <p class="mt-2 text-sm leading-6 text-muted">Control how much of the agent execution trace is shown in normal chat.</p>
          </div>

           <div class="space-y-3">
             <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
               <input
                 type="checkbox"
                 checked={$userPreferences.chat.auto_recap}
                 disabled={busy}
                 class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                 onchange={(event) => void updateChatPreference('auto_recap', event.currentTarget.checked)}
               />
               <span>
                  <span class="block font-medium text-foreground">Automatically recap web chats</span>
                 <span class="mt-1 block text-xs leading-5 text-quiet">After meaningful work and a quiet period, add a short recap to the chat timeline. Never sends a notification.</span>
               </span>
             </label>
             <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
               <input
                 type="checkbox"
                 checked={$userPreferences.chat.enter_to_send}
                 disabled={busy}
                 class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                 onchange={(event) => void updateChatPreference('enter_to_send', event.currentTarget.checked)}
               />
               <span>
                 <span class="block font-medium text-foreground">Enter sends messages with a physical keyboard</span>
                 <span class="mt-1 block text-xs leading-5 text-quiet">When disabled, Enter inserts a newline. Press Command+Enter on Apple devices or Ctrl+Enter elsewhere to send. The on-screen keyboard always uses Enter for a newline.</span>
               </span>
             </label>

             <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.chat.show_thinking_blocks}
                disabled={busy}
                class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                onchange={(event) => void updateChatPreference('show_thinking_blocks', event.currentTarget.checked)}
              />
              <span>
                <span class="block font-medium text-foreground">Show thinking blocks</span>
                <span class="mt-1 block text-xs leading-5 text-quiet">When disabled, reasoning/thinking blocks are hidden from the normal timeline but remain available in raw logs.</span>
              </span>
            </label>

            <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.chat.keep_assistant_messages_separate}
                disabled={busy}
                class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                onchange={(event) => void updateChatPreference('keep_assistant_messages_separate', event.currentTarget.checked)}
              />
              <span>
                <span class="block font-medium text-foreground">Keep assistant messages separate</span>
                <span class="mt-1 block text-xs leading-5 text-quiet">Do not fold assistant text into tool activity groups. Tool and thinking activity stays grouped.</span>
              </span>
            </label>

            <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.chat.group_tool_calls}
                disabled={busy}
                class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                onchange={(event) => void updateChatPreference('group_tool_calls', event.currentTarget.checked)}
              />
              <span>
                <span class="block font-medium text-foreground">Group tool and thinking activity</span>
                <span class="mt-1 block text-xs leading-5 text-quiet">Collapse consecutive tool activity and multi-block thinking under compact semantic groups.</span>
              </span>
            </label>

            <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
              <input
                type="checkbox"
                checked={$userPreferences.chat.show_internal_tool_calls}
                disabled={busy}
                class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                onchange={(event) => void updateChatPreference('show_internal_tool_calls', event.currentTarget.checked)}
              />
              <span>
                <span class="block font-medium text-foreground">Show internal helper tool calls</span>
                <span class="mt-1 block text-xs leading-5 text-quiet">Includes low-level helper calls such as todo updates. Keep this off for a cleaner chat transcript.</span>
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
              <p class="text-xs uppercase tracking-[0.25em] text-muted">Web tools</p>
              <h2 class="mt-1 text-lg font-semibold text-strong">Search and fetch backends</h2>
            </div>
            <p class="text-sm leading-6 text-muted">
              Search and fetch are independent. Each agent's <code>web_search</code> uses the search backend. <code>web_fetch</code> should usually stay on direct extraction so browser fallback can rescue Cloudflare-blocked or JS-required pages automatically.
            </p>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Search backend</span>
              <select bind:value={webSearchBackendForm} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                <option value="direct">Direct (DDGS metasearch)</option>
                <option value="tavily" disabled={!webConfig.available_search_backends.includes('tavily')}>{WEB_BACKEND_INFO.tavily.label}{webConfig.available_search_backends.includes('tavily') ? '' : ' (unavailable)'}</option>
                <option value="brave" disabled={!webConfig.available_search_backends.includes('brave')}>{WEB_BACKEND_INFO.brave.label}{webConfig.available_search_backends.includes('brave') ? '' : ' (unavailable)'}</option>
                <option value="searxng" disabled={!webConfig.available_search_backends.includes('searxng')}>{WEB_BACKEND_INFO.searxng.label}{webConfig.available_search_backends.includes('searxng') ? '' : ' (unavailable)'}</option>
              </select>
            </label>
            <p class="text-sm leading-6 text-muted">{searchBackendDescription(webSearchBackendForm)}</p>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Fetch backend</span>
              <select bind:value={webFetchBackendForm} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                <option value="direct">Direct extraction (httpx + trafilatura)</option>
                <option value="tavily" disabled={!webConfig.available_fetch_backends.includes('tavily')}>{WEB_BACKEND_INFO.tavily.label}{webConfig.available_fetch_backends.includes('tavily') ? '' : ' (unavailable)'}</option>
                <option value="browser">{WEB_BACKEND_INFO.browser.label}</option>
              </select>
            </label>
            <p class="text-sm leading-6 text-muted">{fetchBackendDescription(webFetchBackendForm)}</p>
            <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/50 px-4 py-3 text-sm text-foreground">
              <input bind:checked={webFetchFallbackBrowserForm} type="checkbox" class="mt-1 rounded border-control-border bg-canvas text-accent focus:ring-accent" disabled={webFetchBackendForm === 'browser'} />
              <span class="space-y-1">
                <span class="block font-medium text-foreground">Browser fallback</span>
                <span class="block text-xs leading-5 text-muted">Retry direct fetch failures through the executor browser on Cloudflare/JS-required pages. This is recommended for normal direct fetch usage and ignored when fetch backend is set to always use the browser.</span>
              </span>
            </label>
            <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/50 px-4 py-3 text-sm text-foreground">
              <input bind:checked={webBrowserFetchHeadedFallbackForm} type="checkbox" class="mt-1 rounded border-control-border bg-canvas text-accent focus:ring-accent" />
              <span class="space-y-1">
                <span class="block font-medium text-foreground">Prefer headed browser fallback</span>
                <span class="block text-xs leading-5 text-muted">After direct extraction fails, use headed mode when the executor enables headed browser sessions. Headless mode is selected only on executors that do not support headed sessions.</span>
              </span>
            </label>
            <div class="grid gap-3 md:grid-cols-2">
              <label class="space-y-1 text-sm text-secondary">
                <span class="text-xs text-muted">Navigation timeout (seconds)</span>
                <Input bind:value={webBrowserFetchNavigationTimeoutForm} type="number" min="5" max="300" step="5" />
              </label>
              <label class="flex items-start gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 text-sm">
                <input type="checkbox" checked={$userPreferences.chat.adapt_tts ?? false} disabled={busy}
                  class="mt-1 rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                  onchange={(event) => void updateChatPreference('adapt_tts', event.currentTarget.checked)} />
                <span>
                  <span class="block font-medium text-foreground">Adapt assistant messages for listening</span>
                  <span class="mt-1 block text-xs leading-5 text-quiet">Off by default: read the original message as is. When enabled, the audio narrative model rewrites messages into spoken prose before playback, including conversation mode. Adds model cost and a short delay. The original message is unchanged.</span>
                </span>
              </label>
              <label class="space-y-1 text-sm text-secondary">
                <span class="text-xs text-muted">Initial wait state</span>
                <select bind:value={webBrowserFetchWaitUntilForm} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                  <option value="commit">commit</option>
                  <option value="domcontentloaded">domcontentloaded</option>
                  <option value="load">load</option>
                  <option value="networkidle">networkidle</option>
                </select>
              </label>
              <label class="space-y-1 text-sm text-secondary">
                <span class="text-xs text-muted">Network idle soft wait (seconds)</span>
                <Input bind:value={webBrowserFetchNetworkIdleForm} type="number" min="0" max="30" step="1" />
              </label>
              <label class="space-y-1 text-sm text-secondary">
                <span class="text-xs text-muted">Fetch session idle (seconds)</span>
                <Input bind:value={webBrowserFetchSessionIdleForm} type="number" min="10" max="3600" step="10" />
              </label>
              <label class="space-y-1 text-sm text-secondary">
                <span class="text-xs text-muted">Pool wait timeout (seconds)</span>
                <Input bind:value={webBrowserFetchWaitTimeoutForm} type="number" min="1" max="300" step="1" />
              </label>
            </div>
            <Button class="w-full justify-center" onclick={saveWebBackend} disabled={!isAdmin || busy}>Save</Button>
          </div>
        </Card>

        <!-- Right: backend status + key setup -->
        <Card class="p-5">
          <div class="space-y-4">
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Backend status</p>

            <!-- Direct -->
            <div class="flex items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
              <div>
                <p class="font-medium text-strong">Direct search + fetch path</p>
                <p class="text-xs text-muted">DDGS metasearch for direct search, httpx + trafilatura for direct fetch. Always available, free.</p>
              </div>
              <ProviderStatusBadge status="healthy" />
            </div>

            <div class="flex items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
              <div>
                <p class="font-medium text-strong">Browser fallback</p>
                <p class="text-xs text-muted">{webFetchFallbackBrowserForm ? 'Enabled for direct fetch failures on Cloudflare/JS-required pages.' : 'Disabled. Direct fetch errors are returned without browser retry.'}</p>
              </div>
              <ProviderStatusBadge status={webFetchFallbackBrowserForm ? 'healthy' : 'degraded'} />
            </div>

            <!-- Tavily -->
            <div class="rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <p class="font-medium text-strong">{WEB_BACKEND_INFO.tavily.label}</p>
                  <p class="text-xs text-muted">{webBackendStatusLabel('tavily', webConfig)}</p>
                </div>
                <div class="flex items-center gap-2">
                  <Button size="sm" variant="secondary" onclick={() => openWebBackendEditor('tavily')}>Edit</Button>
                  <ProviderStatusBadge status={webConfig.tavily_configured && webConfig.tavily_enabled ? 'healthy' : 'degraded'} />
                </div>
              </div>
            </div>

            <!-- Brave -->
            <div class="rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <p class="font-medium text-strong">{WEB_BACKEND_INFO.brave.label}</p>
                  <p class="text-xs text-muted">{webBackendStatusLabel('brave', webConfig)}</p>
                </div>
                <div class="flex items-center gap-2">
                  <Button size="sm" variant="secondary" onclick={() => openWebBackendEditor('brave')}>Edit</Button>
                  <ProviderStatusBadge status={webConfig.brave_configured && webConfig.brave_enabled ? 'healthy' : 'degraded'} />
                </div>
              </div>
            </div>

            <!-- SearXNG -->
            <div class="rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <p class="font-medium text-strong">{WEB_BACKEND_INFO.searxng.label}</p>
                  <p class="text-xs text-muted">{webBackendStatusLabel('searxng', webConfig)}</p>
                </div>
                <div class="flex items-center gap-2">
                  <Button size="sm" variant="secondary" onclick={() => openWebBackendEditor('searxng')}>Edit</Button>
                  <ProviderStatusBadge status={webConfig.searxng_configured && webConfig.searxng_enabled ? 'healthy' : 'degraded'} />
                </div>
              </div>
            </div>

            <p class="text-xs text-quiet">
              <code>web_crawl</code> and <code>web_map</code> work with any fetch backend. Tavily enables its native crawl/map engines, while <code>web_research</code> still requires Tavily.
            </p>
          </div>

        </Card>
      </div>
    {:else if activeTab === 'executors'}
      <div class="space-y-5">
        <div class="flex items-center justify-between">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Executors</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">Executor monitor</h2>
            <p class="mt-2 text-sm text-muted">
              Health, resources, and capabilities at a glance. Select a host to inspect or configure it.
            </p>
          </div>
          <Button variant="primary" size="sm" onclick={() => { executorForm = { executor_id: '', name: '', executor_type: 'websocket', labels: '', status: 'active', shared: false, is_default: false }; editingExecutor = null; executorToken = null; showExecutorForm = true; }}>New executor</Button>
        </div>

        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-2 text-xs text-muted" role="status">
            <span class={`h-1.5 w-1.5 rounded-full ${$wsState.status === 'connected' ? 'bg-success' : 'bg-warning'}`}></span>
            {$wsState.status === 'connected' ? 'Live updates' : 'Live connection interrupted'}
            <span class="ml-2 text-quiet">·</span>
            <span>{executorConfigs.length} {executorConfigs.length === 1 ? 'executor' : 'executors'}</span>
          </div>
          <input use:findTarget aria-label="Find executor" placeholder="Find executor…" bind:value={executorSearch}
            class="w-full rounded-xl border border-separator bg-canvas/60 px-3 py-2 text-sm text-foreground sm:w-60" />
        </div>
        <div class="grid gap-4 md:grid-cols-2 2xl:grid-cols-3" data-testid="executor-grid">
          {#each visibleExecutors as executor (executor.executor_id)}
            <ExecutorCard executor={observedExecutor(executor)} history={executorHistory[executor.executor_id] ?? []} onOpen={() => openExecutor(executor)} />
          {/each}
        </div>
        {#if executorSearch && visibleExecutors.length === 0}
          <p class="py-8 text-center text-sm text-muted">No executors match “{executorSearch}”.</p>
        {/if}

        {#snippet executorIdentityForm()}
          <Card class="p-5 space-y-4">
            <h3 class="text-lg font-medium text-strong">{editingExecutor ? 'Edit Executor' : 'New Executor'}</h3>
            <div class="grid gap-4 md:grid-cols-2">
              <label class="space-y-1 text-sm text-foreground">
                <span>Executor ID</span>
                <Input bind:value={executorForm.executor_id} placeholder="auto-generated if empty" disabled={!!editingExecutor} />
              </label>
              <label class="space-y-1 text-sm text-foreground">
                <span>Name</span>
                <Input bind:value={executorForm.name} placeholder="e.g. Local Developer" />
              </label>
               <label class="space-y-1 text-sm text-foreground">
                 <span>Type</span>
                 <select bind:value={executorForm.executor_type} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={!!editingExecutor}>
                   {#each executorTypeChoices(executorStatus, isAdmin) as executorType}
                     <option value={executorType}>{executorType}</option>
                   {/each}
                 </select>
                 {#if localExecutorTypesUnavailable(executorStatus)}
                   <span class="block text-xs text-warning">
                     Local types are unavailable. Install <code>cognis-executor</code> to enable in-process and subprocess executors.
                   </span>
                 {/if}
               </label>
              <label class="space-y-1 text-sm text-foreground">
                <span>Labels (key=value, comma-separated)</span>
                <Input bind:value={executorForm.labels} placeholder="tier=standard, gpu=false" />
              </label>
              {#if isAdmin}
                <label class="flex items-center gap-3 rounded-2xl border border-separator bg-surface/60 px-4 py-3 text-sm text-foreground md:col-span-2">
                  <input bind:checked={executorForm.shared} type="checkbox" class="rounded border-control-border bg-canvas text-accent focus:ring-accent" />
                  <span>Shared executor available to all users</span>
                </label>
              {/if}
              <label class="flex items-center gap-3 rounded-2xl border border-separator bg-surface/60 px-4 py-3 text-sm text-foreground md:col-span-2">
                <input bind:checked={executorForm.is_default} type="checkbox" class="rounded border-control-border bg-canvas text-accent focus:ring-accent" />
                <span>Use as default executor</span>
              </label>
              {#if editingExecutor}
                <label class="space-y-1 text-sm text-foreground">
                  <span>Status</span>
                  <select bind:value={executorForm.status} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                    <option value="active">active</option>
                    <option value="disabled">disabled</option>
                  </select>
                </label>
              {/if}
            </div>
            <div class="flex gap-2 justify-end">
              <Button variant="secondary" size="sm" onclick={() => editingExecutor ? openExecutor(executorConfigs.find((item) => item.executor_id === editingExecutor?.executor_id) ?? editingExecutor) : showExecutorForm = false}>{editingExecutor ? 'Reset changes' : 'Cancel'}</Button>
              <Button variant="primary" size="sm" disabled={!executorForm.name.trim() || executorIdentitySaving} onclick={async () => {
                executorIdentitySaving = true;
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
                  if (!editingExecutor) showExecutorForm = false;
                  executorIdentityBaseline = JSON.stringify(executorForm);
                  await refreshPageState();
                  addToast(editingExecutor ? 'Executor updated.' : 'Executor created.', 'success');
                } catch (e) {
                  error = asApiError(e).message;
                  addToast(error, 'error', 4000, 'Unable to save executor');
                } finally { executorIdentitySaving = false; }
              }}>{executorIdentitySaving ? 'Saving…' : editingExecutor ? 'Save changes' : 'Create'}</Button>
            </div>
          </Card>
        {/snippet}

        {#if showExecutorForm && !editingExecutor}
          <BlockingDialog label="New executor" panelClass="max-w-2xl" onClose={() => showExecutorForm = false} dismissible={!busy} backdropDismissible={JSON.stringify(executorForm) === newExecutorBaseline}>
            {@render executorIdentityForm()}
          </BlockingDialog>
        {/if}

        {#each executorConfigs.filter((executor) => executor.executor_id === selectedExecutorId) as source (source.executor_id)}
           {@const exec = observedExecutor(source)}
           {@const toolGroups = [...new Set(executorTools.map(t => t.category))].sort()}
           {@const canManage = canManageExecutor(exec)}
           {@const unavailable = executorUnavailable(exec)}
           {@const canConfigure = canManage && !unavailable}
            <BlockingDialog label={`Executor: ${exec.name}`} panelClass="max-w-5xl" bodyClass="space-y-5" onClose={closeExecutor} dismissible={!executorIdentitySaving && !isExecutorSaving(exec.executor_id)} backdropDismissible={!((showExecutorForm && JSON.stringify(executorForm) !== executorIdentityBaseline) || executorInferenceDirty)}>
            {#snippet header()}
              <div class="flex items-start justify-between gap-4">
                <div class="min-w-0">
                  <h2 class="truncate text-lg font-semibold text-strong">{exec.name}</h2>
                  <p class="mt-1 break-all font-mono text-xs text-quiet">{exec.executor_id}</p>
                </div>
                <Button size="sm" variant="secondary" onclick={closeExecutor}>Close</Button>
              </div>
              <nav aria-label="Executor details" class="mt-4 flex gap-1 overflow-x-auto">
                {#each executorDetailTabs as tab}
                  <button type="button" aria-current={executorDetailTab === tab.id ? 'page' : undefined}
                    class={`shrink-0 rounded-lg px-3 py-2 text-sm ${executorDetailTab === tab.id ? 'bg-raised text-strong' : 'text-muted hover:bg-surface hover:text-foreground'}`}
                    onclick={() => executorDetailTab = tab.id}>{tab.label}</button>
                {/each}
              </nav>
            {/snippet}
            {#if executorDetailTab === 'settings'}
              {#if canManage && !unavailable}{@render executorIdentityForm()}{/if}
            <div class="space-y-3 border-t border-separator pt-5">
              <p class="text-sm font-medium text-foreground">Connection & removal</p>
              <p class="text-xs text-quiet">Generating a token revokes the previous token and disconnects this executor.</p>
              <div class="flex flex-wrap justify-between gap-2">
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
                      selectedExecutorId = null;
                      editingExecutor = null;
                      showExecutorForm = false;
                      await refreshPageState();
                      addToast('Executor deleted.', 'success');
                    }
                  }}>Delete</Button>
                {/if}
              </div>
            </div>
            {/if}

            {#if executorDetailTab === 'overview'}
            {#if Object.keys(exec.labels || {}).length > 0}
              <div class="flex flex-wrap gap-1.5">
                {#each Object.entries(exec.labels || {}) as [k, v]}
                  <span class="px-2 py-0.5 bg-raised text-secondary text-xs font-mono rounded border border-control-border">{k}={v}</span>
                {/each}
              </div>
             {/if}

             <p class="text-xs text-quiet">{exec.executor_type} · {exec.shared ? 'Shared executor' : 'Private executor'} · Configuration v{exec.applied_config_version}{#if exec.is_default} · Default{/if}</p>
              {#if !unavailable}
                <ExecutorRuntimeDiagnostics executorId={exec.executor_id} initialExecutor={exec}
                  onUpdated={(updated) => { executorConfigs = executorConfigs.map((item) => item.executor_id === updated.executor_id ? updated : item); }} />
                <ExecutorHealthPanel executor={exec} />
               {#if executorRuntimeSummary(exec)}
                 <div class="text-xs {exec.runtime_state === 'degraded' ? 'text-accent' : 'text-quiet'}">{executorRuntimeSummary(exec)}</div>
               {/if}
               {#if exec.desired_config_version !== exec.applied_config_version}
                 <div class="text-xs text-accent">
                   config pending: desired v{exec.desired_config_version}, applied v{exec.applied_config_version}
                 </div>
               {/if}
              {/if}
            {/if}

            {#if executorDetailTab === 'tools'}
              <ExecutorCapabilitiesPanel executor={exec} />
            {/if}
            <div hidden={executorDetailTab !== 'inference'}>
              {#if !unavailable && exec.executor_type === 'websocket'}
                <LocalInferenceSettings executor={exec} editable={canManage}
                  saving={isExecutorSaving(exec.executor_id)}
                  onSave={(config) => saveExecutorLocalInference(exec.executor_id, config)}
                  onDirtyChange={(dirty) => executorInferenceDirty = dirty} />
              {:else}
                <p class="text-sm text-muted">Local inference settings are available for remote WebSocket executors.</p>
              {/if}
            </div>

            {#if executorDetailTab === 'settings' && canManage && executorToken && executorToken.executor_id === exec.executor_id}
              {@const execCommand = `cognis executor run --controller-url ${window.location.origin.replace('http', 'ws')}/api/executor/ws --token ${executorToken.token}`}
              <div class="rounded-2xl border border-success-border bg-success-subtle p-4 space-y-3">
                <p class="text-sm text-success">Copy this token now. It does not expire; generate a new token to revoke older tokens.</p>
                <textarea readonly class="min-h-[72px] w-full rounded-2xl border border-success-border bg-canvas/80 px-4 py-3 font-mono text-xs text-foreground">{executorToken.token}</textarea>
                <p class="text-xs text-muted mt-2">Run this command on the remote machine:</p>
                <pre class="w-full rounded-2xl border border-success-border bg-canvas/80 px-4 py-3 font-mono text-xs text-foreground whitespace-pre-wrap break-all">{execCommand}</pre>
                <div class="flex flex-wrap gap-2">
                  <Button size="sm" variant="secondary" onclick={() => copyToClipboard(executorToken?.token ?? '')}>Copy token</Button>
                  <Button size="sm" variant="secondary" onclick={() => copyToClipboard(execCommand)}>Copy command</Button>
                </div>
              </div>
            {/if}

              {#if canConfigure}
            {#if executorDetailTab === 'tools'}
            <!-- Quick presets -->
            <div class="flex flex-wrap gap-2">
              <span class="text-xs text-muted self-center">Presets:</span>
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
              <span class="text-xs uppercase tracking-wider text-muted">Tool groups</span>
              <div class="mt-2 flex flex-wrap gap-2">
                {#each toolGroups as group}
                  {@const enabled = (exec.enabled_tool_groups || []).includes(group) || (exec.enabled_tools || []).includes('*')}
                  {@const groupToolCount = executorTools.filter(t => t.category === group).length}
                  <button
                    class="px-3 py-1.5 rounded-lg text-sm border transition-colors {enabled ? 'bg-success-subtle border-success-border text-success' : 'bg-surface border-control-border text-muted hover:border-control-border'}"
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
                 Mobile: "Configure individual tools" button opens a dialog
                 with a search field + scrollable list so the 30-40 tiny
                 chips don't overwhelm small viewports.
                 Desktop: grid as before. -->
            <div>
              <div class="flex items-center justify-between gap-3">
                <span class="text-xs uppercase tracking-wider text-muted">Individual tools</span>
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
                  {@const guard = executorToolSelectionGuard(exec, tool.name, tool.category)}
                  {@const guarded = guard.blocked && !enabled}
                  <button
                    class="px-2.5 py-1.5 rounded text-xs text-left border transition-colors {guarded ? 'border-warning-border bg-warning-subtle text-warning' : enabled ? 'bg-success-subtle border-success-border text-success' : 'bg-surface border-control-border text-quiet hover:border-control-border'}"
                    aria-disabled={guarded}
                    title="{tool.description} ({tool.category}){enabledByGroup ? ' — enabled via group' : ''}{guarded ? ` — ${guard.reason}` : ''}"
                    onclick={async () => {
                      if (enabledByGroup) return;
                      if (guarded) return;
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
                    {#if guarded}
                      <span class="ml-1 text-[10px] text-warning" title={guard.reason ?? 'Not ready'}>not ready</span>
                    {/if}
                    {#if tool.non_bypassable}
                      <span class="text-accent ml-0.5" title="Non-bypassable">!</span>
                    {/if}
                  </button>
                {/each}
              </div>
            </div>

            <div class="text-xs text-quiet">
              {(exec.enabled_tools || []).includes('*')
                ? 'All tools enabled'
                : `${(exec.enabled_tools || []).length} individual + ${(exec.enabled_tool_groups || []).length} group(s) enabled`}
            </div>

            {/if}
            {#if executorDetailTab === 'runtime'}
            <p class="text-sm text-muted">Runtime changes apply immediately and may reinitialize tools.</p>
            <!-- LSP Diagnostics settings -->
            {@const lspConfig = exec.config || {}}
            {@const lspEnabled = lspConfig.lsp_enabled !== false}
             {@const lspAutoInstall = lspConfig.lsp_auto_install !== false}
             {@const lspPythonTypes = lspConfig.lsp_python_type_diagnostics === true}
            <details class="group">
              <summary class="cursor-pointer text-xs uppercase tracking-wider text-muted hover:text-secondary select-none">
                LSP Diagnostics
                <span class="ml-1 text-quiet">{lspEnabled ? '(enabled)' : '(disabled)'}</span>
              </summary>
              <div class="mt-3 space-y-3 pl-1">
                <div class="flex flex-wrap gap-4">
                  <label class="flex items-center gap-2 text-sm text-secondary">
                      <input type="checkbox" checked={lspEnabled}
                        class="rounded border-control-border bg-raised text-success focus:ring-success/30"
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
                  <label class="flex items-center gap-2 text-sm text-secondary">
                      <input type="checkbox" checked={lspAutoInstall} disabled={!lspEnabled}
                        class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
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
                   <label class="flex items-center gap-2 text-sm text-secondary" title="Include pyright type errors in automatic post-edit diagnostics for Python. Off by default: ruff only (fast, low noise).">
                       <input type="checkbox" checked={lspPythonTypes} disabled={!lspEnabled}
                         class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                       onchange={async (e) => {
                         const checked = e.currentTarget.checked;
                         const cfg = { ...(exec.config || {}), lsp_python_type_diagnostics: checked };
                         await api.executor.update(exec.executor_id, { config: cfg });
                         await refreshPageState();
                         addToast(`Python type diagnostics ${checked ? 'enabled' : 'disabled'}.`, 'success');
                       }}
                     />
                     Python type diagnostics (pyright)
                   </label>
                 </div>
                <div class="grid gap-3 md:grid-cols-3">
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Diagnostics timeout (ms)</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Idle timeout (seconds)</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Max concurrent servers</span>
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
              <summary class="cursor-pointer text-xs uppercase tracking-wider text-muted hover:text-secondary select-none">
                Office Documents
                <span class="ml-1 text-quiet">{officeEnabled ? (officeAvailable ? '(available)' : '(enabled)') : '(disabled)'}</span>
              </summary>
              <div class="mt-3 space-y-3 pl-1">
                <div class="flex flex-wrap gap-4">
                  <label class="flex items-center gap-2 text-sm text-secondary">
                    <input type="checkbox" checked={officeEnabled}
                      class="rounded border-control-border bg-raised text-success focus:ring-success/30"
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
                  <label class="flex items-center gap-2 text-sm text-secondary">
                    <input type="checkbox" checked={officeAutoInstall} disabled={!officeEnabled}
                      class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Certified version</span>
                    <Input value={officeVersion} disabled={!officeEnabled}
                      onchange={async (e) => {
                        const cfg = { ...(exec.config || {}), officecli: { ...officeConfig, version: e.currentTarget.value.trim() || 'v1.0.102' } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    <span class="block text-xs text-quiet">Only certified Cognis versions expose office tools.</span>
                  </label>
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Binary path override</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Cache directory</span>
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
                <div class="text-xs text-quiet">
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
              <summary class="cursor-pointer text-xs uppercase tracking-wider text-muted hover:text-secondary select-none">
                Browser Automation
                <span class="ml-1 text-quiet">{browserEnabled ? '(enabled)' : '(disabled)'}</span>
              </summary>
              <div class="mt-3 space-y-3 pl-1">
                <div class="flex flex-wrap gap-4">
                  <label class="flex items-center gap-2 text-sm text-secondary">
                    <input type="checkbox" checked={browserEnabled}
                      class="rounded border-control-border bg-raised text-success focus:ring-success/30"
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
                  <label class="flex items-center gap-2 text-sm text-secondary">
                    <input type="checkbox" checked={browserAutoInstall} disabled={!browserEnabled}
                      class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
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
                  <label class="flex items-center gap-2 text-sm text-secondary">
                    <input type="checkbox" checked={browserPersistentProfilesEnabled} disabled={!browserEnabled}
                      class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Runtime</span>
                    <select class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
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
                    <span class="block text-xs text-quiet">
                      Patchright reduces CDP detection but is not a Cloudflare/Turnstile silver bullet. Leave channel empty for bundled Chromium, or set chrome only after installing system Chrome yourself.
                    </span>
                  </label>
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Browser engine</span>
                    <select class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Channel</span>
                    <Input value={browserChannel} disabled={!browserEnabled}
                      placeholder={browserRuntime === 'patchright' ? 'chrome (recommended)' : 'leave empty for bundled'}
                      onchange={async (e) => {
                        const value = e.currentTarget.value.trim();
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, channel: value || undefined } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    <span class="block text-xs text-quiet">Use chrome, msedge, chrome-beta, etc. only when that system browser is already installed. Leave empty for bundled Chromium.</span>
                  </label>
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Max sessions</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Idle timeout (seconds)</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Default profile mode</span>
                    <select class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Profile base dir</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Locale</span>
                    <Input value={String(browserConfig.locale ?? 'en-US')}
                      onchange={async (e) => {
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, locale: e.currentTarget.value || 'en-US' } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                  </label>
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Timezone</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Viewport width</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Viewport height</span>
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
                <label class="flex items-center gap-2 text-sm text-secondary">
                  <input type="checkbox" checked={browserConfig.headed_allowed === true} disabled={!browserEnabled}
                    class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                    onchange={async (e) => {
                      const checked = e.currentTarget.checked;
                      const cfg = { ...(exec.config || {}), browser: { ...browserConfig, headed_allowed: checked } };
                      await api.executor.update(exec.executor_id, { config: cfg });
                      await refreshPageState();
                    }}
                  />
                  Allow headed mode on this executor
                </label>
                <label class="flex items-center gap-2 text-sm text-secondary">
                  <input type="checkbox" checked={browserXvfbAuto} disabled={!browserEnabled}
                    class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                    onchange={async (e) => {
                      const checked = e.currentTarget.checked;
                      const cfg = { ...(exec.config || {}), browser: { ...browserConfig, xvfb_auto: checked } };
                      await api.executor.update(exec.executor_id, { config: cfg });
                      await refreshPageState();
                    }}
                  />
                  Auto-start Xvfb for headed Linux browser launches without DISPLAY
                </label>
                <label class="flex items-center gap-2 text-sm text-secondary">
                  <input type="checkbox" checked={browserRealisticLaunch} disabled={!browserEnabled}
                    class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                    onchange={async (e) => {
                      const checked = e.currentTarget.checked;
                      const cfg = { ...(exec.config || {}), browser: { ...browserConfig, realistic_launch: checked } };
                      await api.executor.update(exec.executor_id, { config: cfg });
                      await refreshPageState();
                    }}
                  />
                  Use realistic launch defaults (persistent profile friendly desktop viewport and reduced automation signals)
                </label>
                <div class="rounded-xl border border-control-border/60 bg-surface/40 p-3 space-y-3">
                  <div class="text-xs uppercase tracking-wider text-muted">
                    Stealth defaults
                    <span class="ml-1 text-quiet">{browserStealthEnabled ? '(enabled)' : '(disabled)'}</span>
                  </div>
                  <label class="flex items-center gap-2 text-sm text-secondary">
                    <input type="checkbox" checked={browserStealthEnabled} disabled={!browserEnabled}
                      class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
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
                  <label class="flex items-center gap-2 text-sm text-secondary">
                    <input type="checkbox" checked={browserRealisticUserAgent} disabled={!browserEnabled || !browserStealthEnabled}
                      class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
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
                    <label class="space-y-1 text-sm text-secondary">
                      <span class="text-xs text-muted">Default timezone (when not overridden)</span>
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
                    <label class="space-y-1 text-sm text-secondary">
                      <span class="text-xs text-muted">Accept-Language header</span>
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
                  <label class="space-y-1 text-sm text-secondary">
                    <span class="text-xs text-muted">Disable specific evasions (comma-separated)</span>
                    <Input value={browserStealthEvasions} disabled={!browserEnabled || !browserStealthEnabled}
                      placeholder="navigator_languages, webgl_vendor, audio_context, battery_api, viewport_jitter"
                      onchange={async (e) => {
                        const items = e.currentTarget.value.split(',').map((entry) => entry.trim()).filter(Boolean);
                        const cfg = { ...(exec.config || {}), browser: { ...browserConfig, stealth_evasions: items.length ? items : undefined } };
                        await api.executor.update(exec.executor_id, { config: cfg });
                        await refreshPageState();
                      }}
                    />
                    <span class="block text-xs text-quiet">
                      Names from playwright_stealth.Stealth (e.g. <code>navigator_webdriver</code>, <code>webgl_vendor</code>) plus cognis fingerprint scripts (<code>audio_context</code>, <code>battery_api</code>, <code>viewport_jitter</code>). Use to skip an evasion that breaks a specific site.
                    </span>
                  </label>
                </div>
                <div class="rounded-xl border border-control-border/60 bg-surface/40 p-3 space-y-3">
                  <div class="text-xs uppercase tracking-wider text-muted">
                    Behaviour
                  </div>
                  <div class="grid gap-3 md:grid-cols-3">
                    <label class="space-y-1 text-sm text-secondary">
                      <span class="text-xs text-muted">Cookie consent</span>
                      <select class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
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
                      <span class="block text-xs text-quiet">Auto-clicks the chosen action on common cookie banners (OneTrust, Cookiebot, Quantcast, Sourcepoint, Didomi, ...).</span>
                    </label>
                    <label class="space-y-1 text-sm text-secondary">
                      <span class="text-xs text-muted">Banner detection delay (ms)</span>
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
                    <label class="space-y-1 text-sm text-secondary">
                      <span class="text-xs text-muted">Disable on these hosts (comma-separated)</span>
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
                    <label class="flex items-center gap-2 text-sm text-secondary">
                      <input type="checkbox" checked={browserHumanizeInput} disabled={!browserEnabled}
                        class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
                        onchange={async (e) => {
                          const checked = e.currentTarget.checked;
                          const cfg = { ...(exec.config || {}), browser: { ...browserConfig, humanize_input: checked } };
                          await api.executor.update(exec.executor_id, { config: cfg });
                          await refreshPageState();
                        }}
                      />
                      Humanize click and type (Bezier mouse paths + jittered key intervals)
                    </label>
                    <label class="space-y-1 text-sm text-secondary">
                      <span class="text-xs text-muted">Humanize intensity</span>
                      <select class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
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
                  <label class="flex items-center gap-2 text-sm text-secondary">
                    <input type="checkbox" checked={browserFingerprintHardening} disabled={!browserEnabled}
                      class="rounded border-control-border bg-raised text-success focus:ring-success/30 disabled:opacity-40"
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
              <summary class="cursor-pointer text-xs uppercase tracking-wider text-muted hover:text-secondary select-none">
                Signal Direct Mode
                <span class="ml-1 text-quiet">{signalDirectEnabled ? '(enabled)' : '(disabled)'}</span>
              </summary>
              <div class="mt-3 space-y-3 pl-1">
                <label class="flex items-center gap-2 text-sm text-secondary">
                  <input
                    type="checkbox"
                    checked={signalDirectEnabled}
                    class="rounded border-control-border bg-raised text-success focus:ring-success/30"
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

                <label class="space-y-1 text-sm text-secondary">
                  <span class="text-xs text-muted">signal-cli command or absolute path</span>
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
                  <p class="text-xs text-quiet">
                    Use this when <code>signal-cli</code> is not on the default PATH for the executor process.
                  </p>
                </label>
              </div>
            </details>

            {/if}
            {#if executorDetailTab === 'tools'}
            <!-- MCP Server Assignment -->
            {#if mcpServerConfigs.length > 0}
              {@const assignedIds = ((exec.config || {}).mcp_server_ids || []) as string[]}
              <details class="group">
                <summary class="cursor-pointer text-xs uppercase tracking-wider text-muted hover:text-secondary select-none">
                  MCP Servers
                  <span class="ml-1 text-quiet">({assignedIds.length} assigned)</span>
                </summary>
                <div class="mt-2 flex flex-wrap gap-2">
                  {#each mcpServerConfigs as srv}
                    {@const assigned = assignedIds.includes(srv.server_id)}
                    <button
                      class="px-3 py-1.5 rounded-lg text-sm border transition-colors {assigned ? 'bg-success-subtle border-success-border text-success' : 'bg-surface border-control-border text-muted hover:border-control-border'}"
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
            {/if}
            {:else if unavailable}
              <div class="space-y-2 rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">
                <p class="font-medium">Local executor configuration is unavailable</p>
                <p class="text-xs text-danger/80">
                  {exec.unavailable_reason || 'Install cognis-executor on the controller to use this executor.'}
                </p>
                <p class="text-xs text-muted">
                  Saved configuration is preserved. Enabled tools:
                  {exec.enabled_tools.length ? exec.enabled_tools.join(', ') : 'none'}.
                  Tool groups: {exec.enabled_tool_groups.length ? exec.enabled_tool_groups.join(', ') : 'none'}.
                </p>
              </div>
            {:else}
              <div class="rounded-2xl border border-separator bg-canvas/60 px-4 py-3 text-sm text-muted">
                This executor is available for use, but you do not have permission to change its configuration.
              </div>
            {/if}
          </BlockingDialog>
        {/each}

        {#if executorConfigs.length === 0}
          <Card class="p-5 text-center text-muted">
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
                <p class="text-xs uppercase tracking-[0.25em] text-muted">Production executor policy</p>
                <h2 class="text-lg font-semibold text-strong">Local executor modes</h2>
                <p class="text-sm text-muted">For multi-user production deployments, disable local executor modes and rely on websocket executors only.</p>
                <div class="grid gap-3 md:grid-cols-2">
                  <div class="rounded-2xl border border-separator bg-canvas/70 px-4 py-4">
                    <div class="flex items-center justify-between gap-3">
                      <p class="font-medium text-strong">Allow in-process executors</p>
                      <ProviderStatusBadge status={settingBool('executors.allow_in_process', true) ? 'healthy' : 'degraded'} />
                    </div>
                    <p class="mt-2 text-sm text-muted">Disable to prevent controller-local tool execution in production.</p>
                  </div>
                  <div class="rounded-2xl border border-separator bg-canvas/70 px-4 py-4">
                    <div class="flex items-center justify-between gap-3">
                      <p class="font-medium text-strong">Allow subprocess executors</p>
                      <ProviderStatusBadge status={settingBool('executors.allow_subprocess', true) ? 'healthy' : 'degraded'} />
                    </div>
                    <p class="mt-2 text-sm text-muted">Disable to require persistent websocket executors instead of local child processes.</p>
                  </div>
                </div>
              </div>
            </Card>
          {/if}

          <Card class="p-5">
            <div class="flex items-center justify-between gap-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-muted">Setup guide</p>
                <h2 class="mt-1 text-lg font-semibold text-strong">Readiness checklist</h2>
              </div>
              <Button variant="secondary" onclick={() => goto('/getting-started')}>Open setup guide</Button>
            </div>
            <div class="mt-4 grid gap-3 md:grid-cols-2">
              {#each deriveGettingStartedSteps(diagnostics) as step}
                <a class="rounded-2xl border border-separator bg-canvas/60 px-4 py-4" href={step.href}>
                  <div class="flex items-center justify-between gap-3">
                    <p class="font-medium text-strong">{step.label}</p>
                    <ProviderStatusBadge status={step.done ? 'healthy' : 'degraded'} />
                  </div>
                  <p class="mt-2 text-sm text-muted">{step.description}</p>
                </a>
              {/each}
            </div>
          </Card>

          <div class="grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
            <Card class="p-5">
              <div class="space-y-3">
                <p class="text-xs uppercase tracking-[0.25em] text-muted">Health</p>
                {#if health}
                  {#each Object.entries(health.providers) as [name, provider]}
                    <div class="flex items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
                      <span class="font-medium text-strong">{name}</span>
                      <ProviderStatusBadge status={provider.status} />
                    </div>
                  {/each}
                {/if}
              </div>
            </Card>

            <Card class="p-5">
              <div class="grid gap-4 md:grid-cols-2">
                <div class="rounded-2xl border border-separator bg-canvas/70 px-4 py-4">
                  <p class="text-xs uppercase tracking-[0.25em] text-muted">Database</p>
                  <p class="mt-2 text-sm text-foreground">{String(diagnostics.database.drivername ?? '')}</p>
                  <p class="mt-1 text-xs text-muted">Migration: {String(diagnostics.database.migration_version ?? 'unknown')}</p>
                </div>
                <div class="rounded-2xl border border-separator bg-canvas/70 px-4 py-4">
                  <p class="text-xs uppercase tracking-[0.25em] text-muted">JWT key fingerprint</p>
                  <p class="mt-2 text-sm text-foreground">{diagnostics.key_fingerprint ?? 'unavailable'}</p>
                </div>
              </div>

              <div class="mt-4 rounded-2xl border border-separator bg-canvas/70 p-4">
                <div class="flex items-center justify-between gap-3">
                  <p class="font-medium text-strong">Configuration summary</p>
                  <Button size="sm" variant="secondary" onclick={() => copyToClipboard(diagnosticsEnvBlock())}>Copy env block</Button>
                </div>
                <pre class="mt-3 whitespace-pre-wrap text-xs text-secondary">{diagnosticsEnvBlock()}</pre>
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
              <p class="text-xs uppercase tracking-[0.25em] text-muted">Step Profiles</p>
              <h2 class="mt-1 text-lg font-semibold text-strong">Preset management</h2>
              <p class="mt-2 text-sm text-muted">
                These presets are used by workflows and direct chat tool exposure. Edits here change the runtime defaults globally.
              </p>
            </div>

            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="text-sm text-muted">Seeded profiles can be overridden and reset. Custom profiles can be created for new workflow shapes.</p>
              <Button variant="secondary" size="sm" onclick={() => { creatingStepProfile = !creatingStepProfile; }}>New preset</Button>
            </div>

            {#if creatingStepProfile}
              <div class="rounded-2xl border border-separator bg-canvas/60 p-4 space-y-4">
                <div class="grid gap-4 md:grid-cols-3">
                  <label class="space-y-1 text-sm text-foreground">
                    <span>ID</span>
                    <Input bind:value={newStepProfileForm.profile_id} placeholder="custom:office-lite" />
                  </label>
                  <label class="space-y-1 text-sm text-foreground">
                    <span>Name</span>
                    <Input bind:value={newStepProfileForm.name} placeholder="Office Lite" />
                  </label>
                  <label class="space-y-1 text-sm text-foreground">
                    <span>Mode</span>
                    <select class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" bind:value={newStepProfileForm.mode}>
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
                  class="rounded-2xl border border-separator bg-canvas/60 p-4"
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
                          <p class="font-medium text-strong">{profile.name}</p>
                          <span class="rounded-full border border-control-border bg-surface px-2 py-0.5 text-xs text-secondary">{profile.mode}</span>
                          <span class="rounded-full border border-control-border bg-surface px-2 py-0.5 text-xs text-secondary">{profile.matrix.length} groups</span>
                          {#if profile.is_custom}
                            <span class="rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-xs text-accent">custom</span>
                          {:else if profile.has_override}
                            <span class="rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-xs text-accent">customized</span>
                          {/if}
                        </div>
                        <p class="mt-1 text-xs text-quiet">{profile.profile_id}</p>
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
                    <label class="space-y-1 text-sm text-foreground">
                      <span>Name</span>
                      <Input
                        value={profile.name}
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, name: event.currentTarget.value }))}
                      />
                    </label>
                    <label class="space-y-1 text-sm text-foreground">
                      <span>Mode</span>
                      <select
                        class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                        value={profile.mode}
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, mode: (event.currentTarget.value === 'hard' ? 'hard' : 'soft') }))}
                      >
                        <option value="soft">Soft</option>
                        <option value="hard">Hard</option>
                      </select>
                    </label>
                    <label class="flex items-center gap-3 rounded-xl border border-separator bg-canvas/60 px-3 py-2 text-sm text-foreground">
                      <input
                        type="checkbox"
                        checked={profile.allowToolSearch}
                        class="h-4 w-4 rounded border-control-border bg-canvas"
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, allowToolSearch: event.currentTarget.checked }))}
                      />
                      <span>Allow tool search</span>
                    </label>
                    </div>

                    <div class="rounded-2xl border border-separator bg-canvas/60 p-4">
                    <div class="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p class="text-sm font-medium text-foreground">Capability matrix</p>
                        <p class="mt-1 text-xs text-muted">Rows are tool groups. Columns define the capabilities this preset exposes or allows.</p>
                      </div>
                      {#if availableStepProfileCategories(profile).length > 0}
                        <select
                          class="rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
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
                      <table class="min-w-full border-separate border-spacing-y-2 text-sm text-foreground">
                        <thead>
                          <tr class="text-left text-xs uppercase tracking-[0.2em] text-quiet">
                            <th class="px-3 py-2">Group</th>
                            {#each STEP_PROFILE_CAPABILITIES as capability}
                              <th class="px-3 py-2">{capability}</th>
                            {/each}
                            <th class="px-3 py-2"></th>
                          </tr>
                        </thead>
                        <tbody>
                          {#each profile.matrix as row}
                            <tr class="rounded-xl border border-separator bg-canvas/70">
                              <td class="px-3 py-2 font-medium">{row.category}</td>
                              {#each STEP_PROFILE_CAPABILITIES as capability}
                                <td class="px-3 py-2">
                                  <input
                                    type="checkbox"
                                    checked={row.capabilities.includes(capability)}
                                    class="h-4 w-4 rounded border-control-border bg-canvas"
                                    onchange={() => toggleSettingsStepProfileCapability(profile.profile_id, row.category, capability)}
                                  />
                                </td>
                              {/each}
                              <td class="px-3 py-2 text-right">
                                <button type="button" class="text-xs text-muted hover:text-danger" onclick={() => removeSettingsStepProfileCategory(profile.profile_id, row.category)}>Remove</button>
                              </td>
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                    </div>
                    </div>

                    <div class="grid gap-4 md:grid-cols-2">
                    <label class="space-y-1 text-sm text-foreground">
                      <span>Explicit include</span>
                      <Input
                        value={profile.includeText}
                        placeholder="tool_name, mcp:server:tool"
                        onchange={(event) => updateStepProfileForm(profile.profile_id, (current) => ({ ...current, includeText: event.currentTarget.value }))}
                      />
                    </label>
                    <label class="space-y-1 text-sm text-foreground">
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
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Tools</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">MCP Servers</h2>
            <p class="mt-2 text-sm text-muted">
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
            <h3 class="text-lg font-medium text-strong">{editingMcpServer ? 'Edit MCP Server' : 'New MCP Server'}</h3>
            <div class="grid gap-4 md:grid-cols-2">
              <label class="space-y-1 text-sm text-foreground">
                <span>Name</span>
                <Input bind:value={mcpForm.name} placeholder="e.g. github-api" />
              </label>
              <label class="space-y-1 text-sm text-foreground">
                <span>Transport</span>
                <select bind:value={mcpForm.transport} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                  <option value="stdio">stdio</option>
                  <option value="sse">sse</option>
                  <option value="streamable_http">streamable_http</option>
                </select>
              </label>
              {#if mcpForm.transport === 'stdio'}
                <label class="space-y-1 text-sm text-foreground">
                  <span>Command</span>
                  <Input bind:value={mcpForm.command} placeholder="e.g. npx" />
                  {#if validateStdioCommand(mcpForm.command)}
                    <p class="text-xs text-accent">{validateStdioCommand(mcpForm.command)}</p>
                  {:else}
                    <p class="text-xs text-quiet">Use only the executable name or absolute path here. Put flags and package names into Arguments.</p>
                  {/if}
                </label>
              {:else}
                <label class="space-y-1 text-sm text-foreground">
                  <span>URL</span>
                  <Input bind:value={mcpForm.url} placeholder="e.g. http://localhost:3000/sse" />
                </label>
              {/if}
              <label class="space-y-1 text-sm text-foreground">
                <span>Timeout (seconds)</span>
                <Input bind:value={mcpForm.timeout_seconds} type="number" />
              </label>
              {#if isAdmin}
                <label class="flex items-center gap-3 rounded-2xl border border-separator bg-surface/60 px-4 py-3 text-sm text-foreground md:col-span-2">
                  <input bind:checked={mcpForm.shared} type="checkbox" class="rounded border-control-border bg-canvas text-accent focus:ring-accent" />
                  <span>Shared MCP server available to all users</span>
                </label>
              {/if}
            </div>
            {#if mcpForm.transport === 'stdio'}
              <label class="space-y-1 text-sm text-foreground">
                <span>Arguments (one per line)</span>
                <textarea bind:value={mcpForm.args} class="min-h-[60px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet font-mono" placeholder="-y&#10;@doist/todoist-ai"></textarea>
              </label>
            {/if}
            <div class="space-y-1 text-sm text-foreground">
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
              <div class="space-y-4 rounded-2xl border border-separator bg-canvas/40 p-4">
                <div class="grid gap-4 md:grid-cols-2">
                  <label class="space-y-1 text-sm text-foreground">
                    <span>Authentication</span>
                    <select bind:value={mcpForm.authType} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                      <option value="none">None</option>
                      <option value="static_headers">Static headers</option>
                      <option value="oauth2">OAuth 2.1 / MCP authorization</option>
                    </select>
                  </label>
                  <div class="text-xs text-quiet">
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
                    <label class="space-y-1 text-sm text-foreground">
                      <span>Resource</span>
                      <Input bind:value={mcpForm.oauthResource} placeholder="Defaults to MCP server URL" />
                    </label>
                    <label class="space-y-1 text-sm text-foreground">
                      <span>Scopes</span>
                      <Input bind:value={mcpForm.oauthScopes} placeholder="Optional space separated scopes" />
                    </label>
                  </div>
                  <details class="rounded-2xl border border-separator bg-surface/40 p-4">
                    <summary class="cursor-pointer text-sm font-semibold text-foreground">
                      Advanced OAuth settings
                    </summary>
                    <div class="mt-4 grid gap-4 md:grid-cols-2">
                      <label class="space-y-1 text-sm text-foreground">
                        <span>Issuer</span>
                        <Input bind:value={mcpForm.oauthIssuer} placeholder="Discovered automatically when empty" />
                      </label>
                      <label class="space-y-1 text-sm text-foreground">
                        <span>Authorization server</span>
                        <Input bind:value={mcpForm.oauthAuthorizationServer} placeholder="Optional explicit issuer URL" />
                      </label>
                      <label class="space-y-1 text-sm text-foreground">
                        <span>Client ID</span>
                        <Input bind:value={mcpForm.oauthClientId} placeholder="Optional static client ID" />
                      </label>
                      <label class="space-y-1 text-sm text-foreground">
                        <span>Client secret reference</span>
                        <Input bind:value={mcpForm.oauthClientSecretRef} placeholder="secret name or $secret:name" />
                      </label>
                      <label class="space-y-1 text-sm text-foreground">
                        <span>Redirect URI</span>
                        <Input bind:value={mcpForm.oauthRedirectUri} placeholder="Optional override" />
                      </label>
                      <label class="space-y-1 text-sm text-foreground">
                        <span>Callback mode</span>
                        <select
                          bind:value={mcpForm.oauthCallbackMode}
                          class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                        >
                          <option value="auto">Auto</option>
                          <option value="controller_public">Controller public callback</option>
                          <option value="executor_loopback">Executor loopback callback</option>
                        </select>
                        <span class="block text-xs text-quiet">
                          Executor loopback starts a temporary 127.0.0.1 callback listener on the selected executor.
                        </span>
                      </label>
                      <label class="space-y-1 text-sm text-foreground">
                        <span>OAuth executor</span>
                        <select bind:value={mcpForm.oauthExecutorId} disabled={mcpForm.oauthCallbackMode !== 'executor_loopback' && mcpForm.oauthCallbackMode !== 'auto'} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground disabled:opacity-60">
                          <option value="">{defaultOAuthExecutorLabel()}</option>
                          {#each executorConfigs.filter((executor) => executor.executor_type !== 'in_process') as executor}
                            <option value={executor.executor_id}>{executor.name || executor.executor_id}</option>
                          {/each}
                        </select>
                        <span class="block text-xs text-quiet">
                          Configure this when a provider only accepts loopback redirect URIs.
                        </span>
                      </label>
                      <label class="space-y-1 text-sm text-foreground">
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
            <label class="space-y-1 text-sm text-foreground">
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
                <h3 class="text-lg font-medium text-strong">{srv.name}</h3>
                <span class="px-2 py-0.5 bg-inset text-secondary text-xs font-mono rounded">{srv.transport}</span>
                <span class="px-2 py-0.5 rounded text-xs {srv.status === 'active' ? 'bg-success-subtle text-success' : 'bg-inset text-muted'}">{srv.status}</span>
                {#if srv.shared}
                  <span class="px-2 py-0.5 bg-cyan-subtle text-cyan-tone text-xs rounded">shared</span>
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
              <p class="text-xs text-muted font-mono">{srv.command} {(srv.args || []).join(' ')}</p>
            {:else if srv.url}
              <p class="text-xs text-muted font-mono">{srv.url}</p>
            {/if}
            {#if srv.description}
              <p class="text-sm text-muted">{srv.description}</p>
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
                  <div class="rounded-xl border border-separator bg-inset/70 p-3">
                          <p class="mb-1 text-[11px] uppercase tracking-[0.18em] text-muted">
                            Authorization URL{oauthStart.copied ? ' · copied to clipboard' : ''}
                          </p>
                          <p class="break-all font-mono text-xs text-foreground">{oauthStart.authorizationUrl}</p>
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
              <p class="text-sm text-accent">{srv.invalid_reason}</p>
            {/if}
            <div class="text-xs text-quiet font-mono">ID: {srv.server_id}</div>
            {#if !canManageMcp}
              <p class="text-xs text-quiet">This shared MCP server is available to assign to your executors, but only an administrator can edit it.</p>
            {/if}
          </Card>
        {/each}

        {#if mcpServerConfigs.length === 0 && !showMcpForm}
          <Card class="p-5 text-center text-muted">
            <p>No MCP servers configured. Create one to make MCP tools available to executors.</p>
          </Card>
        {/if}

        <!-- Native Tools Reference -->
        {#if executorTools.length > 0}
          {@const nativeGroups = [...new Set(executorTools.map(t => t.category))].sort()}
          <Card class="p-5 space-y-3">
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Native Tools Reference</p>
            <p class="text-sm text-muted">Built-in tools available on executors. Enable them per-executor in the Executors tab.</p>
            {#each nativeGroups as group}
              <div>
                <span class="text-xs font-medium text-secondary">{group}</span>
                <div class="mt-1 flex flex-wrap gap-1.5">
                  {#each executorTools.filter(t => t.category === group) as tool}
                    <span class="px-2 py-0.5 bg-raised text-secondary text-xs font-mono rounded border border-control-border" title={tool.description}>{tool.name}</span>
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
                <p class="text-xs uppercase tracking-[0.25em] text-muted">Administration</p>
                <h2 class="mt-1 text-lg font-semibold text-strong">User management</h2>
              </div>
              <div class="flex items-center gap-3">
                <label class="flex items-center gap-2 text-sm text-secondary">
                  <input type="checkbox" bind:checked={showDisabledUsers}
                    class="rounded border-control-border bg-raised text-success focus:ring-success/30"
                    onchange={() => void loadUsers()}
                  />
                  Show disabled
                </label>
                <Button onclick={openCreateUserModal}>Create user</Button>
              </div>
            </div>

            {#each userList as user}
              {@const isSelf = user.email === auth.getSnapshot().user?.email}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3 {!user.is_active ? 'opacity-60' : ''}">
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <p class="font-medium text-strong truncate">{user.email}</p>
                    {#if !user.is_active}
                      <span class="rounded-full border border-danger-border bg-danger-subtle px-2 py-0.5 text-xs text-danger">disabled</span>
                    {/if}
                    {#if isSelf}
                      <span class="rounded-full border border-success-border bg-success-subtle px-2 py-0.5 text-xs text-success">you</span>
                    {/if}
                  </div>
                  <p class="text-sm text-muted">{user.name ?? ''}</p>
                  <div class="mt-1 flex flex-wrap gap-3 text-xs text-quiet">
                    <span>Role: <span class="text-secondary">{user.role}</span></span>
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
              <p class="text-sm text-muted">No users found.</p>
            {/each}
          </div>
        </Card>
      </div>
    {:else}
      <div class="grid gap-5 lg:grid-cols-[minmax(0,1fr)_420px]">
        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-muted">Account</p>
              <h2 class="mt-1 text-lg font-semibold text-strong">{auth.getSnapshot().user?.name ?? auth.getSnapshot().user?.email}</h2>
              <p class="text-sm text-muted">{auth.getSnapshot().user?.email}</p>
            </div>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Display name</span>
              <Input bind:value={accountNameForm} oninput={() => (accountNameDirty = true)} placeholder="Your name" />
            </label>
            {#if accountNameDirty}
              <Button onclick={saveAccountName} disabled={busy}>Save name</Button>
            {/if}
            <div class="grid gap-4 md:grid-cols-2">
              <label class="space-y-2 text-sm font-medium text-foreground"><span>Current password</span><Input bind:value={passwordForm.current_password} type="password" /></label>
              <label class="space-y-2 text-sm font-medium text-foreground"><span>New password</span><Input bind:value={passwordForm.new_password} type="password" /></label>
              <label class="space-y-2 text-sm font-medium text-foreground md:col-span-2"><span>Confirm new password</span><Input bind:value={passwordForm.confirm_password} type="password" /></label>
            </div>
            <div class="flex flex-wrap gap-2">
              <Button onclick={changePassword} disabled={busy}>Change password</Button>
              <Button variant="secondary" onclick={() => openTargetUi('intaris')}>Open Intaris</Button>
              <Button variant="secondary" onclick={() => openTargetUi('mnemory')}>Open Mnemory</Button>
              <Button variant="danger" onclick={async () => { await auth.logout(); await goto('/login'); }}>Sign out</Button>
            </div>
            <MfaSettings />
            <VideoSettings />
          </div>
        </Card>

        <Card class="p-5">
          <div class="space-y-4">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-muted">API keys</p>
              <h2 class="mt-1 text-lg font-semibold text-strong">Programmatic access</h2>
            </div>
            <label class="space-y-2 text-sm font-medium text-foreground"><span>Name</span><Input bind:value={newApiKeyName} placeholder="CI key" /></label>
            <label class="space-y-2 text-sm font-medium text-foreground"><span>Expires in days (optional)</span><Input bind:value={newApiKeyExpiresInDays} type="number" min="1" /></label>
            <Button class="w-full justify-center" onclick={createApiKey} disabled={busy || !newApiKeyName}>Create API key</Button>

            {#if createdApiKey}
              <div class="rounded-2xl border border-success-border bg-success-subtle px-4 py-4 text-sm text-success">
                <p class="font-medium">Copy this key now</p>
                <pre class="mt-2 whitespace-pre-wrap break-all text-xs">{createdApiKey.api_key}</pre>
                <div class="mt-3 flex justify-end">
                  <Button size="sm" variant="secondary" onclick={() => copyToClipboard(createdApiKey?.api_key ?? '')}>Copy</Button>
                </div>
              </div>
            {/if}

            {#each apiKeys as apiKey}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/70 px-4 py-3">
                <div>
                  <p class="font-medium text-strong">{apiKey.name}</p>
                  <p class="text-xs text-muted">{apiKey.prefix}</p>
                  <p class="text-xs text-quiet">Last used: {apiKey.last_used_at ?? 'never'}</p>
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
   {#if exec && canManageExecutor(exec) && !executorUnavailable(exec)}
    {@const query = toolPickerQuery.trim().toLowerCase()}
    {@const filteredTools = query
      ? executorTools.filter((t) => t.name.toLowerCase().includes(query) || (t.description ?? '').toLowerCase().includes(query) || t.category.toLowerCase().includes(query))
      : executorTools}
    <BlockingDialog onClose={() => (toolPickerExecutorId = null)} label={`Configure tools for ${exec.name}`} panelClass="max-w-lg">
      {#snippet header()}
        <div class="space-y-3">
          <div class="flex items-center justify-between gap-3">
            <div>
            <p class="text-xs uppercase tracking-[0.25em] text-muted">Executor</p>
            <h3 class="mt-1 text-lg font-semibold text-strong">{exec.name}</h3>
            </div>
            <Button variant="secondary" size="sm" onclick={() => toolPickerExecutorId = null}>Done</Button>
          </div>
          <Input find type="search" bind:value={toolPickerQuery} placeholder="Search tools by name, description, or group…" aria-label="Filter tools" />
        </div>
      {/snippet}
      <div class="space-y-1.5">
        {#each filteredTools as tool}
          {@const enabledByGroup = (exec.enabled_tool_groups || []).includes(tool.category)}
          {@const enabledByName = (exec.enabled_tools || []).includes(tool.name) || (exec.enabled_tools || []).includes('*')}
          {@const enabled = enabledByGroup || enabledByName}
          {@const guard = executorToolSelectionGuard(exec, tool.name, tool.category)}
          {@const guarded = guard.blocked && !enabled}
          <button
            type="button"
            aria-disabled={guarded}
            class="w-full rounded-xl border px-3 py-3 text-left transition-colors {guarded ? 'border-warning-border bg-warning-subtle text-warning' : enabled ? 'bg-success-subtle border-success-border text-success' : 'bg-surface border-control-border text-secondary hover:border-control-border'}"
            title={guarded ? guard.reason ?? 'This tool is not ready on this executor.' : undefined}
            onclick={() => {
              if (enabledByGroup) return;
              if (guarded) return;
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
              <span class="font-mono text-sm text-foreground">{tool.name}</span>
              <span class="shrink-0 rounded-full border border-control-border px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted">{tool.category}</span>
            </div>
            {#if tool.description}
              <p class="mt-1.5 text-xs text-muted">{tool.description}</p>
            {/if}
            <div class="mt-1.5 flex flex-wrap gap-2 text-[11px]">
              {#if enabledByGroup}
                <span class="text-accent">via group</span>
              {:else if enabled}
                <span class="text-success">enabled</span>
              {:else}
                <span class="text-quiet">disabled</span>
              {/if}
              {#if guarded}
                <span class="text-warning" title={guard.reason ?? 'Not ready'}>not ready: {guard.reason}</span>
              {/if}
              {#if tool.non_bypassable}
                <span class="text-accent">non-bypassable</span>
              {/if}
            </div>
          </button>
        {/each}
        {#if filteredTools.length === 0}
          <p class="py-6 text-center text-sm text-quiet">No tools match "{toolPickerQuery}".</p>
        {/if}
      </div>
    </BlockingDialog>
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
  <BlockingDialog label="Create credential secret" onClose={() => (showSecretModal = false)} titleId="secret-modal-title" dismissible={!busy} backdropDismissible={JSON.stringify([secretModalName, secretModalValue]) === secretModalBaseline}>
    {#snippet header()}
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs uppercase tracking-[0.25em] text-muted">New credential</p>
          <h3 class="mt-1 text-lg font-semibold text-strong" id="secret-modal-title">{secretModalTarget === 'provider' ? 'Create API key secret' : 'Create environment secret'}</h3>
        </div>
        <Button aria-label="Close credential dialog" size="icon" variant="secondary" onclick={() => (showSecretModal = false)}>&times;</Button>
      </div>
    {/snippet}

    {#snippet children()}
      <div class="space-y-4">
        <label class="space-y-2 text-sm font-medium text-foreground">
          <span>Secret name</span>
          <Input bind:value={secretModalName} placeholder="openai_api_key" />
        </label>
        <label class="space-y-2 text-sm font-medium text-foreground">
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
  <BlockingDialog label="Create user" onClose={() => (showUserCreateModal = false)} titleId="user-create-title" dismissible={!busy} backdropDismissible={!userCreateForm.email && !userCreateForm.name && !userCreateForm.password && !userCreateForm.confirm_password && userCreateForm.role === 'user'}>
    {#snippet header()}
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs uppercase tracking-[0.25em] text-muted">Administration</p>
          <h3 class="mt-1 text-lg font-semibold text-strong" id="user-create-title">Create user</h3>
        </div>
        <Button aria-label="Close user creation dialog" size="icon" variant="secondary" onclick={() => (showUserCreateModal = false)}>&times;</Button>
      </div>
    {/snippet}

    {#snippet children()}
      <div class="space-y-4">
        <label class="space-y-2 text-sm font-medium text-foreground">
          <span>Email</span>
          <Input bind:value={userCreateForm.email} type="email" placeholder="user@example.com" />
        </label>
        <label class="space-y-2 text-sm font-medium text-foreground">
          <span>Name (optional)</span>
          <Input bind:value={userCreateForm.name} placeholder="Display name" />
        </label>
        <label class="space-y-2 text-sm font-medium text-foreground">
          <span>Role</span>
          <select bind:value={userCreateForm.role} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
            <option value="user">user</option>
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
            <option value="service">service</option>
          </select>
        </label>
        <label class="space-y-2 text-sm font-medium text-foreground">
          <span>Password</span>
          <Input bind:value={userCreateForm.password} type="password" />
        </label>
        <label class="space-y-2 text-sm font-medium text-foreground">
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
  <BlockingDialog label="Edit user" onClose={() => { showUserEditModal = false; editingUser = null; }} titleId="user-edit-title" dismissible={!busy} backdropDismissible={userEditForm.name === (editingUser.name ?? '') && userEditForm.role === editingUser.role && !userEditForm.password && !userEditForm.confirm_password}>
    {#snippet header()}
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs uppercase tracking-[0.25em] text-muted">Administration</p>
          <h3 class="mt-1 text-lg font-semibold text-strong" id="user-edit-title">Edit user</h3>
          <p class="mt-1 text-sm text-muted">{editingUser?.email}</p>
        </div>
        <Button aria-label="Close user editor" size="icon" variant="secondary" onclick={() => { showUserEditModal = false; editingUser = null; }}>&times;</Button>
      </div>
    {/snippet}

    {#snippet children()}
      <div class="space-y-4">
        <label class="space-y-2 text-sm font-medium text-foreground">
          <span>Name</span>
          <Input bind:value={userEditForm.name} placeholder="Display name" />
        </label>
        <label class="space-y-2 text-sm font-medium text-foreground">
          <span>Role</span>
          <select bind:value={userEditForm.role} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
            <option value="user">user</option>
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
            <option value="service">service</option>
          </select>
        </label>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>New password</span>
            <Input bind:value={userEditForm.password} type="password" autocomplete="new-password" placeholder="Leave blank to keep current" />
          </label>
          <label class="space-y-2 text-sm font-medium text-foreground">
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
    registry={registryModel(editingModel.model_id)}
    registryChecked={registryCurrent}
    onclose={() => (editingModel = null)}
    onsave={handleSaveModelEdit}
  />
{/if}
