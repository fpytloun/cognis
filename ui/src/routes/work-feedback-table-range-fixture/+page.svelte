<script lang="ts">
  import { dev } from '$app/environment';
  import { onMount } from 'svelte';
  let Fixture = $state<typeof import('$lib/components/feedback/annotations/TableRangeBrowserFixture.svelte').default>();
  onMount(() => {
    if (import.meta.env.DEV && dev) void import('$lib/components/feedback/annotations/TableRangeBrowserFixture.svelte').then(module => { Fixture = module.default; });
  });
</script>
{#if dev && Fixture}<Fixture />{/if}
