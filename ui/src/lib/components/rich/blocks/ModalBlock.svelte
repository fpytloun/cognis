<script lang="ts">
  import { renderInlineMarkdownNoLinks } from '$lib/markdown';
  import { blockChildren, blockTitle, type RichBlock, type RichMediaUrlFor } from '$lib/rich-deliverable';
  import RichBlockList from '../RichBlockList.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = blockChildren(block);
</script>

<details class="rich-panel" data-rich-block-type="modal">
  <!-- <summary> is itself an interactive disclosure trigger; a nested
       markdown link inside its text would be invalid nested interactive
       content, so this uses the "no links" inline renderer. -->
  <summary>{@html renderInlineMarkdownNoLinks(blockTitle(block) || 'Open detail')}</summary>
  <RichBlockList blocks={children} {sources} {mediaUrlFor} />
</details>
