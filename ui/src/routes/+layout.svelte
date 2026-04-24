<script lang="ts">
  import '../app.css';

  import type { Snippet } from 'svelte';
  import { onMount } from 'svelte';

  import PwaBanner from '$lib/components/PwaBanner.svelte';
  import { registerServiceWorker } from '$lib/stores/pwa';
  // Subscribe so visualViewport-backed CSS vars are published as soon as
  // the shell mounts.
  import { viewportMetrics } from '$lib/stores/viewport';

  let { children }: { children: Snippet } = $props();

  onMount(() => {
    (window as Window & { __cognisStartupRecoverySuccess?: () => void }).__cognisStartupRecoverySuccess?.();
    void registerServiceWorker();
    // Keep subscription alive.
    const unsub = viewportMetrics.subscribe(() => {});
    return () => unsub();
  });
</script>

<PwaBanner />
{@render children()}
