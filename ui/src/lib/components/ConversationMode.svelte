<script lang="ts">
  import Headphones from 'lucide-svelte/icons/headphones';
  import MicOff from 'lucide-svelte/icons/mic-off';
  import Mic from 'lucide-svelte/icons/mic';
  import X from 'lucide-svelte/icons/x';

  import { onDestroy, untrack } from 'svelte';
  import { api } from '$lib/api/client';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import ChatV2TimelineList from '$lib/components/chat-v2/ChatV2TimelineList.svelte';
  import { appDocumentAudioClient } from '$lib/app-document-audio';
  import { readDocumentNarration } from '$lib/document-audio';
  import { isVisibleTimelineItem } from '$lib/chat-v2/tool-groups';
  import { haptic } from '$lib/haptics';
  import { audioPlayer } from '$lib/stores/audio-player';
  import { userPreferences } from '$lib/stores/userPreferences';
  import { addToast } from '$lib/stores/toasts';
  import { AudioQueue, type AudioQueueEntry } from '$lib/utils/audio-queue';
  import { SpeechPlayback } from '$lib/utils/speech-playback';
  import { LiveTranscription } from '$lib/utils/live-transcription';
  import { calibrateNoise, VoiceActivity } from '$lib/utils/voice-activity';
  import { ListeningCues } from '$lib/utils/listening-cues';
  import { ConversationSpeech, type SettledVoiceTurn } from '$lib/utils/conversation-speech';
  import type { RuntimeOverlaySnapshot, TimelineItem, TurnCycleState } from '$lib/chat-v2/types';
  import { ScreenWakeLock } from '$lib/utils';
  import {
    audioExtensionForMimeType,
    normalizeVoiceLevel,
    pickSupportedAudioMimeType,
    rmsFromTimeDomainData,
    stopMediaStreamTracks,
  } from '$lib/utils/voice-recorder';
  import type { Agent } from '$lib/types/api';

  /**
   * Bidirectional conversation mode overlay.
   *
   * Projects the admitted Chat v2 turn into spoken sentences; for each it calls
   * ``/api/v1/tts/synthesize`` and enqueues audio for sequential playback.
   * The mic re-arms automatically once the assistant finishes speaking; a
   * simple energy-based VAD detects end of utterance.
   */

  interface Props {
    open: boolean;
    conversationId: string;
    agent: Agent | null;
    onclose: () => void;
    submitText: (text: string) => Promise<string | null>;
    reconcileTurn: (clientMessageId: string) => Promise<SettledVoiceTurn | null>;
    stopAssistant: () => Promise<boolean>;
    runtime: RuntimeOverlaySnapshot | null;
    items?: readonly TimelineItem[];
    cycleStates?: TurnCycleState[];
    cues?: ListeningCues;
  }

  let {
    open,
    conversationId,
    agent,
    onclose,
    submitText,
    reconcileTurn,
    stopAssistant,
    runtime,
    items = [],
    cycleStates = [],
    cues = new ListeningCues(),
  }: Props = $props();
  const loopOpen = $derived(open);

  type ModeState = 'idle' | 'listening' | 'processing' | 'waiting' | 'speaking';

  let modeState: ModeState = $state('idle');
  let muted = $state(false);
  let transcript: Array<{ role: 'user' | 'assistant'; text: string }> = $state([]);
  let transcriptOpen = $state(false);
  let voiceLevel = $state(0);
  let listeningHint = $state('');
  let missedUtterance = $state('');

  let queue: AudioQueue | null = null;
  let recorder: MediaRecorder | null = null;
  let chunks: Blob[] = [];

  let audioContext: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let vadHandle: ReturnType<typeof setTimeout> | null = null;
  let speakingDetected = false;
  let speech: ConversationSpeech | null = null;
  let admissionPending = $state(false);
  let admissionRuntime: RuntimeOverlaySnapshot[] = [];
  let sttController: AbortController | null = null;
  let transcribing = false;
  let unsubscribePlaying: (() => void) | null = null;
  let unsubscribeIdle: (() => void) | null = null;
  let unsubscribePlaybackError: (() => void) | null = null;
  let wakeLock: ScreenWakeLock | null = null;
  let loopActive = false;
  let assistantTurnActive = $state(false);
  let pendingSentenceSyntheses = $state(0);
  let listeningGeneration = 0;
  let listeningStarting = $state(false);
  let listeningReady = $state(false);
  const listeningCues = untrack(() => cues);
  let voiceTurnId = $state<string | null>(null);
  let playingAudio = $state(false);
  let preparingDeliverables = $state(false);
  const deliverables: string[] = [];
  const sentenceTasks = new Set<Promise<void>>();
  const activityItems = $derived([...new Map(
    [...items, ...(runtime?.volatile_items ?? [])]
      .filter(item => (item.kind === 'tool_call' || item.kind === 'thinking')
        && voiceTurnId !== null && item.turn_id === voiceTurnId)
      .map(item => [item.id, item]),
  ).values()]);
  const showActivity = $derived(
    (assistantTurnActive || pendingSentenceSyntheses > 0 || playingAudio)
    && activityItems.some(item => item.kind === 'tool_call' && isVisibleTimelineItem(item, $userPreferences)),
  );
  let audioMessageId: string | null = null;
  let nextAudioSentenceIndex = 0;
  const activeSentenceKeys = new Set<string>();
  const ignoredTtsMessageIds = new Set<string>();
  const readyAudioByIndex = new Map<number, AudioQueueEntry>();
  let streamingTts = false;
  let streamingStt = false;
  let sttSampleRate = 24_000;
  let liveStt: LiveTranscription | null = null;
  let liveStartup: Promise<LiveTranscription> | null = null;
  let liveTranscript = $state('');
  const failedAudioIndexes = new Set<number>();
  const pendingTtsControllers = new Set<AbortController>();
  /**
   * Every ``MediaStream`` ever acquired by this overlay. iOS Safari (PWA)
   * keeps the microphone indicator alive until every track on every stream
   * is explicitly stopped, so we register each stream the moment we
   * receive it and walk the registry on teardown / watchdog.
   */
  const acquiredStreams = new Set<MediaStream>();
  let teardownWatchdog: ReturnType<typeof setTimeout> | null = null;
  let missedRestartTimer: number | null = null;
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
  let playbackNeedsGesture = $state(false);
  let playbackErrorNotified = false;
  let assistantTurnWatchdog: ReturnType<typeof setTimeout> | null = null;
  let cycle = 0;
  let setupError = $state('');
  let interrupting = $state(false);
  let adaptMessages = false;
  const narrativeFrames = new Map<string, Map<number, string>>();
  const voiceWarnings = new Set<string>();

  const VAD_FRAME_MS = 100;
  const VAD_HINT_MS = 5000;
  // A timed-out synthesis may have been billed; never retry it automatically.
  const TTS_SENTENCE_TIMEOUT_MS = 15_000;
  const STT_TIMEOUT_MS = 30_000;
  const ASSISTANT_TURN_WATCHDOG_MS = 120_000;

  function clearVad(): void {
    if (vadHandle !== null) {
      clearInterval(vadHandle);
      vadHandle = null;
    }
    voiceLevel = 0;
    // Note: ``speakingDetected`` is intentionally NOT reset here so the
    // ``recorder.onstop`` handler that runs immediately after a VAD-driven
    // ``endUtterance`` can decide whether the captured blob actually
    // contained speech. ``startListening()`` resets it for the next cycle.
  }

  function followActivity(node: HTMLElement) {
    let following = true;
    const onScroll = () => {
      following = node.scrollHeight - node.clientHeight - node.scrollTop < 48;
    };
    const observer = new ResizeObserver(() => {
      if (following) node.scrollTop = node.scrollHeight;
    });
    observer.observe(node.firstElementChild!);
    node.addEventListener('scroll', onScroll, { passive: true });
    return { destroy() { observer.disconnect(); node.removeEventListener('scroll', onScroll); } };
  }

  function clearMissedRestartTimer(): void {
    if (missedRestartTimer !== null) {
      clearTimeout(missedRestartTimer);
      missedRestartTimer = null;
    }
  }

  function clearAssistantTurnWatchdog(): void {
    if (assistantTurnWatchdog !== null) {
      clearTimeout(assistantTurnWatchdog);
      assistantTurnWatchdog = null;
    }
  }

  function armAssistantTurnWatchdog(): void {
    clearAssistantTurnWatchdog();
    assistantTurnWatchdog = setTimeout(() => {
      assistantTurnWatchdog = null;
      if (disposed) return;
      // Do not re-arm the mic over an execution that may still be running.
      listeningHint = 'The assistant is taking longer than expected. Tap to interrupt, or keep waiting.';
    }, ASSISTANT_TURN_WATCHDOG_MS);
  }

  function sentenceCacheMessageId(messageId: string, sentenceIndex: number): string {
    return `${messageId}:tts_sentence:${sentenceIndex}`;
  }

  function shouldStartListening(): boolean {
    return (
      !disposed &&
      open &&
      !muted &&
      !listeningStarting &&
      !transcribing &&
      !admissionPending &&
      !assistantTurnActive &&
      pendingSentenceSyntheses === 0 &&
      (queue?.isEmpty() ?? true) &&
      recorder === null &&
      acquiredStreams.size === 0
    );
  }

  function maybeStartListening(): void {
    if (shouldStartListening()) {
      void startListening();
    }
  }

  function drainReadyAudio(): void {
    while (true) {
      if (failedAudioIndexes.has(nextAudioSentenceIndex)) {
        failedAudioIndexes.delete(nextAudioSentenceIndex);
        nextAudioSentenceIndex += 1;
        continue;
      }
      const entry = readyAudioByIndex.get(nextAudioSentenceIndex);
      if (!entry) break;
      readyAudioByIndex.delete(nextAudioSentenceIndex);
      queue?.enqueue(entry);
      nextAudioSentenceIndex += 1;
    }
  }

  function resetAudioOrdering(messageId: string | null): void {
    audioMessageId = messageId;
    nextAudioSentenceIndex = 0;
    readyAudioByIndex.clear();
    failedAudioIndexes.clear();
    playbackErrorNotified = false;
  }

  function selectedVoice(): string | null {
    const llmConfig = (agent?.llm_config ?? {}) as Record<string, unknown>;
    return typeof llmConfig.voice === 'string' && llmConfig.voice.trim() ? llmConfig.voice : null;
  }

  /**
   * Run a single TTS attempt with a hard client-side timeout via
   * ``AbortController``. The controller is registered in
   * ``pendingTtsControllers`` so ``teardown`` can abort everything in
   * flight on close.
   */
  async function ttsAttempt(frame: {
    message_id: string;
    sentence_index: number;
    text: string;
  }): Promise<Awaited<ReturnType<typeof api.tts.synthesize>>> {
    const controller = new AbortController();
    pendingTtsControllers.add(controller);
    const timeout = window.setTimeout(() => controller.abort(), TTS_SENTENCE_TIMEOUT_MS);
    try {
      return await api.tts.synthesize(
        {
          text: frame.text,
          message_id: sentenceCacheMessageId(frame.message_id, frame.sentence_index),
          agent_id: agent?.agent_id ?? null,
          low_latency: true
        },
        { signal: controller.signal }
      );
    } finally {
      window.clearTimeout(timeout);
      pendingTtsControllers.delete(controller);
    }
  }

  /** One paid attempt. An uncertain outcome requires explicit user action. */
  async function synthesizeSentence(frame: {
    message_id: string;
    sentence_index: number;
    text: string;
  }): Promise<Awaited<ReturnType<typeof api.tts.synthesize>>> {
    if (disposed) throw new DOMException('Aborted', 'AbortError');
    return ttsAttempt(frame);
  }

  async function unlockPlayback(): Promise<boolean> {
    listeningCues.unlock();
    if (!queue) return false;
    const ok = await queue.unlock();
    playbackNeedsGesture = !ok;
    return ok;
  }

  function notifyPlaybackSkipped(): void {
    if (playbackErrorNotified || disposed) return;
    playbackErrorNotified = true;
    addToast('Voice playback was blocked or timed out, so audio was skipped.', 'warning', 3_000);
  }

  function notifyVoiceWarnings(warnings: string[] = []): void {
    for (const warning of warnings) {
      if (voiceWarnings.has(warning) || disposed) continue;
      voiceWarnings.add(warning);
      addToast(warning, 'warning', 6_000);
    }
  }

  /**
   * Force-stop every track on every stream we have ever acquired in this
   * overlay session. Called during teardown and again from the watchdog
   * to ensure iOS Safari (PWA) releases the microphone indicator.
   */
  function stopAllAcquiredStreams(): void {
    for (const acquired of acquiredStreams) {
      stopMediaStreamTracks(acquired);
    }
    acquiredStreams.clear();
  }

  function markListeningStopped(): void {
    for (const stream of acquiredStreams) {
      for (const track of stream.getAudioTracks()) track.enabled = false;
    }
    if (!listeningReady) return;
    listeningReady = false;
    if (!disposed) void listeningCues.play('stop');
  }

  function teardownAudio(): void {
    if (listeningStarting) listeningCues.stop();
    markListeningStopped();
    listeningStarting = false;
    sttController?.abort();
    sttController = null;
    liveStt?.cancel();
    liveStt = null;
    liveStartup = null;
    listeningGeneration += 1;
    clearVad();
    // Forced stop. Detach the onstop handler so it does NOT fire
    // ``onUtterance`` with whatever was captured before the user ended
    // the session — Whisper happily hallucinates phrases on near-silent
    // audio, which would then be ``submitText``-ed as a chat message.
    utteranceFinalizing = false;
    if (recorder) {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      recorder.onerror = null;
      if (recorder.state !== 'inactive') {
        try {
          recorder.stop();
        } catch {
          // ignore
        }
      }
    }
    recorder = null;
    chunks = [];
    stopAllAcquiredStreams();
    if (audioContext) {
      const ctx = audioContext;
      audioContext = null;
      // Close fire-and-forget so teardown stays synchronous from the
      // user's tap. AudioContext.close() can take a frame on iOS.
      queueMicrotask(() => {
        try {
          void ctx.close();
        } catch {
          // ignore
        }
      });
    }
    analyser = null;
    speakingDetected = false;
    modeState = 'idle';
  }

  async function startListening(): Promise<void> {
    if (!shouldStartListening()) return;
    const generation = ++listeningGeneration;
    listeningStarting = true;
    try {
      await beginListening(generation);
    } finally {
      if (generation === listeningGeneration) listeningStarting = false;
    }
  }

  async function beginListening(generation: number): Promise<void> {
    void wakeLock?.acquire();
    listeningReady = false;
    modeState = 'listening';
    listeningHint = '';
    liveTranscript = '';
    missedUtterance = '';
    voiceLevel = 0;
    const vadAvailable = typeof AudioContext !== 'undefined';
    if (!vadAvailable) listeningHint = 'Tap the orb when you finish speaking.';
    chunks = [];
    speakingDetected = false;
    const controller = new AbortController();
    sttController = controller;
    let nextStream: MediaStream;
    try {
      nextStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
    } catch {
      if (disposed || generation !== listeningGeneration || !open) return;
      addToast('Microphone access denied. Allow it in browser settings.', 'error');
      teardownAudio();
      onclose();
      return;
    }
    // Always register the stream so teardown / watchdog can release it
    // even if startListening bails out at the next gate below.
    acquiredStreams.add(nextStream);
    for (const track of nextStream.getAudioTracks()) track.enabled = false;
    if (disposed || generation !== listeningGeneration || !open || muted) {
      stopMediaStreamTracks(nextStream);
      acquiredStreams.delete(nextStream);
      return;
    }
    const mimeType = pickSupportedAudioMimeType();
    let nextRecorder: MediaRecorder;
    try {
      nextRecorder = mimeType
        ? new MediaRecorder(nextStream, { mimeType })
        : new MediaRecorder(nextStream);
    } catch {
      stopMediaStreamTracks(nextStream);
      acquiredStreams.delete(nextStream);
      if (disposed || generation !== listeningGeneration || !open) {
        teardownAudio();
        return;
      }
      addToast('Recording is not supported in this browser', 'error');
      teardownAudio();
      onclose();
      return;
    }
    recorder = nextRecorder;
    nextRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    };
    nextRecorder.onstop = () => {
      if (recorder === nextRecorder) {
        recorder = null;
      }
      // Once the recorder has stopped its tracks are no longer needed.
      // Stop them eagerly so iOS Safari drops the microphone indicator
      // even if VAD did not get there first.
      stopMediaStreamTracks(nextStream);
      acquiredStreams.delete(nextStream);
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
      void onUtterance(captured, audioExtensionForMimeType(mimeType));
    };
    nextRecorder.onerror = () => {
      if (recorder === nextRecorder) teardownAudio();
      stopMediaStreamTracks(nextStream);
      acquiredStreams.delete(nextStream);
    };

    let activity: VoiceActivity | null = null;
    let readRms: (() => number) | null = null;
    if (vadAvailable) {
      try {
        const context = new AudioContext();
        audioContext = context;
        const meter = context.createAnalyser();
        analyser = meter;
        meter.fftSize = 1024;
        context.createMediaStreamSource(nextStream).connect(meter);
        const buffer = new Float32Array(meter.fftSize);
        readRms = () => {
          if (context.state !== 'running') return Number.NaN;
          meter.getFloatTimeDomainData(buffer);
          return rmsFromTimeDomainData(buffer);
        };
        if (context.state === 'suspended') void context.resume().catch(() => {});
        for (const track of nextStream.getAudioTracks()) track.enabled = true;
        const floor = await calibrateNoise(readRms, controller.signal);
        if (floor !== null) activity = new VoiceActivity(floor);
      } catch {
        // A restricted audio context leaves manual finish available.
      }
    }
    for (const track of nextStream.getAudioTracks()) track.enabled = false;
    if (disposed || generation !== listeningGeneration || !open || muted || recorder !== nextRecorder) {
      stopMediaStreamTracks(nextStream);
      acquiredStreams.delete(nextStream);
      return;
    }

    if (streamingStt) {
      try {
        const startup = LiveTranscription.open(
          nextStream, sttSampleRate, controller.signal,
          (text) => { if (generation === listeningGeneration && !disposed) liveTranscript = text; },
          () => {
            if (generation !== listeningGeneration || disposed) return;
            addToast('Live transcription failed. Tap to start a new utterance.', 'error', 8_000);
            teardownAudio();
          },
        );
        liveStartup = startup;
        const capture = await startup;
        if (disposed || generation !== listeningGeneration ||
            modeState !== 'listening' || recorder !== nextRecorder) {
          capture.cancel();
          return;
        }
        liveStt = capture;
      } catch {
        if (disposed || generation !== listeningGeneration) return;
        if (!controller.signal.aborted) addToast('Live transcription could not start. Tap to retry.', 'error', 8_000);
        teardownAudio();
        return;
      }
    }

    await listeningCues.play('start');
    if (disposed || generation !== listeningGeneration || !open || muted || recorder !== nextRecorder) {
      stopMediaStreamTracks(nextStream);
      acquiredStreams.delete(nextStream);
      return;
    }
    for (const track of nextStream.getAudioTracks()) track.enabled = true;
    nextRecorder.start();
    listeningReady = true;

    if (!activity || !readRms) {
      listeningHint = 'Tap the orb when you finish speaking.';
      return;
    }
    const utteranceStartedAt = Date.now();
    const detector = activity;
    const readLevel = readRms;

    vadHandle = setInterval(() => {
      if (disposed || generation !== listeningGeneration || !open) {
        teardownAudio();
        return;
      }
      const rms = readLevel();
      if (!Number.isFinite(rms)) return;
      const now = Date.now();
      const sample = detector.sample(rms, now);
      speakingDetected = sample.speaking;
      voiceLevel = normalizeVoiceLevel(rms, sample.threshold);
      if (!speakingDetected && now - utteranceStartedAt > VAD_HINT_MS) {
        listeningHint = 'Speak when ready, or tap the orb when done.';
      }
      if (sample.speaking) listeningHint = sample.silenceMs >= 1_500
        ? 'Still listening. Tap the orb when you’re done.' : '';
      if (sample.shouldFinish) endUtterance();
    }, VAD_FRAME_MS);
  }

  function endUtterance(): void {
    // VAD-driven stop. Mark this as a real utterance handoff so the
    // ``recorder.onstop`` handler proceeds with STT.
    utteranceFinalizing = true;
    markListeningStopped();
    clearVad();
    if (recorder && recorder.state !== 'inactive') {
      try {
        recorder.stop();
      } catch {
        utteranceFinalizing = false;
      }
    }
    if (audioContext) {
      const ctx = audioContext;
      audioContext = null;
      queueMicrotask(() => {
        try {
          void ctx.close();
        } catch {
          // ignore
        }
      });
    }
    analyser = null;
    // ``recorder.onstop`` will release the stream for us; if no recorder
    // is attached for some reason, fall through and stop everything.
    if (!recorder) {
      stopAllAcquiredStreams();
    }
  }

  function finishListeningTurn(): void {
    if (!recorder || modeState !== 'listening') return;
    haptic.light();
    speakingDetected = true;
    endUtterance();
  }

  async function onUtterance(blob: Blob, ext: string): Promise<void> {
    const admittedCycle = cycle;
    if (disposed) return;
    if (blob.size === 0) {
      teardownAudio();
      void startListening();
      return;
    }
    modeState = 'processing';
    transcribing = true;
    listeningHint = '';
    voiceLevel = 0;
    try {
      const filename = `voice-${Date.now()}.${ext}`;
      const capture = liveStt ?? (streamingStt ? await liveStartup : null);
      liveStt = null;
      liveStartup = null;
      const controller = sttController ?? new AbortController();
      sttController = controller;
      const timeout = window.setTimeout(() => controller.abort(), STT_TIMEOUT_MS);
      const result = await (capture
        ? capture.commit()
        : api.stt.transcribe(blob, { filename, signal: controller.signal })).finally(() => {
        window.clearTimeout(timeout);
        if (sttController === controller) sttController = null;
      });
      if (disposed || admittedCycle !== cycle) return;
      transcribing = false;
      const text = result.text.trim();
      if (!text) {
        missedUtterance = "I didn't catch that.";
        haptic.warning();
        clearMissedRestartTimer();
        missedRestartTimer = window.setTimeout(() => {
          missedRestartTimer = null;
          if (!disposed) void startListening();
        }, 650);
        return;
      }
      haptic.success();
      transcript = [...transcript, { role: 'user', text }];
      assistantTurnActive = true;
      modeState = 'waiting';
      armAssistantTurnWatchdog();
      narrativeFrames.clear();
      deliverables.length = 0;
      sentenceTasks.clear();
      voiceTurnId = null;
      speech = null;
      admissionRuntime = [];
      admissionPending = true;
      const clientMessageId = await submitText(text);
      if (disposed || admittedCycle !== cycle) return;
      admissionPending = false;
      if (clientMessageId) {
        const activeSpeech = new ConversationSpeech(clientMessageId,
          frame => {
            const task = handleSentenceReady(frame);
            sentenceTasks.add(task);
            void task.finally(() => sentenceTasks.delete(task));
          },
          handleMessageComplete,
          () => { void reconcileSpeech(clientMessageId, activeSpeech); },
          ref => { deliverables.push(ref); });
        speech = activeSpeech;
        for (const runtime of admissionRuntime) speech.update(runtime);
        if (runtime) speech.update(runtime, items);
        voiceTurnId = speech.activeTurnId;
      } else {
        handleMessageComplete();
      }
      admissionRuntime = [];
    } catch (err) {
      if (disposed || admittedCycle !== cycle) return;
      assistantTurnActive = false;
      transcribing = false;
      admissionPending = false;
      admissionRuntime = [];
      clearAssistantTurnWatchdog();
      const message = err instanceof Error ? err.message : 'Transcription failed';
      const aborted = err instanceof DOMException && err.name === 'AbortError';
      missedUtterance = aborted || message === 'The operation was aborted.'
        ? "I didn't catch that."
        : message;
      haptic.warning();
      clearMissedRestartTimer();
      missedRestartTimer = window.setTimeout(() => {
        missedRestartTimer = null;
        if (!disposed) void startListening();
      }, 650);
    }
  }

  async function reconcileSpeech(
    clientMessageId: string, activeSpeech: ConversationSpeech,
  ): Promise<void> {
    await activeSpeech.reconcile(
      () => reconcileTurn(clientMessageId),
      () => !disposed && !interrupting && speech === activeSpeech,
      () => {
        listeningHint = 'Waiting to recover the assistant response. You can interrupt or end the call.';
      },
    );
  }

  async function handleSentenceReady(frame: {
    message_id: string;
    sentence_index: number;
    text: string;
  }): Promise<void> {
    const admittedCycle = cycle;
    if (disposed || interrupting) return;
    if (ignoredTtsMessageIds.has(frame.message_id)) return;
    if (audioMessageId !== frame.message_id) {
      resetAudioOrdering(frame.message_id);
    }
    const sentenceKey = `${frame.message_id}:${frame.sentence_index}`;
    if (activeSentenceKeys.has(sentenceKey)) return;
    activeSentenceKeys.add(sentenceKey);
    if (adaptMessages) {
      const sentences = narrativeFrames.get(frame.message_id) ?? new Map<number, string>();
      sentences.set(frame.sentence_index, frame.text);
      narrativeFrames.set(frame.message_id, sentences);
      transcript = updateTranscriptForAssistant(transcript, frame.message_id, frame.text);
      return;
    }
    if (streamingTts) {
      readyAudioByIndex.set(frame.sentence_index, {
        id: sentenceKey,
        stream: (signal) => api.tts.stream({
          text: frame.text, message_id: sentenceCacheMessageId(frame.message_id, frame.sentence_index),
          agent_id: agent?.agent_id ?? null, low_latency: true,
        }, { signal, timeoutMs: TTS_SENTENCE_TIMEOUT_MS }),
      });
      drainReadyAudio();
      transcript = updateTranscriptForAssistant(transcript, frame.message_id, frame.text);
      return;
    }
    pendingSentenceSyntheses += 1;
    modeState = 'speaking';
    voiceLevel = 0;
    try {
      const result = await synthesizeSentence(frame);
      if (disposed || admittedCycle !== cycle) return;
      if (ignoredTtsMessageIds.has(frame.message_id)) return;
      notifyVoiceWarnings(result.warnings);
      readyAudioByIndex.set(frame.sentence_index, {
        url: result.audio_url,
        id: `${frame.message_id}:${frame.sentence_index}`,
      });
      drainReadyAudio();
      if (frame.sentence_index === 0) haptic.light();
      // Update transcript drawer (append to last assistant entry or create one).
      transcript = updateTranscriptForAssistant(transcript, frame.message_id, frame.text);
    } catch {
      if (disposed || admittedCycle !== cycle) return;
      failedAudioIndexes.add(frame.sentence_index);
      drainReadyAudio();
      notifyPlaybackSkipped();
    } finally {
      if (admittedCycle !== cycle) return;
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
    clearAssistantTurnWatchdog();
    assistantTurnActive = false;
    listeningHint = '';
    voiceTurnId = speech?.activeTurnId ?? voiceTurnId;
    if (deliverables.length) void speakDeliverables();
    else if (adaptMessages && narrativeFrames.size) void speakNarrative();
    // Queue may still be playing the last sentence; the idle callback
    // re-arms the mic when playback drains.
    maybeStartListening();
  }

  async function speakDeliverables(): Promise<void> {
    const admittedCycle = cycle;
    const refs = deliverables.splice(0);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 600_000);
    pendingTtsControllers.add(controller);
    pendingSentenceSyntheses += 1;
    preparingDeliverables = true;
    try {
      // Preserve message-before-document ordering, including delayed non-streaming sentences.
      await Promise.all(sentenceTasks);
      if (disposed || admittedCycle !== cycle || controller.signal.aborted) return;
      if (adaptMessages && narrativeFrames.size) await speakNarrative();
      for (const ref of refs) {
        controller.signal.throwIfAborted();
        const job = await readDocumentNarration(appDocumentAudioClient, ref, controller.signal, part => {
          if (!disposed && admittedCycle === cycle && !controller.signal.aborted) {
            queue?.enqueue({ url: part.audio_url, id: part.artifact_id });
          }
        });
        if (disposed || admittedCycle !== cycle) return;
        notifyVoiceWarnings(job.warnings);
      }
    } catch {
      if (!disposed && admittedCycle === cycle) {
        addToast('Unable to read the deliverable. Open its Reading controls to inspect or retry.', 'warning', 6_000);
      }
    } finally {
      window.clearTimeout(timeout);
      pendingTtsControllers.delete(controller);
      if (admittedCycle === cycle) {
        preparingDeliverables = false;
        pendingSentenceSyntheses = Math.max(0, pendingSentenceSyntheses - 1);
        maybeStartListening();
      }
    }
  }

  async function speakNarrative(): Promise<void> {
    const admittedCycle = cycle;
    const text = [...narrativeFrames.values()].flatMap(sentences =>
      [...sentences.entries()].sort((a, b) => a[0] - b[0]).map(([, sentence]) => sentence)
    ).join(' ');
    narrativeFrames.clear();
    if (streamingTts) {
      queue?.enqueue({
        stream: (signal) => api.tts.stream(
          { text, agent_id: agent?.agent_id ?? null, adaptation: 'listenable' },
          { signal, timeoutMs: 600_000 },
        ),
      });
      return;
    }
    const controller = new AbortController();
    pendingTtsControllers.add(controller);
    pendingSentenceSyntheses += 1;
    modeState = 'speaking';
    try {
      const result = await api.tts.synthesize(
        { text, agent_id: agent?.agent_id ?? null, adaptation: 'listenable' },
        { signal: controller.signal, timeoutMs: 600_000 }
      );
      if (disposed || controller.signal.aborted || admittedCycle !== cycle) return;
      notifyVoiceWarnings(result.warnings);
      for (const part of result.parts ?? [{ audio_url: result.audio_url }]) {
        queue?.enqueue({ url: part.audio_url });
      }
    } catch {
      if (!disposed && !controller.signal.aborted && admittedCycle === cycle) notifyPlaybackSkipped();
    } finally {
      pendingTtsControllers.delete(controller);
      if (admittedCycle === cycle) {
        pendingSentenceSyntheses = Math.max(0, pendingSentenceSyntheses - 1);
        maybeStartListening();
      }
    }
  }

  async function startConversationLoop(): Promise<void> {
    if (loopActive) return;
    loopActive = true;
    disposed = false;
    const admittedCycle = ++cycle;
    setupError = '';
    interrupting = false;
    adaptMessages = $userPreferences.chat.adapt_tts ?? false;
    voiceWarnings.clear();
    narrativeFrames.clear();
    playbackNeedsGesture = false;
    if (!wakeLock) wakeLock = new ScreenWakeLock();
    void wakeLock.acquire();
    assistantTurnActive = false;
    pendingSentenceSyntheses = 0;
    resetAudioOrdering(null);
    activeSentenceKeys.clear();
    ignoredTtsMessageIds.clear();
    transcript = [];
    transcriptOpen = false;
    if (!queue) {
      queue = new AudioQueue();
      unsubscribeIdle = queue.onIdle(() => {
        // Only re-arm when the assistant turn is complete, all sentence
        // synthesis calls have settled, and playback has drained.
        maybeStartListening();
      });
      unsubscribePlaying = queue.onPlayingChange((playing) => {
        if (disposed) return;
        playingAudio = playing;
        if (playing) {
          modeState = 'speaking';
          // Pause any per-message TTS playback to enforce single-stream invariant.
          audioPlayer.stop();
        } else if (assistantTurnActive || pendingSentenceSyntheses > 0) {
          modeState = 'waiting';
        }
      });
      unsubscribePlaybackError = queue.onPlaybackError(() => {
        if (disposed) return;
        playbackNeedsGesture = true;
        notifyPlaybackSkipped();
      });
    }
    void unlockPlayback();
    try {
      const capabilities = await api.tts.capabilities(agent?.agent_id);
      streamingTts = capabilities.streaming_tts && SpeechPlayback.supported();
      streamingStt = capabilities.streaming_stt && LiveTranscription.supported();
      sttSampleRate = capabilities.stt_sample_rate;
      if (disposed || admittedCycle !== cycle) return;
      if (!capabilities.tts || !capabilities.stt) {
        setupError = capabilities.detail ?? 'Configure speech routes in Settings → Models.';
        return;
      }
    } catch {
      if (!disposed && admittedCycle === cycle) setupError = 'Speech setup could not be checked. Check your connection and reopen conversation mode.';
      return;
    }
    haptic.success();
    void startListening();
  }

  function teardown(): void {
    listeningCues.dispose();
    loopActive = false;
    disposed = true;
    cycle += 1;
    speech = null;
    admissionPending = false;
    admissionRuntime = [];
    sttController?.abort();
    sttController = null;
    transcribing = false;
    narrativeFrames.clear();
    deliverables.length = 0;
    sentenceTasks.clear();
    voiceTurnId = null;
    playingAudio = false;
    preparingDeliverables = false;
    playbackNeedsGesture = false;
    assistantTurnActive = false;
    listeningHint = '';
    missedUtterance = '';
    voiceLevel = 0;
    pendingSentenceSyntheses = 0;
    resetAudioOrdering(null);
    activeSentenceKeys.clear();
    ignoredTtsMessageIds.clear();
    clearMissedRestartTimer();
    clearAssistantTurnWatchdog();
    for (const controller of pendingTtsControllers) {
      try {
        controller.abort();
      } catch {
        // ignore
      }
    }
    pendingTtsControllers.clear();
    if (queue) {
      queue.dispose();
      queue = null;
    }
    teardownAudio();
    if (wakeLock) {
      const lock = wakeLock;
      queueMicrotask(() => {
        void lock.release();
      });
    }
    if (unsubscribePlaying) {
      unsubscribePlaying();
      unsubscribePlaying = null;
    }
    if (unsubscribeIdle) {
      unsubscribeIdle();
      unsubscribeIdle = null;
    }
    if (unsubscribePlaybackError) {
      unsubscribePlaybackError();
      unsubscribePlaybackError = null;
    }
    // Defense in depth for iOS Safari (PWA): some browsers keep the
    // microphone indicator alive for a short window after the synchronous
    // teardown. Re-walk the registry shortly after to make sure every
    // track has actually been stopped.
    if (teardownWatchdog !== null) {
      clearTimeout(teardownWatchdog);
    }
    teardownWatchdog = setTimeout(() => {
      teardownWatchdog = null;
      stopAllAcquiredStreams();
    }, 250);
  }

  function handleClose(): void {
    // Mark disposed first so any in-flight async work bails out before
    // we spend time on the parent state flip and synchronous teardown.
    disposed = true;
    haptic.warning();
    onclose();
    teardown();
  }

  function handleOverlayPointerDown(event: PointerEvent): void {
    listeningCues.unlock();
    if ((event.target as HTMLElement | null)?.closest('button')) return;
    if (playbackNeedsGesture) {
      void unlockPlayback();
    }
  }

  function toggleMute(): void {
    muted = !muted;
    haptic.light();
    if (muted) {
      const previousMode = modeState;
      teardownAudio();
      modeState = previousMode === 'listening' ? 'idle' : previousMode;
    } else {
      maybeStartListening();
    }
  }

  async function interruptAssistant(relisten = true): Promise<void> {
    if (admissionPending || interrupting) return;
    if (!queue && pendingTtsControllers.size === 0 && !assistantTurnActive) return;
    const admittedCycle = ++cycle;
    const wasActive = assistantTurnActive;
    const activeSpeech = speech;
    interrupting = true;
    speech = null;
    admissionPending = false;
    admissionRuntime = [];
    listeningHint = '';
    narrativeFrames.clear();
    haptic.warning();
    deliverables.length = 0;
    sentenceTasks.clear();
    preparingDeliverables = false;
    for (const controller of pendingTtsControllers) {
      try {
        controller.abort();
      } catch {
        // ignore
      }
    }
    pendingTtsControllers.clear();
    queue?.clear();
    pendingSentenceSyntheses = 0;
    assistantTurnActive = false;
    clearAssistantTurnWatchdog();
    playbackNeedsGesture = false;
    if (audioMessageId) ignoredTtsMessageIds.add(audioMessageId);
    resetAudioOrdering(null);
    if (wasActive) {
      assistantTurnActive = true;
      modeState = 'waiting';
      let stopped = false;
      try {
        stopped = await stopAssistant();
      } catch {
        // A failed cancellation must not admit a competing voice turn.
      }
      if (disposed || admittedCycle !== cycle) return;
      if (!stopped) {
        interrupting = false;
        speech = activeSpeech;
        listeningHint = 'Unable to interrupt the assistant. Try again or close conversation mode.';
        if (runtime) speech?.update(runtime, items);
        return;
      }
      assistantTurnActive = false;
    }
    interrupting = false;
    if (relisten) maybeStartListening();
  }

  $effect(() => {
    const update = runtime;
    const visibleItems = items;
    if (update) untrack(() => {
      if (disposed || interrupting || !assistantTurnActive) return;
      if (admissionPending) admissionRuntime.push(update);
      else {
        speech?.update(update, visibleItems);
        voiceTurnId = speech?.activeTurnId ?? null;
      }
    });
  });

  $effect(() => {
    if (loopOpen) {
      untrack(startConversationLoop);
      return () => {
        untrack(teardown);
      };
    }
  });

  $effect(() => {
    if (!open || muted || playingAudio
      || !['processing', 'waiting', 'speaking'].includes(modeState)) return;
    let interval: ReturnType<typeof setInterval> | undefined;
    const start = setTimeout(() => {
      void listeningCues.play('waiting');
      interval = setInterval(() => { void listeningCues.play('waiting'); }, 8000);
    }, 250);
    return () => {
      clearTimeout(start);
      clearInterval(interval);
      listeningCues.stop();
    };
  });

  onDestroy(() => {
    teardown();
    if (teardownWatchdog !== null) {
      clearTimeout(teardownWatchdog);
      teardownWatchdog = null;
    }
    stopAllAcquiredStreams();
  });

  function stateLabel(s: ModeState): string {
    if (muted) return 'Muted — tap to listen';
    if (s === 'listening' && !listeningReady) return 'Getting microphone ready…';
    if (s === 'listening' && liveTranscript) return liveTranscript;
    if (missedUtterance) return missedUtterance;
    if (listeningHint) return listeningHint;
    if (preparingDeliverables && !playingAudio) return 'Preparing deliverable audio…';
    switch (s) {
      case 'listening':
        return listeningHint || 'Listening…';
      case 'processing':
        return 'Transcribing…';
      case 'waiting':
        return 'Waiting for the assistant…';
      case 'speaking':
        return playingAudio ? 'Speaking…' : 'Preparing audio…';
      default:
        return 'Connecting...';
    }
  }

  function stateToneClass(): string {
    if (muted) return 'from-muted/35 via-secondary/25 to-inset/35';
    if (missedUtterance) return 'from-warning/40 via-info/25 to-muted/30';
    if (modeState === 'listening') return 'from-success/45 via-info/30 to-cyan-tone/35';
    if (modeState === 'processing') return 'from-info/45 via-cyan-tone/30 to-violet-tone/35';
    if (modeState === 'speaking') return 'from-info/45 via-cyan-tone/30 to-violet-tone/40';
    return 'from-muted/35 via-info/20 to-inset/35';
  }

  function agentLabel(): string {
    return agent?.display_name ?? agent?.name ?? 'Cognis';
  }

  function voiceLabel(): string {
    return selectedVoice() ?? 'system voice';
  }
