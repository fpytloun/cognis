<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks } from '$lib/markdown';
  import { blockText, blockTitle, blockType, type RichBlock } from '$lib/rich-deliverable';
  import { objectList, stringList, valueText } from '../block-helpers';
  import { claimItems, confidenceLabel, confidencePercent, evidenceItems, normalizeSources, resolveSourceRefs, sourceMeta } from '../evidence-helpers';
  import { citationNumber, orderedSources } from '../publication';
  import { getPublicationContext } from '../publication-context';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];

  const publicationContext = getPublicationContext();
  $: claims = claimItems(block);
  $: globalSources = normalizeSources(sources);
  $: scopedSources = block.sources === undefined ? [] : resolveSourceRefs(block.sources, globalSources);
  $: availableSources = orderedSources((scopedSources.length > 0 ? scopedSources : globalSources).map((source) => source.raw));
  $: citationRegistry = $publicationContext;
  $: caveats = stringList(block.caveats);
  $: contradictions = stringList(block.contradictions);
</script>

<section class="rich-evidence-report" data-rich-block-type={blockType(block) === 'claim_cards' ? 'claim_cards' : 'evidence_report'}>
  {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
  {#if blockText(block, 'description')}<p class="rich-evidence-description">{@html renderInlineMarkdown(blockText(block, 'description'))}</p>{/if}

  <div class="rich-claim-grid">
    {#each claims as claim}
      {@const percent = confidencePercent(claim.confidence ?? claim.score)}
      {@const snippets = evidenceItems(claim.evidence ?? claim.snippets)}
      {@const claimSources = resolveSourceRefs(claim.source_ids ?? claim.citations ?? claim.sources, availableSources)}
      {@const claimBody = claim.content ?? claim.summary ?? (claim.title ? claim.claim : '')}
      <article class="rich-claim-card">
        <header>
          <span>{@html renderInlineMarkdown(String(claim.label ?? claim.category ?? 'Claim'))}</span>
          <strong>{@html renderInlineMarkdown(String(claim.title ?? claim.claim ?? ''))}</strong>
        </header>
        {#if claimBody}<p>{@html renderInlineMarkdown(String(claimBody))}</p>{/if}
        <div class="rich-confidence" aria-label={`Confidence ${confidenceLabel(claim.confidence ?? claim.score)}`}>
          <span>{confidenceLabel(claim.confidence ?? claim.score)}</span>
          <div><i style={`width: ${percent}%`}></i></div>
        </div>

        {#if snippets.length > 0}
          <details>
            <summary>Evidence snippets</summary>
            <ul>
              {#each snippets as snippet}
                <li>
                  <blockquote>{@html renderInlineMarkdown(String(snippet.text ?? snippet.quote ?? snippet.content ?? ''))}</blockquote>
                  {#if snippet.source || snippet.url}<small>{valueText(snippet.source ?? snippet.url)}</small>{/if}
                </li>
              {/each}
            </ul>
          </details>
        {/if}

        {#if claimSources.length > 0}
          <div class="rich-claim-sources">
            {#each claimSources as source}
              {#if source.url}
                <a href={source.url} target="_blank" rel="noreferrer">[{citationNumber(citationRegistry, source)}] {@html renderInlineMarkdownNoLinks(source.title)}</a>
              {:else}
                <span>[{citationNumber(citationRegistry, source)}] {@html renderInlineMarkdown(source.title)}</span>
              {/if}
              {#if sourceMeta(source)}<small>{sourceMeta(source)}</small>{/if}
            {/each}
          </div>
        {/if}
      </article>
    {/each}
  </div>

  {#if caveats.length > 0 || contradictions.length > 0}
    <div class="rich-evidence-caveats">
      {#if caveats.length > 0}
        <section>
          <strong>Caveats</strong>
          <ul>{#each caveats as caveat}<li>{@html renderInlineMarkdown(caveat)}</li>{/each}</ul>
        </section>
      {/if}
      {#if contradictions.length > 0}
        <section>
          <strong>Contradictions</strong>
          <ul>{#each contradictions as contradiction}<li>{@html renderInlineMarkdown(contradiction)}</li>{/each}</ul>
        </section>
      {/if}
    </div>
  {/if}
</section>
