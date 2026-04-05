import { describe, expect, it } from 'vitest';

import { collectModelOptions, createProviderForm, deriveProviderId, detectProviderPreset, providerFormToPayload } from '$lib/providers';
import type { LLMProvider } from '$lib/types/api';

describe('provider presets', () => {
  const provider: LLMProvider = {
    provider_id: 'default',
    display_name: 'OpenAI',
    location: 'controller',
    backend: 'litellm',
    config: { preset: 'openai', default_model: 'gpt-4o-mini', models: [{ model_id: 'gpt-4o-mini' }] },
    is_default: false,
    status: 'active',
    created_at: null,
    updated_at: null,
    models: [{ model_id: 'gpt-4o-mini' }],
    last_test: null
  };

  it('detects preset and builds form state', () => {
    expect(detectProviderPreset(provider)).toBe('openai');
    const form = createProviderForm(provider);
    expect(form.default_model).toBe('gpt-4o-mini');
    expect(form.auth_mode).toBe('env');
    expect(form.auth_env_var).toBe('OPENAI_API_KEY');
  });

  it('maps structured form state back to provider payload', () => {
    const form = createProviderForm(provider);
    form.additional_models = 'gpt-4o';
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.preset).toBe('openai');
    expect(config.default_model).toBe('gpt-4o-mini');
    expect(config.auth_config).toEqual({ mode: 'env', env_var: 'OPENAI_API_KEY' });
    expect(config.models).toEqual([{ model_id: 'gpt-4o-mini' }, { model_id: 'gpt-4o' }]);
    expect(config.use_responses_api).toBe(true);
  });

  it('preserves disabled Responses transport in provider form state and payload', () => {
    const disabledResponsesProvider: LLMProvider = {
      ...provider,
      config: {
        ...provider.config,
        use_responses_api: false
      }
    };

    const form = createProviderForm(disabledResponsesProvider);
    expect(form.use_responses_api).toBe(false);

    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.use_responses_api).toBe(false);
  });

  it('maps secret auth mode to payload', () => {
    const form = createProviderForm(provider);
    form.auth_mode = 'secret';
    form.auth_secret_name = 'my_openai_key';
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.auth_config).toEqual({ mode: 'secret', secret_name: 'my_openai_key' });
  });

  it('handles litellm_proxy preset with base_url', () => {
    const proxyProvider: LLMProvider = {
      provider_id: 'my-proxy',
      display_name: 'LiteLLM Proxy',
      location: 'controller',
      backend: 'litellm',
      config: {
        preset: 'litellm_proxy',
        default_model: 'gpt-oss-120b',
        models: [{ model_id: 'gpt-oss-120b' }],
        base_url: 'http://localhost:4000',
        api_base: 'http://localhost:4000',
        auth_config: { mode: 'env', env_var: 'LITELLM_PROXY_API_KEY' }
      },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [{ model_id: 'gpt-oss-120b' }],
      last_test: null
    };
    expect(detectProviderPreset(proxyProvider)).toBe('litellm_proxy');
    const form = createProviderForm(proxyProvider);
    expect(form.preset).toBe('litellm_proxy');
    expect(form.base_url).toBe('http://localhost:4000');
    expect(form.auth_env_var).toBe('LITELLM_PROXY_API_KEY');
  });

  it('defaults base_url for new litellm_proxy preset', () => {
    const newProxy: LLMProvider = {
      provider_id: 'proxy-new',
      display_name: 'Proxy',
      location: 'controller',
      backend: 'litellm',
      config: { preset: 'litellm_proxy' },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [],
      last_test: null
    };
    const form = createProviderForm(newProxy);
    expect(form.base_url).toBe('http://localhost:4000');
  });

  it('collects model options with provider metadata', () => {
    expect(collectModelOptions([provider])).toEqual([
      {
        value: 'gpt-4o-mini',
        label: 'gpt-4o-mini · OpenAI',
        providerId: 'default'
      }
    ]);
  });

  it('derives provider ids from display names and omits empty provider ids from payload', () => {
    expect(deriveProviderId('My OpenAI Proxy')).toBe('my-openai-proxy');

    const form = createProviderForm();
    form.display_name = 'My OpenAI Proxy';
    form.default_model = 'gpt-4o-mini';
    const payload = providerFormToPayload(form);

    expect(payload).not.toHaveProperty('provider_id');
  });
});
