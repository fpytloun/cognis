<script lang="ts">
  import { onDestroy } from 'svelte';
  import type { LLMProvider } from '$lib/types/api';
  import { api } from '$lib/api/client';
  import { audioPlayer } from '$lib/stores/audio-player';
  let { voices = {}, legacyVoice = '', providers = [], providerId = '', model = '', voice = '', disabled = false, onchange, onbindingchange, onvoicechange }: {
    voices?: Record<string, string>;
    legacyVoice?: string;
    providers?: LLMProvider[];
    providerId?: string;
    model?: string;
    voice?: string;
    disabled?: boolean;
    onchange: (providerId: string, voice: string) => void;
    onbindingchange?: (providerId: string, model: string) => void;
    onvoicechange?: (voice: string) => void;
  } = $props();
  let catalog = $state<Awaited<ReturnType<typeof api.tts.voices>> | null>(null);
  let loading = $state(false);
  let previewing = $state(false);
  let error = $state('');
  let notice = $state('');
  let controller: AbortController | null = null;
  let catalogGeneration = 0;
  const key = `voice-preview:${crypto.randomUUID()}`;
  let configured = $derived(voice || (providerId || model ? '' : catalog ? voices[catalog.provider_id] ?? legacyVoice : ''));
  let supported = $derived(catalog?.voices.some((voice) => voice.voice_id === configured) ?? false);
  let selected = $derived(supported ? configured : '');
  function speechModels(id: string) {
    return providers.find((provider) => provider.provider_id === id)?.models.filter((entry) =>
      entry.supports_speech_synthesis === true || entry.model_mode === 'audio_speech') ?? [];
  }
  function selectBinding(id: string, selectedModel: string) {
    controller?.abort();
    audioPlayer.stopIfKey(key);
    notice = '';
    onbindingchange?.(id, selectedModel);
  }
  async function load(selectedProvider = providerId, selectedModel = model) {
    const generation = ++catalogGeneration;
    catalog = null;
    loading = false;
    error = '';
    if (selectedProvider && !selectedModel) {
      error = 'Choose a speech model for this provider.';
      return;
    }
    loading = true;
    try {
      const result = await api.tts.voices({
        provider_id: selectedProvider || undefined, model: selectedModel || undefined
      });
      if (generation === catalogGeneration) catalog = result;
    }
    catch (caught) {
      if (generation === catalogGeneration) error = caught instanceof Error ? caught.message : 'Could not discover voices.';
    }
    finally { if (generation === catalogGeneration) loading = false; }
  }
  async function preview() {
    if (!catalog) return;
    if (audioPlayer.isCurrent(key)) { audioPlayer.stop(); return; }
    controller = new AbortController();
    const signal = controller.signal;
    audioPlayer.prepare(key, () => controller?.abort());
    previewing = true;
    error = '';
    try {
      await audioPlayer.unlock(signal);
      if (signal.aborted) return;
      const result = await api.tts.synthesize({
        text: 'This is a preview of the voice used to read assistant messages and documents.',
        voice: selected || catalog.default_voice,
        provider_id: providerId || undefined,
        model: model || undefined
      }, { signal });
      notice = (result.warnings ?? []).join(' ');
      if (!signal.aborted) await audioPlayer.play(key, result.audio_url);
    } catch (caught) {
      audioPlayer.stopIfKey(key);
      if (!signal.aborted) error = caught instanceof Error ? caught.message : 'Voice preview failed.';
    } finally { previewing = false; }
  }
  onDestroy(() => { catalogGeneration++; controller?.abort(); audioPlayer.stopIfKey(key); });
  $effect(() => { void load(providerId, model); });
</script>

<div class="space-y-2">
  <label class="block text-sm">TTS provider
    <select aria-label="TTS provider" value={providerId} disabled={disabled}
      class="mt-1 w-full rounded-lg border border-control-border bg-canvas p-2"
      onchange={(event) => {
        const id = event.currentTarget.value;
        selectBinding(id, speechModels(id)[0]?.model_id ?? '');
      }}>
      <option value="">Inherit system TTS settings</option>
      {#each providers.filter((provider) => speechModels(provider.provider_id).length || provider.provider_id === providerId) as provider}
        <option value={provider.provider_id}>{provider.display_name}</option>
      {/each}
    </select>
  </label>
  <label class="block text-sm">TTS model
    <select aria-label="TTS model" value={model} disabled={disabled || !providerId}
      class="mt-1 w-full rounded-lg border border-control-border bg-canvas p-2"
      onchange={(event) => selectBinding(providerId, event.currentTarget.value)}>
      <option value="">{providerId ? 'Select a speech model' : 'Inherit system TTS model'}</option>
      {#each speechModels(providerId) as entry}<option value={entry.model_id}>{entry.display_name || entry.model_id}</option>{/each}
    </select>
  </label>
  {#if providerId && !speechModels(providerId).length}
    <p role="status" class="text-xs text-muted">Discover or enrich the provider's speech models before selecting one.</p>
  {/if}
  <button type="button" disabled={loading || previewing} onclick={() => void load()} class="min-h-11 text-sm text-accent disabled:opacity-50">{loading ? 'Loading speech voices…' : 'Refresh speech voices'}</button>
  {#if catalog}
    <p class="text-xs text-muted">Voice for {catalog.provider_id} · {catalog.model}.</p>
    <label class="block text-sm">Provider voice
      <select class="mt-1 w-full rounded-lg border border-control-border bg-canvas p-2" aria-label="Provider voice"
        value={selected} disabled={disabled || loading || previewing}
        onchange={(event) => {
          if (catalog) onchange(catalog.provider_id, event.currentTarget.value);
          onvoicechange?.(event.currentTarget.value);
        }}>
        <option value="">Provider default ({catalog.default_voice})</option>
        {#each catalog.voices as voice}<option value={voice.voice_id}>{voice.name}</option>{/each}
      </select>
    </label>
    {#if configured && !supported && catalog.voices.length}
      <p role="status" class="text-xs text-muted">The saved voice is not available for this provider/model. The provider default will be used with a warning.</p>
    {:else if !catalog.voices.length}
      <p role="status" class="text-xs text-muted">This provider does not publish a voice catalogue. Only its default is selectable here.</p>
    {/if}
    <button type="button" disabled={disabled || loading} onclick={() => void preview()} class="min-h-11 text-sm text-accent">
      {previewing ? 'Cancel preview' : 'Preview voice'}
    </button>
    <p class="text-xs text-muted">Preview generates a short sample and may incur a provider charge.</p>
  {/if}
  {#if error}<p role="alert" class="text-xs text-danger">{error}</p>{/if}
  {#if notice}<p role="status" class="text-xs text-muted">{notice}</p>{/if}
</div>
