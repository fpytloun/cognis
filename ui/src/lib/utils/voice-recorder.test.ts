import { describe, expect, it, vi } from 'vitest';

import {
  audioExtensionForMimeType,
  formatVoiceDuration,
  normalizeVoiceLevel,
  pickSupportedAudioMimeType,
  rmsFromTimeDomainData,
  stopMediaStreamTracks,
} from './voice-recorder';

describe('voice recorder helpers', () => {
  it('selects the first supported audio MIME type', () => {
    const mediaRecorder = {
      isTypeSupported: (candidate: string) => candidate === 'audio/ogg;codecs=opus',
    };

    expect(pickSupportedAudioMimeType(mediaRecorder)).toBe('audio/ogg;codecs=opus');
  });

  it('falls back to an empty MIME type when no MediaRecorder support exists', () => {
    expect(pickSupportedAudioMimeType(undefined)).toBe('');
  });

  it('maps common MIME types to file extensions', () => {
    expect(audioExtensionForMimeType('audio/webm;codecs=opus')).toBe('webm');
    expect(audioExtensionForMimeType('audio/ogg')).toBe('ogg');
    expect(audioExtensionForMimeType('audio/mp4')).toBe('m4a');
    expect(audioExtensionForMimeType('audio/wav')).toBe('wav');
    expect(audioExtensionForMimeType('application/octet-stream')).toBe('bin');
  });

  it('formats elapsed recording durations', () => {
    expect(formatVoiceDuration(0)).toBe('0:00');
    expect(formatVoiceDuration(9)).toBe('0:09');
    expect(formatVoiceDuration(65)).toBe('1:05');
  });

  it('computes RMS and normalized voice level', () => {
    const rms = rmsFromTimeDomainData(new Float32Array([0.2, -0.2, 0.2, -0.2]));
    expect(rms).toBeCloseTo(0.2);
    expect(normalizeVoiceLevel(rms, 0.05)).toBeCloseTo(1);
    expect(normalizeVoiceLevel(0, 0.05)).toBe(0);
  });

  it('stops every track on a stream defensively', () => {
    const stop = vi.fn();
    const stream = {
      getTracks: () => [{ stop }, { stop }],
    } as unknown as MediaStream;

    stopMediaStreamTracks(stream);

    expect(stop).toHaveBeenCalledTimes(2);
  });
});
