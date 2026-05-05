<script lang="ts">
  import Headphones from 'lucide-svelte/icons/headphones';
  import MicOff from 'lucide-svelte/icons/mic-off';
  import Mic from 'lucide-svelte/icons/mic';
  import X from 'lucide-svelte/icons/x';

  import { onDestroy, untrack } from 'svelte';
  import { api } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import { audioPlayer } from '$lib/stores/audio-player';
  import { addToast } from '$lib/stores/toasts';
  import { AudioQueue } from '$lib/utils/audio-queue';
  import type { Agent } from '$lib/types/api';

  /**
   * Bidirectional conversation mode overlay.
   *
   * Sends ``enable_tts`` over the chat WebSocket on open and listens to
   * ``tts_sentence_ready`` frames; for each sentence it calls
   * ``/api/v1/tts/synthesize`` and enqueues audio for sequential playback.
   * The mic re-arms automatically once the assistant finishes speaking; a
   * simple energy-based VAD detects end of utterance.
   */

  interface Props {
    open: boolean;
    conversationId: string;
    agent: Agent | null;
    onclose: () => void;
    sendEnableTts: (voice: string | null) => void;
    sendDisableTts: () => void;
    submitText: (text: string) => void;
    /** Subscribe to ``tts_sentence_ready`` frames; returns unsubscribe. */
    subscribeSentenceReady: (
      handler: (frame: { message_id: string; sentence_index: number; text: string }) => void
    ) => () => void;
    /** Subscribe to ``message_complete`` frames (so we know to re-listen). */
    subscribeMessageComplete: (handler: () => void) => () => void;
  }

  let {
    open,
    conversationId,
    agent,
    onclose,
    sendEnableTts,
    sendDisableTts,
    submitText,
    subscribeSentenceReady,
    subscribeMessageComplete
  }: Props = $props();

  type ModeState = 'idle' | 'listening' | 'processing' | 'speaking';

  let modeState: ModeState = $state('idle');
  let muted = $state(false);
  let transcript: Array<{ role: 'user' | 'assistant'; text: string }> = $state([]);

  let queue: AudioQueue | null = null;
  let recorder: MediaRecorder | null = null;
  let stream: MediaStream | null = null;
  let chunks: Blob[] = [];

  let audioContext: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let vadHandle: ReturnType<typeof setTimeout> | null = null;
  let lastVoiceAt = 0;
  let speakingDetected = false;
  let unsubscribeSentence: (() => void) | null = null;
  let unsubscribeMessage: (() => void) | null = null;
  let unsubscribePlaying: (() => void) | null = null;
  let unsubscribeIdle: (() => void) | null = null;
  let loopActive = false;
  let assistantTurnActive = false;
  let pendingSentenceSyntheses = 0;
  let listeningGeneration = 0;
  const activeSentenceKeys = new Set<string>();
  // True only between ``endUtterance()`` (VAD-driven) and the resulting
  // ``recorder.onstop`` firing. ``teardownAudio`` does NOT set this, so a
  // forced stop (e.g. user clicked End conversation, mute, or close) can
  // be distinguished from a real utterance handoff and skip STT entirely.
  let utteranceFinalizing = false;
  // True once ``teardown()`` has been called for this open cycle. Async
  // continuations (STT response, TTS sentence frames, queue idle) all
  // gate on this so they do not fire ``submitText``/``audioPlayer.play``
  // after the overlay has been closed.
  let disposed = true;

  const VAD_FRAME_MS = 100;
  const VAD_RMS_THRESHOLD = 0.018;
  const VAD_SILENCE_MS = 1500;
  const MIN_UTTERANCE_MS = 500;

  function pickMimeType(): string {
    const candidates = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/mp4'
    ];
    for (const candidate of candidates) {
      if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(candidate)) {
        return candidate;
      }
    }
    return '';
  }

  function extensionFor(mimeType: string): string {
    if (mimeType.includes('webm')) return 'webm';
    if (mimeType.includes('ogg')) return 'ogg';
    if (mimeType.includes('mp4')) return 'm4a';
    if (mimeType.includes('wav')) return 'wav';
    return 'bin';
  }

  function clearVad(): void {
    if (vadHandle !== null) {
      clearInterval(vadHandle);
      vadHandle = null;
    }
    lastVoiceAt = 0;
    // Note: ``speakingDetected`` is intentionally NOT reset here so the
    // ``recorder.onstop`` handler that runs immediately after a VAD-driven
    // ``endUtterance`` can decide whether the captured blob actually
    // contained speech. ``startListening()`` resets it for the next cycle.
  }

  function sentenceCacheMessageId(messageId: string, sentenceIndex: number): string {
    return `${messageId}:tts_sentence:${sentenceIndex}`;
  }

  function shouldStartListening(): boolean {
    return (
      !disposed &&
      open &&
      !muted &&
      !assistantTurnActive &&
      pendingSentenceSyntheses === 0 &&
      (queue?.isEmpty() ?? true) &&
      recorder === null &&
      stream === null
    );
  }

  function maybeStartListening(): void {
    if (shouldStartListening()) {
      void startListening();
    }
  }

  function teardownAudio(): void {
    listeningGeneration += 1;
    clearVad();
    // Forced stop. Detach the onstop handler so it does NOT fire
    // ``onUtterance`` with whatever was captured before the user ended
    // the session — Whisper happily hallucinates phrases on near-silent
    // audio, which would then be ``submitText``-ed as a chat message.
    utteranceFinalizing = false;
    if (recorder && recorder.state !== 'inactive') {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      try {
        recorder.stop();
      } catch {
        // ignore
      }
    }
    recorder = null;
    chunks = [];
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
    if (audioContext) {
      try {
        void audioContext.close();
      } catch {
        // ignore
      }
      audioContext = null;
    }
    analyser = null;
    speakingDetected = false;
    modeState = 'idle';
  }

  async function startListening(): Promise<void> {
    if (!shouldStartListening()) return;
    const generation = ++listeningGeneration;
    modeState = 'listening';
    chunks = [];
    speakingDetected = false;
    lastVoiceAt = 0;
    let nextStream: MediaStream;
    try {
      nextStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      if (disposed || generation !== listeningGeneration || !open) return;
      addToast('Microphone access denied. Allow it in browser settings.', 'error');
      teardownAudio();
      onclose();
      return;
    }
    if (disposed || generation !== listeningGeneration || !open || muted) {
      nextStream.getTracks().forEach((track) => track.stop());
      return;
    }
    stream = nextStream;
    const mimeType = pickMimeType();
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch {
      if (disposed || generation !== listeningGeneration || !open) {
        teardownAudio();
        return;
      }
      addToast('Recording is not supported in this browser', 'error');
      teardownAudio();
      onclose();
      return;
    }
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    };
    recorder.onstop = () => {
      if (generation === listeningGeneration) {
        recorder = null;
      }
      // Only treat this as a real utterance handoff when ``endUtterance``
      // (VAD-driven) marked it. Forced stops from teardown/mute/close are
      // explicitly NOT finalizing — drop the bytes and do not call STT.
      const finalizing = utteranceFinalizing;
      utteranceFinalizing = false;
      if (!finalizing || disposed || !speakingDetected) {
        chunks = [];
        return;
      }
      const captured = new Blob(chunks, { type: mimeType || 'audio/webm' });
      chunks = [];
      void onUtterance(captured, extensionFor(mimeType));
    };
    recorder.start();

    // Set up energy-based VAD on the same stream.
    if (disposed || generation !== listeningGeneration || !open || stream === null) {
      teardownAudio();
      return;
    }
    audioContext = new AudioContext();
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 1024;
    const source = audioContext.createMediaStreamSource(stream);
    source.connect(analyser);
    const buffer = new Float32Array(analyser.fftSize);
    const utteranceStartedAt = Date.now();

    vadHandle = setInterval(() => {
      if (disposed || generation !== listeningGeneration || !open) {
        teardownAudio();
        return;
      }
      if (!analyser) return;
      analyser.getFloatTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i += 1) {
        sum += buffer[i] * buffer[i];
      }
      const rms = Math.sqrt(sum / buffer.length);
      const now = Date.now();
      if (rms > VAD_RMS_THRESHOLD) {
        speakingDetected = true;
        lastVoiceAt = now;
      } else if (
        speakingDetected &&
        now - lastVoiceAt > VAD_SILENCE_MS &&
        now - utteranceStartedAt > MIN_UTTERANCE_MS
      ) {
        endUtterance();
      }
    }, VAD_FRAME_MS);
  }

  function endUtterance(): void {
    // VAD-driven stop. Mark this as a real utterance handoff so the
    // ``recorder.onstop`` handler proceeds with STT.
    utteranceFinalizing = true;
    clearVad();
    if (recorder && recorder.state !== 'inactive') {
      try {
        recorder.stop();
      } catch {
        utteranceFinalizing = false;
      }
    }
    if (audioContext) {
      try {
        void audioContext.close();
      } catch {
        // ignore
      }
      audioContext = null;
    }
    analyser = null;
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
  }

  async function onUtterance(blob: Blob, ext: string): Promise<void> {
    if (disposed) return;
    if (blob.size === 0) {
      void startListening();
      return;
    }
    modeState = 'processing';
    try {
      const filename = `voice-${Date.now()}.${ext}`;
      const result = await api.stt.transcribe(blob, { filename });
      if (disposed) return;
      const text = result.text.trim();
      if (!text) {
        // Heard nothing — go back to listening.
        void startListening();
        return;
      }
      transcript = [...transcript, { role: 'user', text }];
      assistantTurnActive = true;
      submitText(text);
    } catch (err) {
      if (disposed) return;
      const message = err instanceof Error ? err.message : 'Transcription failed';
      addToast(message, 'error', 4_000, 'Voice transcription failed');
      void startListening();
    }
  }

  async function handleSentenceReady(frame: {
    message_id: string;
    sentence_index: number;
    text: string;
  }): Promise<void> {
    if (disposed) return;
    const sentenceKey = `${frame.message_id}:${frame.sentence_index}`;
    if (activeSentenceKeys.has(sentenceKey)) return;
    activeSentenceKeys.add(sentenceKey);
    pendingSentenceSyntheses += 1;
    modeState = 'speaking';
    try {
      const result = await api.tts.synthesize({
        text: frame.text,
        message_id: sentenceCacheMessageId(frame.message_id, frame.sentence_index),
        agent_id: agent?.agent_id ?? null
      });
      if (disposed) return;
      queue?.enqueue({ url: result.audio_url, id: `${frame.message_id}:${frame.sentence_index}` });
      // Update transcript drawer (append to last assistant entry or create one).
      transcript = updateTranscriptForAssistant(transcript, frame.message_id, frame.text);
    } catch (err) {
      if (disposed) return;
      const message = err instanceof Error ? err.message : 'Synthesis failed';
      addToast(message, 'error', 4_000, 'Voice synthesis failed');
    } finally {
      pendingSentenceSyntheses = Math.max(0, pendingSentenceSyntheses - 1);
      maybeStartListening();
    }
  }

  function updateTranscriptForAssistant(
    existing: Array<{ role: 'user' | 'assistant'; text: string }>,
    _messageId: string,
    appended: string
  ): Array<{ role: 'user' | 'assistant'; text: string }> {
    const last = existing[existing.length - 1];
    if (last && last.role === 'assistant') {
      const updated = { ...last, text: `${last.text} ${appended}`.trim() };
      return [...existing.slice(0, -1), updated];
    }
    return [...existing, { role: 'assistant', text: appended }];
  }

  function handleMessageComplete(): void {
    if (disposed) return;
    assistantTurnActive = false;
    // Queue may still be playing the last sentence; the idle callback
    // re-arms the mic when playback drains.
    maybeStartListening();
  }

  function startConversationLoop(): void {
    if (loopActive) return;
    loopActive = true;
    disposed = false;
    assistantTurnActive = false;
    pendingSentenceSyntheses = 0;
    activeSentenceKeys.clear();
    transcript = [];
    if (!queue) {
      queue = new AudioQueue();
      unsubscribeIdle = queue.onIdle(() => {
        // Only re-arm when the assistant turn is complete, all sentence
        // synthesis calls have settled, and playback has drained.
        maybeStartListening();
      });
      unsubscribePlaying = queue.onPlayingChange((playing) => {
        if (disposed) return;
        if (playing) {
          modeState = 'speaking';
          // Pause any per-message TTS playback to enforce single-stream invariant.
          audioPlayer.stop();
        }
      });
    }
    unsubscribeSentence = subscribeSentenceReady((frame) => void handleSentenceReady(frame));
    unsubscribeMessage = subscribeMessageComplete(handleMessageComplete);
    const llmConfig = (agent?.llm_config ?? {}) as Record<string, unknown>;
    const voice = typeof llmConfig.voice === 'string' && llmConfig.voice.trim() ? llmConfig.voice : null;
    sendEnableTts(voice);
    void startListening();
  }

  function teardown(): void {
    const shouldDisableTts = loopActive;
    loopActive = false;
    disposed = true;
    assistantTurnActive = false;
    pendingSentenceSyntheses = 0;
    activeSentenceKeys.clear();
    if (queue) {
      queue.clear();
      queue = null;
    }
    teardownAudio();
    if (unsubscribeSentence) {
      unsubscribeSentence();
      unsubscribeSentence = null;
    }
    if (unsubscribeMessage) {
      unsubscribeMessage();
      unsubscribeMessage = null;
    }
    if (unsubscribePlaying) {
      unsubscribePlaying();
      unsubscribePlaying = null;
    }
    if (unsubscribeIdle) {
      unsubscribeIdle();
      unsubscribeIdle = null;
    }
    if (shouldDisableTts) {
      sendDisableTts();
    }
  }

  function handleClose(): void {
    teardown();
    onclose();
  }

  function toggleMute(): void {
    muted = !muted;
    if (muted) {
      teardownAudio();
      modeState = 'idle';
    } else {
      maybeStartListening();
    }
  }

  $effect(() => {
    if (open) {
      untrack(startConversationLoop);
      return () => {
        untrack(teardown);
      };
    }
  });

  onDestroy(() => {
    teardown();
  });

  function stateLabel(s: ModeState): string {
    if (muted) return 'Muted — tap to listen';
    switch (s) {
      case 'listening':
        return 'Listening…';
      case 'processing':
        return 'Transcribing…';
      case 'speaking':
        return 'Speaking…';
      default:
        return 'Connecting…';
    }
  }
