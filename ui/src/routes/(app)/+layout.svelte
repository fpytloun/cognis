<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import {
    Bot,
    BrainCircuit,
    CircleHelp,
    ListTodo,
    Menu,
    MessageSquareText,
    RefreshCw,
    ServerCrash,
    Settings,
    ShieldAlert,
    Wifi,
    Workflow,
    X
  } from 'lucide-svelte';

  import { api } from '$lib/api/client';
  import { deriveGettingStartedSteps, isGettingStartedDismissed } from '$lib/getting-started';
  import ShortcutHelp from '$lib/components/ShortcutHelp.svelte';
  import ToastViewport from '$lib/components/ToastViewport.svelte';
  import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { closeShortcutHelp, openShortcutHelp, requestCancelActiveTurn, requestChatComposerFocus, shortcutHelpOpen } from '$lib/shortcuts';
  import { auth } from '$lib/stores/auth';
  import { workspaceHealth } from '$lib/system';
  import type { SystemDiagnostics } from '$lib/types/api';
  import { wsClient, wsState } from '$lib/ws/client';

  const navigationItems = [
    { href: '/chat', label: 'Chat', icon: MessageSquareText },
    { href: '/agents', label: 'Agents', icon: Bot },
    { href: '/tasks', label: 'Tasks', icon: ListTodo },
    { href: '/workflows', label: 'Workflows', icon: Workflow },
    { href: '/settings', label: 'Settings', icon: Settings }
  ];

  let bootstrapped = false;
  let diagnostics: SystemDiagnostics | null = null;
  let mobileNavOpen = false;

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
    const activeTagIsInput = isTextInputTarget(event.target);
    if (event.key === 'Escape') {
      if ($shortcutHelpOpen) {
        event.preventDefault();
        closeShortcutHelp();
        return;
      }
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

  onMount(() => {
    void auth.bootstrap().then(async () => {
      bootstrapped = true;
      if (auth.getSnapshot().status !== 'authenticated') {
        await goto('/login', { replaceState: true });
        return;
      }
      await loadDiagnosticsIfNeeded();
      wsClient.connect();
      workspaceHealth.start();
    });

    window.addEventListener('keydown', handleGlobalShortcuts);

    return () => {
      window.removeEventListener('keydown', handleGlobalShortcuts);
      wsClient.disconnect();
      workspaceHealth.stop();
    };
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
  <div class="mx-auto flex min-h-screen max-w-5xl items-center justify-center px-6 py-16">
    <LoadingState label="Loading workspace" description="Restoring your Cognis session and preparing the UI shell." />
  </div>
{:else}
  <a class="skip-link" href="#main-content">Skip to content</a>
  <ToastViewport />
  <ConfirmDialog />
  <ShortcutHelp />
  <div class="min-h-screen">
    <div class="mx-auto flex min-h-screen max-w-[1600px] gap-6 px-4 py-4 lg:px-6">
      <aside class="hidden w-72 shrink-0 rounded-3xl border border-slate-800/80 bg-slate-900/80 p-5 shadow-card backdrop-blur lg:flex lg:flex-col lg:justify-between">
        <div>
          <div class="space-y-3 border-b border-slate-800/80 pb-6">
            <p class="text-sm font-medium uppercase tracking-[0.3em] text-sky-300">Cognis</p>
            <div>
              <h1 class="text-2xl font-semibold text-white">Agent workspace</h1>
              <p class="mt-2 text-sm leading-6 text-slate-400">
                Manage conversations, workflows, and controller settings from one SPA shell.
              </p>
            </div>
          </div>

          <nav class="mt-6 space-y-2">
            {#each navigationItems as item}
              <a
                aria-label={`Open ${item.label}`}
                class={`flex items-center justify-between rounded-2xl px-4 py-3 text-sm transition ${$page.url.pathname.startsWith(item.href) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-800 hover:text-white'}`}
                href={item.href}
              >
                <span class="flex items-center gap-3">
                  <svelte:component this={item.icon} class="h-4 w-4" />
                  <span>{item.label}</span>
                </span>
              </a>
            {/each}
          </nav>
        </div>

        <div class="space-y-4 border-t border-slate-800/80 pt-6">
          <div class="space-y-1">
            <p class="text-sm font-medium text-white">{$auth.user?.name ?? $auth.user?.email}</p>
            <p class="text-xs text-slate-400">{$auth.user?.email}</p>
          </div>
          <Button class="w-full justify-center" variant="secondary" onclick={handleLogout}>Sign out</Button>
        </div>
      </aside>

      <main class="flex min-h-[calc(100vh-2rem)] min-w-0 flex-1 flex-col gap-4 rounded-3xl border border-slate-800/80 bg-slate-950/70 p-4 shadow-card backdrop-blur lg:p-6" id="main-content">
        <header class="flex flex-col gap-3 rounded-2xl border border-slate-800/80 bg-slate-900/80 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p class="text-sm font-medium uppercase tracking-[0.25em] text-slate-400">Cognis</p>
            <h2 class="mt-1 text-xl font-semibold text-white">{currentTitle($page.url.pathname)}</h2>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <Button aria-label="Open navigation" class="lg:hidden" size="sm" variant="secondary" onclick={() => (mobileNavOpen = true)}>
              <Menu class="h-4 w-4" />
            </Button>
            <Button size="sm" variant="secondary" onclick={() => goto('/getting-started')}>Getting started</Button>
            <Button aria-label="Open keyboard shortcuts" size="sm" variant="secondary" onclick={openShortcutHelp}>
              <CircleHelp class="h-4 w-4" />
            </Button>
            <Badge class={$wsState.status === 'connected' ? 'border-emerald-400/40 bg-emerald-500/10 text-emerald-200' : 'border-amber-400/40 bg-amber-500/10 text-amber-200'}>
              <span class="inline-flex items-center gap-2">
                <Wifi class="h-3.5 w-3.5" />
                <span>WebSocket: {$wsState.status}</span>
                {#if $wsState.status === 'reconnecting' || $wsState.status === 'stalled'}
                  <span class="text-[11px] uppercase tracking-[0.2em] opacity-80">attempt {$wsState.attempts}/10</span>
                {/if}
              </span>
            </Badge>
            {#if $wsState.status === 'stalled'}
              <Button size="sm" variant="secondary" onclick={() => wsClient.connect()}>Reconnect</Button>
            {/if}
          </div>
        </header>

        {#if outageBanners().length > 0}
          <div class="space-y-3">
            {#each outageBanners() as banner (banner.id)}
              <div class={`rounded-2xl border px-4 py-4 text-sm ${banner.variant === 'warning' ? 'border-amber-500/30 bg-amber-500/10 text-amber-100' : 'border-rose-500/30 bg-rose-500/10 text-rose-100'}`}>
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div class="flex min-w-0 items-start gap-3">
                    <svelte:component this={banner.icon} class="mt-0.5 h-5 w-5 shrink-0" />
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

        <div class="min-h-0 flex-1">
          <slot />
        </div>
      </main>
    </div>
  </div>

  {#if mobileNavOpen}
    <div class="fixed inset-0 z-[70] bg-slate-950/80 backdrop-blur lg:hidden" role="presentation">
      <div class="ml-auto flex h-full w-[min(22rem,100vw)] flex-col border-l border-slate-800 bg-slate-950 px-5 py-5 shadow-card">
        <div class="flex items-center justify-between gap-3 border-b border-slate-800 pb-5">
          <div>
            <p class="text-sm uppercase tracking-[0.25em] text-sky-300">Cognis</p>
            <p class="mt-1 text-sm text-slate-400">{$auth.user?.email}</p>
          </div>
          <Button aria-label="Close navigation" size="sm" variant="secondary" onclick={() => (mobileNavOpen = false)}>
            <X class="h-4 w-4" />
          </Button>
        </div>

        <nav class="mt-5 space-y-2">
          {#each navigationItems as item}
            <a
              class={`flex items-center gap-3 rounded-2xl px-4 py-3 text-sm transition ${$page.url.pathname.startsWith(item.href) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-900 hover:text-white'}`}
              href={item.href}
              onclick={() => (mobileNavOpen = false)}
            >
              <svelte:component this={item.icon} class="h-4 w-4" />
              <span>{item.label}</span>
            </a>
          {/each}
        </nav>

        <div class="mt-auto space-y-3 border-t border-slate-800 pt-5">
          <Button class="w-full justify-center" variant="secondary" onclick={handleLogout}>Sign out</Button>
        </div>
      </div>
    </div>
  {/if}
{/if}
