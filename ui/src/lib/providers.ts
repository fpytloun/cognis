import { slugify } from '$lib/agents';
import type { LLMProvider, ModelEntry, ProviderMonitoringOptions } from '$lib/types/api';
import { defaultModelEntry } from '$lib/types/api';

export type ProviderPreset = 'openai' | 'openai_compatible' | 'anthropic' | 'ollama' | 'litellm_proxy' | 'chatgpt' | 'groq' | 'openrouter' | 'gemini' | 'systemone' | 'bedrock' | 'elevenlabs';
export type AuthMode = 'env' | 'secret' | 'oauth' | 'none';
export type ProviderOwnerScope = 'user' | 'system';
export type CodexTransport = 'litellm' | 'direct';

export interface AdvancedSetting {
  key: string;
  value: string;
}

export interface ProviderFormState {
  provider_id: string;
  display_name: string;
  location: string;
  executor_id: string;
  executor_selector: string;
  backend: string;
  status: string;
  monitoring: ProviderMonitoringOptions;
  preset: ProviderPreset;
  owner_scope: ProviderOwnerScope;
  base_url: string;
  aws_region_name: string;
  aws_profile_name: string;
  default_model: string;
  models: ModelEntry[];
  auth_mode: AuthMode;
  auth_env_var: string;
  auth_secret_name: string;
  auth_secret_value: string;
  codex_transport: CodexTransport;
  use_responses_api: boolean;
  advanced_settings: AdvancedSetting[];
  discovered_models: ModelEntry[];
}

export interface ProviderModelOption {
  value: string;
  label: string;
  providerId: string;
  preferred: boolean;
}

/** Default env var names by preset. */
const PRESET_ENV_VARS: Record<string, string> = {
  elevenlabs: 'ELEVENLABS_API_KEY',
  openai: 'OPENAI_API_KEY',
  openai_compatible: 'OPENAI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
  litellm_proxy: 'LITELLM_PROXY_API_KEY',
  groq: 'GROQ_API_KEY',
  openrouter: 'OPENROUTER_API_KEY',
  gemini: 'GEMINI_API_KEY',
  systemone: 'TYPESAFE_API_KEY',
  bedrock: ''
};

export const PRESET_BASE_URLS: Partial<Record<ProviderPreset, string>> = {
  elevenlabs: 'https://api.elevenlabs.io',
  ollama: 'http://localhost:11434',
  litellm_proxy: 'http://localhost:4000',
  groq: 'https://api.groq.com/openai/v1',
  openrouter: 'https://openrouter.ai/api/v1',
  gemini: 'https://generativelanguage.googleapis.com',
  systemone: 'https://api.typesafe.ai/v1'
};

/** Display names for presets. */
export const PRESET_LABELS: Record<ProviderPreset, string> = {
  elevenlabs: 'ElevenLabs',
  openai: 'OpenAI',
  openai_compatible: 'OpenAI Compatible',
  anthropic: 'Anthropic',
  ollama: 'Ollama (local)',
  litellm_proxy: 'LiteLLM Proxy',
  chatgpt: 'ChatGPT Subscription (Codex)',
  groq: 'Groq',
  openrouter: 'OpenRouter',
  gemini: 'Google Gemini',
  systemone: 'System One (TypeSafe Jev)',
  bedrock: 'AWS Bedrock (IAM)'
};
export const PROVIDER_PRESETS = Object.keys(PRESET_LABELS) as ProviderPreset[];

/** Config keys that are handled by structured form fields. */
const KNOWN_CONFIG_KEYS = new Set([
  'preset', 'default_model', 'models', 'auth_config', 'codex_transport', 'use_responses_api',
  'base_url', 'api_base', 'executor_id', 'executor_labels', 'executor_backend', 'scope', 'owner_scope', 'monitoring', 'aws_region_name', 'aws_profile_name'
]);

const DEFAULT_CODEX_TRANSPORT: CodexTransport = 'direct';

