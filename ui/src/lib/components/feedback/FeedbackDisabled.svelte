<script lang="ts">
  import type { Snippet } from 'svelte';
  let { children }: { children?: Snippet; [key: string]: unknown } = $props();
</script>
{#if children}{@render children()}{/if}
