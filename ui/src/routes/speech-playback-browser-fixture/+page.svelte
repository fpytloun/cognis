<script lang="ts">
  import AudioPlaylist from '$lib/components/AudioPlaylist.svelte';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import ConversationMode from '$lib/components/ConversationMode.svelte';
  import type { RuntimeOverlaySnapshot } from '$lib/chat-v2/types';
  import { speechRuntime } from '$lib/utils/conversation-speech.test-fixtures';
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth';
  let conversationOpen = $state(false);
  let runtime = $state<RuntimeOverlaySnapshot | null>(null);
  let submittedText = $state('');
  onMount(() => {
    void auth.bootstrap();
    const respond = () => {
      runtime = speechRuntime('This is the spoken response.', {
        runtime_revision: 2, has_active_turn: false,
      });
    };
    window.addEventListener('voice-fixture-response', respond);
    return () => window.removeEventListener('voice-fixture-response', respond);
  });
</script>

<svelte:head><title>Speech playback browser fixture</title></svelte:head>

<main id="speech-fixture" class="p-4">
  <AudioPlaylist
    playbackKey="browser-synthetic-audio"
    parts={[{
      artifact_id: 'browser-synthetic-part',
      audio_url: '/speech-fixture.wav',
      content_type: 'audio/wav'
    }]}
  />
</main>

<section id="speech-message-fixture" class="p-4">
  <p>Fixture session: {$auth.status} {$auth.error ?? ''}</p>
  {#if $auth.status === 'authenticated'}<ChatMessage item={{
    id: 'speech-browser-message',
    messageId: 'speech-browser-message',
    kind: 'message',
    role: 'assistant',
    content: 'This assistant message exercises the complete speech playback path.',
    html: '',
    seq: 1,
    timestamp: null
  }} />{/if}
</section>

<section class="p-4">
  <button onclick={() => { conversationOpen = true; }}>Start voice conversation</button>
  <output data-testid="voice-submitted-text">{submittedText}</output>
</section>

<ConversationMode
  open={conversationOpen}
  agent={null}
  conversationId="browser-voice-conversation"
  onclose={() => { conversationOpen = false; }}
  {runtime}
  submitText={async (text) => {
    submittedText = text;
    runtime = speechRuntime('');
    return 'voice-input';
  }}
  reconcileTurn={async () => runtime?.has_active_turn ? null : {
    turn_id: 'voice-turn', items: runtime?.volatile_items ?? [],
  }}
  stopAssistant={async () => true}
/>