/** All known ModelEntry keys (including optional ones that defaultModelEntry omits). */
export const MODEL_ENTRY_KEYS: Array<keyof ModelEntry> = [
  'model_id', 'display_name', 'context_window', 'max_input_tokens', 'max_context_window', 'max_output_tokens',
  'supports_tools', 'supports_streaming', 'supports_vision', 'supports_audio_input',
  'supports_transcription', 'supports_speech_synthesis',
  'supports_pdf_input', 'supports_file_input', 'supports_embedding', 'supports_reasoning', 'supports_fast_mode', 'fast_mode_tier', 'fast_mode_parameter', 'reasoning_efforts',
  'supports_prompt_caching', 'supports_tool_search', 'supports_defer_loading',
  'primary_cache_ttl', 'auxiliary_cache_ttl', 'supported_cache_ttls',
  'supports_openai_namespace_tools', 'supports_openai_allowed_tools',
  'supports_openai_apply_patch', 'supports_responses_api', 'supports_extended_thinking',
  'supports_image_generation', 'supported_image_mime_types', 'supported_audio_mime_types',
  'supports_video_generation', 'supports_video_input', 'video_capabilities',
  'supported_openai_params', 'max_tools', 'input_cost_per_mtok', 'output_cost_per_mtok', 'tier',
  'provider_metadata', 'runtime_metadata', 'cache_read_cost_per_mtok', 'cache_write_cost_per_mtok', 'model_mode', 'pricing',
  'supports_parallel_tools', 'supports_structured_output'
];

/** Parse a raw model dict from the DB into a typed ModelEntry. */
function parseModelEntry(raw: Record<string, unknown>, effective?: ModelEntry): ModelEntry {
  const base = defaultModelEntry(typeof raw.model_id === 'string' ? raw.model_id : '');
  const source = { ...effective, ...raw };
  for (const key of MODEL_ENTRY_KEYS) {
    if (key in source && source[key] !== undefined && source[key] !== null) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (base as any)[key] = source[key];
    }
  }
  return { ...base, _configuredFields: Object.keys(raw), _initialValues: { ...base } };
}

/** Initialize an editable model without turning unknown runtime defaults into overrides. */
export function modelEntryFromMetadata(raw: ModelEntry): ModelEntry {
  const sources = raw.provider_metadata?.field_sources;
  if (!sources || typeof sources !== 'object' || Array.isArray(sources)) {
    return parseModelEntry(raw as unknown as Record<string, unknown>);
  }
  const supplied = Object.fromEntries(Object.entries(raw).filter(([key]) =>
    key === 'model_id' || key === 'provider_metadata' || key === 'runtime_metadata' || Object.hasOwn(sources, key)
  ));
  return parseModelEntry(supplied, raw);
}

/** Serialize a ModelEntry for the config.models array, omitting default values. */
function serializeModelEntry(entry: ModelEntry): Record<string, unknown> {
  const defaults = defaultModelEntry(entry.model_id);
  const result: Record<string, unknown> = { model_id: entry.model_id };
  for (const key of MODEL_ENTRY_KEYS) {
    if (key === 'model_id' || key === 'supported_cache_ttls') continue;
    const val = entry[key];
    const def = defaults[key];
    // Include if value differs from default, or if it's set and default is undefined
    const changed = entry._initialValues
      ? JSON.stringify(val) !== JSON.stringify(entry._initialValues[key])
      : JSON.stringify(val) !== JSON.stringify(def);
    if (val !== undefined && (changed || entry._configuredFields?.includes(key))) {
      result[key] = val;
    }
  }
  return result;
}

