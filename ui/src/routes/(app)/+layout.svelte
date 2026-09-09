<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import type { Snippet } from 'svelte';
  import { onMount } from 'svelte';
  import Bot from 'lucide-svelte/icons/bot';
import Box from 'lucide-svelte/icons/box';
import BookOpen from 'lucide-svelte/icons/book-open';
import ChevronsLeft from 'lucide-svelte/icons/chevrons-left';
import ChevronsRight from 'lucide-svelte/icons/chevrons-right';
import Clock from 'lucide-svelte/icons/clock';
import FolderKanban from 'lucide-svelte/icons/folder-kanban';
import Library from 'lucide-svelte/icons/library';
import LayoutDashboard from 'lucide-svelte/icons/layout-dashboard';
import ListTodo from 'lucide-svelte/icons/list-todo';
import Menu from 'lucide-svelte/icons/menu';
import MessageSquareText from 'lucide-svelte/icons/message-square-text';
import Radio from 'lucide-svelte/icons/radio';
import RefreshCw from 'lucide-svelte/icons/refresh-cw';
import Settings from 'lucide-svelte/icons/settings';
import Workflow from 'lucide-svelte/icons/workflow';
import Wrench from 'lucide-svelte/icons/wrench';
import X from 'lucide-svelte/icons/x';

  import ToastViewport from '$lib/components/ToastViewport.svelte';
  import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { adaptiveBottomInset } from '$lib/actions/adaptiveBottomInset';
  import { edgeSwipe } from '$lib/actions/edgeSwipe';
  import { scrollPersist } from '$lib/actions/scrollPersist';
  import { sidebarTooltip } from '$lib/actions/sidebarTooltip';
  import BottomTabBar from '$lib/components/BottomTabBar.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { auth } from '$lib/stores/auth';
  import { mobileNavOpen as mobileNavOpenStore, mobileNavOpenSignal } from '$lib/stores/mobileNav';
   import { resetOverlayState } from '$lib/stores/overlays';
  import { workspaceHealth } from '$lib/system';
  import { wsClient, wsState } from '$lib/ws/client';
  import { invalidateAllWorkScopes, invalidateWorkFromSocket } from '$lib/work/workViewState';

  let { children }: { children: Snippet } = $props();

  const navigationItems = [
    { href: '/', label: 'Dashboard', icon: LayoutDashboard },
    { href: '/chat', label: 'Chat', icon: MessageSquareText },
    { href: '/agents', label: 'Agents', icon: Bot },
    { href: '/projects', label: 'Projects', icon: FolderKanban },
    { href: '/tasks', label: 'Tasks', icon: ListTodo },
    { href: '/workflows', label: 'Workflows', icon: Workflow },
    { href: '/schedules', label: 'Schedules', icon: Clock },
    { href: '/docs', label: 'Docs', icon: BookOpen },
    { href: '/tools', label: 'Tools', icon: Wrench },
    { href: '/channels', label: 'Channels', icon: Radio },
    { href: '/local-models', label: 'Local Models', icon: Box },
    { href: '/knowledge', label: 'Knowledge', icon: Library },
    { href: '/settings', label: 'Settings', icon: Settings }
  ];

  function navigationItemActive(href: string, pathname: string): boolean {
    return href === '/' ? pathname === '/' : pathname.startsWith(href);
  }

  let bootstrapped = $state(false);
  let mobileNavOpen = $state(false);
  let sidebarCollapsed = $state(false);
  let mobileHeaderEl = $state<HTMLElement | null>(null);
  let workspaceRunning = false;
  let workspaceLoadingSlow = $state(false);
  let workspaceLoadingSlowTimer: number | null = null;

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

  function currentTitle(pathname: string): string {
    if (pathname === '/') return 'Control Center';
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
  let isChatWindowMode = $derived(isChatDetailRoute && $page.url.searchParams.get('window') === '1');
  let isDashboardRoute = $derived($page.url.pathname === '/');
  let showMobileHeader = $derived(!isChatDetailRoute);
  let shouldReserveBottomTabSpace = $derived(!isChatDetailRoute && !isChatWindowMode);
  let contentShellClass = $derived.by(() => {
    if (isChatWindowMode) {
      return 'min-h-0 min-w-0 flex-1 overflow-hidden';
    }
    if (isChatRoute) {
      return `min-h-0 min-w-0 flex-1 overflow-hidden ${
        showMobileHeader
          ? 'pt-[var(--app-shell-top-offset,0px)] lg:pt-0'
          : 'app-chat-mobile-safe-top lg:pt-0'
      }`;
    }
    // The dashboard is the one non-chat route with a viewport-bounded desktop/
    // tablet layout: at md+ this scroll surface should never need to scroll
    // because ControlCenter fills it exactly (fixed chrome, internal list
    // scrolling). It keeps `overflow-y-auto` (rather than `overflow-hidden`)
    // so it still degrades safely if content ever exceeds the viewport, and
    // narrow/phone widths keep the natural page-scroll behavior below md.
    const dashboardMdLayout = isDashboardRoute ? 'md:flex md:h-full md:min-h-0 md:flex-col' : '';
    return `min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain px-3 sm:px-4 lg:px-0 ${dashboardMdLayout} pt-[calc(var(--app-shell-top-offset,0px)+0.75rem)] lg:pt-0`;
  });

  // Non-chat routes drop the shell's right padding so the data-app-content
  // scroll surface reaches the viewport right edge (no decorative scrollbar
  // gutter). Chat detail/window mode keep symmetric left/right padding so
  // their layout is not regressed to an asymmetric one-sided gutter.
  let shellSpacingClass = $derived(
    isChatRoute
      ? 'app-chat-shell-safe lg:gap-4 lg:px-4'
      : 'app-shell-safe-block lg:gap-4 lg:pl-4'
  );

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

  function clearWorkspaceLoadingSlowTimer(): void {
    if (workspaceLoadingSlowTimer !== null) {
      window.clearTimeout(workspaceLoadingSlowTimer);
      workspaceLoadingSlowTimer = null;
    }
  }

  function startAuthBootstrap(): void {
    bootstrapped = false;
    workspaceLoadingSlow = false;
    clearWorkspaceLoadingSlowTimer();
    void auth.bootstrap().finally(() => {
      bootstrapped = true;
    });
  }

  function retryWorkspaceLoad(): void {
    startAuthBootstrap();
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
    startAuthBootstrap();

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
    const unsubscribeWork = wsClient.subscribe((event) => {
      if (event.type === 'work_invalidated') invalidateWorkFromSocket(event);
    });
    let previousWsStatus = $wsState.status;
    const unsubscribeWsState = wsState.subscribe((state) => {
      if (state.status === 'connected' && previousWsStatus === 'reconnecting') {
        invalidateAllWorkScopes();
      }
      previousWsStatus = state.status;
    });

    return () => {
      resetOverlayState();
       window.removeEventListener('resize', syncMobileHeaderOffset);
      setShellOffsetVariable('--app-shell-top-offset', 0);
      unsubscribeMobileNav();
      unsubscribeWork();
      unsubscribeWsState();
      clearWorkspaceLoadingSlowTimer();
      stopWorkspace();
    };
  });

  $effect(() => {
    if (typeof window === 'undefined') return;
    const loadingWorkspace = !bootstrapped || $auth.status === 'loading';
    if (!loadingWorkspace) {
      workspaceLoadingSlow = false;
      clearWorkspaceLoadingSlowTimer();
      return;
    }
    if (workspaceLoadingSlowTimer !== null) return;
    workspaceLoadingSlowTimer = window.setTimeout(() => {
      workspaceLoadingSlowTimer = null;
      workspaceLoadingSlow = true;
    }, 10_000);
  });

  $effect(() => {
    if (!bootstrapped) {
      return;
    }

    if ($auth.status === 'authenticated') {
      startWorkspace();
      return;
    }

    stopWorkspace();
    const returnTo = `${$page.url.pathname}${$page.url.search}`;
    void goto(`/login?returnTo=${encodeURIComponent(returnTo)}`, { replaceState: true });
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
  <div class="app-fullscreen-safe mx-auto flex max-w-5xl items-center justify-center">
    {#if workspaceLoadingSlow}
      <section class="mx-4 max-w-md rounded-3xl border border-sky-500/30 bg-sky-500/10 px-6 py-8 text-center text-sm text-sky-100 shadow-card">
        <p class="font-medium">Workspace is still loading.</p>
        <p class="mt-2 text-sky-50/80">The app shell is taking longer than expected to restore your session.</p>
        <div class="mt-5 flex justify-center">
          <Button variant="secondary" onclick={retryWorkspaceLoad}>Retry</Button>
        </div>
      </section>
    {:else}
      <LoadingState label="Loading workspace" description="Restoring your Cognis session and preparing the UI shell." />
    {/if}
  </div>
{:else}
  <a class="skip-link" href="#main-content">Skip to content</a>
  <ToastViewport />
  <ConfirmDialog />
  <div class="app-shell-viewport app-viewport-frame fixed inset-x-0 overflow-hidden overscroll-none bg-slate-950">
    <div class={`flex h-full w-full min-w-0 overflow-hidden ${shellSpacingClass}`}>
      {#if !isChatWindowMode}
      <aside
        class={`hidden min-h-0 shrink-0 overflow-hidden whitespace-nowrap rounded-t-3xl border border-b-0 border-slate-800/80 bg-slate-900 shadow-card transition-all duration-200 ease-in-out lg:flex lg:flex-col lg:justify-between ${sidebarExpanded ? 'w-64 p-4' : 'w-14 p-2'}`}
      >
        <div class="min-w-0 min-h-0 flex-1 overflow-y-auto">
          {#if sidebarExpanded}
            <a
              class="flex items-center gap-3 border-b border-slate-800/80 pb-5 rounded-xl transition hover:bg-slate-800/60"
              href="/"
              aria-label="Open Control Center"
              data-testid="sidebar-logo-link"
            >
              <img alt="" class="h-11 w-11 rounded-2xl shadow-card" src="/pwa/icon-192.png" />
              <div class="min-w-0 space-y-1">
                <p class="text-sm font-medium uppercase tracking-[0.3em] text-sky-300">Cognis</p>
                <h1 class="text-xl font-semibold text-white">Agent workspace</h1>
              </div>
            </a>
          {:else}
            <div class="flex justify-center border-b border-slate-800/80 pb-4">
              <a href="/" aria-label="Open Control Center" data-testid="sidebar-logo-link" class="rounded-xl transition hover:opacity-80">
                <img alt="Cognis" class="h-9 w-9 rounded-xl shadow-card" src="/pwa/icon-192.png" />
              </a>
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
                   class={`flex items-center rounded-2xl text-sm transition ${navigationItemActive(item.href, $page.url.pathname) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-800 hover:text-white'} gap-3 px-4 py-3`}
                  href={item.href}
                >
                  <item.icon class="h-4 w-4 shrink-0" />
                  <span>{item.label}</span>
                </a>
              {:else}
                <a
                  use:sidebarTooltip={item.label}
                  aria-label={`Open ${item.label}`}
                   class={`flex items-center justify-center rounded-2xl px-2 py-3 text-sm transition ${navigationItemActive(item.href, $page.url.pathname) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-800 hover:text-white'}`}
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
                {#if $wsState.status === 'stalled'}
                  <Button class="w-full justify-center" size="sm" variant="secondary" onclick={() => wsClient.connect()}>
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
      {/if}

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
        <!-- The iOS-managed viewport already starts below the status area. -->
        <header bind:this={mobileHeaderEl} class="app-keyboard-stable-header fixed inset-x-0 top-0 z-[70] flex shrink-0 items-center justify-between gap-2 border-b border-slate-800/80 bg-slate-950 px-3 py-1 sm:gap-3 sm:px-4 sm:py-1 lg:hidden" style="padding-left: max(0.75rem, env(safe-area-inset-left)); padding-right: max(0.75rem, env(safe-area-inset-right));">
          <div class="flex min-w-0 flex-1 items-center gap-2 lg:hidden">
            <Button aria-label="Open navigation" class="h-10 w-10 lg:hidden md:h-9 md:w-9" size="icon" variant="secondary" onclick={openMobileNav}>
              <Menu class="h-5 w-5" />
            </Button>
            <div class="min-w-0">
              <h2 class="truncate text-base font-semibold text-white sm:text-lg">{currentTitle($page.url.pathname)}</h2>
            </div>
          </div>

          <div class="flex shrink-0 items-center gap-2">
            <span
              class={`inline-flex h-2.5 w-2.5 rounded-full ${$wsState.status === 'connected' ? 'bg-emerald-400' : $wsState.status === 'stalled' ? 'bg-rose-400' : 'bg-sky-400'}`}
              aria-label={`WebSocket ${$wsState.status}`}
              title={`WebSocket ${$wsState.status}${$wsState.status === 'reconnecting' || $wsState.status === 'stalled' ? ` (attempt ${$wsState.attempts}/10)` : ''}`}
            ></span>
            {#if $wsState.status === 'stalled'}
              <Button
                aria-label="Reconnect WebSocket"
                class="h-10 w-10 md:h-9 md:w-9"
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
          use:adaptiveBottomInset={{ disabled: !shouldReserveBottomTabSpace }}
          use:scrollPersist={{ key: $page.url.pathname, disabled: isChatDetailRoute }}
          use:edgeSwipe={{ edge: 'left', onTrigger: handleLeftEdgeSwipe, disabled: isChatDetailRoute || mobileNavOpen || isChatWindowMode }}
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
           class={`flex min-h-[48px] items-center gap-3 rounded-2xl px-4 py-3 text-base transition ${navigationItemActive(item.href, $page.url.pathname) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-900 hover:text-white'}`}
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
          {#if $wsState.status === 'stalled'}
            <Button class="w-full justify-center" variant="secondary" onclick={() => { closeMobileNav(); wsClient.connect(); }}>
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
  <BottomTabBar hidden={isChatDetailRoute || isChatWindowMode} />
{/if}
