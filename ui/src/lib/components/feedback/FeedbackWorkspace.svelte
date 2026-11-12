<script lang="ts">
  import { onMount, type Snippet } from 'svelte';
  import { claimFeedbackWorkspace, getFeedbackSurface } from '$lib/feedback/context';
  let { children, contentClass = '' }: { children: Snippet; panel?: Snippet; open?: boolean; contentClass?: string } = $props();
  const surface = getFeedbackSurface();
  const owner = claimFeedbackWorkspace(surface) || !surface;
  let source = $state<HTMLDivElement>();
  let releaseBounds: (() => void) | undefined;
  onMount(() => {
    if (!owner) return;
    const unregister = surface?.registerWorkspace?.();
    return () => { unregister?.(); releaseBounds?.(); };
  });
  $effect(() => {
    void surface?.binding;
    const node = source;
    const report = surface?.reportBounds;
    if (!owner || !node || !report) return;
    releaseBounds = report ? () => report(null) : undefined;
    let disposed = false, stop: (() => void) | undefined;
    void import('$lib/feedback/source-bounds').then(({ observeSourceWorkspace }) => {
      if (!disposed) stop = observeSourceWorkspace(node, report, null, () => {});
    });
    return () => { disposed = true; stop?.(); };
  });
</script>
<div class="relative flex h-full min-h-0 min-w-0 flex-1" data-feedback-workspace>
  <div class="min-h-0 min-w-0 flex-1">
    <div bind:this={source} class={`h-full min-h-0 min-w-0 ${contentClass}`} data-feedback-source-viewport={owner || undefined}>{@render children()}</div>
  </div>
</div>