export function detectProviderPreset(provider: LLMProvider | null): ProviderPreset {
  if (!provider) {
    return 'openai';
  }

  const config = provider.config ?? {};
  if (typeof config.preset === 'string') {
    const raw = config.preset as string;
    // Migrate deprecated 'custom' preset to 'openai_compatible'
    if (raw === 'custom') {
      return 'openai_compatible';
    }
    if (PROVIDER_PRESETS.includes(raw as ProviderPreset)) {
      return raw as ProviderPreset;
    }
  }

  const defaultModel = typeof config.default_model === 'string' ? config.default_model : '';
  if (defaultModel.startsWith('ollama/')) {
    return 'ollama';
  }
  if (defaultModel.startsWith('litellm_proxy/')) {
    return 'litellm_proxy';
  }
  if (defaultModel.startsWith('chatgpt/')) {
    return 'chatgpt';
  }
  if (defaultModel.startsWith('claude') || defaultModel.startsWith('anthropic/')) {
    return 'anthropic';
  }
  if (
    defaultModel.startsWith('gpt-') ||
    defaultModel.startsWith('o1') ||
    defaultModel.startsWith('o3')
  ) {
    return 'openai';
  }
  return 'openai_compatible';
}

function readAuthConfig(config: Record<string, unknown>): {
  auth_mode: AuthMode;
  auth_env_var: string;
  auth_secret_name: string;
} {
  const auth = config.auth_config;
  if (typeof auth !== 'object' || auth === null || Array.isArray(auth)) {
    return { auth_mode: 'env', auth_env_var: '', auth_secret_name: '' };
  }
  const authConfig = auth as Record<string, unknown>;
  const mode = authConfig.mode === 'secret' ? 'secret' : authConfig.mode === 'oauth' ? 'oauth' : authConfig.mode === 'none' ? 'none' : 'env';
  return {
    auth_mode: mode,
    auth_env_var: typeof authConfig.env_var === 'string' ? authConfig.env_var : '',
    auth_secret_name: typeof authConfig.secret_name === 'string' ? authConfig.secret_name : ''
  };
}

/** Extract unknown config keys into advanced settings KV pairs. */
function extractAdvancedSettings(config: Record<string, unknown>): AdvancedSetting[] {
  const settings: AdvancedSetting[] = [];
  for (const [key, value] of Object.entries(config)) {
    if (KNOWN_CONFIG_KEYS.has(key)) continue;
    if (value === undefined || value === null) continue;
    settings.push({
      key,
      value: typeof value === 'string' ? value : JSON.stringify(value)
    });
  }
  return settings;
}

export function createProviderForm(provider: LLMProvider | null = null): ProviderFormState {
  const preset = detectProviderPreset(provider);
  const config = provider?.config ?? {};

  // Parse models from config — preserve full model properties
  const rawModels = Array.isArray(config.models) ? (config.models as Array<Record<string, unknown>>) : [];
  const models: ModelEntry[] = rawModels
    .filter((item): item is Record<string, unknown> => typeof item?.model_id === 'string' && item.model_id !== '')
    .map((raw) => parseModelEntry(raw, provider?.models?.find((m) => m.model_id === raw.model_id)));

  const authInfo = readAuthConfig(config);
  const defaultEnvVar = PRESET_ENV_VARS[preset] ?? '';

  return {
    provider_id: provider?.provider_id ?? '',
    display_name: provider?.display_name ?? '',
    location: preset === 'chatgpt' || (preset === 'anthropic' && authInfo.auth_mode === 'oauth')
      ? 'controller'
      : (provider?.location ?? 'controller'),
    executor_id: typeof config.executor_id === 'string' ? config.executor_id : '',
    executor_selector:
      typeof config.executor_labels === 'object' && config.executor_labels !== null && !Array.isArray(config.executor_labels)
        ? Object.entries(config.executor_labels as Record<string, unknown>)
            .map(([key, value]) => `${key}=${String(value)}`)
            .join(', ')
        : '',
    backend: provider?.backend ?? 'litellm',
    status: provider?.status ?? 'active',
    monitoring: {
      enabled: true, interval_minutes: 15, warning_percent: 20, critical_percent: 0,
      ...(typeof config.monitoring === 'object' && config.monitoring !== null ? config.monitoring : {})
    },
    preset,
    owner_scope: !provider
      ? 'user'
      : provider.owner_email === 'system@cognis.local' || !provider.owner_email
        ? 'system'
        : 'user',
    base_url:
      typeof config.base_url === 'string'
        ? config.base_url
        : typeof config.api_base === 'string'
          ? (config.api_base as string)
           : PRESET_BASE_URLS[preset] ?? '',
    default_model: typeof config.default_model === 'string' ? config.default_model : '',
    aws_region_name: typeof config.aws_region_name === 'string' ? config.aws_region_name : '',
    aws_profile_name: typeof config.aws_profile_name === 'string' ? config.aws_profile_name : '',
    models,
    auth_mode: preset === 'ollama' || preset === 'bedrock' ? 'none' : preset === 'chatgpt' ? 'oauth' : authInfo.auth_mode,
    auth_env_var: authInfo.auth_env_var || defaultEnvVar,
    auth_secret_name: authInfo.auth_secret_name || `${preset}_api_key`,
    auth_secret_value: '',
    codex_transport:
      config.codex_transport === 'litellm' || config.codex_transport === 'direct'
        ? config.codex_transport
        : DEFAULT_CODEX_TRANSPORT,
    use_responses_api: config.use_responses_api !== false,
    advanced_settings: extractAdvancedSettings(config),
    discovered_models: []
  };
}

