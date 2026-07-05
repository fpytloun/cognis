<script lang="ts">
  import Headphones from 'lucide-svelte/icons/headphones';
  import MicOff from 'lucide-svelte/icons/mic-off';
  import Mic from 'lucide-svelte/icons/mic';
  import X from 'lucide-svelte/icons/x';

  import { onDestroy, untrack } from 'svelte';
  import { api } from '$lib/api/client';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { haptic } from '$lib/haptics';
  import { audioPlayer } from '$lib/stores/audio-player';
  import { addToast } from '$lib/stores/toasts';
  import { AudioQueue } from '$lib/utils/audio-queue';
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
  let lastVoiceAt = 0;
  let speakingDetected = false;
  let unsubscribeSentence: (() => void) | null = null;
  let unsubscribeMessage: (() => void) | null = null;
  let unsubscribePlaying: (() => void) | null = null;
  let unsubscribeIdle: (() => void) | null = null;
  let unsubscribePlaybackError: (() => void) | null = null;
  let wakeLock: ScreenWakeLock | null = null;
  let loopActive = false;
  let assistantTurnActive = $state(false);
  let pendingSentenceSyntheses = $state(0);
  let listeningGeneration = 0;
  let audioMessageId: string | null = null;
  let nextAudioSentenceIndex = 0;
  const activeSentenceKeys = new Set<string>();
  const ignoredTtsMessageIds = new Set<string>();
  const readyAudioByIndex = new Map<number, { url: string; id: string }>();
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
  let retryNotified = false;
  let assistantTurnWatchdog: ReturnType<typeof setTimeout> | null = null;

  const VAD_FRAME_MS = 100;
  const VAD_RMS_THRESHOLD = 0.018;
  const VAD_CALIBRATION_MS = 700;
  const VAD_NOISE_MULTIPLIER = 3.2;
  const VAD_SILENCE_MS = 1500;
  const VAD_HINT_MS = 5000;
  const MIN_UTTERANCE_MS = 500;
  const MAX_UTTERANCE_MS = 45000;
  // Single TTS attempt timeout. Backend low-latency synthesize occasionally
  // takes 10-20s on a slow provider; we accept that to avoid skipping
  // sentences. A second retry is attempted on timeout/failure with the
  // same ceiling.
  const TTS_SENTENCE_TIMEOUT_MS = 15_000;
  const TTS_MAX_ATTEMPTS = 2;
  const STT_TIMEOUT_MS = 30_000;
  const ASSISTANT_TURN_WATCHDOG_MS = 120_000;

  function clearVad(): void {
    if (vadHandle !== null) {
      clearInterval(vadHandle);
      vadHandle = null;
    }
    lastVoiceAt = 0;
    voiceLevel = 0;
    // Note: ``speakingDetected`` is intentionally NOT reset here so the
    // ``recorder.onstop`` handler that runs immediately after a VAD-driven
    // ``endUtterance`` can decide whether the captured blob actually
    // contained speech. ``startListening()`` resets it for the next cycle.
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
      assistantTurnActive = false;
      pendingSentenceSyntheses = 0;
      queue?.clear();
      playbackNeedsGesture = false;
      addToast('Voice conversation timed out waiting for the assistant. Listening again.', 'warning', 4_000);
      maybeStartListening();
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
    retryNotified = false;
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

  /**
   * Synthesize a sentence with up to ``TTS_MAX_ATTEMPTS`` tries. Aborts
   * immediately when the overlay is disposed so closing the conversation
   * never has to wait on a retry.
   */
  async function synthesizeSentence(frame: {
    message_id: string;
    sentence_index: number;
    text: string;
  }): Promise<Awaited<ReturnType<typeof api.tts.synthesize>>> {
    let lastError: unknown = null;
    for (let attempt = 1; attempt <= TTS_MAX_ATTEMPTS; attempt += 1) {
      if (disposed) throw new DOMException('Aborted', 'AbortError');
      try {
        return await ttsAttempt(frame);
      } catch (err) {
        lastError = err;
        if (disposed) throw err;
        if (attempt < TTS_MAX_ATTEMPTS) {
          notifyTtsRetry();
        }
      }
    }
    throw lastError instanceof Error ? lastError : new Error('Synthesis failed');
  }

  async function unlockPlayback(): Promise<boolean> {
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

  function notifyTtsRetry(): void {
    if (retryNotified || disposed) return;
    retryNotified = true;
    addToast('Voice synthesis is slow, retrying…', 'info', 3_000);
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

  function teardownAudio(): void {
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
    void wakeLock?.acquire();
    const generation = ++listeningGeneration;
    modeState = 'listening';
    listeningHint = '';
    missedUtterance = '';
    voiceLevel = 0;
    const vadAvailable = typeof AudioContext !== 'undefined';
    if (!vadAvailable) listeningHint = 'Tap the orb when you finish speaking.';
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
    // Always register the stream so teardown / watchdog can release it
    // even if startListening bails out at the next gate below.
    acquiredStreams.add(nextStream);
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
      if (recorder === nextRecorder) {
        recorder = null;
      }
      stopMediaStreamTracks(nextStream);
      acquiredStreams.delete(nextStream);
    };
    nextRecorder.start();

    // Set up energy-based VAD on the same stream.
    if (disposed || generation !== listeningGeneration || !open) {
      teardownAudio();
      return;
    }
    if (!vadAvailable) {
      listeningHint = 'Tap the orb when you finish speaking.';
      return;
    }
    try {
      audioContext = new AudioContext();
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      const source = audioContext.createMediaStreamSource(nextStream);
      source.connect(analyser);
    } catch {
      listeningHint = 'Tap the orb when you finish speaking.';
      return;
    }
    const buffer = new Float32Array(analyser.fftSize);
    const utteranceStartedAt = Date.now();
    let speechStartedAt = 0;
    let calibrationSamples = 0;
    let calibrationTotal = 0;
    let vadThreshold = VAD_RMS_THRESHOLD;

    vadHandle = setInterval(() => {
      if (disposed || generation !== listeningGeneration || !open) {
        teardownAudio();
        return;
      }
      if (!analyser) return;
      analyser.getFloatTimeDomainData(buffer);
      const rms = rmsFromTimeDomainData(buffer);
      const now = Date.now();
      if (!speakingDetected && now - utteranceStartedAt <= VAD_CALIBRATION_MS) {
        calibrationSamples += 1;
        calibrationTotal += rms;
        const noiseFloor = calibrationTotal / calibrationSamples;
        vadThreshold = Math.max(VAD_RMS_THRESHOLD, noiseFloor * VAD_NOISE_MULTIPLIER);
      }
      voiceLevel = normalizeVoiceLevel(rms, vadThreshold);
      if (!speakingDetected && now - utteranceStartedAt > VAD_HINT_MS) {
        listeningHint = 'Speak when ready, or tap the orb when done.';
      }
      if (speechStartedAt > 0 && now - speechStartedAt > MAX_UTTERANCE_MS) {
        endUtterance();
      } else if (rms > vadThreshold) {
        if (!speakingDetected) speechStartedAt = now;
        speakingDetected = true;
        listeningHint = '';
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
    if (disposed) return;
    if (blob.size === 0) {
      void startListening();
      return;
    }
    modeState = 'processing';
    listeningHint = '';
    voiceLevel = 0;
    try {
      const filename = `voice-${Date.now()}.${ext}`;
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), STT_TIMEOUT_MS);
      const result = await api.stt.transcribe(blob, { filename, signal: controller.signal }).finally(() => {
        window.clearTimeout(timeout);
      });
      if (disposed) return;
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
      armAssistantTurnWatchdog();
      submitText(text);
    } catch (err) {
      if (disposed) return;
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

  async function handleSentenceReady(frame: {
    message_id: string;
    sentence_index: number;
    text: string;
  }): Promise<void> {
    if (disposed) return;
    if (ignoredTtsMessageIds.has(frame.message_id)) return;
    if (audioMessageId !== frame.message_id) {
      resetAudioOrdering(frame.message_id);
    }
    const sentenceKey = `${frame.message_id}:${frame.sentence_index}`;
    if (activeSentenceKeys.has(sentenceKey)) return;
    activeSentenceKeys.add(sentenceKey);
    pendingSentenceSyntheses += 1;
    modeState = 'speaking';
    voiceLevel = 0;
    try {
      const result = await synthesizeSentence(frame);
      if (disposed) return;
      if (ignoredTtsMessageIds.has(frame.message_id)) return;
      readyAudioByIndex.set(frame.sentence_index, {
        url: result.audio_url,
        id: `${frame.message_id}:${frame.sentence_index}`,
      });
      drainReadyAudio();
      if (frame.sentence_index === 0) haptic.light();
      // Update transcript drawer (append to last assistant entry or create one).
      transcript = updateTranscriptForAssistant(transcript, frame.message_id, frame.text);
    } catch {
      if (disposed) return;
      failedAudioIndexes.add(frame.sentence_index);
      drainReadyAudio();
      notifyPlaybackSkipped();
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
    clearAssistantTurnWatchdog();
    assistantTurnActive = false;
    // Queue may still be playing the last sentence; the idle callback
    // re-arms the mic when playback drains.
    maybeStartListening();
  }

  function startConversationLoop(): void {
    if (loopActive) return;
    loopActive = true;
    disposed = false;
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
        if (playing) {
          modeState = 'speaking';
          // Pause any per-message TTS playback to enforce single-stream invariant.
          audioPlayer.stop();
        }
      });
      unsubscribePlaybackError = queue.onPlaybackError(() => {
        if (disposed) return;
        playbackNeedsGesture = true;
        notifyPlaybackSkipped();
      });
    }
    void unlockPlayback();
    unsubscribeSentence = subscribeSentenceReady((frame) => void handleSentenceReady(frame));
    unsubscribeMessage = subscribeMessageComplete(handleMessageComplete);
    haptic.success();
    sendEnableTts(selectedVoice());
    void startListening();
  }

  function teardown(): void {
    const shouldDisableTts = loopActive;
    loopActive = false;
    disposed = true;
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
      queue.clear();
      queue = null;
    }
    teardownAudio();
    if (wakeLock) {
      const lock = wakeLock;
      queueMicrotask(() => {
        void lock.release();
      });
    }
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
    if (unsubscribePlaybackError) {
      unsubscribePlaybackError();
      unsubscribePlaybackError = null;
    }
    if (shouldDisableTts) {
      sendDisableTts();
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
    if ((event.target as HTMLElement | null)?.closest('button')) return;
    if (playbackNeedsGesture) {
      void unlockPlayback();
    }
  }

  function toggleMute(): void {
    muted = !muted;
    haptic.light();
    if (muted) {
      teardownAudio();
      modeState = 'idle';
    } else {
      maybeStartListening();
    }
  }

  function interruptAssistant(relisten = true): void {
    if (!queue && pendingTtsControllers.size === 0 && !assistantTurnActive) return;
    haptic.warning();
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
    sendDisableTts();
    sendEnableTts(selectedVoice());
    if (relisten) maybeStartListening();
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
    if (teardownWatchdog !== null) {
      clearTimeout(teardownWatchdog);
      teardownWatchdog = null;
    }
    stopAllAcquiredStreams();
  });

  function stateLabel(s: ModeState): string {
    if (muted) return 'Muted — tap to listen';
    if (missedUtterance) return missedUtterance;
    if (listeningHint) return listeningHint;
    switch (s) {
      case 'listening':
        return listeningHint || 'Listening…';
      case 'processing':
        return 'Transcribing…';
      case 'speaking':
        return pendingSentenceSyntheses > 0 ? 'Preparing audio…' : 'Speaking…';
      default:
        return 'Connecting...';
    }
  }

  function stateToneClass(): string {
    if (muted) return 'from-slate-500/35 via-slate-600/25 to-slate-800/35';
    if (missedUtterance) return 'from-amber-500/40 via-sky-500/25 to-slate-600/30';
    if (modeState === 'listening') return 'from-emerald-400/45 via-sky-400/30 to-cyan-500/35';
    if (modeState === 'processing') return 'from-sky-400/45 via-cyan-400/30 to-violet-500/35';
    if (modeState === 'speaking') return 'from-sky-500/45 via-cyan-500/30 to-violet-500/40';
    return 'from-slate-500/35 via-sky-500/20 to-slate-800/35';
  }

  function agentLabel(): string {
    return agent?.display_name ?? agent?.name ?? 'Cognis';
  }

  function voiceLabel(): string {
    return selectedVoice() ?? 'system voice';
  }
</script>

{#if open}
  <div
    class="app-viewport-overlay app-safe-fullscreen z-50 bg-slate-950/95 backdrop-blur-md"
    role="dialog"
    aria-modal="true"
    aria-label="Voice conversation mode"
    tabindex="-1"
    onpointerdown={handleOverlayPointerDown}
  >
    <div class="app-safe-fullscreen__toolbar flex justify-end">
      <button
        type="button"
        class="inline-flex h-11 w-11 items-center justify-center rounded-full bg-slate-900/90 text-slate-300 shadow-lg backdrop-blur transition hover:bg-slate-800 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300"
        aria-label="End conversation"
        onclick={handleClose}
      >
        <X class="h-5 w-5" />
      </button>
    </div>

    <div class="app-safe-fullscreen__content mx-auto flex w-full max-w-2xl flex-col items-center justify-between gap-5">
      <div class="flex w-full items-center gap-3">
        <AgentAvatar name={agentLabel()} avatarUrl={agent?.avatar_url ?? null} class="h-11 w-11 rounded-2xl" />
        <div class="min-w-0">
          <p class="truncate text-base font-semibold text-white">{agentLabel()}</p>
          <p class="truncate text-xs text-slate-500">Conversation mode · {voiceLabel()}</p>
        </div>
      </div>

      <div class="flex min-h-0 flex-1 flex-col items-center justify-center gap-6 text-center">
        <button
          type="button"
          class="relative h-56 w-56 rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-300 sm:h-72 sm:w-72"
          aria-label={playbackNeedsGesture ? 'Enable audio playback' : modeState === 'speaking' ? 'Interrupt assistant and listen' : modeState === 'listening' ? 'Finish speaking' : 'Voice conversation status'}
          onclick={() => {
            if (playbackNeedsGesture) void unlockPlayback();
            else if (modeState === 'speaking') interruptAssistant();
            else if (modeState === 'listening') finishListeningTurn();
          }}
        >
          <span
            class={`absolute inset-0 rounded-full bg-gradient-to-br ${stateToneClass()} transition`}
            class:animate-pulse={modeState === 'speaking' || modeState === 'listening'}
          ></span>
          <span class="absolute inset-3 rounded-full bg-slate-950 shadow-2xl shadow-sky-500/30"></span>
          <span class="absolute inset-0 flex flex-col items-center justify-center gap-3">
            <Headphones class="h-12 w-12 text-sky-300 sm:h-14 sm:w-14" />
            <span class="text-xs font-medium uppercase tracking-[0.3em] text-slate-300">Conversation</span>
            {#if modeState === 'listening' && !muted}
              <span class="mt-1 flex h-10 items-end gap-1" aria-hidden="true">
                {#each [0.35, 0.7, 1, 0.6, 0.85, 0.45] as scale}
                  <span
                    class="w-1 rounded-full bg-sky-300 transition-[height] duration-75"
                    style={`height: ${Math.max(8, Math.round(38 * Math.max(0.15, voiceLevel * scale)))}px;`}
                  ></span>
                {/each}
              </span>
            {/if}
          </span>
        </button>

        <div>
          <p class="text-lg font-semibold text-slate-100" aria-live="polite">{stateLabel(modeState)}</p>
          <p class="mt-2 text-sm text-slate-500">
            {#if playbackNeedsGesture}
              Tap the orb to enable audio playback.
            {:else if modeState === 'speaking'}
              Tap the orb to interrupt and speak.
            {:else if modeState === 'listening'}
              Tap the orb when you finish speaking.
            {:else}
              Keep this screen open for hands-free replies.
            {/if}
          </p>
        </div>
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
        <p class="text-center text-xs text-slate-500">Audio playback was blocked. Tap the orb to retry; text remains available.</p>
      {/if}

      {#if transcriptOpen && transcript.length > 0}
        <div id="conversation-mode-transcript" class="max-h-56 w-full overflow-y-auto rounded-2xl border border-slate-800 bg-slate-900/70 p-3 text-left text-sm text-slate-200">
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
