<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import Download from 'lucide-svelte/icons/download';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import Share from 'lucide-svelte/icons/share';
  import X from 'lucide-svelte/icons/x';

  import Button from '$lib/components/ui/Button.svelte';
  import {
    applyUpdate,
    displayMode,
    dismissInstallPromptForNow,
    dismissUpdateBanner,
    installPromptAvailable,
    isInstallPromptDismissed,
    isIosSafari,
    promptInstall,
    updateAvailable,
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
   * Update prompts are shown only for controlled pages with a genuinely
   * waiting newer worker. First install and hard-reset registration do not
   * trigger this banner.
   */

  const DISMISS_KEY_IOS = 'cognis-pwa-ios-dismissed';

  let installDismissed = $state(false);
  let iosDismissed = $state(false);
  let showIosHint = $state(false);
  let updateApplying = $state(false);
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

  async function handleApplyUpdate(): Promise<void> {
    updateApplying = true;
    await applyUpdate();
  }
</script>

{#if $updateAvailable}
  <div
    class="app-floating-bottom-overlay z-[70] mx-auto flex max-w-xl items-start gap-3 rounded-2xl border border-amber-400/40 bg-slate-900/95 px-4 py-3 text-sm text-slate-100 shadow-card backdrop-blur"
    role="region"
    aria-label="Cognis update available"
  >
    <RefreshCw class={`mt-0.5 h-4 w-4 shrink-0 text-amber-300 ${updateApplying ? 'animate-spin' : ''}`} />
    <div class="min-w-0 flex-1">
      <p class="font-medium">Update available</p>
      <p class="text-xs text-slate-400">Reload Cognis to use the latest app version and avoid stale PWA state.</p>
    </div>
    <div class="flex shrink-0 gap-2">
      <Button size="sm" variant="secondary" disabled={updateApplying} onclick={dismissUpdateBanner}>Later</Button>
      <Button size="sm" disabled={updateApplying} onclick={() => void handleApplyUpdate()}>
        {updateApplying ? 'Reloading…' : 'Reload'}
      </Button>
    </div>
  </div>
{:else if shouldShowInstallUi && $installPromptAvailable && !installDismissed && $displayMode === 'browser'}
  <div
    class="app-floating-bottom-overlay z-[70] mx-auto flex max-w-xl items-start gap-3 rounded-2xl border border-sky-400/40 bg-slate-900/95 px-4 py-3 text-sm text-slate-100 shadow-card backdrop-blur"
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
    class="app-floating-bottom-overlay z-[70] mx-auto flex max-w-xl items-start gap-3 rounded-2xl border border-sky-400/40 bg-slate-900/95 px-4 py-3 text-sm text-slate-100 shadow-card backdrop-blur"
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