export function deriveProviderId(displayName: string): string {
  return slugify(displayName);
}

export function applyProviderPresetDefaults(form: ProviderFormState): void {
  form.base_url = PRESET_BASE_URLS[form.preset] ?? '';
  form.auth_env_var = PRESET_ENV_VARS[form.preset] ?? '';
  form.auth_secret_name = `${form.preset}_api_key`;
  form.auth_secret_value = '';
  if (form.preset === 'ollama' || form.preset === 'bedrock') form.auth_mode = 'none';
  else if (form.auth_mode === 'none') form.auth_mode = 'env';
}

export function isDecisionModel(model: Pick<ModelEntry, 'model_id' | 'model_mode'>): boolean {
  if (model.model_mode) return model.model_mode === 'systemone';
  // Compatibility for manual native IDs without metadata, not every jev-* product.
  return /^(?:~?typesafe\/)?jev-(?:latest|preview|\d+(?:\.\d+)+(?:-\d{8})?)$/.test(model.model_id);
}

export function isChatModel(model: Pick<ModelEntry, 'model_id' | 'model_mode'> & Partial<Pick<ModelEntry, 'supports_embedding'>>): boolean {
  return !isDecisionModel(model) && !model.supports_embedding &&
    (!model.model_mode || ['chat', 'completion', 'responses'].includes(model.model_mode));
}

export function providerRequiresExecutorLocation(_preset: ProviderPreset): boolean {
  return false;
}

export function providerExecutorTargetError(form: ProviderFormState): string | null {
  if (form.location !== 'executor') {
    return null;
  }
  const hasExecutorId = form.executor_id.trim().length > 0;
  const hasExecutorSelector = parseExecutorSelector(form.executor_selector) !== null;
  if (hasExecutorId && hasExecutorSelector) {
    return 'Choose either one executor or a label selector, not both.';
  }
  if (!hasExecutorId && !hasExecutorSelector) {
    return 'Choose an executor or enter at least one executor label selector.';
  }
  return null;
}

export function parseExecutorSelector(raw: string): Record<string, string> | null {
  const entries = raw
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
    .map((entry) => {
      const [key, ...rest] = entry.split('=');
      return [key.trim(), rest.join('=').trim()] as const;
    })
    .filter(([key, value]) => key && value);
  if (entries.length === 0) {
    return null;
  }
  return Object.fromEntries(entries);
}