</script>

{#if open}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/95 backdrop-blur-md" role="dialog" aria-modal="true" aria-label="Voice conversation mode">
    <button
      type="button"
      class="absolute right-5 top-5 inline-flex h-10 w-10 items-center justify-center rounded-full bg-slate-900/80 text-slate-300 transition hover:bg-slate-800 hover:text-white"
      aria-label="End conversation"
      onclick={handleClose}
    >
      <X class="h-5 w-5" />
    </button>

    <div class="flex w-full max-w-2xl flex-col items-center gap-6 px-6">
      <div class="relative h-48 w-48 sm:h-64 sm:w-64">
        <div
          class="absolute inset-0 rounded-full bg-gradient-to-br from-sky-500/40 via-cyan-500/30 to-violet-500/40 transition"
          class:animate-pulse={modeState === 'speaking' || modeState === 'listening'}
        ></div>
        <div class="absolute inset-3 rounded-full bg-slate-950 shadow-2xl shadow-sky-500/30"></div>
        <div class="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center">
          <Headphones class="h-12 w-12 text-sky-300" />
          <span class="text-sm font-medium uppercase tracking-[0.3em] text-slate-300">Conversation</span>
        </div>
      </div>

      <div class="text-center">
        <p class="text-base font-medium text-slate-100">{stateLabel(modeState)}</p>
        <p class="mt-1 text-xs text-slate-400">{conversationId}</p>
      </div>

      <div class="flex items-center gap-3">
        <Button variant="secondary" type="button" onclick={toggleMute} aria-pressed={muted}>
          {#if muted}
            <Mic class="h-4 w-4 sm:mr-2" />
            <span class="hidden sm:inline">Unmute</span>
          {:else}
            <MicOff class="h-4 w-4 sm:mr-2" />
            <span class="hidden sm:inline">Mute mic</span>
          {/if}
        </Button>
        <Button variant="danger" type="button" onclick={handleClose}>
          End conversation
        </Button>
      </div>

      {#if transcript.length > 0}
        <div class="max-h-48 w-full overflow-y-auto rounded-xl border border-slate-800 bg-slate-900/50 p-3 text-sm text-slate-200">
          {#each transcript as entry, i (i)}
            <p class={`mb-2 last:mb-0 ${entry.role === 'user' ? 'text-sky-300' : 'text-slate-200'}`}>
              <span class="text-[10px] font-medium uppercase tracking-wider text-slate-500">{entry.role}</span>
              <span class="ml-2">{entry.text}</span>
            </p>
          {/each}
        </div>
      {/if}
    </div>
  </div>
{/if}
