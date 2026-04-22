import { describe, expect, it } from 'vitest';

import { collectModelOptions, createProviderForm, deriveProviderId, detectProviderPreset, formatTokenCount, providerFormToPayload } from '$lib/providers';
import { defaultModelEntry, type LLMProvider } from '$lib/types/api';

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
    models: [defaultModelEntry('gpt-4o-mini')],
    last_test: null
  };

  it('detects preset and builds form state', () => {
    expect(detectProviderPreset(provider)).toBe('openai');
    const form = createProviderForm(provider);
    expect(form.default_model).toBe('gpt-4o-mini');
    expect(form.auth_mode).toBe('env');
    expect(form.auth_env_var).toBe('OPENAI_API_KEY');
    expect(form.models).toHaveLength(1);
    expect(form.models[0].model_id).toBe('gpt-4o-mini');
  });

  it('maps structured form state back to provider payload', () => {
    const form = createProviderForm(provider);
    // Add a second model
    form.models = [...form.models, defaultModelEntry('gpt-4o')];
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.preset).toBe('openai');
    expect(config.default_model).toBe('gpt-4o-mini');
    expect(config.auth_config).toEqual({ mode: 'env', env_var: 'OPENAI_API_KEY' });
    const models = config.models as Array<Record<string, unknown>>;
    expect(models).toHaveLength(2);
    expect(models[0].model_id).toBe('gpt-4o-mini');
    expect(models[1].model_id).toBe('gpt-4o');
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
      models: [defaultModelEntry('gpt-oss-120b')],
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

  it('migrates custom preset to openai_compatible', () => {
    const customProvider: LLMProvider = {
      provider_id: 'custom-prov',
      display_name: 'Custom',
      location: 'controller',
      backend: 'litellm',
      config: {
        preset: 'custom',
        default_model: 'my-model',
        models: [{ model_id: 'my-model' }],
        base_url: 'http://custom.example.com',
        some_extra_key: 'extra_value'
      },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [defaultModelEntry('my-model')],
      last_test: null
    };
    expect(detectProviderPreset(customProvider)).toBe('openai_compatible');
    const form = createProviderForm(customProvider);
    expect(form.preset).toBe('openai_compatible');
    expect(form.base_url).toBe('http://custom.example.com');
    expect(form.models).toHaveLength(1);
    expect(form.models[0].model_id).toBe('my-model');
    // Extra config keys should be in advanced_settings
    expect(form.advanced_settings.some((s) => s.key === 'some_extra_key')).toBe(true);
  });

  it('preserves model properties through save/load cycle', () => {
    const richModel = {
      ...defaultModelEntry('gpt-5.4'),
      context_window: 1048576,
      max_output_tokens: 65536,
      supports_vision: true,
      supports_reasoning: true,
      supports_tool_search: true,
      supports_openai_namespace_tools: true,
      supports_openai_allowed_tools: true,
      input_cost_per_mtok: 2.5,
      output_cost_per_mtok: 10.0
    };
    const richProvider: LLMProvider = {
      provider_id: 'rich',
      display_name: 'Rich',
      location: 'controller',
      backend: 'litellm',
      config: {
        preset: 'openai',
        default_model: 'gpt-5.4',
        models: [richModel]
      },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [richModel],
      last_test: null
    };
    const form = createProviderForm(richProvider);
    expect(form.models[0].context_window).toBe(1048576);
    expect(form.models[0].supports_vision).toBe(true);
    expect(form.models[0].supports_openai_namespace_tools).toBe(true);
    expect(form.models[0].supports_openai_allowed_tools).toBe(true);
    expect(form.models[0].input_cost_per_mtok).toBe(2.5);

    // Save and verify properties are preserved
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    const models = config.models as Array<Record<string, unknown>>;
    expect(models[0].context_window).toBe(1048576);
    expect(models[0].max_output_tokens).toBe(65536);
    expect(models[0].supports_vision).toBe(true);
    expect(models[0].supports_tool_search).toBe(true);
    expect(models[0].supports_openai_namespace_tools).toBe(true);
    expect(models[0].supports_openai_allowed_tools).toBe(true);
    expect(models[0].input_cost_per_mtok).toBe(2.5);
  });

  it('advanced settings are merged into config on save', () => {
    const form = createProviderForm();
    form.display_name = 'Test';
    form.default_model = 'gpt-4o-mini';
    form.advanced_settings = [
      { key: 'api_version', value: '2024-02-01' },
      { key: 'max_retries', value: '3' }
    ];
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.api_version).toBe('2024-02-01');
    expect(config.max_retries).toBe(3); // parsed as JSON number
  });
});

describe('formatTokenCount', () => {
  it('formats millions', () => {
    expect(formatTokenCount(1048576)).toBe('~1M');
    expect(formatTokenCount(2000000)).toBe('2M');
    expect(formatTokenCount(1000000)).toBe('1M');
  });

  it('formats thousands', () => {
    expect(formatTokenCount(128000)).toBe('128k');
    expect(formatTokenCount(16384)).toBe('16.4k');
  });

  it('formats small numbers', () => {
    expect(formatTokenCount(512)).toBe('512');
  });
});
