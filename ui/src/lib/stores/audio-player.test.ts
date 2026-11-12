import { beforeEach, describe, expect, it, vi } from 'vitest';

class MockAudio {
  static instances: MockAudio[] = [];

  src = '';
  preload = '';
  currentTime = 0;
  duration = 30;
  paused = true;
  onloadedmetadata: (() => void) | null = null;
  load = vi.fn();
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onpause: (() => void) | null = null;
  onplaying: (() => void) | null = null;
  play = vi.fn(async () => {
    this.paused = false;
    this.onplaying?.();
  });
  pause = vi.fn(() => {
    this.paused = true;
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
  it('carries message seek offsets across parts and resumes without synthesis', async () => {
    const { audioPlayer } = await import('./audio-player');
    const audio = await audioPlayer.play('multipart-message', ['/first.mp3', '/second.mp3']) as unknown as MockAudio;
    audio.currentTime = 25;
    audioPlayer.seek(10);
    expect(audio.src).toBe('/second.mp3');
    audio.duration = 40;
    audio.onloadedmetadata?.();
    expect(audio.currentTime).toBe(5);
    audioPlayer.seek(-10);
    expect(audio.src).toBe('/first.mp3');
    audio.duration = 30;
    audio.onloadedmetadata?.();
    expect(audio.currentTime).toBe(25);
    await audioPlayer.togglePause();
    expect(audio.paused).toBe(true);
    await audioPlayer.togglePause();
    expect(audio.paused).toBe(false);
    audioPlayer.stop();
  });
  it('reloads consecutive identical cached parts rather than stalling', async () => {
    const { audioPlayer } = await import('./audio-player');
    const audio = await audioPlayer.play('repeated-message', ['/same.mp3', '/same.mp3']) as unknown as MockAudio;
    audio.onended?.();
    expect(audio.load).toHaveBeenCalledOnce();
    audio.onloadedmetadata?.();
    expect(audio.play).toHaveBeenCalledTimes(2);
    audioPlayer.stop();
  });

  it('tracks component-owned playback without destroying its source when another player takes over', async () => {
    const { audioPlayer } = await import('./audio-player');
    const { get } = await import('svelte/store');
    const audio = new MockAudio('/document.mp3');
    audioPlayer.attach('document-recording', audio as unknown as HTMLAudioElement);
    expect(get(audioPlayer)).toEqual({ currentKey: 'document-recording', isPlaying: true, isLoading: false, playbackRate: 1 });
    audio.pause();
    expect(get(audioPlayer).isPlaying).toBe(false);
    await audioPlayer.play('chat-message', '/message.mp3');
    expect(audio.src).toBe('/document.mp3');
    expect(audio.pause).toHaveBeenCalled();
    audioPlayer.attach('document-recording', audio as unknown as HTMLAudioElement);
    expect(get(audioPlayer).currentKey).toBe('document-recording');
    audioPlayer.stop();
  });

  it('does not let an old viewer stop another viewer of the same recording', async () => {
    const { audioPlayer } = await import('./audio-player');
    const { get } = await import('svelte/store');
    const oldViewer = new MockAudio('/recording.mp3');
    const newViewer = new MockAudio('/recording.mp3');
    audioPlayer.attach('shared-recording', oldViewer as unknown as HTMLAudioElement);
    audioPlayer.attach('shared-recording', newViewer as unknown as HTMLAudioElement);
    audioPlayer.stopIfKey('shared-recording', oldViewer as unknown as HTMLAudioElement);
    expect(get(audioPlayer)).toEqual({ currentKey: 'shared-recording', isPlaying: true, isLoading: false, playbackRate: 1 });
    audioPlayer.stopIfKey('shared-recording', newViewer as unknown as HTMLAudioElement);
    expect(get(audioPlayer).currentKey).toBeNull();
  });

  it('shares speed across owned and component media without restarting them', async () => {
    const { audioPlayer } = await import('./audio-player');
    const { get } = await import('svelte/store');
    await audioPlayer.play('first', '/first.mp3');
    const active = MockAudio.instances.at(-1)!;
    active.currentTime = 8;
    audioPlayer.setPlaybackRate(1.5);
    expect((active as unknown as HTMLAudioElement).playbackRate).toBe(1.5);
    expect(active.currentTime).toBe(8);
    expect(active.play).toHaveBeenCalledOnce();
    audioPlayer.stop();
    const componentAudio = new MockAudio('/deliverable.mp3');
    audioPlayer.attach('deliverable', componentAudio as unknown as HTMLAudioElement);
    expect((componentAudio as unknown as HTMLAudioElement).playbackRate).toBe(1.5);
    expect(get(audioPlayer).playbackRate).toBe(1.5);
    expect(() => audioPlayer.setPlaybackRate(Number.NaN)).toThrow(RangeError);
    expect(get(audioPlayer).playbackRate).toBe(1.5);
    audioPlayer.stop();
  });
});
