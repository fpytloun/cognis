<script lang="ts">
  import { blockChildren, blockText, blockTitle, type RichBlock, type RichMediaUrlFor } from '$lib/rich-deliverable';
  import RichBlockList from '../RichBlockList.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = blockChildren(block);
</script>

<section
  class="rich-diagram"
  data-mermaid-id={typeof block.__publication_mermaid_id === 'string' ? block.__publication_mermaid_id : undefined}
  data-rich-block-type="mermaid"
>
  {#if blockTitle(block)}<h4>{blockTitle(block)}</h4>{/if}
  <pre class="rich-code" data-mermaid-source>{blockText(block, 'source') || blockText(block, 'code') || blockText(block)}</pre>
  <RichBlockList blocks={children} {sources} {mediaUrlFor} />
</section>
