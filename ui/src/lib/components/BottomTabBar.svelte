<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import Bot from 'lucide-svelte/icons/bot';
import ListTodo from 'lucide-svelte/icons/list-todo';
  import MessageSquareText from 'lucide-svelte/icons/message-square-text';
  import Settings from 'lucide-svelte/icons/settings';
  import { blockingOverlayActive } from '$lib/stores/overlays';

  /**
   * Mobile bottom tab bar. Shown only below `lg` breakpoint (matches `isMobile`
   * store's pivot). Contains the four most-used top-level destinations.
   *
   * Hidden inside chat detail screens so the composer owns the bottom safe-area.
   */

  interface Props {
    hidden?: boolean;
  }

  let { hidden = false }: Props = $props();
  let navEl = $state<HTMLElement | null>(null);

  const tabs = [
    { href: '/chat', label: 'Chat', icon: MessageSquareText },
    { href: '/tasks', label: 'Tasks', icon: ListTodo },
    { href: '/agents', label: 'Agents', icon: Bot },
    { href: '/settings', label: 'Settings', icon: Settings }
  ];

  function isActive(href: string, pathname: string): boolean {
    if (href === '/chat' && pathname.startsWith('/chat')) return true;
    return pathname.startsWith(href);
  }

  // True when the current route is represented by one of the four tabs.
  const isOnTabRoute = $derived(tabs.some((tab) => isActive(tab.href, $page.url.pathname)));

  function setBottomOffset(value: number): void {
    if (typeof document === 'undefined') return;
    document.documentElement.style.setProperty('--app-shell-bottom-offset', `${Math.max(0, Math.round(value))}px`);
  }

  function syncBottomOffset(): void {
    if (typeof window === 'undefined') return;
    const shouldReserve = !hidden && !$blockingOverlayActive && window.innerWidth < 1024;
    setBottomOffset(shouldReserve ? navEl?.offsetHeight ?? 0 : 0);
  }

  $effect(() => {
    if (typeof window === 'undefined') return;
    void hidden;
    void $blockingOverlayActive;
    void navEl;
    const rafId = window.requestAnimationFrame(syncBottomOffset);
    return () => window.cancelAnimationFrame(rafId);
  });

  $effect(() => {
    if (typeof ResizeObserver === 'undefined') return;
    const element = navEl;
    if (!element) {
      syncBottomOffset();
      return;
    }
    const observer = new ResizeObserver(syncBottomOffset);
    observer.observe(element);
    return () => observer.disconnect();
  });

  onMount(() => {
    syncBottomOffset();
    window.addEventListener('resize', syncBottomOffset);
    return () => {
      window.removeEventListener('resize', syncBottomOffset);
      setBottomOffset(0);
    };
  });
</script>

{#if !hidden && !$blockingOverlayActive}
  <nav
    bind:this={navEl}
    class="fixed inset-x-0 bottom-0 z-[60] border-t border-slate-800/80 bg-slate-950/95 backdrop-blur lg:hidden"
    style="padding-bottom: env(safe-area-inset-bottom, 0); padding-left: env(safe-area-inset-left, 0); padding-right: env(safe-area-inset-right, 0);"
    aria-label="Primary"
  >
    <ul class="grid grid-cols-4">
      {#each tabs as tab}
        {@const active = isActive(tab.href, $page.url.pathname)}
        {@const Icon = tab.icon}
        <li class="contents">
          <a
            class={`flex min-h-[56px] flex-col items-center justify-center gap-0.5 px-2 py-1.5 text-[11px] transition ${
              active
                ? 'text-sky-300'
                : isOnTabRoute
                  ? 'text-slate-400 hover:text-white'
                  : 'text-slate-500 hover:text-slate-300'
            }`}
            href={tab.href}
            aria-current={active ? 'page' : undefined}
          >
            <Icon class="h-5 w-5" aria-hidden="true" />
            <span class="font-medium">{tab.label}</span>
          </a>
        </li>
      {/each}
    </ul>
    {#if !isOnTabRoute}
      <p class="pb-0.5 text-center text-[10px] text-slate-600">Use ☰ for more pages</p>
    {/if}
  </nav>
{/if}
