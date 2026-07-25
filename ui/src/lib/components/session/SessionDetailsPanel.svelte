<script lang="ts">
  import { api } from '$lib/api/client';
  import SessionDetailsContent from './SessionDetailsContent.svelte';
  import type { IntarisSessionDetail } from '$lib/types/api';

  let {
    sessionId,
    onOpenIntaris,
    onLoaded,
  } = $props<{
    sessionId: string;
    onOpenIntaris?: ((intarisSessionId: string) => void | Promise<void>) | undefined;
    onLoaded?: ((detail: IntarisSessionDetail) => void) | undefined;
  }>();

  let detail = $state<IntarisSessionDetail | null>(null);
  let loading = $state(false);
  let error = $state('');
  let requestGeneration = 0;

  $effect(() => {
    const requestedSessionId = sessionId;
    const generation = ++requestGeneration;
    detail = null;
    error = '';
    loading = true;
    void api.sessions.intarisDetail(requestedSessionId)
      .then((value) => {
        if (generation !== requestGeneration || requestedSessionId !== sessionId) return;
        detail = value;
        onLoaded?.(value);
      })
      .catch((caught) => {
        if (generation !== requestGeneration || requestedSessionId !== sessionId) return;
        error = caught instanceof Error ? caught.message : 'Unable to load session details.';
      })
      .finally(() => {
        if (generation === requestGeneration && requestedSessionId === sessionId) loading = false;
      });
    return () => { requestGeneration += 1; };
  });
</script>

<section class="max-h-[min(70vh,calc(var(--app-viewport-height,100dvh)-8rem))] overflow-y-auto overscroll-contain border-b border-slate-800/80 bg-slate-900/40 px-4 py-4" data-testid="session-details-panel">
  {#if loading}
    <p class="text-xs text-slate-500">Loading session details…</p>
  {:else if error}
    <p class="text-xs text-rose-300">{error}</p>
  {:else if detail}
    <SessionDetailsContent {detail} {onOpenIntaris} />
  {/if}
</section>
