<script lang="ts" module>
  export interface DataColumn {
    key: string;
    label: string;
    numeric?: boolean;
    /** Sortable columns need `value` to return a comparable value. */
    sortable?: boolean;
  }
</script>

<script lang="ts" generics="T">
  import type { Snippet } from 'svelte';

  /**
   * Lookup table: sticky header, optional client-side sort, right-aligned
   * tabular numbers. Unknown values sort last in either direction.
   */
  let { columns, rows, rowKey, cell, value, label, empty = 'No data', sortKey = null, descending = true, onrowclick, selected = null }: {
    columns: DataColumn[];
    rows: T[];
    rowKey: (row: T, index: number) => string;
    cell: Snippet<[T, DataColumn]>;
    value?: (row: T, key: string) => number | string | null | undefined;
    label: string;
    empty?: string;
    sortKey?: string | null;
    descending?: boolean;
    onrowclick?: (row: T) => void;
    selected?: string | null;
  } = $props();

  let activeKey = $state<string | null>(null);
  let activeDescending = $state(true);
  $effect.pre(() => { activeKey = sortKey; activeDescending = descending; });

  const sorted = $derived.by(() => {
    if (!activeKey || !value) return rows;
    const key = activeKey;
    return [...rows].sort((a, b) => {
      const av = value(a, key), bv = value(b, key);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const order = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv));
      return activeDescending ? -order : order;
    });
  });
  function sortBy(column: DataColumn) {
    if (activeKey === column.key) activeDescending = !activeDescending;
    else { activeKey = column.key; activeDescending = Boolean(column.numeric); }
  }
</script>

<div class="max-h-[640px] min-w-0 overflow-auto">
  <table class="w-full border-collapse text-sm" aria-label={label}>
    <thead class="sticky top-0 z-[1] bg-surface">
      <tr>
        {#each columns as column (column.key)}
          <th scope="col" class={['whitespace-nowrap border-b border-separator px-3 py-2 text-xs font-semibold text-secondary', column.numeric ? 'text-right' : 'text-left']}
            aria-sort={activeKey === column.key ? (activeDescending ? 'descending' : 'ascending') : undefined}>
            {#if column.sortable && value}
              <button type="button" class="inline-flex min-h-[40px] items-center gap-1 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent md:min-h-0" onclick={() => sortBy(column)}>
                {column.label}<span aria-hidden="true" class="w-2">{activeKey === column.key ? (activeDescending ? '↓' : '↑') : ''}</span>
              </button>
            {:else}{column.label}{/if}
          </th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each sorted as row, index (rowKey(row, index))}
        <tr class={['border-b border-separator/70 hover:bg-surface-hover', onrowclick && 'cursor-pointer', selected === rowKey(row, index) && 'bg-accent-subtle']}
          onclick={onrowclick ? () => onrowclick(row) : undefined}>
          {#each columns as column, columnIndex (column.key)}
            {#if columnIndex === 0}
              <th scope="row" class={['px-3 py-2.5 text-left align-middle font-normal', column.numeric ? 'whitespace-nowrap text-right tabular-nums' : 'min-w-0']}>{@render cell(row, column)}</th>
            {:else}
              <td class={['px-3 py-2.5 align-middle', column.numeric ? 'whitespace-nowrap text-right tabular-nums' : 'min-w-0']}>{@render cell(row, column)}</td>
            {/if}
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
  {#if !rows.length}<p class="py-6 text-center text-sm text-secondary">{empty}</p>{/if}
</div>
