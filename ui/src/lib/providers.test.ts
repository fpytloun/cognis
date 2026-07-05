import { describe, expect, it } from 'vitest';

import { collectModelOptions, createProviderForm, deriveProviderId, detectProviderPreset, formatTokenCount, presetHasBaseUrl, providerExecutorTargetError, providerFormToPayload, providerRequiresExecutorLocation } from '$lib/providers';
import { defaultModelEntry, type LLMProvider } from '$lib/types/api';

describe('provider presets', () => {
  const provider: LLMProvider = {
    provider_id: 'default',
    display_name: 'OpenAI',
    location: 'controller',
    backend: 'litellm',
    owner_email: null,
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
    expect(config.scope).toBe('system');
  });

  it('defaults new provider payloads to user-owned scope', () => {
    const form = createProviderForm();
    form.display_name = 'Personal OpenAI';
    form.default_model = 'gpt-4o-mini';
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(form.owner_scope).toBe('user');
    expect(config.scope).toBe('user');
  });

  it('preserves shared system provider scope for existing shared providers', () => {
    const form = createProviderForm({ ...provider, owner_email: 'system@cognis.local' });
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(form.owner_scope).toBe('system');
    expect(config.scope).toBe('system');
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

  it('allows Anthropic-compatible providers to use a custom base_url', () => {
    const anthropicProvider: LLMProvider = {
      provider_id: 'meridian-claude',
      display_name: 'Meridian Claude',
      location: 'controller',
      backend: 'litellm',
      config: {
        preset: 'anthropic',
        default_model: 'claude-opus-4-7',
        models: [{ model_id: 'claude-opus-4-7' }],
        base_url: 'http://localhost:4000',
        api_base: 'http://localhost:4000',
        auth_config: { mode: 'none' }
      },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [defaultModelEntry('claude-opus-4-7')],
      last_test: null
    };

    expect(presetHasBaseUrl('anthropic')).toBe(true);
    const form = createProviderForm(anthropicProvider);
    expect(form.preset).toBe('anthropic');
    expect(form.base_url).toBe('http://localhost:4000');

    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.base_url).toBe('http://localhost:4000');
    expect(config.api_base).toBe('http://localhost:4000');
  });

  it('handles chatgpt subscription preset with oauth auth', () => {
    const chatgptProvider: LLMProvider = {
      provider_id: 'chatgpt',
      display_name: 'ChatGPT Subscription',
      location: 'controller',
      backend: 'litellm',
      config: {
        preset: 'chatgpt',
        default_model: 'gpt-5.3-codex',
        models: [{ model_id: 'gpt-5.3-codex' }],
        codex_transport: 'direct',
        auth_config: { mode: 'oauth', provider: 'chatgpt' }
      },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [defaultModelEntry('gpt-5.3-codex')],
      last_test: null
    };
    expect(detectProviderPreset(chatgptProvider)).toBe('chatgpt');
    const form = createProviderForm(chatgptProvider);
    expect(form.auth_mode).toBe('oauth');
    expect(form.codex_transport).toBe('direct');
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.auth_config).toEqual({ mode: 'oauth', provider: 'chatgpt' });
    expect(config.codex_transport).toBe('direct');
  });

  it('defaults ChatGPT subscription providers to direct Codex transport', () => {
    const chatgptProvider: LLMProvider = {
      provider_id: 'chatgpt',
      display_name: 'ChatGPT Subscription',
      location: 'controller',
      backend: 'litellm',
      config: {
        preset: 'chatgpt',
        default_model: 'gpt-5.3-codex',
        models: [{ model_id: 'gpt-5.3-codex' }],
        auth_config: { mode: 'oauth', provider: 'chatgpt' }
      },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [defaultModelEntry('gpt-5.3-codex')],
      last_test: null
    };

    const form = createProviderForm(chatgptProvider);
    expect(form.codex_transport).toBe('direct');

    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.codex_transport).toBe('direct');
  });

  it('serializes Anthropic subscription auth as controller-managed oauth', () => {
    const anthropicProvider: LLMProvider = {
      provider_id: 'claude-subscription',
      display_name: 'Claude Subscription',
      location: 'controller',
      backend: 'litellm',
      config: {
        preset: 'anthropic',
        default_model: 'claude-sonnet-4-5',
        models: [{ model_id: 'claude-sonnet-4-5' }],
        auth_config: { mode: 'oauth', provider: 'anthropic_subscription' }
      },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [defaultModelEntry('claude-sonnet-4-5')],
      last_test: null
    };

    const form = createProviderForm(anthropicProvider);
    expect(form.preset).toBe('anthropic');
    expect(form.auth_mode).toBe('oauth');
    expect(form.location).toBe('controller');

    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.auth_config).toEqual({ mode: 'oauth', provider: 'anthropic_subscription' });
    expect(payload.location).toBe('controller');
  });

  it('omits Codex transport from non-ChatGPT provider payloads', () => {
    const form = createProviderForm(provider);
    form.codex_transport = 'direct';
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.codex_transport).toBeUndefined();
  });

  it('handles Anthropic-compatible Meridian as an executor-routed LiteLLM provider', () => {
    const meridianProvider: LLMProvider = {
      provider_id: 'meridian-claude',
      display_name: 'Meridian Claude',
      location: 'executor',
      backend: 'litellm',
      owner_email: 'user@example.com',
      config: {
        preset: 'anthropic',
        default_model: 'claude-opus-4-7',
        models: [{ model_id: 'claude-opus-4-7', supports_tools: true }],
        auth_config: { mode: 'none' },
        executor_id: 'maitrea',
        base_url: 'http://127.0.0.1:8090',
        api_base: 'http://127.0.0.1:8090'
      },
      is_default: false,
      status: 'active',
      created_at: null,
      updated_at: null,
      models: [defaultModelEntry('claude-opus-4-7')],
      last_test: null
    };

    expect(detectProviderPreset(meridianProvider)).toBe('anthropic');
    const form = createProviderForm(meridianProvider);
    expect(form.location).toBe('executor');
    expect(form.executor_id).toBe('maitrea');
    expect(form.backend).toBe('litellm');
    expect(form.auth_mode).toBe('none');
    expect(form.base_url).toBe('http://127.0.0.1:8090');

    const payload = providerFormToPayload(form);
    expect(payload.backend).toBe('litellm');
    const config = payload.config as Record<string, unknown>;
    expect(config.auth_config).toEqual({ mode: 'none' });
    expect(config.executor_id).toBe('maitrea');
    expect(config.base_url).toBe('http://127.0.0.1:8090');
    expect(config.api_base).toBe('http://127.0.0.1:8090');
  });

  it('supports explicit executor targets for executor-routed providers', () => {
    const form = createProviderForm(provider);
    form.location = 'executor';
    form.executor_id = 'maitrea';
    form.executor_selector = 'location=local';

    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.executor_id).toBe('maitrea');
    expect(config.executor_labels).toBeUndefined();
    form.executor_selector = '';
    expect(providerExecutorTargetError(form)).toBeNull();
  });

  it('validates executor-routed provider targets in form state', () => {
    const form = createProviderForm(provider);
    form.location = 'executor';
    expect(providerExecutorTargetError(form)).toContain('Choose an executor');

    form.executor_selector = 'location=local';
    expect(providerExecutorTargetError(form)).toBeNull();

    form.executor_id = 'maitrea';
    expect(providerExecutorTargetError(form)).toContain('either one executor');
    expect(providerRequiresExecutorLocation('anthropic')).toBe(false);
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
        label: 'gpt-4o-mini · OpenAI (preferred)',
        providerId: 'default',
        preferred: true
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
      context_window: 1050000,
      max_context_window: 1050000,
      max_input_tokens: 922000,
      max_output_tokens: 128000,
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
    expect(form.models[0].context_window).toBe(1050000);
    expect(form.models[0].max_context_window).toBe(1050000);
    expect(form.models[0].max_input_tokens).toBe(922000);
    expect(form.models[0].supports_vision).toBe(true);
    expect(form.models[0].supports_openai_namespace_tools).toBe(true);
    expect(form.models[0].supports_openai_allowed_tools).toBe(true);
    expect(form.models[0].input_cost_per_mtok).toBe(2.5);

    // Save and verify properties are preserved
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    const models = config.models as Array<Record<string, unknown>>;
    expect(models[0].context_window).toBe(1050000);
    expect(models[0].max_context_window).toBe(1050000);
    expect(models[0].max_input_tokens).toBe(922000);
    expect(models[0].max_output_tokens).toBe(128000);
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
