<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import {
    blockText,
    blockTitle,
    safeImageUrl,
    safeUrl,
    type RichBlock,
    type RichMediaUrlFor,
  } from '$lib/rich-deliverable';

  export let block: RichBlock;
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: media = block.media && typeof block.media === 'object' && !Array.isArray(block.media)
    ? block.media as Record<string, unknown>
    : {};
  $: mediaKey = typeof media.key === 'string' ? media.key : '';
  $: mediaSource = block.src ?? block.url ?? media.src ?? (mediaKey ? mediaUrlFor(mediaKey) : undefined);
  $: src = typeof mediaSource === 'string' && mediaSource ? safeImageUrl(mediaSource) : '';
  $: alt = blockText(block, 'alt') || (typeof media.alt === 'string' ? media.alt : '');
  $: sourceUrl = safeUrl(block.source_url ?? media.source_url);
  $: sourceLabel = blockText(block, 'source')
    || blockText(block, 'source_label')
    || (typeof media.source === 'string' ? media.source : '')
    || (typeof media.source_label === 'string' ? media.source_label : '');
  $: timestamp = blockText(block, 'timestamp') || (typeof media.timestamp === 'string' ? media.timestamp : '');
</script>

<figure class="rich-figure" data-rich-block-type="figure">
  {#if src}<img src={src} {alt} loading="lazy" />{/if}
  {#if blockText(block, 'caption') || blockTitle(block) || block.__figure_number}
    <figcaption>
      {#if block.__figure_number}<strong>Figure {String(block.__figure_number)}.</strong>{' '}{/if}{@html renderInlineMarkdown(blockText(block, 'caption') || blockTitle(block))}
    </figcaption>
  {/if}
  {#if sourceLabel || sourceUrl || timestamp}
    <p class="rich-figure-source">
      {#if sourceLabel || sourceUrl}
        Source:
        {#if sourceUrl}<a href={sourceUrl} target="_blank" rel="noreferrer">{sourceLabel || sourceUrl}</a>{:else}{@html renderInlineMarkdown(sourceLabel)}{/if}
        {#if timestamp} · {/if}
      {:else}
        Updated:
      {/if}
      {timestamp}
    </p>
  {/if}
</figure>
