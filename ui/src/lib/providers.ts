import { slugify } from '$lib/agents';
import type { LLMProvider, ModelEntry } from '$lib/types/api';
import { defaultModelEntry } from '$lib/types/api';

export type ProviderPreset = 'openai' | 'openai_compatible' | 'anthropic' | 'ollama' | 'litellm_proxy' | 'chatgpt';
export type AuthMode = 'env' | 'secret' | 'oauth' | 'none';

export interface AdvancedSetting {
  key: string;
  value: string;
}

export interface ProviderFormState {
  provider_id: string;
  display_name: string;
  location: string;
  executor_selector: string;
  backend: string;
  status: string;
  preset: ProviderPreset;
  base_url: string;
  default_model: string;
  models: ModelEntry[];
  auth_mode: AuthMode;
  auth_env_var: string;
  auth_secret_name: string;
  auth_secret_value: string;
  use_responses_api: boolean;
  advanced_settings: AdvancedSetting[];
  discovered_models: ModelEntry[];
}

export interface ProviderModelOption {
  value: string;
  label: string;
  providerId: string;
}

/** Default env var names by preset. */
const PRESET_ENV_VARS: Record<string, string> = {
  openai: 'OPENAI_API_KEY',
  openai_compatible: 'OPENAI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
  litellm_proxy: 'LITELLM_PROXY_API_KEY'
};

/** Display names for presets. */
export const PRESET_LABELS: Record<ProviderPreset, string> = {
  openai: 'OpenAI',
  openai_compatible: 'OpenAI Compatible',
  anthropic: 'Anthropic',
  ollama: 'Ollama (local)',
  litellm_proxy: 'LiteLLM Proxy',
  chatgpt: 'ChatGPT Subscription (Codex)'
};

/** Config keys that are handled by structured form fields. */
const KNOWN_CONFIG_KEYS = new Set([
  'preset', 'default_model', 'models', 'auth_config', 'use_responses_api',
  'base_url', 'api_base', 'executor_labels'
]);

/** All known ModelEntry keys (including optional ones that defaultModelEntry omits). */
const MODEL_ENTRY_KEYS: Array<keyof ModelEntry> = [
  'model_id', 'display_name', 'context_window', 'max_output_tokens',
  'supports_tools', 'supports_streaming', 'supports_vision', 'supports_audio_input',
  'supports_pdf_input', 'supports_file_input', 'supports_reasoning', 'reasoning_efforts',
  'supports_prompt_caching', 'supports_tool_search', 'supports_defer_loading',
  'supports_openai_namespace_tools', 'supports_openai_allowed_tools',
  'supports_responses_api', 'supports_extended_thinking', 'supports_image_generation',
  'supported_openai_params', 'max_tools', 'input_cost_per_mtok', 'output_cost_per_mtok', 'tier'
];

/** Parse a raw model dict from the DB into a typed ModelEntry. */
function parseModelEntry(raw: Record<string, unknown>): ModelEntry {
  const base = defaultModelEntry(typeof raw.model_id === 'string' ? raw.model_id : '');
  for (const key of MODEL_ENTRY_KEYS) {
    if (key in raw && raw[key] !== undefined && raw[key] !== null) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (base as any)[key] = raw[key];
    }
  }
  return base;
}

