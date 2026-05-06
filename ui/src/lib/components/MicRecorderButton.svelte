<script lang="ts">
  import Mic from 'lucide-svelte/icons/mic';
  import Square from 'lucide-svelte/icons/square';
  import ArrowUp from 'lucide-svelte/icons/arrow-up';
  import X from 'lucide-svelte/icons/x';
  import { onDestroy } from 'svelte';

  import { addToast } from '$lib/stores/toasts';
  import { haptic } from '$lib/haptics';
  import type { AttachmentRef } from '$lib/types/api';
  import { ScreenWakeLock } from '$lib/utils';
  import {
    audioExtensionForMimeType,
    formatVoiceDuration,
    normalizeVoiceLevel,
    pickSupportedAudioMimeType,
    rmsFromTimeDomainData,
    stopMediaStreamTracks,
  } from '$lib/utils/voice-recorder';

  /**
   * iMessage-style microphone recorder.
   *
   * Press the mic icon → starts recording (timer pill shows the duration).
   * Press stop → finishes recording, uploads to ``/api/v1/artifacts/upload``
   * and emits an ``onrecorded`` callback with an ``AttachmentRef``-shaped
   * object. The composer adds it to its attachment tray as a voice
   * recording so the user can preview, delete, or send it.
   *
   * The actual transcription happens on send (``transcribe-on-send``) so
   * recordings can coexist with typed text in a single message.
   */

  interface Props {
    onrecorded: (attachment: AttachmentRef) => void | Promise<void>;
    onsendrecorded?: () => void | Promise<void>;
    disabled?: boolean;
    class?: string;
  }

  let { onrecorded, onsendrecorded = undefined, disabled = false, class: className = '' }: Props = $props();

  let recorder: MediaRecorder | null = null;
  let stream: MediaStream | null = null;
  let chunks: Blob[] = [];
  let recording = $state(false);
  let busy = $state(false);
  let uploading = $state(false);
  let elapsedSeconds = $state(0);
  let voiceLevel = $state(0);
  let cancelling = $state(false);
  let timerHandle: ReturnType<typeof setInterval> | null = null;
  let meterHandle: ReturnType<typeof setInterval> | null = null;
  let startedAt = 0;
  let sendAfterStop = false;
  let cancelAfterStop = false;
  let audioContext: AudioContext | null = null;
  let audioSource: MediaStreamAudioSourceNode | null = null;
  let analyser: AnalyserNode | null = null;
  let pressPointerId: number | null = null;
  let pressStartX = 0;
  let holdTimer: ReturnType<typeof setTimeout> | null = null;
  let holdStartRequested = false;
  let stopAfterHoldStart = false;
  let cancelAfterHoldStart = false;
  let cancelThresholdNotified = false;
  let suppressNextClick = false;
  const HOLD_TO_RECORD_MS = 260;
  const CANCEL_DRAG_PX = 72;
  const wakeLock = new ScreenWakeLock();

  function addPressWindowListeners(): void {
    window.addEventListener('pointermove', handleMicPointerMove, true);
    window.addEventListener('pointerup', handleMicPointerUp, true);
    window.addEventListener('pointercancel', handleMicPointerCancel, true);
  }

  function removePressWindowListeners(): void {
    window.removeEventListener('pointermove', handleMicPointerMove, true);
    window.removeEventListener('pointerup', handleMicPointerUp, true);
    window.removeEventListener('pointercancel', handleMicPointerCancel, true);
  }

  function clearTimer(): void {
    if (timerHandle !== null) {
      clearInterval(timerHandle);
      timerHandle = null;
    }
  }

  function clearHoldTimer(): void {
    if (holdTimer !== null) {
      clearTimeout(holdTimer);
      holdTimer = null;
    }
  }

  function clearMeter(): void {
    if (meterHandle !== null) {
      clearInterval(meterHandle);
      meterHandle = null;
    }
    audioSource?.disconnect();
    audioSource = null;
    analyser = null;
    voiceLevel = 0;
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
  }

  function stopTracks(): void {
    stopMediaStreamTracks(stream);
    stream = null;
  }

  function setupMeter(nextStream: MediaStream): void {
    clearMeter();
    if (typeof AudioContext === 'undefined') return;
    try {
      audioContext = new AudioContext();
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      audioSource = audioContext.createMediaStreamSource(nextStream);
      audioSource.connect(analyser);
      const buffer = new Float32Array(analyser.fftSize);
      meterHandle = setInterval(() => {
        if (!analyser) return;
        analyser.getFloatTimeDomainData(buffer);
        voiceLevel = normalizeVoiceLevel(rmsFromTimeDomainData(buffer));
      }, 90);
    } catch {
      clearMeter();
    }
  }

  async function startRecording(): Promise<void> {
    if (recording || busy) return;
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      addToast('Microphone is not available in this browser', 'error');
      return;
    }
    busy = true;
    try {
      void wakeLock.acquire();
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      busy = false;
      stopAfterHoldStart = false;
      cancelAfterHoldStart = false;
      void wakeLock.release();
      addToast('Microphone access denied. Allow it in browser settings.', 'error');
      return;
    }
    const mimeType = pickSupportedAudioMimeType();
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch (err) {
      busy = false;
      stopTracks();
      void wakeLock.release();
      addToast('Recording is not supported in this browser', 'error');
      return;
    }
    setupMeter(stream);
    chunks = [];
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        chunks.push(event.data);
      }
    };
    recorder.onstop = () => {
      void handleStop(mimeType, sendAfterStop);
      sendAfterStop = false;
    };
    recorder.onerror = () => {
      stopTracks();
      clearTimer();
      clearMeter();
      recording = false;
      busy = false;
      void wakeLock.release();
    };
    recorder.start();
    startedAt = Date.now();
    elapsedSeconds = 0;
    cancelling = false;
    cancelAfterStop = false;
    timerHandle = setInterval(() => {
      elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
    }, 250);
    recording = true;
    busy = false;
    haptic.medium();
    if (stopAfterHoldStart) {
      const shouldCancel = cancelAfterHoldStart;
      stopAfterHoldStart = false;
      cancelAfterHoldStart = false;
      if (shouldCancel) cancelRecording();
      else stopRecording(false);
    }
  }

  async function handleStop(originalMime: string, sendNow: boolean): Promise<void> {
    clearTimer();
    clearMeter();
    stopTracks();
    recording = false;
    const wasCancelled = cancelAfterStop;
    cancelAfterStop = false;
    cancelling = false;
    if (wasCancelled) {
      chunks = [];
      elapsedSeconds = 0;
      busy = false;
      void wakeLock.release();
      haptic.warning();
      return;
    }
    if (chunks.length === 0) {
      busy = false;
      void wakeLock.release();
      return;
    }
    const blob = new Blob(chunks, { type: originalMime || 'audio/webm' });
    chunks = [];
    if (blob.size === 0) {
      busy = false;
      void wakeLock.release();
      return;
    }
    const ext = audioExtensionForMimeType(blob.type);
    const filename = `voice-${Date.now()}.${ext}`;
    const duration = Math.max(1, Math.floor((Date.now() - startedAt) / 1000));
    const blobUrl = URL.createObjectURL(blob);
    busy = true;
    uploading = true;
    try {
      const form = new FormData();
      form.append('file', blob, filename);
      form.append('purpose', 'chat_input');
      const response = await fetch('/api/v1/artifacts/upload', {
        method: 'POST',
        body: form,
        credentials: 'include'
      });
      if (!response.ok) {
        let detail = 'Upload failed';
        try {
          const body = await response.json();
          detail = body?.error?.message ?? detail;
        } catch {
          // ignore
        }
        throw new Error(detail);
      }
      const payload = await response.json();
      const attachment: AttachmentRef = {
        artifact_id: payload.artifact_id,
        kind: 'audio',
        mime_type: payload.mime_type ?? blob.type,
        filename: payload.filename ?? filename,
        size_bytes: payload.size_bytes ?? blob.size,
        url: payload.url ?? null,
        duration_seconds: duration,
        blob_url: blobUrl,
        voice_recording: true
      };
      await onrecorded(attachment);
      if (sendNow) {
        await onsendrecorded?.();
      }
      haptic.success();
    } catch (err) {
      URL.revokeObjectURL(blobUrl);
      const message = err instanceof Error ? err.message : 'Failed to upload recording';
      addToast(message, 'error', 4_000, 'Voice recording failed');
      haptic.error();
    } finally {
      busy = false;
      uploading = false;
      void wakeLock.release();
    }
  }

  function stopRecording(sendNow = false): void {
    if (!recorder || !recording) return;
    sendAfterStop = sendNow;
    try {
      recorder.stop();
    } catch {
      sendAfterStop = false;
      stopTracks();
      clearTimer();
      clearMeter();
      recording = false;
      void wakeLock.release();
    }
  }

  function cancelRecording(): void {
    if (!recorder || !recording) return;
    cancelAfterStop = true;
    try {
      recorder.stop();
    } catch {
      cancelAfterStop = false;
      chunks = [];
      stopTracks();
      clearTimer();
      clearMeter();
      recording = false;
      cancelling = false;
      busy = false;
      void wakeLock.release();
    }
  }

  function handleClick(event?: MouseEvent): void {
    if (suppressNextClick) {
      event?.preventDefault();
      suppressNextClick = false;
      return;
    }
    if (recording) {
      stopRecording();
    } else {
      void startRecording();
    }
  }

  function handleMicPointerDown(event: PointerEvent): void {
    if (disabled || busy || recording || event.pointerType === 'mouse') return;
    pressPointerId = event.pointerId;
    pressStartX = event.clientX;
    holdStartRequested = false;
    stopAfterHoldStart = false;
    cancelAfterHoldStart = false;
    cancelling = false;
    cancelThresholdNotified = false;
    suppressNextClick = false;
    try {
      (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    } catch {
      // ignore
    }
    addPressWindowListeners();
    clearHoldTimer();
    holdTimer = setTimeout(() => {
      holdTimer = null;
      holdStartRequested = true;
      suppressNextClick = true;
      void startRecording();
    }, HOLD_TO_RECORD_MS);
  }

  function handleMicPointerMove(event: PointerEvent): void {
    if (pressPointerId !== event.pointerId) return;
    if (!holdStartRequested) return;
    const nextCancelling = event.clientX - pressStartX < -CANCEL_DRAG_PX;
    if (nextCancelling && !cancelThresholdNotified) {
      cancelThresholdNotified = true;
      haptic.warning();
    }
    cancelling = nextCancelling;
    if (stopAfterHoldStart) cancelAfterHoldStart = nextCancelling;
  }

  function handleMicPointerUp(event: PointerEvent): void {
    if (pressPointerId !== event.pointerId) return;
    const wasHold = holdStartRequested;
    pressPointerId = null;
    removePressWindowListeners();
    clearHoldTimer();
    try {
      (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    } catch {
      // ignore
    }
    if (!wasHold) return;
    suppressNextClick = true;
    if (recording) {
      if (cancelling) cancelRecording();
      else stopRecording(false);
    } else {
      stopAfterHoldStart = true;
      cancelAfterHoldStart = cancelling;
    }
    window.setTimeout(() => {
      suppressNextClick = false;
    }, 450);
  }

  function handleMicPointerCancel(event: PointerEvent): void {
    if (pressPointerId !== event.pointerId) return;
    pressPointerId = null;
    removePressWindowListeners();
    clearHoldTimer();
    suppressNextClick = true;
    if (recording) {
      cancelRecording();
    } else if (holdStartRequested) {
      stopAfterHoldStart = true;
      cancelAfterHoldStart = true;
    }
    window.setTimeout(() => {
      suppressNextClick = false;
    }, 450);
  }

  onDestroy(() => {
    stopTracks();
    clearTimer();
    clearHoldTimer();
    removePressWindowListeners();
    clearMeter();
    void wakeLock.release();
  });
</script>

<span class={`inline-flex items-center gap-1 ${className}`}>
  {#if uploading}
    <span class="inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-900/90 px-3 py-1.5 text-[11px] font-medium text-slate-300" aria-live="polite">
      <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-sky-400"></span>
      Uploading voice…
    </span>
  {:else if recording}
    <span
      class={`inline-flex items-center gap-2 rounded-full px-2.5 py-1 text-[11px] font-medium ${cancelling ? 'bg-rose-500/25 text-rose-100' : 'bg-rose-500/20 text-rose-300'}`}
      aria-live="polite"
    >
      <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-rose-400"></span>
      <span class="tabular-nums">{formatVoiceDuration(elapsedSeconds)}</span>
      <span class="hidden items-end gap-0.5 sm:inline-flex" aria-hidden="true">
        {#each [0.35, 0.65, 0.95, 0.55, 0.8] as scale}
          <span
            class="w-0.5 rounded-full bg-current opacity-80 transition-[height] duration-75"
            style={`height: ${Math.max(4, Math.round(18 * Math.max(0.15, voiceLevel * scale)))}px;`}
          ></span>
        {/each}
      </span>
      <span class="hidden text-slate-400 sm:inline">{cancelling ? 'Release to cancel' : 'Release to save'}</span>
    </span>
    <button
      type="button"
      onclick={cancelRecording}
      disabled={disabled || busy}
      aria-label="Cancel recording"
      title="Cancel recording"
      class="inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full bg-slate-800 text-slate-300 transition hover:bg-rose-500/25 hover:text-rose-200 disabled:pointer-events-none disabled:opacity-40"
    >
      <X class="h-4 w-4" />
    </button>
    <button
      type="button"
      onclick={(event) => handleClick(event)}
      disabled={disabled || (busy && !recording)}
      aria-label="Stop recording"
      title="Stop recording"
      class="inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full bg-rose-500/15 text-rose-300 transition hover:bg-rose-500/25 hover:text-rose-200 disabled:pointer-events-none disabled:opacity-40"
    >
      <Square class="h-4 w-4 fill-current" />
    </button>
    <button
      type="button"
      onclick={() => stopRecording(true)}
      disabled={disabled || busy}
      aria-label="Finish recording and send"
      title="Finish recording and send"
      class="inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full bg-sky-500 text-slate-950 transition hover:bg-sky-400 disabled:pointer-events-none disabled:opacity-40"
    >
      <ArrowUp class="h-4 w-4" stroke-width="2.5" />
    </button>
  {:else}
    <button
      type="button"
      onclick={(event) => handleClick(event)}
      onpointerdown={handleMicPointerDown}
      onpointermove={handleMicPointerMove}
      onpointerup={handleMicPointerUp}
      onpointercancel={handleMicPointerCancel}
      disabled={disabled || busy}
      aria-label="Record voice message. Tap to start, or press and hold to record."
      title="Tap to record, or hold and release to save"
      class="inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-200 focus-within:bg-slate-800/60 focus-within:text-slate-200 disabled:pointer-events-none disabled:opacity-40"
    >
      <Mic class="h-4 w-4" />
    </button>
  {/if}
</span>
