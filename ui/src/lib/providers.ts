import { slugify } from '$lib/agents';
import type { LLMProvider } from '$lib/types/api';

export type ProviderPreset = 'openai' | 'openai_compatible' | 'anthropic' | 'ollama' | 'litellm_proxy' | 'custom';
export type AuthMode = 'env' | 'secret' | 'none';

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
  additional_models: string;
  custom_json: string;
  auth_mode: AuthMode;
  auth_env_var: string;
  auth_secret_name: string;
  auth_secret_value: string;
  discovered_models: Array<{ model_id: string; name: string }>;
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
  custom: 'Custom (raw JSON)'
};

function normalizeModelRows(modelIds: string[]): Array<Record<string, unknown>> {
  return modelIds.map((model_id) => ({ model_id }));
}

export function detectProviderPreset(provider: LLMProvider | null): ProviderPreset {
  if (!provider) {
    return 'openai';
  }

  const config = provider.config ?? {};
  if (typeof config.preset === 'string') {
    const raw = config.preset as string;
    if (['openai', 'openai_compatible', 'anthropic', 'ollama', 'litellm_proxy', 'custom'].includes(raw)) {
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
  return 'custom';
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
  const mode = authConfig.mode === 'secret' ? 'secret' : authConfig.mode === 'none' ? 'none' : 'env';
  return {
    auth_mode: mode,
    auth_env_var: typeof authConfig.env_var === 'string' ? authConfig.env_var : '',
    auth_secret_name: typeof authConfig.secret_name === 'string' ? authConfig.secret_name : ''
  };
}

export function createProviderForm(provider: LLMProvider | null = null): ProviderFormState {
  const preset = detectProviderPreset(provider);
  const config = provider?.config ?? {};
  const models = Array.isArray(config.models)
    ? (config.models as Array<Record<string, unknown>>)
        .map((item) => (typeof item?.model_id === 'string' ? item.model_id : ''))
        .filter(Boolean)
    : [];

  const authInfo = readAuthConfig(config);
  const defaultEnvVar = PRESET_ENV_VARS[preset] ?? '';

  return {
    provider_id: provider?.provider_id ?? '',
    display_name: provider?.display_name ?? '',
    location: provider?.location ?? 'controller',
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
    additional_models: models.join('\n'),
    custom_json: JSON.stringify(config, null, 2),
    auth_mode: preset === 'ollama' ? 'none' : authInfo.auth_mode,
    auth_env_var: authInfo.auth_env_var || defaultEnvVar,
    auth_secret_name: authInfo.auth_secret_name || `${preset}_api_key`,
    auth_secret_value: '',
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

  if (form.preset === 'custom') {
    const config = JSON.parse(form.custom_json || '{}');
    if (form.location === 'executor' && Object.keys(executorLabels).length > 0) {
      config.executor_labels = executorLabels;
    }
    return {
      ...(form.provider_id.trim() ? { provider_id: form.provider_id } : {}),
      display_name: form.display_name,
      location: form.location,
      backend: form.backend,
      status: form.status,
      config
    };
  }

  const modelIds = [form.default_model, ...form.additional_models.split(/\n+/)]
    .map((value) => value.trim())
    .filter(Boolean);

  const authConfig: Record<string, unknown> =
    form.auth_mode === 'secret'
      ? { mode: 'secret', secret_name: form.auth_secret_name }
      : form.auth_mode === 'env'
        ? { mode: 'env', env_var: form.auth_env_var }
        : { mode: 'none' };

  return {
    ...(form.provider_id.trim() ? { provider_id: form.provider_id } : {}),
    display_name: form.display_name,
    location: form.location,
    backend: 'litellm',
    status: form.status,
      config: {
        preset: form.preset,
        default_model: form.default_model,
        models: normalizeModelRows([...new Set(modelIds)]),
        auth_config: authConfig,
        ...(form.location === 'executor' && Object.keys(executorLabels).length > 0
          ? { executor_labels: executorLabels }
          : {}),
        ...(form.base_url ? { base_url: form.base_url, api_base: form.base_url } : {})
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
  return preset !== 'ollama' && preset !== 'custom';
}

/** Whether the preset allows configuring base URL. */
export function presetHasBaseUrl(preset: ProviderPreset): boolean {
  return preset === 'openai_compatible' || preset === 'ollama' || preset === 'litellm_proxy';
}
