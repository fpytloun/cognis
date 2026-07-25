<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks, stripMarkdown } from '$lib/markdown';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { valueText } from '../block-helpers';
  import {
    matrixColumns,
    matrixRows,
    recommendedRow,
    rowEvidence,
    resolveSourceRefs,
    sortMatrixRows,
    type MatrixSort,
    type SortDirection,
  } from '../evidence-helpers';
  import { citationNumber, orderedSources } from '../publication';
  import { getPublicationContext } from '../publication-context';

  export let block: RichBlock;
  export let type = 'decision_matrix';
  export let sources: Record<string, unknown>[] = [];

  const publicationContext = getPublicationContext();
  let sort: MatrixSort | null = null;
  let expandedRow = '';

  $: rows = matrixRows(block);
  $: columns = matrixColumns(block, rows);
  $: sortedRows = sortMatrixRows(rows, sort);
  $: globalSources = orderedSources(sources);
  $: scopedSources = block.sources === undefined ? [] : resolveSourceRefs(block.sources, globalSources);
  $: availableSources = scopedSources.length > 0 ? orderedSources(scopedSources.map((source) => source.raw)) : globalSources;
  $: citationRegistry = $publicationContext;
  $: hasEvidence = rows.some((row) => rowEvidence(row).length > 0 || rowSources(row).length > 0);

  function rowKey(row: Record<string, unknown>, index: number): string {
    return String(row.id ?? row.key ?? row.option ?? row.name ?? row.title ?? index);
  }

  function sortBy(key: string) {
    const direction: SortDirection = sort?.key === key && sort.direction === 'asc' ? 'desc' : 'asc';
    sort = { key, direction };
  }

  function rowSources(row: Record<string, unknown>) {
    return resolveSourceRefs(row.source_ids ?? row.citations ?? row.sources, availableSources);
  }

  function sourceLabel(source: ReturnType<typeof rowSources>[number], index: number): string {
    const globalIndex = availableSources.findIndex((candidate) => candidate.key === source.key);
    return String(citationNumber(citationRegistry, source) || (globalIndex >= 0 ? globalIndex + 1 : index + 1));
  }
</script>

<section class="rich-table-card rich-decision-matrix" data-rich-block-type={type}>
  {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
  {#if blockText(block, 'description')}<p>{@html renderInlineMarkdown(blockText(block, 'description'))}</p>{/if}
  <div class="rich-table-wrap">
    <table>
      <thead>
        <tr>
          {#each columns as col}
            <th class:align-right={col.align === 'right'}>
              <button type="button" on:click={() => sortBy(col.key)} aria-label={`Sort by ${stripMarkdown(col.label)}`}>
                {@html renderInlineMarkdownNoLinks(col.label)}
                {#if sort?.key === col.key}<span aria-hidden="true">{sort.direction === 'asc' ? '↑' : '↓'}</span>{/if}
              </button>
            </th>
          {/each}
          {#if hasEvidence}<th><span>Evidence</span></th>{/if}
        </tr>
      </thead>
      <tbody>
        {#each sortedRows as row, index}
          {@const key = rowKey(row, index)}
          {@const evidence = rowEvidence(row)}
          {@const rowSourceList = rowSources(row)}
          <tr class:recommended={recommendedRow(row)}>
            {#each columns as col, columnIndex}
              <td data-label={stripMarkdown(col.label)} class:align-right={col.align === 'right'}>
                {#if recommendedRow(row) && (col.key === 'option' || col.key === 'name' || col.key === 'title' || columnIndex === 0)}
                  <strong>{@html renderInlineMarkdown(valueText(row[col.key]))}</strong><span class="rich-recommendation">Recommended</span>
                {:else}
                  {@html renderInlineMarkdown(valueText(row[col.key]))}
                {/if}
              </td>
            {/each}
            {#if hasEvidence}<td data-label="Evidence">
              {#if evidence.length > 0}
                <button type="button" class="rich-row-evidence-toggle" aria-expanded={expandedRow === key} on:click={() => expandedRow = expandedRow === key ? '' : key}>
                  {expandedRow === key ? 'Hide evidence' : 'Show evidence'}
                </button>
              {:else if rowSourceList.length > 0}
                <button type="button" class="rich-row-evidence-toggle" aria-expanded={expandedRow === key} on:click={() => expandedRow = expandedRow === key ? '' : key}>
                  {expandedRow === key ? 'Hide sources' : 'Show sources'}
                </button>
              {:else}
                —
              {/if}
            </td>{/if}
          </tr>
          {#if expandedRow === key && (evidence.length > 0 || rowSourceList.length > 0)}
            <tr class="rich-row-evidence">
              <td colspan={columns.length + (hasEvidence ? 1 : 0)}>
                {#if evidence.length > 0}<ul>
                  {#each evidence as item}
                    <li>
                      <strong>{@html renderInlineMarkdown(String(item.title ?? item.label ?? 'Evidence'))}</strong>
                      {#if item.text || item.content || item.quote}<p>{@html renderInlineMarkdown(String(item.text ?? item.content ?? item.quote))}</p>{/if}
                    </li>
                  {/each}
                </ul>{/if}
                {#if rowSourceList.length > 0}
                  <ul class="rich-row-evidence-sources">
                    {#each rowSourceList as source, sourceIndex}
                      <li>
                        {#if source.url}
                          <a href={source.url} target="_blank" rel="noreferrer">[{sourceLabel(source, sourceIndex)}] {@html renderInlineMarkdownNoLinks(source.title)}</a>
                        {:else}
                          <span>[{sourceLabel(source, sourceIndex)}] {@html renderInlineMarkdown(source.title)}</span>
                        {/if}
                      </li>
                    {/each}
                  </ul>
                {/if}
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  </div>
</section>
