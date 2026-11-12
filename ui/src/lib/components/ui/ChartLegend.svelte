<script lang="ts">
  export interface LegendItem { key: string; label: string; color: string; hidden?: boolean }

  /**
   * Chart series legend. Clicking a series toggles it; hovering highlights it.
   * The last visible series cannot be hidden.
   */
  let { items, label, ontoggle, onhover }: {
    items: LegendItem[];
    label: string;
    ontoggle?: (key: string) => void;
    onhover?: (key: string | null) => void;
  } = $props();
  const visible = $derived(items.filter((item) => !item.hidden).length);
</script>

<ul class="m-0 flex list-none flex-wrap gap-x-3 gap-y-1 p-0 text-xs text-secondary" aria-label={label}>
  {#each items as item (item.key)}
    <li>
      {#if ontoggle}
        <button type="button" class="inline-flex min-h-[28px] items-center gap-1.5 rounded-md px-1 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent aria-[pressed=false]:opacity-45"
          aria-pressed={!item.hidden} disabled={!item.hidden && visible <= 1} title={item.hidden ? `Show ${item.label}` : `Hide ${item.label}`}
          onclick={() => ontoggle(item.key)} onmouseenter={() => onhover?.(item.key)} onmouseleave={() => onhover?.(null)}
          onfocus={() => onhover?.(item.key)} onblur={() => onhover?.(null)}>
          <i class="h-2 w-2 shrink-0 rounded-sm" style:background={item.color} aria-hidden="true"></i>{item.label}
        </button>
      {:else}
        <span class="inline-flex items-center gap-1.5"><i class="h-2 w-2 shrink-0 rounded-sm" style:background={item.color} aria-hidden="true"></i>{item.label}</span>
      {/if}
    </li>
  {/each}
</ul>
