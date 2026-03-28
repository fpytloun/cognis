import type { LLMProvider } from '$lib/types/api';

export type ProviderPreset = 'openai' | 'anthropic' | 'ollama' | 'custom';

export interface ProviderFormState {
  provider_id: string;
  display_name: string;
  location: string;
  backend: string;
  status: string;
  preset: ProviderPreset;
  base_url: string;
  default_model: string;
  additional_models: string;
  custom_json: string;
}

export interface ProviderModelOption {
  value: string;
  label: string;
  providerId: string;
}

function normalizeModelRows(modelIds: string[]): Array<Record<string, unknown>> {
  return modelIds.map((model_id) => ({ model_id }));
}

export function detectProviderPreset(provider: LLMProvider | null): ProviderPreset {
  if (!provider) {
    return 'openai';
  }

  const config = provider.config ?? {};
  if (typeof config.preset === 'string') {
    return config.preset as ProviderPreset;
  }

  const defaultModel = typeof config.default_model === 'string' ? config.default_model : '';
  if (defaultModel.startsWith('ollama/')) {
    return 'ollama';
  }
  if (defaultModel.startsWith('claude') || defaultModel.startsWith('anthropic/')) {
    return 'anthropic';
  }
  if (defaultModel.startsWith('gpt-') || defaultModel.startsWith('o1') || defaultModel.startsWith('o3')) {
    return 'openai';
  }
  return 'custom';
}

export function createProviderForm(provider: LLMProvider | null = null): ProviderFormState {
  const preset = detectProviderPreset(provider);
  const config = provider?.config ?? {};
  const models = Array.isArray(config.models)
    ? config.models
        .map((item) => (typeof item?.model_id === 'string' ? item.model_id : ''))
        .filter(Boolean)
    : [];

  return {
    provider_id: provider?.provider_id ?? '',
    display_name: provider?.display_name ?? '',
    location: provider?.location ?? 'controller',
    backend: provider?.backend ?? 'litellm',
    status: provider?.status ?? 'active',
    preset,
    base_url:
      typeof config.base_url === 'string'
        ? config.base_url
        : typeof config.api_base === 'string'
          ? config.api_base
          : preset === 'ollama'
            ? 'http://localhost:11434'
            : '',
    default_model: typeof config.default_model === 'string' ? config.default_model : '',
    additional_models: models.join('\n'),
    custom_json: JSON.stringify(config, null, 2)
  };
}

export function providerFormToPayload(form: ProviderFormState): Record<string, unknown> {
  if (form.preset === 'custom') {
    return {
      provider_id: form.provider_id,
      display_name: form.display_name,
      location: form.location,
      backend: form.backend,
      status: form.status,
      config: JSON.parse(form.custom_json || '{}')
    };
  }

  const modelIds = [form.default_model, ...form.additional_models.split(/\n+/)]
    .map((value) => value.trim())
    .filter(Boolean);

  return {
    provider_id: form.provider_id,
    display_name: form.display_name,
    location: form.location,
    backend: 'litellm',
    status: form.status,
    config: {
      preset: form.preset,
      default_model: form.default_model,
      models: normalizeModelRows([...new Set(modelIds)]),
      ...(form.base_url ? { base_url: form.base_url, api_base: form.base_url } : {})
    }
  };
}

export function collectModelOptions(providers: LLMProvider[]): ProviderModelOption[] {
  const options: ProviderModelOption[] = [];
  for (const provider of providers) {
    for (const model of provider.models ?? []) {
      const value = typeof model.model_id === 'string' ? model.model_id : '';
      if (!value) {
        continue;
      }
      options.push({
        value,
        label: `${value} · ${provider.display_name}`,
        providerId: provider.provider_id
      });
    }
  }
  return options.sort((left, right) => left.label.localeCompare(right.label));
}
