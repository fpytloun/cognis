<script lang="ts">
  import { onMount } from 'svelte';
  import ChatV2Composer from '$lib/components/chat-v2/ChatV2Composer.svelte';
  let { conversationId, onInitialLoaded, composerFocusRequest, readOnly = false }:
    { conversationId: string; onInitialLoaded?: () => void; composerFocusRequest?: string | null; readOnly?: boolean } = $props();
  onMount(() => { onInitialLoaded?.(); });
</script>
<output data-testid={`fixture-${conversationId}`}>{conversationId}</output>
{#if !readOnly}<ChatV2Composer focusRequest={composerFocusRequest} onSend={() => {}} showMic={false} />{/if}
