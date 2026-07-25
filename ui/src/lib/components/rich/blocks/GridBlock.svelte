<script lang="ts">
  import { blockChildren, type RichBlock, type RichMediaUrlFor } from '$lib/rich-deliverable';
  import RichBlockList from '../RichBlockList.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let type = 'grid';
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = blockChildren(block);
  $: layout = block.layout && typeof block.layout === 'object' && !Array.isArray(block.layout)
    ? block.layout as Record<string, unknown> : {};
  // Only pin an explicit column count when the author actually specified
  // one. Falling back to 0/undefined here is load-bearing: the CSS rule is
  // `grid-template-columns: repeat(var(--rich-columns, auto-fit), ...)`, so
  // an unset variable lets the grid auto-fit responsively. Previously this
  // computed to 1 whenever no columns were authored (the common case),
  // forcing every grid/columns block -- metric rows, card grids, etc. -- to
  // render as a single full-width vertical stack regardless of viewport.
  $: requestedColumns = Number(block.columns ?? layout.columns);
  $: columns = Number.isFinite(requestedColumns) && requestedColumns > 0
    ? Math.max(1, Math.min(4, Math.round(requestedColumns)))
    : 0;
</script>

<div class:rich-columns={type === 'columns'} class:rich-grid={type !== 'columns'} style:--rich-columns={columns || undefined} data-rich-block-type={type}>
  <RichBlockList blocks={children} {sources} {mediaUrlFor} />
</div>
