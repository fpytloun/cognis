<script lang="ts">
  import { onMount } from 'svelte';
  import ViewerIcon from './ViewerIcon.svelte';
  import type { ShareLinkOptions } from './share-options';
  import './document-actions.css';
  export let createLink: ((options: ShareLinkOptions) => Promise<string>) | null | undefined = undefined;
  export let internalUrl = '';
  export let allowModeChoice = false;
  export let defaultMode: 'view' | 'download' = 'view';
  export let label = '';
  let menu: typeof import('./ShareMenu.svelte')['default'] | null = null;
  let error = '';
  onMount(() => {
    let active = true;
    import('./ShareMenu.svelte')
      .then(module => { if (active) menu = module.default; })
      .catch(() => { if (active) error = 'Share controls could not load. Reload the page to retry.'; });
    return () => { active = false; };
  });
</script>

{#if menu}
  <svelte:component this={menu} {internalUrl} {createLink} {allowModeChoice} {defaultMode} {label} />
{:else}
  <button class="document-icon-action" type="button" aria-label="Share link" disabled><ViewerIcon name="share" />{#if label}<span>{label}</span>{/if}</button>
  {#if error}<span role="status">{error}</span>{/if}
{/if}
