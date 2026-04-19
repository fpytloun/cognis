<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import Download from 'lucide-svelte/icons/download';
import Share from 'lucide-svelte/icons/share';
import X from 'lucide-svelte/icons/x';

  import Button from '$lib/components/ui/Button.svelte';
  import {
    displayMode,
    dismissInstallPromptForNow,
    installPromptAvailable,
    isInstallPromptDismissed,
    isIosSafari,
    promptInstall,
  } from '$lib/stores/pwa';

  /**
   * PWA install-related banners.
   *
   * Rules:
   *   - On Android/Chrome/Edge, show "Install Cognis" when `beforeinstallprompt`
   *     has fired (controlled by `installPromptAvailable`).
   *   - On iOS Safari (not standalone), show a one-time hint explaining how to
   *     Add to Home Screen — since iOS has no install prompt event.
   *   - All banners are dismissible and remember dismissal in localStorage.
   *
   * The former \"Update available\" banner was removed: we could not reliably
   * distinguish between \"a genuinely newer SW is waiting\" and \"the browser
   * just registered a fresh SW after a hard reset\", which meant the banner
   * kept re-appearing on boot. Until we have a robust signal, the SW updates
   * silently on the next navigation and users simply see the new UI on their
   * next reload.
   */

  const DISMISS_KEY_IOS = 'cognis-pwa-ios-dismissed';

  let installDismissed = $state(false);
  let iosDismissed = $state(false);
  let showIosHint = $state(false);
  let shouldShowInstallUi = $derived(
    page.url.pathname === '/getting-started' || page.url.pathname === '/login' || page.url.pathname === '/setup'
  );

  onMount(() => {
    if (typeof window === 'undefined') return;
    installDismissed = isInstallPromptDismissed();
    iosDismissed = window.localStorage.getItem(DISMISS_KEY_IOS) === '1';
    // Show iOS hint only if on iOS Safari, not installed, and not previously dismissed.
    if (isIosSafari() && !iosDismissed && shouldShowInstallUi) {
      showIosHint = true;
    }
  });

  $effect(() => {
    if (!shouldShowInstallUi) {
      installDismissed = true;
      showIosHint = false;
      return;
    }
    installDismissed = isInstallPromptDismissed();
    showIosHint = isIosSafari() && !iosDismissed;
  });

  async function handleInstall(): Promise<void> {
    const outcome = await promptInstall();
    if (outcome === 'accepted') {
      installDismissed = true;
    }
  }

  function dismissInstall(): void {
    installDismissed = true;
    dismissInstallPromptForNow();
  }

  function dismissIos(): void {
    iosDismissed = true;
    showIosHint = false;
    window.localStorage.setItem(DISMISS_KEY_IOS, '1');
  }
</script>

{#if shouldShowInstallUi && $installPromptAvailable && !installDismissed && $displayMode === 'browser'}
  <div
    class="fixed inset-x-2 bottom-2 z-[70] mx-auto flex max-w-xl items-start gap-3 rounded-2xl border border-sky-400/40 bg-slate-900/95 px-4 py-3 text-sm text-slate-100 shadow-card backdrop-blur lg:bottom-4"
    style="margin-bottom: max(env(safe-area-inset-bottom), 0.5rem);"
    role="region"
    aria-label="Install Cognis as app"
  >
    <Download class="mt-0.5 h-4 w-4 shrink-0 text-sky-300" />
    <div class="min-w-0 flex-1">
      <p class="font-medium">Install Cognis</p>
      <p class="text-xs text-slate-400">Get a dedicated window, offline shell, and app-icon launch.</p>
    </div>
    <div class="flex shrink-0 gap-2">
      <Button size="sm" variant="secondary" onclick={dismissInstall}>Not now</Button>
      <Button size="sm" onclick={() => void handleInstall()}>Install</Button>
    </div>
  </div>
{/if}

{#if shouldShowInstallUi && showIosHint && $displayMode === 'browser'}
  <div
    class="fixed inset-x-2 bottom-2 z-[70] mx-auto flex max-w-xl items-start gap-3 rounded-2xl border border-sky-400/40 bg-slate-900/95 px-4 py-3 text-sm text-slate-100 shadow-card backdrop-blur lg:bottom-4"
    style="margin-bottom: max(env(safe-area-inset-bottom), 0.5rem);"
    role="region"
    aria-label="Add Cognis to Home Screen"
  >
    <Share class="mt-0.5 h-4 w-4 shrink-0 text-sky-300" />
    <div class="min-w-0 flex-1">
      <p class="font-medium">Add to Home Screen</p>
      <p class="text-xs text-slate-400">Tap the Share icon in Safari, then "Add to Home Screen" for a native-app feel.</p>
    </div>
    <Button aria-label="Dismiss" class="h-11 w-11 md:h-9 md:w-9" size="icon" variant="ghost" onclick={dismissIos}>
      <X class="h-4 w-4" />
    </Button>
  </div>
{/if}
