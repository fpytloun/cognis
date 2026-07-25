<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { listBackedItems } from '../block-helpers';

  export let block: RichBlock;
  export let type = 'timeline';

  $: items = listBackedItems(block);
</script>

<section class="rich-timeline" class:rich-steps={type === 'steps'} data-rich-block-type={type}>
  {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
  <ol>
    {#each items as item, index}
      <li class="tone-{String(item.tone ?? item.status ?? 'neutral')}">
        <span>{@html renderInlineMarkdown(String(item.time ?? item.step ?? index + 1))}</span>
        <div>
          <strong>{@html renderInlineMarkdown(String(item.title ?? item.label ?? `Step ${index + 1}`))}</strong>
          {#if item.content || item.description}<p>{@html renderInlineMarkdown(String(item.content ?? item.description))}</p>{/if}
        </div>
      </li>
    {/each}
  </ol>
</section>
