<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks } from '$lib/markdown';
  import { blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { resolveSourceRefs } from '../evidence-helpers';
  import { orderedSources, safeSourceUrl, sourceDetails } from '../publication';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];

  $: globalSources = orderedSources(sources);
  $: sourceRefs = block.sources ?? block.source_ids ?? block.citations;
  $: resolvedSources = sourceRefs === undefined
    ? globalSources
    : resolveSourceRefs(sourceRefs, globalSources);
</script>

<section class="rich-sources" data-rich-block-type="source_list">
  {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
  <ol>
    {#each resolvedSources as source}
       {@const href = safeSourceUrl(source)}
       <li>
         <a href={href || undefined} target="_blank" rel="noreferrer">{@html renderInlineMarkdownNoLinks(source.title)}</a>
         {#if sourceDetails(source)}<span>{sourceDetails(source)}</span>{/if}
         {#if source.snippet}<details><summary>Source preview</summary><p>{@html renderInlineMarkdown(source.snippet)}</p></details>{/if}
      </li>
    {/each}
  </ol>
</section>
