<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks, stripMarkdown } from '$lib/markdown';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { asTypedCell, blockSpan, blockSurface, tableColumns, tableRows, valueText, type TypedTableCell } from '../block-helpers';
  import { sortMatrixRows, type MatrixSort, type SortDirection } from '../evidence-helpers';
  import RichProgressBar from '../RichProgressBar.svelte';

  export let block: RichBlock;
  export let type = 'table';

  let sort: MatrixSort | null = null;

  $: rows = tableRows(block);
  $: columns = tableColumns(block, rows);
  $: sortedRows = sortMatrixRows(rows, sort);
  $: surface = blockSurface(block);
  $: span = blockSpan(block);

  function sortBy(key: string) {
    const direction: SortDirection = sort?.key === key && sort.direction === 'asc' ? 'desc' : 'asc';
    sort = { key, direction };
  }

  function ariaSort(key: string): 'ascending' | 'descending' | 'none' {
    if (sort?.key !== key) return 'none';
    return sort.direction === 'asc' ? 'ascending' : 'descending';
  }

  function cellAlign(colAlign: string | undefined, cell: TypedTableCell | null): 'start' | 'center' | 'end' | '' {
    if (cell?.align) return cell.align;
    if (colAlign === 'right') return 'end';
    if (colAlign === 'left') return 'start';
    if (colAlign === 'center') return 'center';
    return '';
  }

  function progressLabel(cell: TypedTableCell): string {
    if (cell.label) return cell.label;
    const max = Number.isFinite(cell.max) ? cell.max : 100;
    return `${valueText(cell.value)} / ${max}`;
  }
</script>

<section class="rich-table-card" data-rich-block-type={type} data-rich-surface={surface} style:grid-column={span ? `span ${span}` : undefined}>
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
          <tr>
            {#each columns as col}
              {@const cell = asTypedCell(row[col.key])}
              {@const align = cellAlign(col.align, cell)}
              <td
                data-label={stripMarkdown(col.label)}
                data-cell-type={cell?.type}
                class:align-right={col.align === 'right'}
                class:align-start={align === 'start'}
                class:align-center={align === 'center'}
                class:align-end={align === 'end'}
              >
                {#if !cell}
                  {@html renderInlineMarkdown(valueText(row[col.key]))}
                {:else if cell.type === 'badge'}
                  <span class="rich-table-badge tone-{cell.tone ?? 'neutral'}">{@html renderInlineMarkdownNoLinks(cell.label ?? valueText(cell.value))}</span>
                {:else if cell.type === 'progress'}
                  <RichProgressBar value={cell.value} max={cell.max} label={progressLabel(cell)} tone={cell.tone ?? 'neutral'} />
                {:else if cell.type === 'code'}
                  <code class="rich-table-code">{valueText(cell.value)}</code>
                {:else}
                  <span class="rich-table-cell-text emphasis-{cell.emphasis ?? 'normal'}">{@html renderInlineMarkdown(cell.label ?? valueText(cell.value))}</span>
                {/if}
              </td>
            {/each}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>
