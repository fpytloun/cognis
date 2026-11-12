<script lang="ts">
  import { onMount } from 'svelte';
  import AudioPlaylist from '$lib/components/AudioPlaylist.svelte';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import ConversationMode from '$lib/components/ConversationMode.svelte';
  import DeliverableAudio from '$lib/components/DeliverableAudio.svelte';
  import ProviderUsageBars from '$lib/components/ProviderUsageBars.svelte';
  import { elevenlabsUsage } from '$lib/provider-monitor.test-fixtures';
  import ToastViewport from '$lib/components/ToastViewport.svelte';
  import { toastStore } from '$lib/stores/toasts';
  import { auth } from '$lib/stores/auth';
  import { speechRuntime } from '$lib/utils/conversation-speech.test-fixtures';
  import { ListeningCues } from '$lib/utils/listening-cues';
  import { loadUserPreferences } from '$lib/stores/userPreferences';
  import type { MessageTimelineItem } from '$lib/timeline-render-model';
  import type { RuntimeOverlaySnapshot, TimelineItem } from '$lib/chat-v2/types';

  let open = $state(false);
  let cues = $state<ListeningCues>();
  const diagnostics = new URLSearchParams(window.location.search).has('audio-diagnostics');
  let submitted = $state('');
  let runtime = $state<RuntimeOverlaySnapshot | null>(null);
  let items = $state<TimelineItem[]>([]);
  const message: MessageTimelineItem = {
    id: 'speech-browser-message', kind: 'message', role: 'assistant',
    content: 'This is a message for reading.', html: '<p>This is a message for reading.</p>',
    seq: 1, timestamp: null, streaming: false,
  };
  onMount(() => {
    void auth.bootstrap().then(() => {
      if (new URLSearchParams(location.search).has('group_tools')) return loadUserPreferences();
    });
    const unsubscribe = toastStore.subscribe(toasts => {
      for (const toast of toasts) {
        if (toast.variant === 'error') console.error('Speech fixture toast:', toast.message);
      }
    });
    const answer = () => { runtime = speechRuntime('This is the spoken response.', {
      has_active_turn: false, runtime_revision: 2,
    }); };
    window.addEventListener('voice-fixture-response', answer);
    const update = (event: Event) => {
      const detail = (event as CustomEvent<{ runtime: RuntimeOverlaySnapshot; items: TimelineItem[] }>).detail;
      items = detail.items;
      runtime = detail.runtime;
    };
    window.addEventListener('voice-fixture-turn', update);
    return () => {
      unsubscribe();
      window.removeEventListener('voice-fixture-response', answer);
      window.removeEventListener('voice-fixture-turn', update);
    };
  });
</script>

<main style="max-width: 760px; margin: 2rem auto; padding: 1rem">
  {#if $auth.status === 'authenticated'}
  {#if diagnostics}
    <h1>Audio diagnostics</h1>
    <section aria-label="Document audio">
      <p>Document narration with a failed native speech stream.</p>
      <DeliverableAudio sourceRef="dlv_11111111111111111111111111111111" />
    </section>
    <section aria-label="ElevenLabs subscription">
      <h2>ElevenLabs</h2>
      <ProviderUsageBars usage={elevenlabsUsage()} />
    </section>
  {:else}
  <section id="speech-fixture">
    <AudioPlaylist playbackKey="speech-browser-playlist" parts={[{
      artifact_id: 'speech-browser-recording', audio_url: '/speech-fixture.wav',
      content_type: 'audio/wav',
    }]} />
  </section>
  <section id="speech-message-fixture"><ChatMessage item={message} /></section>
   <button onclick={() => { cues = new ListeningCues(); cues.unlock(); open = true; }}>Start voice conversation</button>
  <output data-testid="voice-submitted-text">{submitted}</output>
   {#if open}<ConversationMode {open} {cues} agent={null} conversationId="voice-browser-conversation"
    {runtime} {items} onclose={() => { open = false; }}
    submitText={async (text) => {
      submitted = text; runtime = speechRuntime('');
      return 'voice-input';
    }}
    stopAssistant={async () => true}
    reconcileTurn={async () => ({
      turn_id: 'voice-turn', items: [...items, ...(runtime?.volatile_items ?? [])],
     })} />{/if}
  {/if}
  {:else}
    <p>Verifying fixture account…</p>
  {/if}
</main>
<ToastViewport />
