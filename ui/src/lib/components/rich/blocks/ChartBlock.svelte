<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { blockChildren, blockText, blockTitle, safeUrl, type RichBlock, type RichMediaUrlFor } from '$lib/rich-deliverable';
  import RichBlockList from '../RichBlockList.svelte';
  import RichChart from '../RichChart.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = blockChildren(block);
  $: sourceUrl = safeUrl(block.source_url);
  $: sourceLabel = blockText(block, 'source') || blockText(block, 'source_label');
</script>

<section class="rich-chart-card" data-rich-block-type="chart">
  {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
  {#if blockText(block, 'description')}<p>{@html renderInlineMarkdown(blockText(block, 'description'))}</p>{/if}
  <RichChart {block} />
  {#if sourceLabel || sourceUrl || blockText(block, 'timestamp')}
    <p class="rich-data-source">
      {#if sourceLabel || sourceUrl}Source: {#if sourceUrl}<a href={sourceUrl} target="_blank" rel="noreferrer">{sourceLabel || sourceUrl}</a>{:else}{@html renderInlineMarkdown(sourceLabel)}{/if}{/if}
      {#if blockText(block, 'timestamp')}<span>Updated: {blockText(block, 'timestamp')}</span>{/if}
    </p>
  {/if}
  <RichBlockList blocks={children} {sources} {mediaUrlFor} />
</section>
