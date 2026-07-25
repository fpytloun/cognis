<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { blockDescription, blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { blockTone, valueText } from '../block-helpers';
  import RichIcon from '../RichIcon.svelte';

  export let block: RichBlock;

  $: tone = blockTone(block);
</script>

<article class="rich-metric tone-{tone}" data-rich-block-type="metric">
  <div class="rich-metric-heading">
    {#if block.icon}<RichIcon icon={block.icon} label={blockText(block, 'icon_label')} />{/if}
    <span>{@html renderInlineMarkdown(blockTitle(block) || 'Metric')}</span>
    {#if blockText(block, 'timestamp') || blockText(block, 'time')}<time>{blockText(block, 'timestamp') || blockText(block, 'time')}</time>{/if}
  </div>
  <div>
    <strong>{@html renderInlineMarkdown(valueText(block.value ?? blockText(block)))}</strong>
    {#if blockText(block, 'delta')}<em>{@html renderInlineMarkdown(blockText(block, 'delta'))}</em>{/if}
  </div>
  {#if blockDescription(block)}<p>{@html renderInlineMarkdown(blockDescription(block))}</p>{/if}
</article>
