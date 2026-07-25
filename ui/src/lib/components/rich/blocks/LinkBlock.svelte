<script lang="ts">
  import { renderInlineMarkdownNoLinks } from '$lib/markdown';
  import { blockText, blockTitle, safeImageUrl, safeUrl, type RichBlock } from '$lib/rich-deliverable';

  export let block: RichBlock;
  export let type = 'link';

  $: rawHref = typeof (block.href ?? block.url) === 'string' ? String(block.href ?? block.url) : '';
  $: internal = rawHref.startsWith('#');
  $: href = internal ? rawHref : safeUrl(rawHref);
  $: media = block.media && typeof block.media === 'object' && !Array.isArray(block.media)
    ? block.media as Record<string, unknown>
    : {};
  $: rawThumbnail = block.thumbnail ?? block.image ?? block.image_url ?? media.thumbnail ?? media.src ?? media.url;
  $: thumbnail = typeof rawThumbnail === 'string' ? safeImageUrl(rawThumbnail) : '';
  $: thumbnailAlt = blockText(block, 'thumbnail_alt') || blockText(block, 'alt') || '';
</script>

<a
  class="rich-link-preview"
  data-rich-block-type={type}
  href={href || undefined}
  target={internal ? undefined : '_blank'}
  rel={internal ? undefined : 'noreferrer'}
>
  {#if thumbnail}
    <img class="rich-link-preview-thumbnail" src={thumbnail} alt={thumbnailAlt} loading="lazy" />
  {/if}
  <span class="rich-link-preview-body">
  <!-- This whole preview is one <a>; every field below uses the "no links"
       inline renderer so authored markdown links in these fields never
       produce an invalid nested <a>, which would also hijack the click
       away from this block's own href. -->
  <span>{@html renderInlineMarkdownNoLinks(blockText(block, 'site') || blockText(block, 'domain') || 'Source')}</span>
   <strong>{@html renderInlineMarkdownNoLinks(blockTitle(block) || href || 'Unsafe link')}</strong>
   {#if blockText(block, 'description')}<small>{@html renderInlineMarkdownNoLinks(blockText(block, 'description'))}</small>{/if}
  </span>
</a>
