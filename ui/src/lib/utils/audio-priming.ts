/** Bound gesture-time priming; an empty WAV can leave play() pending forever. */
export const AUDIO_PRIME_TIMEOUT_MS = 2_000;

export function autoplayDenied(error: unknown): boolean {
  return error !== null && typeof error === 'object' && 'name' in error && error.name === 'NotAllowedError';
}

let silentDataUrl: string | null = null;

function silentRecording(): string {
  if (silentDataUrl) return silentDataUrl;
  const samples = 80;
  const bytes = new Uint8Array(44 + samples * 2);
  const view = new DataView(bytes.buffer);
  const text = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++) bytes[offset + i] = value.charCodeAt(i);
  };
  text(0, 'RIFF'); view.setUint32(4, bytes.length - 8, true);
  text(8, 'WAVE'); text(12, 'fmt '); view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 8000, true); view.setUint32(28, 16000, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, 'data'); view.setUint32(40, samples * 2, true);
  silentDataUrl = `data:audio/wav;base64,${btoa(String.fromCharCode(...bytes))}`;
  return silentDataUrl;
}

/** Start synchronously inside the gesture, but never gate synthesis indefinitely. */
export function primeAudio(audio: HTMLAudioElement, signal?: AbortSignal): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);
  audio.src = silentRecording();
  return new Promise((resolve) => {
    let settled = false;
    const finish = (success: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      try {
        audio.pause();
        if (success) audio.currentTime = 0;
        else audio.src = '';
      } catch {
        success = false;
        audio.src = '';
      }
      resolve(success);
    };
    const abort = () => finish(false);
    const timer = setTimeout(() => finish(false), AUDIO_PRIME_TIMEOUT_MS);
    signal?.addEventListener('abort', abort, { once: true });
    try {
      void Promise.resolve(audio.play()).then(() => finish(true), () => finish(false));
    } catch {
      finish(false);
    }
  });
}
