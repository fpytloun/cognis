<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import FeedbackPanel from './FeedbackPanel.svelte';
  import ChatV2Composer from '../chat-v2/ChatV2Composer.svelte';
  import { conversationComposer } from '$lib/chat-v2/conversation-composer';
  import type { FeedbackController } from '$lib/feedback/state';
  let { controller, conversationId, onSend }: {
    controller: FeedbackController; conversationId: string; onSend: () => void;
  } = $props();
  const composer = conversationComposer();
  let feedback = $state(untrack(() => composer.hydrate(conversationId).input.feedback));
  onMount(() => composer.subscribe(conversationId, input => { feedback = input.feedback; }));
</script>
<FeedbackPanel {controller} />
<ChatV2Composer {conversationId} {feedback} {onSend} onStop={() => {}} onFiles={() => {}}
  sendAriaLabel="Send reviewed feedback" showMic={false} />
