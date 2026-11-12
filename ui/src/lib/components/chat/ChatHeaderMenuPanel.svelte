<script lang="ts">
  import { onMount, type ComponentProps } from 'svelte';
  import PinConversationButton from './PinConversationButton.svelte';
  import type { ChatHeaderMenuItem } from './header-menu-definition';
  import MessageCircle from 'lucide-svelte/icons/message-circle';
  import MessagesSquare from 'lucide-svelte/icons/messages-square';
  import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
  import Search from 'lucide-svelte/icons/search';
  const icons = { comment: MessageCircle, comments: MessagesSquare, resume: RotateCcw, search: Search };
  let { items, conversation, trigger, onClose }: { items: ChatHeaderMenuItem[];
    conversation?: ComponentProps<typeof PinConversationButton>['conversation']; trigger?: HTMLButtonElement; onClose: () => void } = $props();
  let root: HTMLDivElement;
  onMount(() => root.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus({ preventScroll: true }));
</script>
<svelte:window onpointerdown={event => { if (event.target instanceof Node && !root?.contains(event.target) && !trigger?.contains(event.target)) onClose(); }}
  onkeydown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); trigger?.focus(); } }} />
<div bind:this={root} role="menu" tabindex="-1" aria-label="Conversation options" class="rounded-xl border border-separator bg-elevated p-1 text-sm text-foreground shadow-xl"
  onkeydown={event => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled),a')];
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    event.preventDefault(); buttons[(index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length]?.focus();
  }}
  onclick={event => { if (event.target instanceof Element && event.target.closest('button,a')) onClose(); }}>
  {#each items as item}
    {@const Icon = icons[item.icon]}
    <button type="button" role={item.checked === undefined ? 'menuitem' : 'menuitemcheckbox'} aria-checked={item.checked}
      class="flex min-h-11 w-full items-center gap-3 rounded-lg px-3 text-left hover:bg-surface-hover disabled:opacity-40" disabled={item.disabled} onclick={event => { event.stopPropagation(); onClose(); trigger?.focus({ preventScroll: true }); item.run(); }}>
      <Icon class="h-4 w-4 shrink-0" aria-hidden="true" />
      <span class="flex-1">{item.label}</span>
      {#if item.checked}<span aria-hidden="true">✓</span>{/if}
    </button>
  {/each}
  {#if conversation}<PinConversationButton {conversation} />{/if}
</div>
