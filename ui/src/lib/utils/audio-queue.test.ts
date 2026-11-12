import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AudioQueue } from './audio-queue';

vi.mock('$lib/stores/audio-player', () => ({
  audioPlayer: { stop: vi.fn(), unlock: vi.fn().mockResolvedValue(true) }
}));

class AudioStub {
  src = '';
  preload = '';
  duration = 180;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onloadedmetadata: (() => void) | null = null;
  pause = vi.fn();
  play = vi.fn().mockResolvedValue(undefined);
  removeAttribute = vi.fn();
}

describe('AudioQueue', () => {
  let audio: AudioStub;
  beforeEach(() => {
    vi.useFakeTimers();
    audio = new AudioStub();
    vi.stubGlobal('Audio', class { constructor() { return audio; } });
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

  it('does not cut a long podcast part at the sentence watchdog limit', async () => {
    const queue = new AudioQueue();
    const failed = vi.fn();
    queue.onPlaybackError(failed);
    queue.enqueue({ url: '/part-1' });
    await Promise.resolve();
    audio.onloadedmetadata?.();
    await vi.advanceTimersByTimeAsync(61_000);
    expect(failed).not.toHaveBeenCalled();
    expect(queue.isEmpty()).toBe(false);
    audio.onended?.();
    expect(queue.isEmpty()).toBe(true);
    queue.clear();
  });

  it('ignores rejection of a cleared playback attempt', async () => {
    let reject!: (reason: Error) => void;
    audio.play.mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }));
    const queue = new AudioQueue();
    const failed = vi.fn();
    queue.onPlaybackError(failed);
    queue.enqueue({ url: '/old' });
    await Promise.resolve();
    queue.clear();
    queue.enqueue({ url: '/new' });
    await Promise.resolve();
    reject(new Error('old playback failed'));
    await Promise.resolve();
    expect(failed).not.toHaveBeenCalled();
    expect(audio.src).toBe('/new');
    expect(audio.pause).toHaveBeenCalled();
    queue.clear();
  });

  it('reports audible playback only after media starts and ignores a cancelled start', async () => {
    let oldStart!: () => void, currentStart!: () => void, nextStart!: () => void;
    audio.play
      .mockImplementationOnce(() => new Promise<void>(resolve => { oldStart = resolve; }))
      .mockImplementationOnce(() => new Promise<void>(resolve => { currentStart = resolve; }))
      .mockImplementationOnce(() => new Promise<void>(resolve => { nextStart = resolve; }));
    const queue = new AudioQueue(), playing = vi.fn();
    queue.onPlayingChange(playing);
    queue.enqueue({ url: '/old' });
    expect(queue.isEmpty()).toBe(false);
    expect(playing).not.toHaveBeenCalled();
    queue.clear();
    queue.enqueue({ url: '/current' });
    oldStart();
    await Promise.resolve();
    expect(playing.mock.calls.map(([value]) => value)).toEqual([]);
    currentStart();
    await Promise.resolve();
    expect(playing.mock.calls.map(([value]) => value)).toEqual([true]);
    queue.enqueue({ url: '/next' });
    audio.onended?.();
    expect(playing.mock.calls.map(([value]) => value)).toEqual([true, false]);
    expect(queue.isEmpty()).toBe(false);
    nextStart();
    await Promise.resolve();
    expect(playing.mock.calls.map(([value]) => value)).toEqual([true, false, true]);
    audio.onended?.();
    expect(playing.mock.calls.map(([value]) => value)).toEqual([true, false, true, false]);
    queue.dispose();
  });
});
