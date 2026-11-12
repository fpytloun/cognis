<script lang="ts">
  import '../app.css';

  import type { Snippet } from 'svelte';
  import { onMount, untrack } from 'svelte';
  import { page } from '$app/stores';
  import { goto, afterNavigate, beforeNavigate } from '$app/navigation';
  import { installNativeRuntime } from '$lib/native/runtime';
  import { isNativeHost, isWorkspaceReplica, postNative, forwardNativeReference } from '$lib/native/host';
  import { installWorkspaceBridge } from '$lib/native/workspace-runtime';
  import { startConversationWorkspace } from '$lib/dashboard/conversation-workspace-runtime';
  import { auth } from '$lib/stores/auth';
  import { startUserPreferences } from '$lib/stores/userPreferences';
  import { startAppearance } from '$lib/theme/appearance';
  import { wsClient } from '$lib/ws/client';
  import { startInbox } from '$lib/stores/inbox';
  import { installOfflineSession } from '$lib/offline/session';

  import PwaBanner from '$lib/components/PwaBanner.svelte';
  import { installSelectionCopy } from '$lib/selection-copy';
  import { installFind, invalidateFindContext } from '$lib/find';
  import { installLazyCodeHighlighting } from '$lib/syntax/lazy-highlight';
  import { registerServiceWorker } from '$lib/stores/pwa';
  // Subscribe so visualViewport-backed CSS vars are published as soon as
  // the shell mounts.
  import { viewportMetrics } from '$lib/stores/viewport';

  let { children }: { children: Snippet } = $props();
  const publicFeedback = $derived($page.url.pathname === '/feedback');
  let mounted = $state(false);
  onMount(() => { mounted = true; });
  beforeNavigate((navigation) => {
    if (navigation.type === 'link' && navigation.to && forwardNativeReference(navigation.to.url)) navigation.cancel();
  });
  afterNavigate(({ to }) => {
    invalidateFindContext();
    if (to) postNative('route', { path: to.url.pathname + to.url.search });
  });

  $effect(() => {
    if (!mounted) return;
    if (publicFeedback) {
      // Public hydration does not depend on private session setup or source loading.
      (window as Window & { __cognisStartupRecoverySuccess?: () => void }).__cognisStartupRecoverySuccess?.();
      const uninstall = installSelectionCopy();
      const stopViewport = viewportMetrics.subscribe(() => {});
      return () => { uninstall(); stopViewport(); };
    }
    return untrack(() => {
    if (isNativeHost()) installOfflineSession();
    (window as Window & { __cognisStartupRecoverySuccess?: () => void }).__cognisStartupRecoverySuccess?.();
    if (!isNativeHost()) void registerServiceWorker();
    const stopAppearance = startAppearance();
    const stopNative = installNativeRuntime(goto);
    const stopPreferences = startUserPreferences();
    const stopInbox = isNativeHost() && !isWorkspaceReplica() ? startInbox() : () => {};
    // Native main and pinned entrypoints share one authenticated runtime lifetime.
    const stopWorkspace = isNativeHost() ? startConversationWorkspace() : () => {};
    const stopWorkspaceBridge = installWorkspaceBridge();
    const stopAuth = isNativeHost() ? auth.subscribe((state) => {
      if (state.status === 'authenticated' && !isWorkspaceReplica()) wsClient.connect();
      else if (state.status === 'anonymous') wsClient.disconnect();
    }) : () => {};
    if (isNativeHost()) void auth.bootstrap();
    const uninstallSelectionCopy = installSelectionCopy();
    const uninstallFind = installFind();
    const uninstallCodeHighlighting = installLazyCodeHighlighting();
    // Keep subscription alive.
    const unsub = viewportMetrics.subscribe(() => {});
    return () => {
      stopNative();
      stopPreferences();
      stopAppearance();
      stopWorkspaceBridge();
      stopInbox();
      stopAuth(); stopWorkspace();
      if (isNativeHost()) wsClient.disconnect();
      uninstallSelectionCopy();
      uninstallFind();
      uninstallCodeHighlighting();
      unsub();
    };
    });
  });
</script>

{#if !isNativeHost() && !publicFeedback}<PwaBanner />{/if}
{@render children()}
