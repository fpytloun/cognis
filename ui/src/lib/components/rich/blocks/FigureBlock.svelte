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
  let previewOpen = false;
  let imageViewer: typeof import('../ImageViewer.svelte')['default'] | null = null;
  let previewTrigger: HTMLButtonElement;
  let previewError = '';

  async function openPreview() {
    previewError = '';
    try {
      imageViewer = (await import('../ImageViewer.svelte')).default;
      previewOpen = true;
    } catch {
      previewError = 'Image preview could not load. Reload the page to retry.';
    }
  }

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
  {#if src}
    <button class="figure-preview" type="button" bind:this={previewTrigger} aria-label={`Enlarge image: ${alt || blockTitle(block) || 'Figure'}`} on:click={openPreview}>
      <img src={src} {alt} loading="lazy" />
    </button>
  {/if}
  {#if previewError}<p role="status">{previewError}</p>{/if}
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

{#if previewOpen && src && imageViewer}
  <svelte:component this={imageViewer} {src} {alt} returnFocus={previewTrigger} label={blockTitle(block) || alt || 'Image preview'} on:close={() => previewOpen = false} />
{/if}

<style>
  .figure-preview { display: block; width: fit-content; max-width: 100%; padding: 0; border: 0; background: transparent; cursor: zoom-in; text-align: inherit; }
  .figure-preview:focus-visible { outline: 2px solid var(--rich-accent, rgb(var(--color-accent))); outline-offset: 4px; }
</style>
