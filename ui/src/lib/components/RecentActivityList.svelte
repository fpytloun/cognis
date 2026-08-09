<script lang="ts">
  export type RecentActivityItem = { id: string; title: string; detail?: string; timestamp?: string };
  let { items = [], onSelect }: { items?: RecentActivityItem[]; onSelect?: (item: RecentActivityItem) => void } = $props();
</script>
<ol class="divide-y divide-slate-800/80" data-testid="recent-activity-list">
  {#each items as item (item.id)}
    <li><button type="button" class="w-full min-w-0 px-2 py-2 text-left hover:bg-slate-800/50" onclick={() => onSelect?.(item)}>
      <span class="block truncate text-sm text-slate-200">{item.title}</span>
      {#if item.detail}<span class="block truncate text-xs text-slate-500">{item.detail}</span>{/if}
    </button></li>
  {/each}
</ol>
