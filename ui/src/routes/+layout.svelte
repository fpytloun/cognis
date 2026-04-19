<script lang="ts">
  import '../app.css';

  import type { Snippet } from 'svelte';
  import { onMount } from 'svelte';

  import PwaBanner from '$lib/components/PwaBanner.svelte';
  import { registerServiceWorker } from '$lib/stores/pwa';
  // Subscribe to keyboardOffset so it starts publishing the --kb-offset CSS var
  // as soon as the shell mounts (value itself is unused here).
  import { keyboardOffset } from '$lib/stores/viewport';

  let { children }: { children: Snippet } = $props();

  onMount(() => {
    (window as Window & { __cognisStartupRecoverySuccess?: () => void }).__cognisStartupRecoverySuccess?.();
    void registerServiceWorker();
    // Keep subscription alive.
    const unsub = keyboardOffset.subscribe(() => {});
    return () => unsub();
  });
</script>

<a class="skip-link" href="#main">Skip to content</a>
<PwaBanner />
{@render children()}
