import { describe, expect, it } from 'vitest';

import { collectModelOptions, createProviderForm, detectProviderPreset, providerFormToPayload } from '$lib/providers';
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
  });

  it('maps secret auth mode to payload', () => {
    const form = createProviderForm(provider);
    form.auth_mode = 'secret';
    form.auth_secret_name = 'my_openai_key';
    const payload = providerFormToPayload(form);
    const config = payload.config as Record<string, unknown>;
    expect(config.auth_config).toEqual({ mode: 'secret', secret_name: 'my_openai_key' });
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
});