export function providerFormToPayload(form: ProviderFormState): Record<string, unknown> {
  const executorLabels = parseExecutorSelector(form.executor_selector);
  const executorId = form.executor_id.trim();

  // Serialize models with full properties (only non-default values)
  const serializedModels = form.models.map(serializeModelEntry);

  const oauthProvider = form.preset === 'anthropic' ? 'anthropic_subscription' : form.preset;
  const authConfig: Record<string, unknown> =
    form.auth_mode === 'secret'
      ? { mode: 'secret', secret_name: form.auth_secret_name }
      : form.auth_mode === 'oauth'
        ? { mode: 'oauth', provider: oauthProvider }
      : form.auth_mode === 'env'
        ? { mode: 'env', env_var: form.auth_env_var }
        : { mode: 'none' };

  // Merge advanced settings into config
  const advancedConfig: Record<string, unknown> = {};
  for (const { key, value } of form.advanced_settings) {
    if (!key.trim()) continue;
    // Try to parse JSON values, fall back to string
    try {
      advancedConfig[key.trim()] = JSON.parse(value);
    } catch {
      advancedConfig[key.trim()] = value;
    }
  }

  return {
    ...(form.provider_id.trim() ? { provider_id: form.provider_id } : {}),
    display_name: form.display_name,
    location: form.location,
    backend: 'litellm',
    status: form.status,
    config: {
      preset: form.preset,
      monitoring: form.monitoring,
      scope: form.owner_scope,
      default_model: form.default_model,
      models: serializedModels,
      auth_config: authConfig,
      ...(form.preset === 'bedrock' && form.aws_region_name.trim() ? { aws_region_name: form.aws_region_name.trim() } : {}),
      ...(form.preset === 'bedrock' && form.aws_profile_name.trim() ? { aws_profile_name: form.aws_profile_name.trim() } : {}),
      ...(form.preset === 'chatgpt' ? { codex_transport: form.codex_transport } : {}),
      use_responses_api: form.use_responses_api,
      ...(form.location === 'executor' && executorId ? { executor_id: executorId } : {}),
      ...(form.location === 'executor' && !executorId && executorLabels
        ? { executor_labels: executorLabels }
        : {}),
      ...(form.base_url ? { base_url: form.base_url, api_base: form.base_url } : {}),
      ...advancedConfig
    }
  };
}

export function providerFormToUpdatePayload(form: ProviderFormState): Record<string, unknown> {
  const payload = providerFormToPayload(form);
  return {
    display_name: payload.display_name,
    location: payload.location,
    backend: payload.backend,
    owner_scope: form.owner_scope,
    config: payload.config,
    status: payload.status
  };
}

/** Match speech routing capabilities before using legacy name-only hints. */
export function isSpeechModelForRoute(
  route: 'speech_to_text' | 'text_to_speech', entry: ModelEntry | null | undefined, modelRef: string
): boolean {
  const transcription = route === 'speech_to_text';
  if (transcription ? entry?.supports_transcription : entry?.supports_speech_synthesis) return true;
  if (entry?.model_mode) {
    return entry.model_mode === (transcription ? 'audio_transcription' : 'audio_speech');
  }
  const hints = transcription
    ? ['transcribe', 'whisper', 'speech-to-text']
    : ['tts', 'text-to-speech', 'speech-1', 'eleven', 'elevenlabs', 'piper'];
  return [modelRef, entry?.display_name ?? ''].some(value => {
    const normalized = value.trim().toLowerCase().replaceAll('_', '-');
    return hints.some(hint => normalized.includes(hint));
  });
}

export function collectModelOptions(providers: LLMProvider[], includeNonChatModels = false): ProviderModelOption[] {
  const options = new Map<string, ProviderModelOption>();

  const currentDefaultProviderId =
    providers.find((provider) => provider.is_default)?.provider_id ??
    providers.find((provider) => provider.provider_id === 'default')?.provider_id ??
    null;

  const pushOption = (provider: LLMProvider, value: string): void => {
    const normalized = value.trim();
    if (!normalized) {
      return;
    }
    const preferred = provider.provider_id === currentDefaultProviderId;
    const existing = options.get(normalized);
    if (!existing) {
      options.set(normalized, {
        value: normalized,
        label: preferred
          ? `${normalized} · ${provider.display_name} (preferred)`
          : `${normalized} · ${provider.display_name}`,
        providerId: provider.provider_id,
        preferred
      });
      return;
    }

    if (preferred && !existing.preferred) {
      options.set(normalized, {
        value: normalized,
        label: `${normalized} · ${provider.display_name} (preferred)`,
        providerId: provider.provider_id,
        preferred: true
      });
    }
  };

  for (const provider of providers) {
    if (!includeNonChatModels && provider.config?.preset === 'systemone') continue;
    const defaultModel =
      typeof provider.config?.default_model === 'string' ? provider.config.default_model : '';
    const defaultEntry = provider.models?.find((model) => model.model_id === defaultModel);
    if (includeNonChatModels || isChatModel(defaultEntry ?? { model_id: defaultModel })) pushOption(provider, defaultModel);

    for (const model of provider.models ?? []) {
      if (!includeNonChatModels && !isChatModel(model)) continue;
      const value = typeof model.model_id === 'string' ? model.model_id : '';
      pushOption(provider, value);
    }
  }
  return Array.from(options.values()).sort((left, right) => left.label.localeCompare(right.label));
}

