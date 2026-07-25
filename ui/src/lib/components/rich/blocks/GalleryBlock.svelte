<script lang="ts">
  import { blockChildren, type RichBlock, type RichMediaUrlFor } from '$lib/rich-deliverable';
  import { galleryBlocks } from '../block-helpers';
  import RichBlockList from '../RichBlockList.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = galleryBlocks(blockChildren(block));
</script>

<div class="rich-grid rich-gallery" data-rich-block-type="gallery">
  <RichBlockList blocks={children} {sources} {mediaUrlFor} />
</div>
