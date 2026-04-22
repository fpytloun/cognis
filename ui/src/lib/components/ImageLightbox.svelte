<script lang="ts">
  import { onMount } from 'svelte';

  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import Download from 'lucide-svelte/icons/download';
  import X from 'lucide-svelte/icons/x';

  /**
   * Full-screen image lightbox.
   *
   * - Click the backdrop or the close button to dismiss.
   * - Press Escape to dismiss.
   * - The download button triggers a real save (uses an ``<a download>``
   *   so the user gets the actual filename, not a hashed artifact id).
   * - Locks the body scroll while open so touchmove on the backdrop on
   *   iOS Safari doesn't rubber-band the page behind the lightbox.
   *   Without the lock the user can drag the page underneath, and the
   *   backdrop-blur layer appears to shift / lose its fixed position,
   *   and the toolbar scrolls out of the visible area.
   * - Toolbar is absolutely positioned inside the fixed viewport with
   *   safe-area padding so it always lands below the Dynamic Island
   *   and stays visible regardless of the image's aspect ratio.
   */

  let { src, alt = 'Image', filename = null, onClose } = $props<{
    src: string;
    alt?: string;
    filename?: string | null;
    onClose: () => void;
  }>();

  let overlayId = $state<string | null>(null);

  onMount(() => {
    const handle = registerOverlay({ kind: 'fullscreen', blocksChrome: true });
    overlayId = handle.id;
    return () => {
      handle.unregister();
      overlayId = null;
    };
  });

  function handleKeydown(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) {
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- svelte-ignore a11y_interactive_supports_focus -->
<div
  role="dialog"
  aria-modal="true"
  aria-label={filename ?? alt}
  tabindex="-1"
  class="app-viewport-overlay z-[95] overflow-hidden overscroll-contain touch-none bg-slate-950/95"
  onclick={onClose}
>
  <!-- Image area. Centered, fills the viewport. Tapping the image
       does not close the lightbox; tapping the surrounding backdrop
       does. -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <img
    {src}
    {alt}
    class="absolute inset-0 m-auto max-h-[calc(100%-5rem)] max-w-[calc(100%-1.5rem)] rounded-2xl object-contain shadow-2xl sm:max-h-[calc(100%-5.5rem)] sm:max-w-[calc(100%-2rem)]"
    onclick={(event) => event.stopPropagation()}
  />

  <!--
    Toolbar. Absolutely positioned inside the fixed lightbox so the
    surrounding image flex layout can never push it out of view, and
    it carries its own solid background so it's always readable
    against any image colour. The parent overlay already respects the
    shared app-shell offsets, so the close button stays below the
    mobile header and above the bottom tab bar.
  -->
  <div
    class="absolute inset-x-0 top-0 flex items-center justify-between gap-2 bg-slate-950/85 px-3 py-3 shadow-lg backdrop-blur sm:px-4 sm:py-4"
    onclick={(event) => event.stopPropagation()}
  >
    <p class="min-w-0 flex-1 truncate text-sm text-slate-200">{filename ?? alt}</p>
    <div class="flex shrink-0 items-center gap-2">
      <a
        aria-label="Download"
        title="Download"
        href={src}
        download={filename ?? ''}
        target="_blank"
        rel="noreferrer"
        class="inline-flex h-10 w-10 items-center justify-center rounded-full bg-slate-800/80 text-slate-100 transition hover:bg-slate-700"
      >
        <Download class="h-5 w-5" />
      </a>
      <button
        aria-label="Close"
        title="Close"
        type="button"
        onclick={onClose}
        class="inline-flex h-10 w-10 items-center justify-center rounded-full bg-slate-800/80 text-slate-100 transition hover:bg-slate-700"
      >
        <X class="h-5 w-5" />
      </button>
    </div>
  </div>
</div>
