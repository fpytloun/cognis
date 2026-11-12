<script lang="ts">
  import { onMount, untrack, type ComponentProps } from 'svelte';
  import type ToolCallDetails from './ToolCallDetails.svelte';
  import ToolCallHeader from './ToolCallHeader.svelte';
  import { loadToolCallDetails, peekToolCallDetails } from './tool-call-details-loader';
  import { normalizedToolName, toolDetailsStartExpanded, toolFrameClass } from '$lib/tool-call-header';

  let props: Omit<ComponentProps<typeof ToolCallDetails>, 'initialExpanded' | 'focusHeader'> = $props();
  let Details = $state.raw(peekToolCallDetails());
  let mounted = $state(false);
  let loading = $state(false);
  let error = $state(false);
  let requestedExpanded = $state<boolean | undefined>(undefined);
  let focusTarget: HTMLElement | null = null;
  let focusHeader = $state(false);
  const initiallyExpanded = $derived(toolDetailsStartExpanded(props.item));
  const expanded = $derived(requestedExpanded ?? (mounted && initiallyExpanded));
  const controlsId = $derived(`tool-call-content-${props.item.callId.replace(/[^a-zA-Z0-9_-]/g, '-')}`);
  const bashRunning = $derived(props.item.status === 'started' && /bash|shell/.test(normalizedToolName(props.item)));

  async function load(): Promise<void> {
    if (Details || loading) return;
    loading = true;
    error = false;
    try {
      const component = await loadToolCallDetails();
      if (!mounted) return;
      focusHeader = focusTarget !== null && document.activeElement === focusTarget;
      Details = component;
    } catch {
      if (mounted) error = true;
    } finally {
      if (mounted) loading = false;
    }
  }
  function toggle(event: MouseEvent): void {
    requestedExpanded = !expanded;
    focusTarget = event.currentTarget as HTMLElement;
    if (requestedExpanded) void load();
  }
  onMount(() => {
    mounted = true;
    return () => { mounted = false; };
  });
  $effect(() => {
    if (mounted && (initiallyExpanded || bashRunning)) untrack(() => void load());
  });
</script>

{#if Details}
  <Details {...props} initialExpanded={requestedExpanded} {focusHeader} />
{:else}
  <article class={toolFrameClass(props.item, props.density)} style:border-width="var(--tool-group-border, 1px)">
    <ToolCallHeader item={props.item} {expanded} {controlsId} onclick={toggle}
      density={props.density} summaryMode={props.summaryMode} compactLabelMode={props.compactLabelMode}
      busy={expanded && loading} />
    {#if expanded}
      <div id={controlsId} class="border-t border-separator px-4 py-3 text-xs text-muted">
        {#if error}
          <p role="alert">Could not load tool details.</p>
          <button type="button" class="mt-2 underline" onclick={() => void load()}>Retry</button>
        {:else}
          <p role="status">Loading tool details…</p>
        {/if}
      </div>
    {/if}
  </article>
{/if}
