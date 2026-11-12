<script lang="ts">
  import type { ComponentProps } from 'svelte';
  import type PinConversationButton from './PinConversationButton.svelte';
  import DeferredView from '../DeferredView.svelte';
  import { headerMenu, type ChatHeaderMenuItem } from './header-menu-definition';
  let { items, conversation }: { items: ChatHeaderMenuItem[];
    conversation?: ComponentProps<typeof PinConversationButton>['conversation'] } = $props();
  let opened = $state(false), trigger = $state<HTMLButtonElement>();
</script>
<div class="relative sm:hidden">
  <button bind:this={trigger} type="button" class="flex h-11 w-10 items-center justify-center rounded-lg text-muted hover:bg-surface-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-accent"
    aria-label="More conversation options" aria-haspopup="menu" aria-expanded={opened} onclick={() => opened = !opened}>
    <svg class="h-5 w-5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg>
  </button>
  {#if opened}
    <div class="absolute right-0 top-full z-50 mt-1 min-w-52">
      <DeferredView load={headerMenu.load} label="Loading conversation actions…">
        {#snippet children(Menu)}<Menu {items} {conversation} {trigger} onClose={() => opened = false} />{/snippet}
      </DeferredView>
    </div>
  {/if}
</div>
