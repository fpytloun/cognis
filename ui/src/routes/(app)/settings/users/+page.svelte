<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  // Clean-URL deep link for /settings/users. Redirects to the canonical
  // /settings?tab=users. Three-layer redirect strategy:
  //   1. `<meta http-equiv="refresh">` — fires before hydration in every
  //      browser so direct visits / bookmarks never see a blank page for
  //      more than a single paint frame (and no JS-disabled blank either).
  //   2. A small interstitial body so even that single paint shows intent.
  //   3. `goto(...)` in onMount — the fast path for the hydrated SPA case
  //      (avoids a full reload when the user already has the app loaded).
  onMount(() => {
    void goto('/settings?tab=users', { replaceState: true });
  });
</script>

<svelte:head>
  <title>Settings · users · Cognis</title>
  <meta http-equiv="refresh" content="0; url=/settings?tab=users" />
</svelte:head>

<div class="flex min-h-[40vh] items-center justify-center p-8 text-sm text-slate-400">
  Opening settings…
</div>
