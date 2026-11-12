<script lang="ts">
  import { projectIcon } from '$lib/projects/icons';

  let { icon = 'briefcase', class: className = '', strokeWidth = 1.8 } = $props<{
    icon?: unknown; class?: string; strokeWidth?: number;
  }>();
  const bundled = $derived(projectIcon(icon ?? 'briefcase'));
  // Defer the full pack to keep the normal chat path's icon bundle small.
  const loaded = $derived(bundled ? null : import('$lib/projects/icon-catalog')
    .then(({ projectIconCatalog }) => projectIconCatalog.find((item) => item.id === icon)?.component
      ?? projectIcon('briefcase')!));
</script>

{#if bundled}
  {@const Icon = bundled}
  <Icon class={className} {strokeWidth} />
{:else}
  {#await loaded}
    <span class={className} aria-hidden="true"></span>
  {:then Icon}
    {#if Icon}<Icon class={className} {strokeWidth} />{/if}
  {:catch}
    <span class={className} role="img" aria-label="Icon unavailable">!</span>
  {/await}
{/if}
