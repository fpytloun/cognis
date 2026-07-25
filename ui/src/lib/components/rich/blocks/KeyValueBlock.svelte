<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { listBackedItems, valueText } from '../block-helpers';

  export let block: RichBlock;
  export let type = 'kv';

  $: items = listBackedItems(block);
</script>

<section class="rich-kv" data-rich-block-type={type}>
  {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
  <dl>
    {#each items as item}
      <div>
        <dt>{@html renderInlineMarkdown(String(item.label ?? item.key ?? item.name ?? ''))}</dt>
        <dd>{@html renderInlineMarkdown(valueText(item.value ?? item.text ?? item.content))}</dd>
      </div>
    {/each}
  </dl>
</section>