</script>

<svelte:window onkeydown={(event) => { if (open && event.key === 'Escape') { event.preventDefault(); handleClose(); } }} />

{#if open}
  <div
    class="app-viewport-overlay app-safe-fullscreen z-50 bg-canvas/95 backdrop-blur-md"
    role="dialog"
    aria-modal="true"
    aria-label="Voice conversation mode"
    tabindex="-1"
    onpointerdown={handleOverlayPointerDown}
  >
    <div class="app-safe-fullscreen__toolbar flex justify-end">
      <button
        type="button"
        class="inline-flex h-11 w-11 items-center justify-center rounded-full bg-surface/90 text-secondary shadow-lg backdrop-blur transition hover:bg-surface-hover hover:text-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        aria-label="End conversation"
        onclick={handleClose}
      >
        <X class="h-5 w-5" />
      </button>
    </div>

    <div class="app-safe-fullscreen__content mx-auto flex w-full flex-col items-center justify-between gap-5"
      class:max-w-2xl={!showActivity} class:max-w-5xl={showActivity}>
      <div class="flex w-full items-center gap-3">
        <AgentAvatar name={agentLabel()} avatarUrl={agent?.avatar_url ?? null} class="h-11 w-11 rounded-2xl" />
        <div class="min-w-0">
          <p class="truncate text-base font-semibold text-strong">{agentLabel()}</p>
          <p class="truncate text-xs text-quiet">Conversation mode · {voiceLabel()}</p>
        </div>
      </div>

      <div class="conversation-stage min-h-0 w-full flex-1" class:conversation-stage--activity={showActivity}>
      <div class="flex min-h-0 flex-col items-center justify-center gap-6 text-center">
        {#if setupError}<p role="alert" class="text-sm text-danger">{setupError} <a href="/settings?tab=models" class="underline">Open settings</a></p>{/if}
        <button
          type="button"
          disabled={interrupting || admissionPending || listeningStarting}
          class="relative h-56 w-56 rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-accent sm:h-72 sm:w-72"
          class:conversation-orb--compact={showActivity}
          aria-label={playbackNeedsGesture ? 'Enable audio playback' : modeState === 'speaking' || modeState === 'waiting' ? 'Interrupt assistant and listen' : modeState === 'listening' ? 'Finish speaking' : 'Voice conversation status'}
          onclick={() => {
            if (playbackNeedsGesture) void unlockPlayback();
            else if (modeState === 'speaking' || modeState === 'waiting') interruptAssistant();
            else if (modeState === 'listening') finishListeningTurn();
          }}
        >
          <span
            class={`absolute inset-0 rounded-full bg-gradient-to-br ${stateToneClass()} transition`}
            class:animate-pulse={modeState === 'speaking' || modeState === 'listening'}
          ></span>
          <span class="absolute inset-3 rounded-full bg-canvas shadow-2xl shadow-accent/30"></span>
          <span class="absolute inset-0 flex flex-col items-center justify-center gap-3">
            <Headphones class="h-12 w-12 text-accent sm:h-14 sm:w-14" />
            <span class="text-xs font-medium uppercase tracking-[0.3em] text-secondary">Conversation</span>
            {#if modeState === 'listening' && !muted}
              <span class="mt-1 flex h-10 items-end gap-1" aria-hidden="true">
                {#each [0.35, 0.7, 1, 0.6, 0.85, 0.45] as scale}
                  <span
                    class="w-1 rounded-full bg-accent transition-[height] duration-75"
                    style={`height: ${Math.max(8, Math.round(38 * Math.max(0.15, voiceLevel * scale)))}px;`}
                  ></span>
                {/each}
              </span>
            {/if}
          </span>
        </button>

        <div>
          <p class="text-lg font-semibold text-foreground" aria-live="polite">{stateLabel(modeState)}</p>
          <p class="mt-2 text-sm text-quiet">
            {#if playbackNeedsGesture}
              Tap the orb to enable audio playback.
            {:else if modeState === 'speaking' || modeState === 'waiting'}
              Tap the orb to interrupt and speak.
            {:else if modeState === 'listening'}
              Tap the orb when you finish speaking.
            {:else}
              Keep this screen open for hands-free replies.
            {/if}
          </p>
        </div>
      </div>

      {#if showActivity}
        <section aria-label="Assistant activity" use:followActivity class="conversation-activity min-h-0 min-w-0 overflow-y-auto overscroll-contain text-left">
          <div>
          <h2 class="mb-3 text-sm font-medium text-secondary">Assistant activity</h2>
          <ChatV2TimelineList items={activityItems} {runtime} {cycleStates} {agent}
            preferences={$userPreferences}
            scope={{ key: `conversation:${conversationId}`, kind: 'conversation', conversation_id: conversationId }} />
          </div>
        </section>
      {/if}
      </div>

      <div class="w-full space-y-3">
        <div class="grid grid-cols-1 gap-3">
          <Button variant="secondary" type="button" class="min-h-[56px] px-4" onclick={toggleMute} aria-pressed={muted}>
          {#if muted}
            <Mic class="h-5 w-5 sm:mr-2" />
            <span class="hidden sm:inline">Unmute</span>
          {:else}
            <MicOff class="h-5 w-5 sm:mr-2" />
            <span class="hidden sm:inline">Mute</span>
          {/if}
          </Button>
        </div>

        <div class="grid grid-cols-2 gap-3">
          <Button variant="secondary" type="button" aria-expanded={transcriptOpen} aria-controls="conversation-mode-transcript" onclick={() => { haptic.light(); transcriptOpen = !transcriptOpen; }}>
            {transcriptOpen ? 'Hide transcript' : 'Show transcript'}
          </Button>
          <Button variant="danger" type="button" onclick={handleClose}>
            End conversation
          </Button>
        </div>
      </div>

      {#if playbackNeedsGesture && !assistantTurnActive && pendingSentenceSyntheses === 0}
        <p class="text-center text-xs text-quiet">Audio playback was blocked. Tap the orb to retry; text remains available.</p>
      {/if}

      {#if transcriptOpen && transcript.length > 0}
        <div id="conversation-mode-transcript" class="max-h-56 w-full overflow-y-auto rounded-2xl border border-separator bg-surface/70 p-3 text-left text-sm text-foreground">
          {#each transcript as entry, i (i)}
            <p class={`mb-2 last:mb-0 ${entry.role === 'user' ? 'text-accent' : 'text-foreground'}`}>
              <span class="text-[10px] font-medium uppercase tracking-wider text-quiet">{entry.role}</span>
              <span class="ml-2">{entry.text}</span>
            </p>
          {/each}
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .conversation-stage { display: grid; align-items: center; }
  .conversation-stage--activity { grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 2rem; align-items: stretch; }
  @media (max-width: 639px) {
    .conversation-stage--activity { grid-template-columns: minmax(0, 1fr); grid-template-rows: minmax(0, 2fr) minmax(0, 1fr); gap: 1rem; }
    .conversation-activity { max-height: calc(100dvh / 3); }
    .conversation-orb--compact { width: 9rem; height: 9rem; }
  }
</style>
