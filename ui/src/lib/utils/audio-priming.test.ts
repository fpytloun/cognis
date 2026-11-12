import { afterEach, describe, expect, it, vi } from 'vitest';
import { AUDIO_PRIME_TIMEOUT_MS, primeAudio } from './audio-priming';

afterEach(() => vi.useRealTimers());

function element(play: () => Promise<void>) {
  return { src: '', currentTime: 0, pause: vi.fn(), play: vi.fn(play) } as unknown as HTMLAudioElement;
}

describe('gesture audio priming', () => {
  it('uses actual PCM samples rather than an empty WAV', async () => {
    const audio = element(() => Promise.resolve());
    expect(await primeAudio(audio)).toBe(true);
    const bytes = Uint8Array.from(atob(audio.src.split(',')[1]), (char) => char.charCodeAt(0));
    const view = new DataView(bytes.buffer);
    expect(view.getUint32(40, true)).toBeGreaterThan(0);
    expect(view.getUint32(40, true)).toBe(bytes.length - 44);
    expect(audio.pause).toHaveBeenCalledOnce();
  });

  it('settles a browser play promise that never resolves', async () => {
    vi.useFakeTimers();
    const audio = element(() => new Promise(() => {}));
    const priming = primeAudio(audio);
    await vi.advanceTimersByTimeAsync(AUDIO_PRIME_TIMEOUT_MS);
    expect(await priming).toBe(false);
    expect(audio.src).toBe('');
  });

  it('cancels priming and ignores a late play completion', async () => {
    let complete!: () => void;
    const audio = element(() => new Promise((resolve) => { complete = resolve; }));
    const controller = new AbortController();
    const priming = primeAudio(audio, controller.signal);
    controller.abort();
    expect(await priming).toBe(false);
    complete();
    await Promise.resolve();
    expect(audio.src).toBe('');
  });
});
