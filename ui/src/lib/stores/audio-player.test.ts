import { beforeEach, describe, expect, it, vi } from 'vitest';

class MockAudio {
  static instances: MockAudio[] = [];

  src = '';
  preload = '';
  currentTime = 0;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onpause: (() => void) | null = null;
  onplaying: (() => void) | null = null;
  play = vi.fn(async () => {
    this.onplaying?.();
  });
  pause = vi.fn(() => {
    this.onpause?.();
  });

  constructor(src = '') {
    this.src = src;
    MockAudio.instances.push(this);
  }
}

describe('audioPlayer', () => {
  beforeEach(() => {
    vi.resetModules();
    MockAudio.instances = [];
    vi.stubGlobal('Audio', MockAudio);
  });

  it('reuses the gesture-primed audio element for delayed playback', async () => {
    const { audioPlayer } = await import('./audio-player');

    await expect(audioPlayer.unlock()).resolves.toBe(true);
    const primedAudio = MockAudio.instances[0];

    await audioPlayer.play('message-1', 'https://audio.example/speech.mp3');

    expect(MockAudio.instances).toHaveLength(1);
    expect(primedAudio.src).toBe('https://audio.example/speech.mp3');
    expect(primedAudio.play).toHaveBeenCalledTimes(2);
  });
});
