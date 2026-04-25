<script lang="ts">
  import type { Snippet } from 'svelte';

  import { cn } from '$lib/utils';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';

  interface Props {
    open?: boolean;
    onClose: () => void;
    label: string;
    titleId?: string;
    class?: string;
    panelClass?: string;
    dismissible?: boolean;
    children: Snippet;
    header?: Snippet;
    footer?: Snippet;
  }

  let {
    open = true,
    onClose,
    label,
    titleId,
    class: className = '',
    panelClass = '',
    dismissible = true,
    children,
    header,
    footer
  }: Props = $props();

  let panelEl = $state<HTMLDivElement | null>(null);
  let previousFocus: HTMLElement | null = null;
  let overlayId = $state<string | null>(null);
  let unregisterOverlay: (() => void) | null = null;

  function focusableElements(): HTMLElement[] {
    if (!panelEl) return [];
    return Array.from(
      panelEl.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
  }

  function focusPanel(): void {
    const focusable = focusableElements()[0];
    if (focusable) {
      focusable.focus();
      return;
    }
    panelEl?.focus();
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (!open || !isTopOverlay(overlayId)) return;
    if (event.key === 'Escape' && dismissible) {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== 'Tab') return;
    const elements = focusableElements();
    if (elements.length === 0) return;
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
    if (!dismissible || !isTopOverlay(overlayId)) return;
    onClose();
  }

  $effect(() => {
    if (!open) {
      unregisterOverlay?.();
      unregisterOverlay = null;
      overlayId = null;
      if (previousFocus) {
        queueMicrotask(() => previousFocus?.focus());
        previousFocus = null;
      }
      return;
    }

    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const handle = registerOverlay({ kind: 'blocking', blocksChrome: true });
    overlayId = handle.id;
    unregisterOverlay = handle.unregister;
    queueMicrotask(() => focusPanel());

    return () => {
      handle.unregister();
      unregisterOverlay = null;
      overlayId = null;
      queueMicrotask(() => previousFocus?.focus());
      previousFocus = null;
    };
  });
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
  <div class={cn('fixed inset-0 z-[90] isolate', className)} role="presentation">
    <button
      aria-label={`Dismiss ${label}`}
      class="absolute inset-0 bg-[color:color-mix(in_srgb,var(--theme-bg-deep)_80%,transparent)] backdrop-blur"
      onclick={closeFromBackdrop}
      tabindex="-1"
      type="button"
    ></button>

    <div
      class="relative z-10 flex h-full w-full items-center justify-center px-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))]"
      style={`padding-top: calc(var(--app-shell-top-offset, 0px) + env(safe-area-inset-top) + 1rem); padding-bottom: calc(var(--app-shell-bottom-offset, 0px) + env(safe-area-inset-bottom) + 1rem);`}
    >
      <div
        bind:this={panelEl}
        aria-label={label}
        aria-labelledby={titleId}
        aria-modal="true"
        class={cn(
          'relative flex max-h-full w-full max-w-lg flex-col overflow-hidden rounded-3xl border border-[color:var(--theme-border)] bg-[color:var(--theme-bg)] shadow-card',
          panelClass
        )}
        role="dialog"
        tabindex="-1"
      >
        {#if header}
          <div class="shrink-0 border-b border-[color:var(--theme-border)] px-5 py-4 sm:px-6">
            {@render header()}
          </div>
        {/if}

        <div class="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-5 sm:px-6">
          {@render children()}
        </div>

        {#if footer}
          <div class="shrink-0 border-t border-[color:var(--theme-border)] px-5 py-4 sm:px-6">
            {@render footer()}
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}
