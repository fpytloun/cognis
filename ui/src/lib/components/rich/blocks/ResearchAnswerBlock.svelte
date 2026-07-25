<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks, stripMarkdown } from '$lib/markdown';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { objectList, stringList } from '../block-helpers';
  import { resolveSourceRefs, type NormalizedSource } from '../evidence-helpers';
  import { citationNumber, orderedSources, safeSourceUrl, sourceDetails, sourceIdentity } from '../publication';
  import { getPublicationContext } from '../publication-context';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];

  const publicationContext = getPublicationContext();
  let openCitation = '';

  $: globalSources = orderedSources(sources);
  $: scopedSources = block.sources === undefined ? [] : resolveSourceRefs(block.sources, globalSources);
  $: availableSources = scopedSources.length > 0 ? orderedSources(scopedSources.map((source) => source.raw)) : globalSources;
  $: paragraphs = objectList(block.paragraphs ?? block.items);
  $: globalRegistry = $publicationContext;
  $: answerSources = resolveSourceRefs(block.source_ids ?? block.citations, availableSources);
  $: citationOrder = buildCitationOrder(paragraphs, answerSources, availableSources);
  $: fallbackAnswer = blockText(block, 'answer') || blockText(block);
  $: keyPoints = stringList(block.key_points ?? block.highlights);

  function citationId(source: NormalizedSource, paragraphIndex: string | number, sourceIndex: number): string {
    const local = `${String(block.__publication_block ?? 'block')}-${paragraphIndex}-${sourceIndex}`;
    return globalRegistry.namespace ? `${globalRegistry.namespace}-${local}` : local;
  }

  function sourceLabel(source: NormalizedSource, fallbackIndex: number): string {
    return String(citationNumber(globalRegistry, source) || fallbackIndex + 1);
  }

  function buildCitationOrder(
    items: Record<string, unknown>[],
    directAnswerSources: NormalizedSource[],
    available: NormalizedSource[],
  ): NormalizedSource[] {
    const result: NormalizedSource[] = [...directAnswerSources];
    for (const paragraph of items) {
      for (const source of resolveSourceRefs(paragraph.source_ids ?? paragraph.citations ?? paragraph.sources, available)) {
        if (!result.some((item) => sourceIdentity(item) === sourceIdentity(source))) result.push(source);
      }
    }
    return result;
  }

  function toggleCitation(id: string) {
    openCitation = openCitation === id ? '' : id;
  }

  function closeOnEscape(event: KeyboardEvent) {
    if (event.key === 'Escape') openCitation = '';
  }
</script>

<section class="rich-research-answer" data-rich-block-type="research_answer">
  <div class="rich-answer-main">
    {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
    {#if blockText(block, 'description')}<p class="rich-answer-description">{@html renderInlineMarkdown(blockText(block, 'description'))}</p>{/if}

    <div class="rich-answer-content">
      {#if paragraphs.length > 0}
        <ul class="rich-answer-claims">
        {#each paragraphs as paragraph, paragraphIndex}
          {@const citedSources = resolveSourceRefs(paragraph.source_ids ?? paragraph.citations ?? paragraph.sources, availableSources)}
          <li class="rich-answer-claim">
            <span class="rich-answer-claim-marker" aria-hidden="true"></span>
            <p>
              {@html renderInlineMarkdown(String(paragraph.text ?? paragraph.content ?? ''))}
            {#each citedSources as source, sourceIndex}
              {@const id = citationId(source, paragraphIndex, sourceIndex)}
              <span class="rich-citation">
                <button
                  type="button"
                  aria-label={`Citation ${sourceLabel(source, sourceIndex)}: ${stripMarkdown(source.title)}`}
                  aria-haspopup="dialog"
                  aria-expanded={openCitation === id}
                  aria-controls={`rich-citation-${id}`}
                  on:click={() => toggleCitation(id)}
                  on:keydown={closeOnEscape}
                >
                  [{sourceLabel(source, sourceIndex)}]
                </button>
                {#if openCitation === id}
                  <span
                    id={`rich-citation-${id}`}
                    class="rich-source-popover"
                    role="dialog"
                    tabindex="-1"
                    aria-label={`Source ${stripMarkdown(source.title)}`}
                    on:keydown={closeOnEscape}
                  >
                    <strong>{@html renderInlineMarkdown(source.title)}</strong>
                    {#if sourceDetails(source)}<small>{sourceDetails(source)}</small>{/if}
                    {#if source.snippet}<span>{@html renderInlineMarkdown(source.snippet)}</span>{/if}
                    {#if source.url}<a href={source.url} target="_blank" rel="noreferrer">Open source</a>{/if}
                  </span>
                {/if}
              </span>
            {/each}
            </p>
          </li>
        {/each}
        </ul>
      {:else if fallbackAnswer}
        <p>
          {@html renderInlineMarkdown(fallbackAnswer)}
          {#each answerSources as source, sourceIndex}
            {@const id = citationId(source, 'answer', sourceIndex)}
            <span class="rich-citation">
              <button
                type="button"
                aria-label={`Citation ${sourceLabel(source, sourceIndex)}: ${stripMarkdown(source.title)}`}
                aria-haspopup="dialog"
                aria-expanded={openCitation === id}
                aria-controls={`rich-citation-${id}`}
                on:click={() => toggleCitation(id)}
                on:keydown={closeOnEscape}
              >
                [{sourceLabel(source, sourceIndex)}]
              </button>
              {#if openCitation === id}
                <span
                  id={`rich-citation-${id}`}
                  class="rich-source-popover"
                  role="dialog"
                  tabindex="-1"
                  aria-label={`Source ${stripMarkdown(source.title)}`}
                  on:keydown={closeOnEscape}
                >
                  <strong>{@html renderInlineMarkdown(source.title)}</strong>
                  {#if sourceDetails(source)}<small>{sourceDetails(source)}</small>{/if}
                  {#if source.snippet}<span>{@html renderInlineMarkdown(source.snippet)}</span>{/if}
                  {#if source.url}<a href={source.url} target="_blank" rel="noreferrer">Open source</a>{/if}
                </span>
              {/if}
            </span>
          {/each}
        </p>
      {/if}
    </div>

    {#if keyPoints.length > 0}
      <ul class="rich-answer-points">
        {#each keyPoints as point}<li>{@html renderInlineMarkdown(point)}</li>{/each}
      </ul>
    {/if}
  </div>

  {#if citationOrder.length > 0}
    <aside class="rich-source-rail" aria-label="Research sources">
      <strong>Sources</strong>
      <ol>
        {#each citationOrder as source, index}
          <li>
            <span>[{sourceLabel(source, index)}]</span>
            {#if safeSourceUrl(source)}
              <a href={safeSourceUrl(source)} target="_blank" rel="noreferrer">{@html renderInlineMarkdownNoLinks(source.title)}</a>
            {:else}
              <em>{@html renderInlineMarkdown(source.title)}</em>
            {/if}
            {#if sourceDetails(source)}<small>{sourceDetails(source)}</small>{/if}
            {#if source.snippet}
              <details><summary>Source preview</summary><p>{@html renderInlineMarkdown(source.snippet)}</p></details>
            {/if}
          </li>
        {/each}
      </ol>
    </aside>
  {/if}
</section>
