import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '$lib/api/client';
import { defaultModelEntry, type LLMProvider } from '$lib/types/api';
import AgentSpeechVoice from './AgentSpeechVoice.svelte';

vi.mock('$lib/api/client', () => ({
  api: { tts: { voices: vi.fn(), synthesize: vi.fn() } }
}));
vi.mock('$lib/stores/audio-player', () => ({
  audioPlayer: {
    isCurrent: vi.fn(() => false), prepare: vi.fn(), unlock: vi.fn(async () => {}),
    play: vi.fn(async () => {}), stop: vi.fn(), stopIfKey: vi.fn()
  }
}));
const provider: LLMProvider = {
  provider_id: 'speech-provider', display_name: 'Speech provider',
  location: 'controller', backend: 'litellm', status: 'active', is_default: false,
  config: {}, created_at: null, updated_at: null, last_test: null,
  models: [
    { ...defaultModelEntry('gpt-4o-mini-tts'), supports_speech_synthesis: true },
    { ...defaultModelEntry('tts-1'), model_mode: 'audio_speech' },
    defaultModelEntry('gpt-4o')
  ]
};
const catalogue = (model = 'gpt-4o-mini-tts') => ({
  provider_id: provider.provider_id, model, default_voice: 'alloy',
  voices: [{ voice_id: 'alloy', name: 'Alloy' }, { voice_id: 'marin', name: 'Marin' }]
});
beforeEach(() => {
  vi.mocked(api.tts.voices).mockResolvedValue(catalogue());
  vi.mocked(api.tts.synthesize).mockResolvedValue({
    audio_url: '/voice.wav', content_type: 'audio/wav',
    voice: 'marin', model: 'gpt-4o-mini-tts', cached: false, duration_seconds: null
  });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('agent TTS settings', () => {
  it('defaults to inheritance and picks a speech model when selecting a provider', async () => {
    const onbindingchange = vi.fn();
    render(AgentSpeechVoice, { providers: [provider], onchange: vi.fn(), onbindingchange });
    expect(screen.getByLabelText('TTS provider')).toHaveValue('');
    expect(screen.getByLabelText('TTS model')).toBeDisabled();
    await fireEvent.change(screen.getByLabelText('TTS provider'), {
      target: { value: provider.provider_id }
    });
    expect(onbindingchange).toHaveBeenCalledWith(provider.provider_id, 'gpt-4o-mini-tts');
  });

  it('filters models and scopes voice discovery and paid preview to the selected binding', async () => {
    render(AgentSpeechVoice, {
      providers: [provider], providerId: provider.provider_id,
      model: 'gpt-4o-mini-tts', voice: 'marin', onchange: vi.fn()
    });
    await screen.findByRole('option', { name: 'Marin' });
    expect(screen.queryByRole('option', { name: 'gpt-4o' })).toBeNull();
    expect(api.tts.voices).toHaveBeenCalledWith({
      provider_id: provider.provider_id, model: 'gpt-4o-mini-tts'
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Preview voice' }));
    await waitFor(() => expect(api.tts.synthesize).toHaveBeenCalledWith(expect.objectContaining({
      provider_id: provider.provider_id, model: 'gpt-4o-mini-tts', voice: 'marin'
    }), expect.objectContaining({ signal: expect.any(AbortSignal) })));
  });

  it('ignores a late voice catalogue after the model selection changes', async () => {
    let release!: (value: ReturnType<typeof catalogue>) => void;
    const old = new Promise<ReturnType<typeof catalogue>>(resolve => { release = resolve; });
    vi.mocked(api.tts.voices).mockImplementation(target => target?.model === 'tts-1'
      ? Promise.resolve({ ...catalogue('tts-1'), voices: [{ voice_id: 'alloy', name: 'Alloy' }] })
      : old);
    const view = render(AgentSpeechVoice, {
      providers: [provider], providerId: provider.provider_id,
      model: 'gpt-4o-mini-tts', onchange: vi.fn()
    });
    await waitFor(() => expect(api.tts.voices).toHaveBeenCalledTimes(1));
    await view.rerender({ model: 'tts-1' });
    await screen.findByRole('option', { name: 'Alloy' });
    release(catalogue());
    await old;
    await tick();
    expect(screen.queryByRole('option', { name: 'Marin' })).toBeNull();
    expect(screen.getByLabelText('TTS model')).toHaveValue('tts-1');
  });

  it('warns about a saved unsupported voice without offering it as a valid option', async () => {
    render(AgentSpeechVoice, {
      providers: [provider], providerId: provider.provider_id,
      model: 'tts-1', voice: 'unavailable-voice', onchange: vi.fn()
    });
    await screen.findByText(/saved voice is not available/);
    expect(screen.getByLabelText('Provider voice')).toHaveValue('');
    expect(screen.queryByRole('option', { name: 'unavailable-voice' })).toBeNull();
  });
});
