<script lang="ts">
  import { renderInlineMarkdown, renderMarkdown } from '$lib/markdown';
  import { blockBody, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { blockTone } from '../block-helpers';

  export let block: RichBlock;

  $: tone = blockTone(block);
</script>

<aside class="rich-callout tone-{tone}" data-rich-block-type="callout">
  <div class="rich-callout-icon" aria-hidden="true"></div>
  <div>
    <strong>{@html renderInlineMarkdown(blockTitle(block) || 'Note')}</strong>
    <div class="rich-markdown">{@html renderMarkdown(blockBody(block))}</div>
  </div>
</aside>