/** Serialize a ModelEntry for the config.models array, omitting default values. */
function serializeModelEntry(entry: ModelEntry): Record<string, unknown> {
  const defaults = defaultModelEntry(entry.model_id);
  const result: Record<string, unknown> = { model_id: entry.model_id };
  if (entry.display_name) result.display_name = entry.display_name;
  for (const key of MODEL_ENTRY_KEYS) {
    if (key === 'model_id' || key === 'display_name') continue;
    const val = entry[key];
    const def = defaults[key];
    // Include if value differs from default, or if it's set and default is undefined
    if (val !== undefined && val !== null && JSON.stringify(val) !== JSON.stringify(def)) {
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
    if (['openai', 'openai_compatible', 'anthropic', 'ollama', 'litellm_proxy', 'chatgpt'].includes(raw)) {
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
    .map(parseModelEntry);

  const authInfo = readAuthConfig(config);
  const defaultEnvVar = PRESET_ENV_VARS[preset] ?? '';

  return {
    provider_id: provider?.provider_id ?? '',
    display_name: provider?.display_name ?? '',
    location: preset === 'chatgpt' ? 'controller' : (provider?.location ?? 'controller'),
    executor_selector:
      typeof config.executor_labels === 'object' && config.executor_labels !== null && !Array.isArray(config.executor_labels)
        ? Object.entries(config.executor_labels as Record<string, unknown>)
            .map(([key, value]) => `${key}=${String(value)}`)
            .join(', ')
        : '',
    backend: provider?.backend ?? 'litellm',
    status: provider?.status ?? 'active',
    preset,
    base_url:
      typeof config.base_url === 'string'
        ? config.base_url
        : typeof config.api_base === 'string'
          ? (config.api_base as string)
          : preset === 'ollama'
            ? 'http://localhost:11434'
            : preset === 'litellm_proxy'
              ? 'http://localhost:4000'
              : '',
    default_model: typeof config.default_model === 'string' ? config.default_model : '',
    models,
    auth_mode: preset === 'ollama' ? 'none' : preset === 'chatgpt' ? 'oauth' : authInfo.auth_mode,
    auth_env_var: authInfo.auth_env_var || defaultEnvVar,
    auth_secret_name: authInfo.auth_secret_name || `${preset}_api_key`,
    auth_secret_value: '',
    use_responses_api: config.use_responses_api !== false,
    advanced_settings: extractAdvancedSettings(config),
    discovered_models: []
  };
}

export function deriveProviderId(displayName: string): string {
  return slugify(displayName);
}

export function providerFormToPayload(form: ProviderFormState): Record<string, unknown> {
  const executorLabels = Object.fromEntries(
    form.executor_selector
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean)
      .map((entry) => {
        const [key, ...rest] = entry.split('=');
        return [key.trim(), rest.join('=').trim()];
      })
      .filter(([key, value]) => key && value)
  );

  // Serialize models with full properties (only non-default values)
  const serializedModels = form.models.map(serializeModelEntry);

  const authConfig: Record<string, unknown> =
    form.auth_mode === 'secret'
      ? { mode: 'secret', secret_name: form.auth_secret_name }
      : form.auth_mode === 'oauth'
        ? { mode: 'oauth', provider: form.preset }
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
      default_model: form.default_model,
      models: serializedModels,
      auth_config: authConfig,
      use_responses_api: form.use_responses_api,
      ...(form.location === 'executor' && Object.keys(executorLabels).length > 0
        ? { executor_labels: executorLabels }
        : {}),
      ...(form.base_url ? { base_url: form.base_url, api_base: form.base_url } : {}),
      ...advancedConfig
    }
  };
}

export function collectModelOptions(providers: LLMProvider[]): ProviderModelOption[] {
  const options: ProviderModelOption[] = [];
  const seen = new Set<string>();

  const pushOption = (provider: LLMProvider, value: string): void => {
    const normalized = value.trim();
    if (!normalized) {
      return;
    }
    const key = `${provider.provider_id}:${normalized}`;
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    options.push({
      value: normalized,
      label: `${normalized} · ${provider.display_name}`,
      providerId: provider.provider_id
    });
  };

  for (const provider of providers) {
    const defaultModel =
      typeof provider.config?.default_model === 'string' ? provider.config.default_model : '';
    pushOption(provider, defaultModel);

    for (const model of provider.models ?? []) {
      const value = typeof model.model_id === 'string' ? model.model_id : '';
      pushOption(provider, value);
    }
  }
  return options.sort((left, right) => left.label.localeCompare(right.label));
}

/** Whether the preset needs authentication credentials. */
export function presetNeedsAuth(preset: ProviderPreset): boolean {
  return preset !== 'ollama';
}

/** Whether the preset allows configuring base URL. */
export function presetHasBaseUrl(preset: ProviderPreset): boolean {
  return preset === 'openai_compatible' || preset === 'ollama' || preset === 'litellm_proxy';
}

/** Format a token count for display (e.g. 1048576 → "~1M", 128000 → "128k"). */
export function formatTokenCount(tokens: number): string {
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
