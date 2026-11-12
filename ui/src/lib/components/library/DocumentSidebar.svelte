<script lang="ts">
  import { onMount, untrack, type Snippet } from 'svelte';
  import X from 'lucide-svelte/icons/x';
  import { getFeedbackSurface } from '$lib/feedback/context';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  /**
   * The document's right sidebar. On wide screens the comment list docks here as a tab next to
   * Details instead of floating over the document; phones keep the comment sheet.
   */
  let { open = $bindable(false), details }: { open?: boolean; details: Snippet } = $props();
  const surface = getFeedbackSurface();
  let wide = $state(false);
  let tab = $state<'details' | 'comments'>('details');
  const docked = $derived(wide && !!surface?.registerDock && !!surface.panel);
  const count = $derived(surface?.commentCount ?? 0);
  onMount(() => {
    const query = matchMedia('(min-width: 1024px)');
    const update = () => { wide = query.matches; };
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  });
  $effect(() => { if (docked) return untrack(() => surface!.registerDock!()); });
  // The comment button, a marker or a thread link opens the list: show it here.
  // Also reruns when crossing the breakpoint, so a list opened on a narrow screen follows into the dock.
  $effect(() => {
    const listOpen = !!surface?.railOpen;
    void docked;
    untrack(() => {
      if (docked && listOpen) { open = true; tab = 'comments'; }
      else if (!listOpen && tab === 'comments') tab = 'details';
    });
  });
  function choose(next: 'details' | 'comments') {
    if (next === 'comments' && !surface?.railOpen) surface?.show?.();
    if (next === 'details' && surface?.railOpen) surface.close?.();
    tab = next;
  }
  function closeSidebar() {
    if (surface?.railOpen) surface.close?.();
    open = false;
  }
</script>
{#if open}
  <aside class={`absolute inset-y-0 right-0 z-10 flex shrink-0 flex-col overflow-hidden border-l border-separator bg-canvas shadow-xl lg:static lg:shadow-none ${tab === 'comments' ? 'w-96' : 'w-72'}`}
    aria-label="Document details">
    <div class="flex shrink-0 items-center gap-1 border-b border-separator px-3 py-2">
      {#if docked}
        <div class="flex gap-1" role="tablist" aria-label="Sidebar">
          <Button variant="ghost" size="sm" role="tab" aria-selected={tab === 'details'} class={tab === 'details' ? 'bg-surface-hover text-foreground' : 'text-muted'} onclick={() => choose('details')}>Details</Button>
          <Button variant="ghost" size="sm" role="tab" aria-selected={tab === 'comments'} class={tab === 'comments' ? 'bg-surface-hover text-foreground' : 'text-muted'} onclick={() => choose('comments')}>Comments{#if count}<Badge class="ml-1.5 border-accent/40 bg-accent-subtle px-1.5 py-0 text-accent">{count}</Badge>{/if}</Button>
        </div>
      {:else}<h3 class="px-2 text-sm font-medium text-foreground">Details</h3>{/if}
      <Button variant="ghost" size="icon" class="ml-auto" aria-label="Close details" onclick={closeSidebar}><X size={16} /></Button>
    </div>
    {#if docked && tab === 'comments'}
      <div class="docked-comments min-h-0 flex-1 overflow-hidden px-3 py-2" role="tabpanel" aria-label="Comments">{@render surface!.panel!()}</div>
    {:else}
      <div class="min-h-0 flex-1 space-y-6 overflow-auto p-5" role={docked ? 'tabpanel' : undefined}>{@render details()}</div>
    {/if}
  </aside>
{/if}
<style>
  /* The tab already names the list and the document; keep only the list's own controls. */
  .docked-comments :global([data-comments-title]) { display: none; }
</style>
