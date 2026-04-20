<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import { api } from '$lib/api/client';

  let {
    name = '',
    description = '',
    personality = {},
    onAccept,
    onClose
  } = $props<{
    name: string;
    description?: string;
    personality?: Record<string, unknown>;
    onAccept: (imageId: string, avatarUrl: string) => void;
    onClose: () => void;
  }>();

  let prompt = $state('');
  let generatingPrompt = $state(false);
  let generatingImage = $state(false);
  let generatedImageUrl = $state('');
  let generatedImageId = $state('');
  let error = $state('');

  async function generatePrompt() {
    generatingPrompt = true;
    error = '';
    try {
      const result = await api.images.generatePrompt({ name, description, personality });
      prompt = result.prompt;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to generate prompt';
    } finally {
      generatingPrompt = false;
    }
  }

  async function generateImage() {
    if (!prompt.trim()) return;
    generatingImage = true;
    error = '';
    generatedImageUrl = '';
    generatedImageId = '';
    try {
      const result = await api.images.generate(prompt);
      generatedImageId = result.image_id;
      generatedImageUrl = result.url;
      if (result.prompt_used && result.prompt_used !== prompt) {
        prompt = result.prompt_used;
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to generate image';
    } finally {
      generatingImage = false;
    }
  }

  function handleAccept() {
    if (generatedImageId && generatedImageUrl) {
      onAccept(generatedImageId, generatedImageUrl);
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }

  // Auto-generate prompt on mount
  $effect(() => {
    if (!prompt && name) {
      generatePrompt();
    }
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- Overlay -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="app-viewport-overlay z-50 flex items-center justify-center overflow-y-auto overscroll-contain bg-black/60 px-4 py-4 backdrop-blur-sm" onclick={onClose}>
  <!-- Dialog -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="max-h-full w-full max-w-lg overflow-y-auto rounded-3xl border border-slate-700 bg-slate-900 p-6 shadow-2xl overscroll-contain" onclick={(e) => e.stopPropagation()}>
    <!-- Header -->
    <div class="mb-4 flex items-center justify-between">
      <h3 class="text-lg font-semibold text-slate-100">Generate Avatar</h3>
      <button type="button" class="text-slate-400 hover:text-slate-200" onclick={onClose}>&times;</button>
    </div>

    <!-- Prompt section -->
    <div class="mb-4">
      <div class="mb-2 flex items-center justify-between">
        <label class="text-sm font-medium text-slate-200" for="avatar-prompt">Prompt</label>
        <button
          type="button"
          class="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300 disabled:opacity-50"
          onclick={generatePrompt}
          disabled={generatingPrompt}
        >
          {#if generatingPrompt}
            <span class="inline-block h-3 w-3 animate-spin rounded-full border border-slate-600 border-t-sky-400"></span>
          {/if}
          Regenerate prompt
        </button>
      </div>
      <textarea
        id="avatar-prompt"
        bind:value={prompt}
        class="min-h-[100px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
        placeholder="Describe the avatar you want to generate..."
        disabled={generatingPrompt}
      ></textarea>
    </div>

    <!-- Preview section -->
    <div class="mb-4">
      <p class="mb-2 text-sm font-medium text-slate-200">Preview</p>
      <div class="flex h-48 items-center justify-center rounded-2xl border border-slate-700 bg-slate-950/80">
        {#if generatingImage}
          <div class="flex flex-col items-center gap-2 text-slate-400">
            <span class="inline-block h-8 w-8 animate-spin rounded-full border-2 border-slate-700 border-t-sky-400"></span>
            <span class="text-sm">Generating...</span>
          </div>
        {:else if generatedImageUrl}
          <img
            src={generatedImageUrl}
            alt="Generated avatar"
            class="h-full max-h-44 rounded-xl object-contain"
          />
        {:else}
          <span class="text-sm text-slate-500">No image generated yet</span>
        {/if}
      </div>
    </div>

    <!-- Error -->
    {#if error}
      <p class="mb-4 text-sm text-rose-300">{error}</p>
    {/if}

    <!-- Actions -->
    <div class="flex items-center justify-end gap-3">
      <Button variant="secondary" onclick={onClose}>Cancel</Button>
      <Button
        variant="secondary"
        onclick={generateImage}
        disabled={generatingImage || !prompt.trim()}
      >
        {generatingImage ? 'Generating...' : generatedImageUrl ? 'Regenerate' : 'Generate'}
      </Button>
      {#if generatedImageId}
        <Button variant="primary" onclick={handleAccept}>Use this avatar</Button>
      {/if}
    </div>
  </div>
</div>
