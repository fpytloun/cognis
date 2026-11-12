<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import {
    blockChildren,
    blockMedia,
    blockText,
    blockTitle,
    type RichBlock,
    type RichMediaUrlFor,
  } from '$lib/rich-deliverable';
  import { heroBadges } from '../hero-badges';
  import RichBlockList from '../RichBlockList.svelte';
  import RichMedia from '../RichMedia.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = blockChildren(block);
  $: tags = heroBadges(block.badges ?? block.tags);
  $: media = blockMedia(block);
  $: hasMedia = Boolean(media);
  // Persisted authored dashboards also use `dek` for the hero summary.
  $: subtitle = blockText(block, 'subtitle') || blockText(block, 'dek');
</script>

<section class="rich-hero" class:has-media={hasMedia} data-rich-block-type="hero">
  {#if hasMedia}
    <!-- A hero image (e.g. an agent-generated banner) renders as a
         full-bleed background behind the identity block, with a legibility
         gradient so title/subtitle text stays readable over any photo. -->
    <RichMedia media={media} fallbackAlt={blockTitle(block)} {mediaUrlFor} placementOverride="background" />
  {/if}
  <div class="rich-hero-content">
    {#if tags.length > 0}
      <div class="rich-chip-row rich-hero-badges">{#each tags as tag}<span class={`tone-${tag.tone}`}>{@html renderInlineMarkdown(tag.label)}</span>{/each}</div>
    {/if}
    {#if blockText(block, 'eyebrow')}<div class="rich-eyebrow">{@html renderInlineMarkdown(blockText(block, 'eyebrow'))}</div>{/if}
    {#if blockTitle(block)}
      {#if block.__document_heading_level === 1}<h1>{@html renderInlineMarkdown(blockTitle(block))}</h1>{:else}<h2>{@html renderInlineMarkdown(blockTitle(block))}</h2>{/if}
    {/if}
    {#if subtitle}<p class="rich-lede">{@html renderInlineMarkdown(subtitle)}</p>{/if}
    {#if children.length > 0}<div class="rich-stack"><RichBlockList blocks={children} {sources} {mediaUrlFor} /></div>{/if}
  </div>
</section>
