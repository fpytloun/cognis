<script lang="ts">
  import type { Snippet } from 'svelte';
  import { openNativeConversationWindow } from '$lib/native/host';

  let { conversationId, class: className = '', children, reload = false }: {
    conversationId: string; class?: string; children: Snippet; reload?: boolean;
  } = $props();

  function open(event: MouseEvent) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (openNativeConversationWindow(conversationId)) event.preventDefault();
  }
</script>

<a href={`/chat/${encodeURIComponent(conversationId)}`} class={className} data-sveltekit-reload={reload || undefined} onclick={open}>{@render children()}</a>
