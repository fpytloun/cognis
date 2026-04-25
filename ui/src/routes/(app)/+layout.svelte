<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import type { Snippet } from 'svelte';
  import { onMount } from 'svelte';
  import Bot from 'lucide-svelte/icons/bot';
import BookOpen from 'lucide-svelte/icons/book-open';
import BrainCircuit from 'lucide-svelte/icons/brain-circuit';
import ChevronsLeft from 'lucide-svelte/icons/chevrons-left';
import ChevronsRight from 'lucide-svelte/icons/chevrons-right';
import CircleHelp from 'lucide-svelte/icons/circle-help';
import Clock from 'lucide-svelte/icons/clock';
import ListTodo from 'lucide-svelte/icons/list-todo';
import Menu from 'lucide-svelte/icons/menu';
import MessageSquareText from 'lucide-svelte/icons/message-square-text';
import Radio from 'lucide-svelte/icons/radio';
import RefreshCw from 'lucide-svelte/icons/refresh-cw';
import ServerCrash from 'lucide-svelte/icons/server-crash';
import Settings from 'lucide-svelte/icons/settings';
import ShieldAlert from 'lucide-svelte/icons/shield-alert';
import Workflow from 'lucide-svelte/icons/workflow';
import Wrench from 'lucide-svelte/icons/wrench';
import X from 'lucide-svelte/icons/x';

  import { api } from '$lib/api/client';
  import { deriveGettingStartedSteps, isGettingStartedDismissed } from '$lib/getting-started';
  import ShortcutHelp from '$lib/components/ShortcutHelp.svelte';
  import ToastViewport from '$lib/components/ToastViewport.svelte';
  import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { edgeSwipe } from '$lib/actions/edgeSwipe';
  import { scrollPersist } from '$lib/actions/scrollPersist';
  import { sidebarTooltip } from '$lib/actions/sidebarTooltip';
  import BottomTabBar from '$lib/components/BottomTabBar.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { openShortcutHelp, requestCancelActiveTurn, requestChatComposerFocus } from '$lib/shortcuts';
  import { auth } from '$lib/stores/auth';
  import { mobileNavOpen as mobileNavOpenStore, mobileNavOpenSignal } from '$lib/stores/mobileNav';
  import { blockingOverlayActive, resetOverlayState } from '$lib/stores/overlays';
  import { workspaceHealth } from '$lib/system';
  import type { SystemDiagnostics } from '$lib/types/api';
  import { wsClient, wsState } from '$lib/ws/client';

  let { children }: { children: Snippet } = $props();

  const navigationItems = [
    { href: '/chat', label: 'Chat', icon: MessageSquareText },
    { href: '/agents', label: 'Agents', icon: Bot },
    { href: '/tasks', label: 'Tasks', icon: ListTodo },
    { href: '/workflows', label: 'Workflows', icon: Workflow },
    { href: '/schedules', label: 'Schedules', icon: Clock },
    { href: '/docs', label: 'Docs', icon: BookOpen },
    { href: '/tools', label: 'Tools', icon: Wrench },
    { href: '/channels', label: 'Channels', icon: Radio },
    { href: '/settings', label: 'Settings', icon: Settings }
  ];

  let bootstrapped = $state(false);
  let diagnostics = $state<SystemDiagnostics | null>(null);
  let mobileNavOpen = $state(false);
  let sidebarCollapsed = $state(false);
  let mobileHeaderEl = $state<HTMLElement | null>(null);
  let workspaceRunning = false;

  function openMobileNav(): void {
    mobileNavOpen = true;
  }

  function closeMobileNav(): void {
    mobileNavOpen = false;
  }

  // Mirror the local mobileNavOpen state into the shared store so
  // child routes (chat detail) can read it and gate their own swipe
  // gestures. The layout is the sole writer; readers treat the store
  // as read-only and call `requestOpenMobileNav()` to open the drawer.
  $effect(() => {
    mobileNavOpenStore.set(mobileNavOpen);
  });

  // Edge-swipe handlers. The `edgeSwipe` action owns the gesture
  // detection (touch + pointer) and prevents iOS from claiming the
  // bezel swipe for native back/forward navigation. Left edge opens
  // the mobile nav drawer on non-chat routes; right edge closes it
  // (active everywhere, including chat detail, so the user can always
  // swipe right to dismiss the open drawer).
  function handleLeftEdgeSwipe(): void {
    if (mobileNavOpen) return;
    openMobileNav();
  }

  function handleRightEdgeSwipe(): void {
    if (!mobileNavOpen) return;
    closeMobileNav();
  }

  function restoreSidebarState(): void {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem('cognis-sidebar-collapsed');
    if (stored !== null) {
      sidebarCollapsed = stored === '1';
    } else {
      // Default: collapsed below lg (1024px). On desktop chat routes, also
      // start collapsed so the conversation view gets more width by default.
      sidebarCollapsed = window.innerWidth < 1024 || $page.url.pathname.startsWith('/chat/');
    }
  }

  function toggleSidebar(): void {
    sidebarCollapsed = !sidebarCollapsed;
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('cognis-sidebar-collapsed', sidebarCollapsed ? '1' : '0');
    }
  }

  // Sidebar state is fully controlled by the explicit collapse toggle.
  // The previous design also expanded on hover, but that made the
  // desktop layout shift every time the user moved their cursor across
  // the left edge. Users can see icon labels via the native `title`
  // tooltip when collapsed, and expand via the chevron button when
  // they want the full labels.
  let sidebarExpanded = $derived(!sidebarCollapsed);

  function isTextInputTarget(target: EventTarget | null): boolean {
    if (!(target instanceof HTMLElement)) {
      return false;
    }
    const tagName = target.tagName.toLowerCase();
    return tagName === 'input' || tagName === 'textarea' || target.isContentEditable;
  }

  function outageBanners() {
    const health = $workspaceHealth.health;
    if (!health) {
      return [];
    }

    const banners = [];
    const memoryStatus = String(health.providers?.memory?.status ?? 'unknown');
    if (memoryStatus !== 'healthy' && memoryStatus !== 'unknown') {
      banners.push({
        id: 'memory',
        variant: 'warning',
        title: 'Memory unavailable',
        description: "Chat still works, but recall is unavailable for this conversation.",
        href: '/settings?tab=system',
        icon: BrainCircuit
      });
    }

    const guardrailsStatus = String(health.providers?.guardrails?.status ?? 'unknown');
    if (guardrailsStatus !== 'healthy' && guardrailsStatus !== 'unknown') {
      banners.push({
        id: 'guardrails',
        variant: 'error',
        title: 'Guardrails unavailable',
        description: 'Tool execution is blocked until Intaris recovers. Check diagnostics.',
        href: '/settings?tab=system',
        icon: ShieldAlert
      });
    }

    const llmStatus = String(health.providers?.llm?.status ?? 'unknown');
    if (llmStatus !== 'healthy' && llmStatus !== 'unknown') {
      const llmDetail = JSON.stringify(health.providers?.llm ?? {}).toLowerCase();
      banners.push({
        id: 'llm',
        variant: 'error',
        title: llmDetail.includes('not configured') || llmDetail.includes('no llm model configured') ? 'No LLM provider configured' : 'LLM provider issue',
        description:
          llmDetail.includes('not configured') || llmDetail.includes('no llm model configured')
            ? 'Configure an LLM provider before using chat and tasks.'
            : 'Chat and tasks are unavailable until the configured provider recovers.',
        href: '/settings?tab=providers',
        icon: ServerCrash
      });
    }

    return banners;
  }

  function handleGlobalShortcuts(event: KeyboardEvent): void {
    if ($blockingOverlayActive) {
      return;
    }
    const activeTagIsInput = isTextInputTarget(event.target);
    if (event.key === 'Escape') {
      requestCancelActiveTurn();
      if (activeTagIsInput && document.activeElement instanceof HTMLElement) {
        document.activeElement.blur();
      }
      return;
    }

    if (activeTagIsInput) {
      return;
    }

    if (event.key === '/') {
      event.preventDefault();
      requestChatComposerFocus();
      return;
    }

    if (event.key === '?') {
      event.preventDefault();
      openShortcutHelp();
      return;
    }

    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') {
      event.preventDefault();
      void goto('/chat/new');
    }
  }

  async function loadDiagnosticsIfNeeded(): Promise<void> {
    if (auth.getSnapshot().user?.role !== 'admin') {
      diagnostics = null;
      return;
    }
    try {
      diagnostics = await api.system.diagnostics();
    } catch {
      diagnostics = null;
    }
  }

  function shouldShowGettingStarted(): boolean {
    if (!diagnostics || isGettingStartedDismissed()) {
      return false;
    }
    return deriveGettingStartedSteps(diagnostics).some((step) => !step.done);
  }

  function currentTitle(pathname: string): string {
    return navigationItems.find((item) => pathname.startsWith(item.href))?.label ?? 'Workspace';
  }

  function setShellOffsetVariable(name: string, value: number): void {
    if (typeof document === 'undefined') return;
    document.documentElement.style.setProperty(name, `${Math.max(0, Math.round(value))}px`);
  }

  function syncMobileHeaderOffset(): void {
    if (typeof window === 'undefined') return;
    const shouldReserve = showMobileHeader && window.innerWidth < 1024;
    setShellOffsetVariable('--app-shell-top-offset', shouldReserve ? mobileHeaderEl?.offsetHeight ?? 0 : 0);
  }

  let isChatRoute = $derived($page.url.pathname.startsWith('/chat'));
  let isChatDetailRoute = $derived(/^\/chat\/[^/]+/.test($page.url.pathname));
  let showMobileHeader = $derived(!isChatDetailRoute);
  let shouldReserveBottomTabSpace = $derived(!isChatDetailRoute);
  let contentShellClass = $derived.by(() => {
    if (isChatRoute) {
      return `min-h-0 min-w-0 flex-1 overflow-hidden ${showMobileHeader ? 'pt-[var(--app-shell-top-offset,0px)] lg:pt-0' : ''}`;
    }
    return 'min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain px-3 pt-[calc(var(--app-shell-top-offset,0px)+0.75rem)] sm:px-4 lg:px-0 lg:pb-0 lg:pt-0';
  });

  $effect(() => {
    if (typeof window === 'undefined') return;
    void showMobileHeader;
    void mobileHeaderEl;
    const rafId = window.requestAnimationFrame(syncMobileHeaderOffset);
    return () => window.cancelAnimationFrame(rafId);
  });

  $effect(() => {
    if (typeof ResizeObserver === 'undefined') return;
    const element = mobileHeaderEl;
    if (!element) {
      syncMobileHeaderOffset();
      return;
    }
    const observer = new ResizeObserver(syncMobileHeaderOffset);
    observer.observe(element);
    return () => observer.disconnect();
  });

  function startWorkspace(): void {
    if (workspaceRunning) return;
    workspaceRunning = true;
    wsClient.connect();
    workspaceHealth.start();
  }

  function stopWorkspace(): void {
    if (!workspaceRunning) return;
    workspaceRunning = false;
    wsClient.disconnect();
    workspaceHealth.stop();
  }

  function websocketStatusLabel(): string {
    if ($wsState.status === 'connected') return 'Connected';
    if ($wsState.status === 'stalled') return 'Disconnected';
    if ($wsState.status === 'reconnecting') return `Reconnecting (${$wsState.attempts}/10)`;
    if ($wsState.status === 'connecting') return 'Connecting';
    return 'Idle';
  }

  function websocketStatusTone(): string {
    if ($wsState.status === 'connected') return 'bg-emerald-400';
    if ($wsState.status === 'stalled') return 'bg-rose-400';
    return 'bg-sky-400';
  }

  onMount(() => {
    restoreSidebarState();
    void auth.bootstrap().finally(() => {
      bootstrapped = true;
    });

    window.addEventListener('keydown', handleGlobalShortcuts);
    window.addEventListener('resize', syncMobileHeaderOffset);

    // Pages that hide the global mobile header (chat detail) use this
    // signal to open the main nav drawer from their own hamburger button.
    let firstSignal = true;
    const unsubscribeMobileNav = mobileNavOpenSignal.subscribe(() => {
      if (firstSignal) {
        firstSignal = false;
        return;
      }
      openMobileNav();
    });

    return () => {
      resetOverlayState();
      window.removeEventListener('keydown', handleGlobalShortcuts);
      window.removeEventListener('resize', syncMobileHeaderOffset);
      setShellOffsetVariable('--app-shell-top-offset', 0);
      unsubscribeMobileNav();
      stopWorkspace();
    };
  });

  $effect(() => {
    if (!bootstrapped) {
      return;
    }

    if ($auth.status === 'authenticated') {
      startWorkspace();
      void loadDiagnosticsIfNeeded();
      return;
    }

    stopWorkspace();
    void goto('/login', { replaceState: true });
  });

  async function handleLogout(): Promise<void> {
    await auth.logout();
    wsClient.disconnect();
    await goto('/login', { replaceState: true });
  }
