<script lang="ts">
  import Download from 'lucide-svelte/icons/download';
  import X from 'lucide-svelte/icons/x';
  import { onMount, tick } from 'svelte';
  import hljs from 'highlight.js/lib/common';
  import 'highlight.js/styles/github-dark.css';

  import { portal } from '$lib/actions/portal';
  import { api } from '$lib/api/client';
  import { previewLanguage } from '$lib/attachments/preview';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import type { AttachmentRef } from '$lib/types/api';

  let {
    attachment,
    kind,
    mediaUrl = null,
    onClose,
    onDownload,
  }: {
    attachment: AttachmentRef;
    kind: 'text' | 'video';
    mediaUrl?: string | null;
    onClose: () => void;
    onDownload: () => void;
  } = $props();

  let panel = $state<HTMLDivElement | null>(null);
  let closeButton = $state<HTMLButtonElement | null>(null);
  let overlayId = $state<string | null>(null);
  let content = $state('');
  let truncated = $state(false);
  let loading = $state(true);
  let error = $state<string | null>(null);

  const highlighted = $derived.by(() => {
    const escaped = () => content.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    const language = previewLanguage(attachment.filename, attachment.mime_type);
    try {
      return language && hljs.getLanguage(language)
        ? hljs.highlight(content, { language, ignoreIllegals: true }).value
        : escaped();
    } catch {
      return escaped();
    }
  });

  function handleKeydown(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== 'Tab' || !panel) return;
    const elements = Array.from(panel.querySelectorAll<HTMLElement>('button:not([disabled]), [href], [controls], [tabindex]:not([tabindex="-1"])'));
    if (elements.length === 0) return;
    const first = elements[0];
    const last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  onMount(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overlay = registerOverlay({ kind: 'blocking', blocksChrome: true });
    overlayId = overlay.id;
    void tick().then(() => closeButton?.focus());
    if (kind === 'text') {
      void api.artifacts.textPreview(attachment.artifact_id).then((result) => {
        content = result.content;
        truncated = result.truncated;
      }).catch(() => {
        error = 'The text preview could not be loaded.';
      }).finally(() => {
        loading = false;
      });
    } else {
      loading = false;
    }
    return () => {
      overlay.unregister();
      overlayId = null;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<div use:portal class="fixed inset-0 z-[90] isolate flex items-center justify-center p-4" role="presentation">
  <button aria-label="Dismiss preview" class="absolute inset-0 bg-slate-950/80 backdrop-blur" onclick={onClose} tabindex="-1" type="button"></button>
  <div bind:this={panel} class="relative z-10 flex max-h-full w-full max-w-6xl flex-col overflow-hidden rounded-3xl border border-slate-800 bg-slate-950 shadow-card" role="dialog" aria-modal="true" aria-labelledby="attachment-preview-title" tabindex="-1" data-blocking-overlay>
    <div class="flex shrink-0 items-center justify-between gap-3 border-b border-slate-800/80 px-5 py-4">
      <div class="min-w-0">
        <h2 id="attachment-preview-title" class="truncate text-lg font-semibold text-slate-100">{attachment.filename}</h2>
        <p class="mt-0.5 text-xs text-slate-400">{attachment.mime_type ?? 'file'}</p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <button class="copy-icon-button" onclick={onDownload} type="button" title="Download" aria-label={`Download ${attachment.filename}`}><Download /></button>
        <button bind:this={closeButton} class="copy-icon-button" onclick={onClose} type="button" title="Close" aria-label="Close preview"><X /></button>
      </div>
    </div>
    <div class="min-h-0 flex-1 overflow-auto p-5">
      {#if loading}
        <p class="text-sm text-slate-400">Loading preview…</p>
      {:else if error}
        <p class="text-sm text-rose-300">{error}</p>
      {:else if kind === 'video' && mediaUrl}
        <!-- svelte-ignore a11y_media_has_caption: previews cannot synthesize a caption track -->
        <video class="mx-auto max-h-[70vh] max-w-full rounded-xl bg-black" src={mediaUrl} controls playsinline></video>
      {:else if kind === 'video'}
        <p class="text-sm text-rose-300">The video preview could not be loaded.</p>
      {:else}
        {#if truncated}<p class="mb-3 text-xs text-amber-300">Preview limited to the first 512 KB.</p>{/if}
        <pre class="min-h-full overflow-x-auto rounded-xl bg-slate-900 p-4 text-sm leading-6 text-slate-100"><code class="hljs">{@html highlighted}</code></pre>
      {/if}
    </div>
  </div>
</div>
