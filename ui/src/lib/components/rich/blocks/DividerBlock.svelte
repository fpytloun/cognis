<script lang="ts">
  import { blockChildren, type RichBlock, type RichMediaUrlFor } from '$lib/rich-deliverable';
  import RichBlockList from '../RichBlockList.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = blockChildren(block);
</script>

<div data-rich-block-type="divider">
  <hr class="rich-divider" />
  <RichBlockList blocks={children} {sources} {mediaUrlFor} />
</div>
