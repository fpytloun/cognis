<script lang="ts">
  import ShareMenu from './ShareMenuLoader.svelte';
  import type { ShareLinkOptions } from './share-options';
  import './document-actions.css';
  import type { Snippet } from 'svelte';
  let { internalUrl, createLink, allowModeChoice = true, defaultMode = 'view', extraActions, shareLabel = '' }: {
    internalUrl: string; createLink?: ((options: ShareLinkOptions) => Promise<string>) | null;
    allowModeChoice?: boolean; defaultMode?: 'view' | 'download';
    extraActions?: Snippet<[boolean?]>;
    shareLabel?: string;
  } = $props();
</script>
{@render extraActions?.()}
<ShareMenu {internalUrl} {createLink} {allowModeChoice} {defaultMode} label={shareLabel} />
