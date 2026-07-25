<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks, stripMarkdown } from '$lib/markdown';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { tableColumns, tableRows, valueText } from '../block-helpers';
  import { sortMatrixRows, type MatrixSort, type SortDirection } from '../evidence-helpers';

  export let block: RichBlock;
  export let type = 'table';

  let sort: MatrixSort | null = null;

  $: rows = tableRows(block);
  $: columns = tableColumns(block, rows);
  $: sortedRows = sortMatrixRows(rows, sort);

  function sortBy(key: string) {
    const direction: SortDirection = sort?.key === key && sort.direction === 'asc' ? 'desc' : 'asc';
    sort = { key, direction };
  }

  function ariaSort(key: string): 'ascending' | 'descending' | 'none' {
    if (sort?.key !== key) return 'none';
    return sort.direction === 'asc' ? 'ascending' : 'descending';
  }
</script>

<section class="rich-table-card" data-rich-block-type={type}>
  {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
  {#if blockText(block, 'description')}<p>{@html renderInlineMarkdown(blockText(block, 'description'))}</p>{/if}
  <div class="rich-table-wrap">
    <table>
      {#if blockText(block, 'caption') || block.__table_number}
        <caption>{#if block.__table_number}<strong>Table {String(block.__table_number)}. </strong>{/if}{@html renderInlineMarkdown(blockText(block, 'caption'))}</caption>
      {/if}
      <thead>
        <tr>
          {#each columns as col}
            <th class:align-right={col.align === 'right'} aria-sort={ariaSort(col.key)}>
              <button type="button" class="rich-table-sort-button" on:click={() => sortBy(col.key)} aria-label={`Sort by ${stripMarkdown(col.label)}`}>
                <span>{@html renderInlineMarkdownNoLinks(col.label)}</span>
                {#if sort?.key === col.key}<span aria-hidden="true">{sort.direction === 'asc' ? '↑' : '↓'}</span>{/if}
              </button>
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each sortedRows as row}
          <tr>{#each columns as col}<td data-label={stripMarkdown(col.label)} class:align-right={col.align === 'right'}>{@html renderInlineMarkdown(valueText(row[col.key]))}</td>{/each}</tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>
