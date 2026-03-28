<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { auth } from '$lib/stores/auth';
  import { wsClient, wsState } from '$lib/ws/client';

  const navigationItems = [
    { href: '/chat', label: 'Chat' },
    { href: '/agents', label: 'Agents' },
    { href: '/tasks', label: 'Tasks' },
    { href: '/workflows', label: 'Workflows' },
    { href: '/settings', label: 'Settings' }
  ];

  let bootstrapped = false;

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
      wsClient.connect();
    });

    return () => {
      wsClient.disconnect();
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
                class={`flex items-center justify-between rounded-2xl px-4 py-3 text-sm transition ${$page.url.pathname.startsWith(item.href) ? 'bg-sky-500/20 text-white' : 'text-slate-300 hover:bg-slate-800 hover:text-white'}`}
                href={item.href}
              >
                <span>{item.label}</span>
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

      <main class="flex min-h-[calc(100vh-2rem)] min-w-0 flex-1 flex-col gap-4 rounded-3xl border border-slate-800/80 bg-slate-950/70 p-4 shadow-card backdrop-blur lg:p-6">
        <header class="flex flex-col gap-3 rounded-2xl border border-slate-800/80 bg-slate-900/80 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p class="text-sm font-medium uppercase tracking-[0.25em] text-slate-400">Openclaw Controller</p>
            <h2 class="mt-1 text-xl font-semibold text-white">{currentTitle($page.url.pathname)}</h2>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <Badge class={$wsState.status === 'connected' ? 'border-emerald-400/40 bg-emerald-500/10 text-emerald-200' : 'border-amber-400/40 bg-amber-500/10 text-amber-200'}>
              WebSocket: {$wsState.status}
            </Badge>
            {#if $wsState.status === 'stalled'}
              <Button size="sm" variant="secondary" onclick={() => wsClient.connect()}>Reconnect</Button>
            {/if}
          </div>
        </header>

        <div class="min-h-0 flex-1">
          <slot />
        </div>
      </main>
    </div>
  </div>
{/if}
