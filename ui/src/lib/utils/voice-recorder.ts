const AUDIO_MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4'
];

export function pickSupportedAudioMimeType(
  mediaRecorderCtor: Pick<typeof MediaRecorder, 'isTypeSupported'> | undefined =
    typeof MediaRecorder !== 'undefined' ? MediaRecorder : undefined
): string {
  if (!mediaRecorderCtor) return '';
  for (const candidate of AUDIO_MIME_CANDIDATES) {
    if (mediaRecorderCtor.isTypeSupported(candidate)) return candidate;
  }
  return '';
}

export function audioExtensionForMimeType(mimeType: string): string {
  const normalized = mimeType.toLowerCase();
  if (normalized.includes('webm')) return 'webm';
  if (normalized.includes('ogg')) return 'ogg';
  if (normalized.includes('mp4')) return 'm4a';
  if (normalized.includes('wav')) return 'wav';
  return 'bin';
}

export function formatVoiceDuration(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(safeSeconds / 60).toString();
  const remaining = (safeSeconds % 60).toString().padStart(2, '0');
  return `${minutes}:${remaining}`;
}

export function stopMediaStreamTracks(stream: MediaStream | null): void {
  if (!stream) return;
  try {
    for (const track of stream.getTracks()) {
      try {
        track.stop();
      } catch {
        // ignore per-track failures
      }
    }
  } catch {
    // ignore malformed stream objects from browser edge cases
  }
}

export function rmsFromTimeDomainData(buffer: Float32Array): number {
  if (buffer.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < buffer.length; i += 1) {
    sum += buffer[i] * buffer[i];
  }
  return Math.sqrt(sum / buffer.length);
}

export function normalizeVoiceLevel(rms: number, threshold = 0.018): number {
  if (!Number.isFinite(rms) || rms <= 0) return 0;
  const normalized = rms / Math.max(0.001, threshold * 4);
  return Math.max(0, Math.min(1, normalized));
}
