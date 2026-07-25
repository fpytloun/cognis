<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks } from '$lib/markdown';
  import {
    blockChildren,
    blockMedia,
    blockText,
    blockTitle,
    safeUrl,
    type RichBlock,
    type RichMediaUrlFor,
  } from '$lib/rich-deliverable';
  import RichBlockList from '../RichBlockList.svelte';
  import RichMedia from '../RichMedia.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let type = 'accordion';
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = blockChildren(block);
</script>

<div class="rich-disclosure-group" data-rich-block-type={type}>
  {#each children as child}
    <details open={type === 'tabs'} class="rich-panel">
      <!-- Everything in <summary> uses the "no links" renderer: it is
           itself an interactive disclosure trigger, so a nested <a> here
           would be invalid nested interactive content. -->
      <summary>
        <span>{@html renderInlineMarkdownNoLinks(blockTitle(child) || 'Details')}</span>
        {#if blockText(child, 'summary') || blockText(child, 'dek')}<small>{@html renderInlineMarkdownNoLinks(blockText(child, 'summary') || blockText(child, 'dek'))}</small>{/if}
      </summary>
      <div class="rich-panel-context">
        <RichMedia
          media={blockMedia(child)}
          fallbackAlt={blockTitle(child)}
          {mediaUrlFor}
          loading="lazy"
          decoding="async"
        />
        {#if blockText(child)}<p class="rich-disclosure-body">{@html renderInlineMarkdown(blockText(child))}</p>{/if}
        {#if safeUrl(child.url ?? child.href)}
          <a
            class="rich-disclosure-source"
            href={safeUrl(child.url ?? child.href)}
            target="_blank"
            rel="noopener noreferrer"
          >Open source</a>
        {/if}
        <RichBlockList blocks={blockChildren(child)} {sources} {mediaUrlFor} />
      </div>
    </details>
  {/each}
</div>
