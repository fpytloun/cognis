<script lang="ts">
  import Mic from 'lucide-svelte/icons/mic';
  import Square from 'lucide-svelte/icons/square';

  import { addToast } from '$lib/stores/toasts';
  import type { AttachmentRef } from '$lib/types/api';

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
    disabled?: boolean;
    class?: string;
  }

  let { onrecorded, disabled = false, class: className = '' }: Props = $props();

  let recorder: MediaRecorder | null = null;
  let stream: MediaStream | null = null;
  let chunks: Blob[] = [];
  let recording = $state(false);
  let busy = $state(false);
  let elapsedSeconds = $state(0);
  let timerHandle: ReturnType<typeof setInterval> | null = null;
  let startedAt = 0;

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

  function clearTimer(): void {
    if (timerHandle !== null) {
      clearInterval(timerHandle);
      timerHandle = null;
    }
  }

  function stopTracks(): void {
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
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
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      busy = false;
      addToast('Microphone access denied. Allow it in browser settings.', 'error');
      return;
    }
    const mimeType = pickMimeType();
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch (err) {
      busy = false;
      stopTracks();
      addToast('Recording is not supported in this browser', 'error');
      return;
    }
    chunks = [];
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        chunks.push(event.data);
      }
    };
    recorder.onstop = () => {
      void handleStop(mimeType);
    };
    recorder.onerror = () => {
      stopTracks();
      clearTimer();
      recording = false;
      busy = false;
    };
    recorder.start();
    startedAt = Date.now();
    elapsedSeconds = 0;
    timerHandle = setInterval(() => {
      elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
    }, 250);
    recording = true;
    busy = false;
  }

  async function handleStop(originalMime: string): Promise<void> {
    clearTimer();
    stopTracks();
    recording = false;
    if (chunks.length === 0) {
      busy = false;
      return;
    }
    const blob = new Blob(chunks, { type: originalMime || 'audio/webm' });
    chunks = [];
    if (blob.size === 0) {
      busy = false;
      return;
    }
    const ext = extensionFor(blob.type);
    const filename = `voice-${Date.now()}.${ext}`;
    const duration = Math.max(1, Math.floor((Date.now() - startedAt) / 1000));
    const blobUrl = URL.createObjectURL(blob);
    busy = true;
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
    } catch (err) {
      URL.revokeObjectURL(blobUrl);
      const message = err instanceof Error ? err.message : 'Failed to upload recording';
      addToast(message, 'error', 4_000, 'Voice recording failed');
    } finally {
      busy = false;
    }
  }

  function stopRecording(): void {
    if (!recorder || !recording) return;
    try {
      recorder.stop();
    } catch {
      stopTracks();
      clearTimer();
      recording = false;
    }
  }

  function handleClick(): void {
    if (recording) {
      stopRecording();
    } else {
      void startRecording();
    }
  }

  function formatTime(seconds: number): string {
    const m = Math.floor(seconds / 60).toString();
    const s = (seconds % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
  }
</script>

<span class={`inline-flex items-center gap-1 ${className}`}>
  {#if recording}
    <span
      class="inline-flex items-center gap-1 rounded-full bg-rose-500/20 px-2 py-0.5 text-[11px] font-medium text-rose-300"
      aria-live="polite"
    >
      <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-rose-400"></span>
      {formatTime(elapsedSeconds)}
    </span>
    <button
      type="button"
      onclick={handleClick}
      disabled={disabled || (busy && !recording)}
      aria-label="Stop recording"
      title="Stop recording"
      class="inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full bg-rose-500/15 text-rose-300 transition hover:bg-rose-500/25 hover:text-rose-200 disabled:pointer-events-none disabled:opacity-40"
    >
      <Square class="h-4 w-4 fill-current" />
    </button>
  {:else}
    <button
      type="button"
      onclick={handleClick}
      disabled={disabled || busy}
      aria-label="Record voice message"
      title="Record voice message"
      class="inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-200 focus-within:bg-slate-800/60 focus-within:text-slate-200 disabled:pointer-events-none disabled:opacity-40"
    >
      <Mic class="h-4 w-4" />
    </button>
  {/if}
</span>
