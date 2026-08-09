<script lang="ts">
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import X from 'lucide-svelte/icons/x';
  import { onMount, tick } from 'svelte';
  import { portal } from '$lib/actions/portal';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import { addToast } from '$lib/stores/toasts';

  let {
    code,
    language,
    onClose
  }: {
    code: string;
    language: string | null;
    onClose: () => void;
  } = $props();

  let panel = $state<HTMLDivElement | null>(null);
  let closeButton = $state<HTMLButtonElement | null>(null);
  let copied = $state(false);
  let overlayId = $state<string | null>(null);

  function focusableElements(): HTMLElement[] {
    if (!panel) return [];
    return Array.from(
      panel.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== 'Tab') return;

    const elements = focusableElements();
    if (elements.length === 0) {
      event.preventDefault();
      panel?.focus();
      return;
    }
    const first = elements[0];
    const last = elements[elements.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function closeFromBackdrop(): void {
    if (isTopOverlay(overlayId)) onClose();
  }

  async function copyCode(): Promise<void> {
    try {
      await navigator.clipboard.writeText(code);
      copied = true;
    } catch {
      addToast('Failed to copy code block', 'error');
    }
  }

  onMount(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overlay = registerOverlay({ kind: 'blocking', blocksChrome: true });
    overlayId = overlay.id;
    void tick().then(() => closeButton?.focus());
    return () => {
      overlay.unregister();
      overlayId = null;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<div
  use:portal
  class="fixed inset-0 z-[90] isolate flex items-center justify-center px-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))]"
  style="padding-top: calc(var(--app-shell-top-offset, 0px) + env(safe-area-inset-top) + 1rem); padding-bottom: calc(var(--app-shell-bottom-offset, 0px) + env(safe-area-inset-bottom) + 1rem);"
  role="presentation"
>
  <button
    aria-label="Dismiss code block"
    class="absolute inset-0 bg-slate-950/80 backdrop-blur"
    onclick={closeFromBackdrop}
    tabindex="-1"
    type="button"
  ></button>
  <div
    bind:this={panel}
    class="relative z-10 flex max-h-full w-full max-w-6xl flex-col overflow-hidden rounded-3xl border border-slate-800 bg-slate-950 shadow-card"
    role="dialog"
    aria-modal="true"
    data-blocking-overlay
    aria-labelledby="code-block-modal-title"
    tabindex="-1"
  >
    <div class="flex shrink-0 items-center justify-between gap-3 border-b border-slate-800/80 px-5 py-4 sm:px-6">
      <div class="min-w-0">
        <h2 class="text-lg font-semibold text-slate-100" id="code-block-modal-title">Code block</h2>
        {#if language}
          <p class="mt-0.5 text-xs text-slate-400">{language}</p>
        {/if}
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <button class="copy-icon-button" onclick={copyCode} type="button" title="Copy code block" aria-label="Copy code block">
          {#if copied}
            <Check />
          {:else}
            <Copy />
          {/if}
        </button>
        <button bind:this={closeButton} class="copy-icon-button" onclick={closeFromBackdrop} type="button" title="Close code block" aria-label="Close code block">
          <X />
        </button>
      </div>
    </div>
    <div class="min-h-0 flex-1 overflow-auto p-5 sm:p-6">
      <pre class="min-h-full rounded-xl bg-slate-900 p-4 text-sm leading-6 text-slate-100"><code>{code}</code></pre>
    </div>
  </div>
</div>
