<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { blockDescription, blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { blockSpan, blockSurface, blockTone, valueText } from '../block-helpers';
  import RichIcon from '../RichIcon.svelte';
  import RichProgressBar from '../RichProgressBar.svelte';

  export let block: RichBlock;

  $: tone = blockTone(block);
  $: surface = blockSurface(block);
  $: span = blockSpan(block);
  $: progress = block.progress && typeof block.progress === 'object' && !Array.isArray(block.progress)
    ? block.progress as Record<string, unknown>
    : null;
</script>

<article
  class="rich-metric tone-{tone}"
  data-rich-block-type="metric"
  data-rich-surface={surface}
  style:grid-column={span ? `span ${span}` : undefined}
>
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
  {#if progress}
    <RichProgressBar
      value={progress.value}
      max={progress.max}
      label={typeof progress.label === 'string' ? progress.label : ''}
      {tone}
    />
  {/if}
</article>