/** Whether the preset needs authentication credentials. */
export function presetNeedsAuth(preset: ProviderPreset): boolean {
  return preset !== 'ollama' && preset !== 'bedrock';
}

/** Whether the preset allows configuring base URL. */
export function presetHasBaseUrl(preset: ProviderPreset): boolean {
  return preset !== 'openai' && preset !== 'chatgpt' && preset !== 'bedrock';
}

/** Format a token count for display (e.g. 1048576 → "~1M", 128000 → "128k"). */
export function formatTokenCount(tokens: number | null | undefined): string {
  if (typeof tokens !== 'number' || !Number.isFinite(tokens) || tokens <= 0) return 'unknown';
  if (tokens >= 1_000_000) {
    const m = tokens / 1_000_000;
    return m === Math.floor(m) ? `${m}M` : `~${Math.round(m)}M`;
  }
  if (tokens >= 1_000) {
    const k = tokens / 1_000;
    return k === Math.floor(k) ? `${k}k` : `${k.toFixed(1)}k`;
  }
  return String(tokens);
}

/** Distinguish discovered/configured metadata from runtime safety defaults. */
export function hasModelMetadata(model: ModelEntry, field: keyof ModelEntry): boolean {
  const sources = model.provider_metadata?.field_sources;
  if (sources && typeof sources === 'object' && !Array.isArray(sources)) {
    return Object.hasOwn(sources, field) || Boolean(model._configuredFields?.includes(field));
  }
  return model[field] !== undefined && model[field] !== null;
}

export function formatModelMetadata(model: ModelEntry): string {
  const parts: string[] = [];
  if (model.model_mode) parts.push(model.model_mode.replaceAll('_', ' '));
  if (!model.model_mode || ['chat', 'responses', 'embedding'].includes(model.model_mode)) {
    parts.push(hasModelMetadata(model, 'context_window') ? `${formatTokenCount(model.context_window)} ctx` : 'Context unknown');
    if (hasModelMetadata(model, 'max_input_tokens') && model.max_input_tokens && model.max_input_tokens !== model.context_window) {
      parts.push(`${formatTokenCount(model.max_input_tokens)} in`);
    }
    parts.push(hasModelMetadata(model, 'max_output_tokens') ? `${formatTokenCount(model.max_output_tokens)} out` : 'Output limit unknown');
  }
  if (model.input_cost_per_mtok != null || model.output_cost_per_mtok != null) {
    parts.push(`$${model.input_cost_per_mtok ?? '?'}/$${model.output_cost_per_mtok ?? '?'} per Mtok`);
  } else if (typeof model.pricing?.input_cost_per_second === 'number') {
    parts.push(`$${model.pricing.input_cost_per_second}/s input`);
  } else {
    parts.push('Price unknown');
  }
  if (model.cache_read_cost_per_mtok != null) parts.push(`$${model.cache_read_cost_per_mtok}/Mtok cache read`);
  if (model.cache_write_cost_per_mtok != null) parts.push(`$${model.cache_write_cost_per_mtok}/Mtok cache write`);
  return parts.join(' \u00b7 ');
}
