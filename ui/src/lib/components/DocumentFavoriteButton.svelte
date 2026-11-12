<script lang="ts">
  import Star from 'lucide-svelte/icons/star';
  import { auth } from '$lib/stores/auth';
  import { favoriteItems, favoriteKey, loadFavorite, saveFavorite } from '$lib/document-favorites';
  import type { LibraryItem, LibraryKind } from '$lib/library';
  import './rich/document-actions.css';
  let { kind, id, initial, showLabel = false }: { kind: LibraryKind; id: string; initial?: LibraryItem; showLabel?: boolean } = $props();
  let busy = $state(false);
  let error = $state('');
  const item = $derived($favoriteItems[favoriteKey(kind, id)]);
  $effect(() => {
    if (!$auth.user?.email) return;
    const currentKind = kind, currentId = id, seed = initial;
    let cancelled = false;
    error = '';
    void loadFavorite(currentKind, currentId, seed).catch(() => { if (!cancelled) error = 'Could not load favorite state. Click to retry.'; });
    return () => { cancelled = true; };
  });
  async function toggle() {
    busy = true; error = '';
    try {
      if (!item) await loadFavorite(kind, id, initial, true);
      else await saveFavorite(kind, id, !item.is_favorite);
    } catch { error = 'Could not update favorite. Please retry.'; }
    finally { busy = false; }
  }
</script>
{#if $auth.user?.email}
  <button type="button" class="document-icon-action document-favorite-action" class:labeled={showLabel} class:favorite={item?.is_favorite}
    aria-label={item?.is_favorite ? 'Remove from favorites' : 'Add to favorites'}
    aria-pressed={item?.is_favorite ?? false} disabled={busy || (!item && !error)}
    title={error || (item?.is_favorite ? 'Remove from favorites' : 'Add to favorites')} onclick={toggle}>
    <Star size={18} fill={item?.is_favorite ? 'currentColor' : 'none'} aria-hidden="true" />
    {#if showLabel}<span>{item?.is_favorite ? 'Remove from favorites' : 'Add to favorites'}</span>{/if}
  </button>
  {#if error}<span role="status" class="sr-only">{error}</span>{/if}
{/if}
<style>
  .document-favorite-action.favorite { color: rgb(var(--color-orange-tone)); }
  .document-favorite-action.labeled { width: auto; gap: .5rem; padding-inline: .5rem; }
  .document-favorite-action :global(svg) { flex: none; }
  .document-favorite-action:focus-visible {
    outline: 2px solid var(--rich-accent, rgb(var(--color-accent))); outline-offset: 2px;
  }
  .document-favorite-action:disabled { opacity: .45; cursor: default; }
  @media (prefers-reduced-motion: reduce) {
    .document-favorite-action { transition: none; }
  }
</style>
