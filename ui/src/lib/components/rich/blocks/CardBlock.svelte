<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks, renderMarkdown } from '$lib/markdown';
  import {
    blockChildren,
    blockMedia,
    blockText,
    blockTitle,
    safeUrl,
    type RichBlock,
    type RichMediaUrlFor,
  } from '$lib/rich-deliverable';
  import { blockTone } from '../block-helpers';
  import RichBlockList from '../RichBlockList.svelte';
  import RichIcon from '../RichIcon.svelte';
  import RichMedia from '../RichMedia.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';
  // Standalone block types that reuse the card visual treatment (e.g.
  // `action`) pass their own type through so debugging/contract assertions
  // that key off `data-rich-block-type` still see the authored type.
  export let dataBlockType = 'card';

  $: children = blockChildren(block);
  $: tone = blockTone(block);
  $: variant = ['editorial', 'feature', 'status', 'action', 'metric', 'compact', 'visual'].includes(String(block.variant))
    ? String(block.variant) : 'editorial';
  $: href = safeUrl(block.href);
  $: summary = blockText(block, 'summary') || blockText(block, 'dek');
  $: media = blockMedia(block);
  // Read-only signal from RichMedia: true only once an image source exists
  // and has not failed to load. Starts false and updates asynchronously
  // once the browser resolves (or fails to resolve) the image.
  let mediaVisible = false;
  // `visual` is an image-forward treatment: the media becomes a full-bleed
  // background behind the header/summary/body, like HeroBlock's
  // `.has-media` overlay. Without a resolvable image (no media authored, an
  // unresolvable source, or a failed load) it gracefully degrades to the
  // same flat card layout as every other variant instead of rendering an
  // empty overlay shell with light-on-dark text and no image behind it.
  $: isVisualMedia = variant === 'visual' && mediaVisible;
</script>

<article
  class="rich-card rich-card-{variant} tone-{tone}"
  class:has-media={isVisualMedia}
  data-rich-block-type={dataBlockType}
  data-rich-card-variant={variant}
>
  <RichMedia
    {media}
    fallbackAlt={blockTitle(block)}
    {mediaUrlFor}
    placementOverride={isVisualMedia ? 'background' : ''}
    bind:hasVisibleMedia={mediaVisible}
  />
  <div class="rich-card-body">
    <div class="rich-card-header">
      {#if block.icon && variant !== 'visual'}<RichIcon icon={block.icon} label={blockText(block, 'icon_label')} />{/if}
      <div>
        {#if blockText(block, 'eyebrow')}<span class="rich-eyebrow">{@html renderInlineMarkdown(blockText(block, 'eyebrow'))}</span>{/if}
        {#if blockTitle(block)}
          {#if href}<h4><a href={href} target="_blank" rel="noreferrer">{@html renderInlineMarkdownNoLinks(blockTitle(block))}</a></h4>
          {:else}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
        {/if}
      </div>
    </div>
    {#if summary}<p class="rich-card-summary">{@html renderInlineMarkdown(summary)}</p>{/if}
    {#if blockText(block)}<div class="rich-markdown">{@html renderMarkdown(blockText(block))}</div>{/if}
    {#if children.length > 0}<RichBlockList blocks={children} {sources} {mediaUrlFor} />{/if}
  </div>
</article>
