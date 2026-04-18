<script lang="ts">
  import { page } from '$app/stores';
  import Bot from 'lucide-svelte/icons/bot';
import ListTodo from 'lucide-svelte/icons/list-todo';
import MessageSquareText from 'lucide-svelte/icons/message-square-text';
import Settings from 'lucide-svelte/icons/settings';

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
</script>

{#if !hidden}
  <nav
    class="fixed inset-x-0 bottom-0 z-[60] border-t border-slate-800/80 bg-slate-950/95 backdrop-blur lg:hidden"
    style="padding-bottom: env(safe-area-inset-bottom, 0);"
    aria-label="Primary"
  >
    <ul class="grid grid-cols-4">
      {#each tabs as tab}
        {@const active = isActive(tab.href, $page.url.pathname)}
        {@const Icon = tab.icon}
        <li class="contents">
          <a
            class={`flex min-h-[56px] flex-col items-center justify-center gap-0.5 px-2 py-1.5 text-[11px] transition ${active ? 'text-sky-300' : 'text-slate-400 hover:text-white'}`}
            href={tab.href}
            aria-current={active ? 'page' : undefined}
          >
            <Icon class="h-5 w-5" aria-hidden="true" />
            <span class="font-medium">{tab.label}</span>
          </a>
        </li>
      {/each}
    </ul>
  </nav>
{/if}
