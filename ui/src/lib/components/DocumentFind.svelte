<script lang="ts">
  import { tick, type Snippet } from 'svelte';
  import { findTarget, requestFind } from '$lib/find';
  import type { DocumentFindAdapter } from '$lib/document-find';

  let { children, adapter = null, primary = true, identity = null, fill = false }: {
    children: Snippet; adapter?: DocumentFindAdapter | null; primary?: boolean; identity?: unknown; fill?: boolean;
  } = $props();
  let root = $state<HTMLElement>(), content = $state<HTMLElement>();
  let Controls = $state<typeof import('./DocumentFindBar.svelte').default | null>(null);
  let controls = $state<{ getInput(): HTMLInputElement | null }>();
  let loadError = $state('');
  let generation = 0;
  let returnFocus: Element | null = null;
  async function open() {
    if (!Controls) returnFocus = document.activeElement;
    const version = generation;
    try {
      const module = await import('./DocumentFindBar.svelte');
      if (version !== generation) return null;
      Controls = module.default; loadError = '';
      await tick();
      return version === generation ? controls?.getInput() ?? null : null;
    } catch { loadError = 'Could not open document search. Try again.'; return null; }
  }
  export function requestOpen() { if (root) requestFind(root); }
  function close() {
    generation++; Controls = null;
    void tick().then(() => {
      if (returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
    });
  }
  $effect(() => { identity; return () => { generation++; Controls = null; }; });
</script>

<section bind:this={root} class="document-find" class:fill data-find-scope tabindex="-1" use:findTarget={{ primary, scopeOnly: !primary, identity: () => identity, open }}>
  <div class="document-find-controls" data-find-ignore>
    {#if Controls}
      <Controls bind:this={controls} {content} {adapter} onclose={close} />
    {/if}
    {#if loadError}<p role="alert">{loadError}</p>{/if}
  </div>
  <div class="document-find-content" bind:this={content}>{@render children()}</div>
</section>

<style>
  .document-find { min-width: 0; }
  .fill { display: flex; flex-direction: column; height: 100%; min-height: 0; }
  .fill > .document-find-content { flex: 1; min-height: 0; }
  .document-find-controls { display: contents; }
  @media print { .document-find-controls { display: none; } }
</style>
