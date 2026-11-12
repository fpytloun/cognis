<script lang="ts">
  import Star from 'lucide-svelte/icons/star';
  import { auth } from '$lib/stores/auth';
  import { favoriteItems, favoriteKey, loadFavorite } from '$lib/document-favorites';
  import type { LibraryKind } from '$lib/library';

  let { kind, id }: { kind: LibraryKind; id: string } = $props();
  const favorite = $derived($favoriteItems[favoriteKey(kind, id)]?.is_favorite);
  $effect(() => {
    if (!$auth.user?.email) return;
    // Favorite metadata is optional; an unavailable lookup must not hide work.
    void loadFavorite(kind, id).catch(() => {});
  });
</script>

{#if favorite}
  <span role="img" aria-label="Favorite" title="Favorite" class="inline-flex shrink-0 items-center" style="color: rgb(var(--color-orange-tone))" data-testid="document-favorite-indicator">
    <Star size={14} fill="currentColor" aria-hidden="true" />
  </span>
{/if}
