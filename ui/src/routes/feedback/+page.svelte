<script lang="ts">
  import { onMount } from 'svelte';
  import PublicFeedbackHost from '$lib/components/feedback/PublicFeedbackHost.svelte';
  type LinkNavigation = { token: string; url: string };
  let navigation = $state<LinkNavigation | null>(null);
  let error = $state('');
  onMount(() => {
    function readLink(event?: HashChangeEvent) {
      const url = event?.newURL ?? location.href;
      // Reopening the same consumed token is a new capability exchange, too.
      try { navigation = { token: decodeURIComponent(new URL(url).hash.slice(1)), url }; error = ''; }
      catch { navigation = null; error = 'This feedback link is malformed.'; }
    }
    readLink();
    window.addEventListener('hashchange', readLink);
    return () => window.removeEventListener('hashchange', readLink);
  });
  function exchanged(current: LinkNavigation) {
    if (current !== navigation || location.href !== current.url) return;
    // The raw link token is consumed once, never kept in displayed history,
    // referrers, owner state, application logs or cross-tab storage.
    history.replaceState(history.state, '', location.pathname + location.search);
  }
</script>
<svelte:head><title>Document feedback · Cognis</title><meta name="referrer" content="no-referrer" /><meta name="robots" content="noindex,nofollow" /></svelte:head>
<main class="h-dvh min-h-0 overflow-y-auto overscroll-contain bg-canvas pb-[var(--app-safe-area-bottom)] text-foreground" aria-label="Public document feedback">
  {#if error}<p role="alert" class="p-4">{error}</p>
  {:else if navigation !== null}
    {#key navigation}
      {@const current = navigation}
      <PublicFeedbackHost shareToken={current.token} onExchange={() => exchanged(current)} />
    {/key}
  {/if}
</main>
