<script lang="ts" generics="P extends Record<string, unknown>">
  import { onDestroy, untrack, type Component } from 'svelte';
  import { documentCoordination } from '$lib/document-coordination';
  import { apiUrl } from '$lib/config';
  import { getFeedbackContext } from '$lib/feedback/context';
  import type { DeferredDefinition } from './deferred-definition';
  let { definition, viewProps, label, onClose, guard, modal = false }: {
    definition: DeferredDefinition<P>; viewProps: P; label: string;
    onClose?: () => void; guard?: () => void; modal?: boolean;
  } = $props();
  let Loaded = $state<Component<P> | null>(untrack(() => definition.peek()));
  let error = $state('');
  let attempt = $state(0);
  let revoked = $state(false);
  const origin = getFeedbackContext();
  const originAtMount = untrack(() => JSON.stringify(origin.context));
  let pendingElement = $state<HTMLDivElement>();
  const returnFocus = untrack(() => typeof document === 'undefined' ? null : document.activeElement);
  onDestroy(() => {
    if (modal && returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus();
  });
  $effect(() => {
    if (Loaded) return;
    const close = onClose;
    if (modal) pendingElement?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && close) { event.preventDefault(); close(); }
      if (event.key === 'Tab' && modal) {
        event.preventDefault();
        const buttons = Array.from(pendingElement?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? []);
        if (buttons.length) {
          const current = buttons.findIndex(button => button === document.activeElement);
          buttons[(current + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
        }
      }
    };
    window.addEventListener('keydown', keydown);
    return () => {
      window.removeEventListener('keydown', keydown);
    };
  });
  $effect(() => {
    void attempt; void viewProps;
    const selected = definition;
    if (revoked) return;
    if (JSON.stringify(origin.context) !== originAtMount) {
      revoked = true; Loaded = null; error = 'The source changed. Close and reopen this view.';
      return;
    }
    let alive = true;
    const coordination = documentCoordination();
    const owner = coordination.owner;
    const profile = apiUrl('');
    const generation = window.__cognisNative?.generation;
    // Presentation fencing is not authorization. Loaded clients retain their own leases.
    const stop = coordination.onInvalidated(() => {
      revoked = true; Loaded = null; error = 'The source account changed. Close and reopen this view.';
    });
    if (!Loaded) {
      error = '';
      void selected.load().then(component => {
        if (!alive || revoked) return;
        if (owner !== coordination.owner || profile !== apiUrl('') || generation !== window.__cognisNative?.generation
          || JSON.stringify(origin.context) !== originAtMount) {
          revoked = true;
          throw new Error('The source account changed. Close and reopen this view.');
        }
        guard?.();
        Loaded = component;
      }).catch(caught => {
        if (alive) error = caught instanceof Error ? caught.message : `${label} could not be loaded.`;
      });
    }
    return () => { alive = false; stop(); };
  });
</script>
{#if Loaded}
  <Loaded {...viewProps} />
{:else}
  <div bind:this={pendingElement} tabindex="-1" role={modal ? 'dialog' : undefined} aria-modal={modal ? true : undefined}
    class={`p-3 text-sm ${modal ? 'fixed inset-0 z-50 flex flex-col items-center justify-center bg-canvas' : ''}`} aria-label={label}>
    {#if error}<p role="alert">{error}</p>
      {#if !revoked}<button type="button" class="min-h-11 text-accent" onclick={() => { attempt++; }}>Retry {label}</button>{/if}
    {:else}<p role="status">Loading {label}…</p>{/if}
    {#if onClose}<button type="button" class="min-h-11 px-3" onclick={onClose}>Close {label}</button>{/if}
  </div>
{/if}
