<script lang="ts">
  import Download from 'lucide-svelte/icons/download';
  import X from 'lucide-svelte/icons/x';

  /**
   * Full-screen image lightbox.
   *
   * - Click the backdrop or the close button to dismiss.
   * - Press Escape to dismiss.
   * - The download button triggers a real save (uses an `<a download>`
   *   so the user gets the actual filename, not a hashed artifact id).
   * - The image fills as much of the viewport as it can while
   *   respecting safe-area insets, so on iPhone PWAs the controls
   *   land below the Dynamic Island and above the home indicator.
   */

  let { src, alt = 'Image', filename = null, onClose } = $props<{
    src: string;
    alt?: string;
    filename?: string | null;
    onClose: () => void;
  }>();

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
    }
  }

  function handleBackdropClick(): void {
    onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions a11y_no_noninteractive_element_interactions a11y_interactive_supports_focus -->
<div
  role="dialog"
  aria-modal="true"
  aria-label={filename ?? alt}
  tabindex="-1"
  class="fixed inset-0 z-[80] flex flex-col items-stretch justify-stretch bg-black/85 backdrop-blur-sm"
  onclick={handleBackdropClick}
>
  <!-- Top toolbar -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="flex shrink-0 items-center justify-between gap-2 px-4 pt-[calc(0.75rem+env(safe-area-inset-top))] pb-3"
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
        class="inline-flex h-10 w-10 items-center justify-center rounded-full bg-slate-900/70 text-slate-100 transition hover:bg-slate-800"
      >
        <Download class="h-5 w-5" />
      </a>
      <button
        aria-label="Close"
        title="Close"
        type="button"
        onclick={onClose}
        class="inline-flex h-10 w-10 items-center justify-center rounded-full bg-slate-900/70 text-slate-100 transition hover:bg-slate-800"
      >
        <X class="h-5 w-5" />
      </button>
    </div>
  </div>

  <!-- Image area -->
  <div class="flex min-h-0 flex-1 items-center justify-center p-4">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <img
      {src}
      {alt}
      class="max-h-full max-w-full rounded-2xl object-contain shadow-2xl"
      onclick={(event) => event.stopPropagation()}
    />
  </div>
</div>