</script>

<svelte:head>
  <title>{currentTitle($page.url.pathname)} · Cognis</title>
</svelte:head>

{#if !bootstrapped || $auth.status === 'loading'}
  <div class="mx-auto flex min-h-[100dvh] max-w-5xl items-center justify-center px-6 py-16">
    <LoadingState label="Loading workspace" description="Restoring your Cognis session and preparing the UI shell." />
  </div>
{:else}
  <a class="skip-link" href="#main-content">Skip to content</a>
  <ToastViewport />
  <ConfirmDialog />
  <ShortcutHelp />
  <div class="app-shell-viewport fixed inset-x-0 top-[var(--app-viewport-offset-top,0px)] h-[var(--app-viewport-height,100dvh)] overflow-hidden overscroll-none bg-slate-950">
    <div class={`mx-auto flex h-full max-w-[1600px] overflow-hidden ${shouldReserveBottomTabSpace ? 'pb-[var(--app-shell-bottom-offset,0px)]' : 'pb-0'} lg:gap-6 lg:px-6 lg:py-4 lg:pb-4`}>
      <aside
        class={`hidden min-h-0 shrink-0 overflow-hidden whitespace-nowrap rounded-3xl border border-slate-800/80 bg-slate-900/80 shadow-card backdrop-blur transition-all duration-200 ease-in-out lg:flex lg:flex-col lg:justify-between ${sidebarExpanded ? 'w-72 p-5' : 'w-16 p-3'}`}
      >
        <div class="min-w-0 min-h-0 flex-1 overflow-y-auto">
          {#if sidebarExpanded}
            <div class="flex items-center gap-3 border-b border-slate-800/80 pb-5">
              <img alt="" class="h-11 w-11 rounded-2xl shadow-card" src="/pwa/icon-192.png" />
              <div class="min-w-0 space-y-1">
                <p class="text-sm font-medium uppercase tracking-[0.3em] text-sky-300">Cognis</p>
                <h1 class="text-xl font-semibold text-white">Agent workspace</h1>
              </div>
            </div>
          {:else}
            <div class="flex justify-center border-b border-slate-800/80 pb-4">
              <img alt="Cognis" class="h-9 w-9 rounded-xl shadow-card" src="/pwa/icon-192.png" />
            </div>
          {/if}

          <!--
            Nav links. When the sidebar is collapsed each link uses the
            \`sidebarTooltip\` action, which renders the label as a
            \`position: fixed\` element appended to \`document.body\`.
            That way the tooltip is not a descendant of the sidebar's
            overflow-hidden / overflow-y-auto ancestors, and can cross
            the sidebar's right edge without forcing a horizontal
            scrollbar on the nav column.
          -->
          <nav class={`space-y-1 ${sidebarExpanded ? 'mt-6 space-y-2' : 'mt-4'}`}>
            {#each navigationItems as item}
              {#if sidebarExpanded}
                <a
                  aria-label={`Open ${item.label}`}
                  class={`flex items-center rounded-2xl text-sm transition ${$page.url.pathname.startsWith(item.href) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-800 hover:text-white'} gap-3 px-4 py-3`}
                  href={item.href}
                >
                  <item.icon class="h-4 w-4 shrink-0" />
                  <span>{item.label}</span>
                </a>
              {:else}
                <a
                  use:sidebarTooltip={item.label}
                  aria-label={`Open ${item.label}`}
                  class={`flex items-center justify-center rounded-2xl px-2 py-3 text-sm transition ${$page.url.pathname.startsWith(item.href) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-800 hover:text-white'}`}
                  href={item.href}
                >
                  <item.icon class="h-4 w-4 shrink-0" />
                </a>
              {/if}
            {/each}
          </nav>
        </div>

        <div class={`shrink-0 space-y-4 border-t border-slate-800/80 ${sidebarExpanded ? 'pt-6' : 'pt-4'}`}>
          {#if sidebarExpanded}
            <div class="space-y-1">
              <p class="text-sm font-medium text-white">{$auth.user?.name ?? $auth.user?.email}</p>
              <p class="text-xs text-slate-400">{$auth.user?.email}</p>
            </div>
            <div class="space-y-2 rounded-2xl border border-slate-800/80 bg-slate-950/60 px-3 py-3 text-sm text-slate-300">
              <div class="flex items-center justify-between gap-3">
                <span class="text-xs font-medium uppercase tracking-[0.2em] text-slate-400">Workspace</span>
                <span class={`inline-flex h-2.5 w-2.5 rounded-full ${websocketStatusTone()}`} aria-label={`WebSocket ${$wsState.status}`}></span>
              </div>
              <div class="flex items-center justify-between gap-3 text-xs text-slate-400">
                <span>WebSocket</span>
                <span class="text-right">{websocketStatusLabel()}</span>
              </div>
              <div class="flex gap-2">
                <Button class="flex-1 justify-center" size="sm" variant="secondary" onclick={openShortcutHelp}>
                  <CircleHelp class="mr-1.5 h-3.5 w-3.5" />
                  Help
                </Button>
                {#if $wsState.status === 'stalled'}
                  <Button class="flex-1 justify-center" size="sm" variant="secondary" onclick={() => wsClient.connect()}>
                    <RefreshCw class="mr-1.5 h-3.5 w-3.5" />
                    Reconnect
                  </Button>
                {/if}
              </div>
              {#if $auth.user?.role === 'admin'}
                <Button class="w-full justify-center" size="sm" variant="secondary" onclick={() => goto('/getting-started')}>
                  <BookOpen class="mr-1.5 h-3.5 w-3.5" />
                  Getting started
                </Button>
              {/if}
            </div>
            <Button class="w-full justify-center" variant="secondary" onclick={handleLogout}>Sign out</Button>
          {:else}
            <!--
              Collapsed footer icons. Each icon uses the same
              fixed-position \`sidebarTooltip\` action as the nav links
              above so the label crosses the sidebar edge without
              producing a scrollbar.
            -->
            <div class="flex flex-col items-center gap-2">
              <span
                use:sidebarTooltip={`WebSocket ${websocketStatusLabel()}`}
                class={`inline-flex h-2.5 w-2.5 rounded-full ${websocketStatusTone()}`}
                aria-label={`WebSocket ${$wsState.status}`}
              ></span>
              <div use:sidebarTooltip={'Help'} class="inline-flex">
                <Button aria-label="Open keyboard shortcuts" class="h-9 w-9" size="icon" variant="ghost" onclick={openShortcutHelp}>
                  <CircleHelp class="h-4 w-4" />
                </Button>
              </div>
              {#if $auth.user?.role === 'admin'}
                <div use:sidebarTooltip={'Getting started'} class="inline-flex">
                  <Button aria-label="Open getting started guide" class="h-9 w-9" size="icon" variant="ghost" onclick={() => goto('/getting-started')}>
                    <BookOpen class="h-4 w-4" />
                  </Button>
                </div>
              {/if}
              {#if $wsState.status === 'stalled'}
                <div use:sidebarTooltip={'Reconnect WebSocket'} class="inline-flex">
                  <Button aria-label="Reconnect WebSocket" class="h-9 w-9" size="icon" variant="ghost" onclick={() => wsClient.connect()}>
                    <RefreshCw class="h-4 w-4" />
                  </Button>
                </div>
              {/if}
            </div>
          {/if}
          {#if sidebarCollapsed}
            <button
              use:sidebarTooltip={'Expand sidebar'}
              class="flex w-full items-center justify-center rounded-xl py-2 text-xs text-slate-400 transition hover:bg-slate-800 hover:text-white"
              onclick={toggleSidebar}
              type="button"
              aria-label="Expand sidebar"
            >
              <ChevronsRight class="h-4 w-4" />
            </button>
          {:else}
            <button
              class="flex w-full items-center justify-center gap-2 rounded-xl px-3 py-2 text-xs text-slate-400 transition hover:bg-slate-800 hover:text-white"
              onclick={toggleSidebar}
              type="button"
              aria-label="Collapse sidebar"
            >
              <ChevronsLeft class="h-4 w-4" />
              <span>Collapse</span>
            </button>
          {/if}
        </div>
      </aside>

      <!--
        The main content container used to wrap everything in a rounded,
        bordered, backdrop-blurred card on lg+. On iPad/desktop that created
        a stack of three nested darker boxes (outer app frame, chat
        sidebar card, chat main card) that looked heavy and wasted
        horizontal breathing room. Follow the Apple "content first"
        principle: content sits directly on the page background, and
        sections rely on subtle dividers and typography for hierarchy.
      -->
      <main class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-transparent" id="main-content">
        {#if showMobileHeader}
        <!--
          Mobile top bar. On iOS PWAs with `black-translucent` status bar,
          the system draws content under the status bar, so pad the header
          top by `env(safe-area-inset-top)` so the hamburger + title sit
          below the camera cutout instead of being obscured by it.
        -->
        <header bind:this={mobileHeaderEl} class="fixed inset-x-0 top-0 z-[70] flex shrink-0 items-center justify-between gap-2 border-b border-slate-800/80 bg-slate-950/95 px-3 pt-[calc(0.625rem+env(safe-area-inset-top))] pb-2.5 backdrop-blur sm:gap-3 sm:px-4 sm:pt-[calc(0.625rem+env(safe-area-inset-top))] sm:pb-2.5 lg:hidden" style="padding-left: max(0.75rem, env(safe-area-inset-left)); padding-right: max(0.75rem, env(safe-area-inset-right));">
          <div class="flex min-w-0 flex-1 items-center gap-2 lg:hidden">
            <Button aria-label="Open navigation" class="h-11 w-11 lg:hidden md:h-9 md:w-9" size="icon" variant="secondary" onclick={openMobileNav}>
              <Menu class="h-5 w-5" />
            </Button>
            <div class="min-w-0">
              <h2 class="truncate text-base font-semibold text-white sm:text-lg">{currentTitle($page.url.pathname)}</h2>
            </div>
          </div>

          <div class="flex shrink-0 items-center gap-2">
            <Button
              aria-label="Open keyboard shortcuts"
              class="h-11 w-11 md:h-9 md:w-9"
              size="icon"
              variant="secondary"
              onclick={openShortcutHelp}
              title="Keyboard shortcuts"
            >
              <CircleHelp class="h-5 w-5" />
            </Button>
            <span
              class={`inline-flex h-2.5 w-2.5 rounded-full ${$wsState.status === 'connected' ? 'bg-emerald-400' : $wsState.status === 'stalled' ? 'bg-rose-400' : 'bg-sky-400'}`}
              aria-label={`WebSocket ${$wsState.status}`}
              title={`WebSocket ${$wsState.status}${$wsState.status === 'reconnecting' || $wsState.status === 'stalled' ? ` (attempt ${$wsState.attempts}/10)` : ''}`}
            ></span>
            {#if $wsState.status === 'stalled'}
              <Button
                aria-label="Reconnect WebSocket"
                class="h-11 w-11 md:h-9 md:w-9"
                size="icon"
                variant="secondary"
                onclick={() => wsClient.connect()}
                title="Reconnect"
              >
                <RefreshCw class="h-4 w-4" />
              </Button>
            {/if}
          </div>
        </header>
        {/if}

        {#if outageBanners().length > 0}
          <div class="space-y-3">
            {#each outageBanners() as banner (banner.id)}
              <div class={`rounded-2xl border px-4 py-4 text-sm ${banner.variant === 'warning' ? 'border-sky-500/30 bg-sky-500/10 text-sky-100' : 'border-rose-500/30 bg-rose-500/10 text-rose-100'}`}>
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div class="flex min-w-0 items-start gap-3">
                     <banner.icon class="mt-0.5 h-5 w-5 shrink-0" />
                    <div>
                      <p class="font-medium">{banner.title}</p>
                      <p class="mt-1 opacity-90">{banner.description}</p>
                    </div>
                  </div>
                  <div class="flex flex-wrap gap-2">
                    <Button size="sm" variant="secondary" onclick={() => workspaceHealth.refresh()}>
                      <RefreshCw class="mr-1.5 h-3.5 w-3.5" />
                      Refresh
                    </Button>
                    <Button size="sm" variant="secondary" onclick={() => goto(banner.href)}>
                      <Settings class="mr-1.5 h-3.5 w-3.5" />
                      Configure
                    </Button>
                  </div>
                </div>
              </div>
            {/each}
          </div>
        {/if}

        {#if shouldShowGettingStarted()}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-4 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p class="font-medium">Finish first-run setup</p>
                <p class="mt-1 text-sky-100/80">Cognis still needs providers, agents, or companion services before the workspace is fully ready.</p>
              </div>
              <Button size="sm" onclick={() => goto('/getting-started')}>Open guide</Button>
            </div>
          </div>
        {/if}

        <!--
          Non-chat pages support a left-edge swipe gesture to open the
          mobile nav drawer — the same affordance iOS and Android apps
          use. Chat detail has its own edge gesture (back-to-list), so
          the handler is only attached when the route is not a chat
          detail. Pointer-based gesture; mouse input is ignored.
        -->
        <div
          class={contentShellClass}
          data-app-content="true"
          role="presentation"
          use:scrollPersist={{ key: $page.url.pathname, disabled: isChatDetailRoute }}
          use:edgeSwipe={{ edge: 'left', onTrigger: handleLeftEdgeSwipe, disabled: isChatDetailRoute || mobileNavOpen }}
          use:edgeSwipe={{ edge: 'right', onTrigger: handleRightEdgeSwipe, disabled: !mobileNavOpen }}
        >
            {@render children()}
        </div>
      </main>
    </div>
  </div>

  <!--
    Mobile navigation sheet: opens from the LEFT edge, aligned with the
    hamburger button on the left of the mobile header. Matches the
    iOS/Android convention where the side drawer slides out from under
    the menu icon. The Sheet also pads its top/bottom/left by the safe
    area so content does not render under the Dynamic Island or the
    home indicator.
  -->
  <Sheet open={mobileNavOpen} onClose={closeMobileNav} side="left" label="Navigation menu">
    {#snippet header()}
      <div class="flex items-center justify-between gap-3">
        <div class="flex min-w-0 items-center gap-3">
          <img alt="" class="h-11 w-11 rounded-2xl shadow-card" src="/pwa/icon-192.png" />
          <div class="min-w-0">
            <p class="text-sm uppercase tracking-[0.25em] text-sky-300">Cognis</p>
            <p class="mt-1 truncate text-sm text-slate-400">{$auth.user?.email}</p>
          </div>
        </div>
        <Button aria-label="Close navigation" class="h-11 w-11 md:h-9 md:w-9" size="icon" variant="secondary" onclick={closeMobileNav}>
          <X class="h-4 w-4" />
        </Button>
      </div>
    {/snippet}

    <nav class="space-y-2">
      {#each navigationItems as item}
        <a
          class={`flex min-h-[48px] items-center gap-3 rounded-2xl px-4 py-3 text-base transition ${$page.url.pathname.startsWith(item.href) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-900 hover:text-white'}`}
          href={item.href}
          onclick={closeMobileNav}
        >
           <item.icon class="h-5 w-5" />
          <span>{item.label}</span>
        </a>
      {/each}
    </nav>

    <div class="mt-6 space-y-3 border-t border-slate-800 pt-5">
      <div class="space-y-2 rounded-2xl border border-slate-800/80 bg-slate-950/60 px-3 py-3 text-sm text-slate-300">
        <div class="flex items-center justify-between gap-3">
          <span class="text-xs font-medium uppercase tracking-[0.2em] text-slate-400">Workspace</span>
          <span class={`inline-flex h-2.5 w-2.5 rounded-full ${websocketStatusTone()}`} aria-label={`WebSocket ${$wsState.status}`}></span>
        </div>
        <div class="flex items-center justify-between gap-3 text-xs text-slate-400">
          <span>WebSocket</span>
          <span class="text-right">{websocketStatusLabel()}</span>
        </div>
        <div class="flex gap-2">
          <Button class="flex-1 justify-center" variant="secondary" onclick={() => { closeMobileNav(); openShortcutHelp(); }}>
            <CircleHelp class="mr-1.5 h-3.5 w-3.5" />
            Help
          </Button>
          {#if $wsState.status === 'stalled'}
            <Button class="flex-1 justify-center" variant="secondary" onclick={() => { closeMobileNav(); wsClient.connect(); }}>
              <RefreshCw class="mr-1.5 h-3.5 w-3.5" />
              Reconnect
            </Button>
          {/if}
        </div>
        {#if $auth.user?.role === 'admin'}
          <Button class="w-full justify-center" variant="secondary" onclick={() => { closeMobileNav(); void goto('/getting-started'); }}>
            <BookOpen class="mr-1.5 h-3.5 w-3.5" />
            Getting started
          </Button>
        {/if}
      </div>
      <Button class="w-full justify-center" variant="secondary" onclick={handleLogout}>Sign out</Button>
    </div>
  </Sheet>

  <!-- Mobile bottom tab bar: primary navigation on small screens. Hidden inside
       chat detail views so the composer owns the bottom safe-area. -->
  <BottomTabBar hidden={isChatDetailRoute} />
{/if}
