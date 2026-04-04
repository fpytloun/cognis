<script lang="ts">
  import Card from '$lib/components/ui/Card.svelte';
  import type { SummaryCardItem } from '$lib/channels';

  export let items: SummaryCardItem[] = [];
</script>

<div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
  {#each items as item (item.id)}
    <Card class="p-4">
      <p class="text-xs uppercase tracking-[0.22em] text-slate-500">{item.label}</p>
      <p class="mt-3 text-3xl font-semibold text-white">{item.value}</p>
      <p class="mt-2 text-sm text-slate-400">{item.detail}</p>
    </Card>
  {/each}
</div>
