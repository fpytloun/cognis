<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { blockText, type RichBlock } from '$lib/rich-deliverable';

  export let block: RichBlock;
</script>

<blockquote class="rich-quote" data-rich-block-type="quote">
  <p>{@html renderInlineMarkdown(blockText(block) || blockText(block, 'quote'))}</p>
  {#if blockText(block, 'byline')}<footer>{@html renderInlineMarkdown(blockText(block, 'byline'))}</footer>{/if}
</blockquote>
