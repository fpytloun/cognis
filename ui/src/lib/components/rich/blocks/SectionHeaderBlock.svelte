<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { blockSpan, blockSurface, blockTone } from '../block-helpers';

  export let block: RichBlock;

  $: tone = blockTone(block);
  $: surface = blockSurface(block);
  $: span = blockSpan(block);
  $: status = blockText(block, 'status');
</script>

<header
  class="rich-section-header"
  data-rich-block-type="section_header"
  data-rich-surface={surface}
  style:grid-column={span ? `span ${span}` : undefined}
>
  <div>
    {#if blockText(block, 'eyebrow')}<span>{@html renderInlineMarkdown(blockText(block, 'eyebrow'))}</span>{/if}
    {#if blockTitle(block)}
      <svelte:element this={block.__document_h1 ? 'h1' : 'h3'}>{@html renderInlineMarkdown(blockTitle(block))}</svelte:element>
    {/if}
    {#if blockText(block, 'subtitle')}<p>{@html renderInlineMarkdown(blockText(block, 'subtitle'))}</p>{/if}
  </div>
  {#if status}<strong class="rich-status-pill tone-{tone}">{@html renderInlineMarkdown(status)}</strong>{/if}
</header>

<style>
  .rich-section-header {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--rich-space-3);
    border-bottom: 1px solid var(--rich-line);
    padding-bottom: var(--rich-space-3);
  }

  .rich-section-header > div {
    min-width: 0;
  }

  .rich-section-header span {
    color: var(--rich-accent-soft);
    font-size: var(--rich-fs-2xs);
    font-weight: var(--rich-fw-bold);
    letter-spacing: var(--rich-ls-wider);
    text-transform: uppercase;
  }

  .rich-section-header h3 {
    margin: .2rem 0 0;
    color: var(--rich-text);
    font-size: var(--rich-fs-lg);
    letter-spacing: var(--rich-ls-tight);
  }

  .rich-section-header p {
    margin: .3rem 0 0;
    max-width: 48rem;
    color: var(--rich-text-secondary);
    line-height: var(--rich-lh-relaxed);
  }

  .rich-status-pill {
    display: inline-flex;
    align-self: flex-start;
    border: 1px solid var(--rich-tone-border, var(--rich-tone-neutral-border));
    border-radius: var(--rich-radius-pill);
    background: var(--rich-tone-bg, var(--rich-tone-neutral-bg));
    color: var(--rich-tone-fg, var(--rich-tone-neutral-fg));
    padding: .32rem .65rem;
    font-size: var(--rich-fs-2xs);
    font-weight: var(--rich-fw-bold);
    letter-spacing: var(--rich-ls-wide);
    text-transform: uppercase;
    white-space: nowrap;
  }
</style>
