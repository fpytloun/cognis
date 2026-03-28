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
    status: 'active',
    created_at: null,
    updated_at: null,
    models: [{ model_id: 'gpt-4o-mini' }],
    last_test: null
  };

  it('detects preset and builds form state', () => {
    expect(detectProviderPreset(provider)).toBe('openai');
    expect(createProviderForm(provider).default_model).toBe('gpt-4o-mini');
  });

  it('maps structured form state back to provider payload', () => {
    const payload = providerFormToPayload({
      provider_id: 'default',
      display_name: 'OpenAI',
      location: 'controller',
      backend: 'litellm',
      status: 'active',
      preset: 'openai',
      base_url: '',
      default_model: 'gpt-4o-mini',
      additional_models: 'gpt-4o',
      custom_json: '{}'
    });
    expect(payload.config).toEqual({
      preset: 'openai',
      default_model: 'gpt-4o-mini',
      models: [{ model_id: 'gpt-4o-mini' }, { model_id: 'gpt-4o' }]
    });
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
