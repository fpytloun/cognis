<script lang="ts">
  import { onMount } from 'svelte';

  import { confirmStore, resolveConfirm } from '$lib/stores/confirm';
  import Button from '$lib/components/ui/Button.svelte';

  let container: HTMLDivElement | null = null;
  let previousFocus: HTMLElement | null = null;

  function focusableElements(): HTMLElement[] {
    if (!container) {
      return [];
    }
    return Array.from(
      container.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      )
    ).filter((element) => !element.hasAttribute('disabled'));
  }

  function trapFocus(event: KeyboardEvent): void {
    if ($confirmStore === null) {
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      resolveConfirm(false);
      return;
    }
    if (event.key !== 'Tab') {
      return;
    }
    const elements = focusableElements();
    if (elements.length === 0) {
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

  $: if ($confirmStore) {
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    queueMicrotask(() => {
      focusableElements()[0]?.focus();
    });
  } else if (previousFocus) {
    queueMicrotask(() => previousFocus?.focus());
  }

  onMount(() => {
    document.addEventListener('keydown', trapFocus);
    return () => {
      document.removeEventListener('keydown', trapFocus);
    };
  });
</script>

{#if $confirmStore}
  <div class="fixed inset-0 z-[90] flex items-center justify-center bg-slate-950/80 px-4 py-6 backdrop-blur" role="presentation">
    <div bind:this={container} aria-modal="true" class="w-full max-w-lg rounded-3xl border border-slate-800 bg-slate-950 p-6 shadow-card" role="dialog" aria-labelledby="confirm-title">
      <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Confirmation required</p>
      <h2 class="mt-3 text-xl font-semibold text-white" id="confirm-title">{$confirmStore.title}</h2>
      <p class="mt-3 text-sm leading-6 text-slate-300">{$confirmStore.message}</p>
      <div class="mt-6 flex flex-wrap justify-end gap-3">
        <Button variant="secondary" onclick={() => resolveConfirm(false)}>{$confirmStore.cancelLabel}</Button>
        <Button variant={$confirmStore.variant === 'primary' ? 'primary' : 'danger'} onclick={() => resolveConfirm(true)}>{$confirmStore.confirmLabel}</Button>
      </div>
    </div>
  </div>
{/if}
