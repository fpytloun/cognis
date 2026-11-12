import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/svelte';
import { SpeechPlayback } from './speech-playback';
import type { SpeechStreamEvent } from '$lib/speech';
import type { TtsSynthesizeResponse } from '$lib/types/api';

class Source {
  buffer: AudioBuffer | null = null;
  playbackRate = { value: 1 };
  onended: (() => void) | null = null;
  connect = vi.fn(); disconnect = vi.fn(); start = vi.fn(); stop = vi.fn();
}
class Context {
  static instances: Context[] = [];
  state: AudioContextState = 'running';
  currentTime = 0; destination = {}; sources: Source[] = [];
  constructor() { Context.instances.push(this); }
  resume = vi.fn(async () => { this.state = 'running'; });
  suspend = vi.fn(async () => { this.state = 'suspended'; });
  close = vi.fn(async () => { this.state = 'closed'; });
  createBuffer(_channels: number, length: number, rate: number): AudioBuffer {
    return { duration: length / rate, getChannelData: () => new Float32Array(length) } as unknown as AudioBuffer;
  }
  createBufferSource(): AudioBufferSourceNode {
    const source = new Source(); this.sources.push(source);
    return source as unknown as AudioBufferSourceNode;
  }
}
class Audio {
  src = ''; preload = ''; currentTime = 0; duration = 10; paused = true;
  onended: (() => void) | null = null; onerror: (() => void) | null = null;
  play = vi.fn(async () => { this.paused = false; });
  pause = vi.fn(() => { this.paused = true; });
  static instances: Audio[] = [];
  constructor() { Audio.instances.push(this); }
}
const response = {
  audio_url: '/complete.mp3', content_type: 'audio/mpeg', voice: 'alloy',
  model: 'tts-1', cached: false, parts: [{
    artifact_id: 'art_11111111111111111111111111111111', audio_url: '/complete.mp3',
    content_type: 'audio/mpeg', duration_seconds: null,
  }], adapted: false, script_artifact_id: null, warnings: [], duration_seconds: null,
} satisfies TtsSynthesizeResponse;
const start: SpeechStreamEvent = { type: 'audio_start', part_index: 0, sample_rate: 24000 };
const frame = { type: 'audio', part_index: 0, sample_rate: 24000, sample_offset: 0, audio_base64: 'AAA=' } satisfies SpeechStreamEvent;
const receipt: SpeechStreamEvent = { type: 'part', part_index: 0, streamed: true, part: response.parts[0] };
beforeEach(() => {
  Context.instances = []; Audio.instances = [];
  vi.stubGlobal('AudioContext', Context); vi.stubGlobal('Audio', Audio);
});
afterEach(() => vi.unstubAllGlobals());

it('plays before generation finishes but waits for audible drain', async () => {
  const player = new SpeechPlayback(); await player.unlock();
  let finish!: () => void;
  const barrier = new Promise<void>((resolve) => { finish = resolve; });
  async function* events(): AsyncGenerator<SpeechStreamEvent> {
    yield start; yield frame; await barrier;
    yield { type: 'audio_done', part_index: 0 }; yield receipt; yield { type: 'complete', response };
  }
  let settled = false;
  const result = player.play(events, new AbortController().signal).finally(() => { settled = true; });
  await waitFor(() => expect(Context.instances[0].sources).toHaveLength(1));
  expect(Context.instances[0].sources[0].start).toHaveBeenCalledOnce();
  expect(settled).toBe(false);
  finish(); await Promise.resolve();
  expect(settled).toBe(false);
  Context.instances[0].sources[0].onended?.();
  await expect(result).resolves.toEqual(response);
  player.dispose();
});

it('cancels capture and scheduled sound without replaying the request', async () => {
  const player = new SpeechPlayback(); await player.unlock();
  const controller = new AbortController();
  let calls = 0;
  async function* events(signal: AbortSignal): AsyncGenerator<SpeechStreamEvent> {
    calls += 1; yield start; yield frame;
    await new Promise<void>((_, reject) => signal.addEventListener('abort', () => reject(new DOMException('Stopped', 'AbortError')), { once: true }));
  }
  const result = player.play(events, controller.signal);
  const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' });
  await waitFor(() => expect(Context.instances[0].sources).toHaveLength(1));
  controller.abort(); await rejected;
  expect(Context.instances[0].sources[0].stop).toHaveBeenCalled();
  expect(calls).toBe(1); player.dispose();
});

it('reuses its gesture-primed element for cached media', async () => {
  const player = new SpeechPlayback(); await player.unlock();
  async function* events(): AsyncGenerator<SpeechStreamEvent> {
    yield { type: 'part', part_index: 0, streamed: false, part: response.parts[0] };
    yield { type: 'complete', response: { ...response, cached: true } };
  }
  const result = player.play(events, new AbortController().signal);
  await waitFor(() => expect(Audio.instances[0].src).toBe('/complete.mp3'));
  expect(Audio.instances).toHaveLength(1);
  Audio.instances[0].onended?.();
  await expect(result).resolves.toMatchObject({ cached: true }); player.dispose();
});

it('rejects reordered PCM instead of replaying another generation', async () => {
  const player = new SpeechPlayback(); await player.unlock();
  async function* events(): AsyncGenerator<SpeechStreamEvent> {
    yield start; yield { ...frame, sample_offset: 9 };
  }
  await expect(player.play(events, new AbortController().signal)).rejects.toThrow();
  expect(Context.instances[0].sources).toHaveLength(0); player.dispose();
});

it('changes PCM speed without losing position, pause, seek, or drain ownership', async () => {
  const player = new SpeechPlayback();
  player.setPlaybackRate(1.25);
  await player.unlock();
  const audio_base64 = btoa('\0'.repeat(4800));
  async function* events(): AsyncGenerator<SpeechStreamEvent> {
    yield start;
    yield { ...frame, audio_base64 };
    yield { ...frame, audio_base64, sample_offset: 2400 };
    yield { type: 'audio_done', part_index: 0 }; yield receipt; yield { type: 'complete', response };
  }
  const result = player.play(events, new AbortController().signal);
  const context = Context.instances[0];
  await waitFor(() => expect(context.sources).toHaveLength(2));
  expect(context.sources[0].playbackRate.value).toBe(1.25);
  expect(context.sources[1].start.mock.calls[0][0] - context.sources[0].start.mock.calls[0][0]).toBeCloseTo(0.08);
  context.currentTime = 0.1;
  player.setPlaybackRate(2);
  expect(context.sources[0].stop).toHaveBeenCalledOnce();
  expect(context.sources[2].playbackRate.value).toBe(2);
  expect(context.sources[2].start.mock.calls[0][1]).toBeCloseTo(0.09375);
  await player.togglePause();
  player.setPlaybackRate(0.75);
  expect(context.state).toBe('suspended');
  expect(context.sources[4].start.mock.calls[0][1]).toBeCloseTo(0.09375);
  await player.togglePause();
  player.seek(10);
  await expect(result).resolves.toEqual(response);
  player.dispose();
});

it('applies speed to cached media and rejects invalid values without restarting it', async () => {
  const player = new SpeechPlayback();
  player.setPlaybackRate(1.5);
  await player.unlock();
  async function* events(): AsyncGenerator<SpeechStreamEvent> {
    yield { type: 'part', part_index: 0, streamed: false, part: response.parts[0] };
    yield { type: 'complete', response };
  }
  const result = player.play(events, new AbortController().signal);
  await waitFor(() => expect(Audio.instances[0].src).toBe('/complete.mp3'));
  const audio = Audio.instances[0] as unknown as HTMLAudioElement;
  expect(audio.playbackRate).toBe(1.5);
  expect(() => player.setPlaybackRate(0)).toThrow(RangeError);
  player.setPlaybackRate(2);
  expect(audio.playbackRate).toBe(2);
  expect(audio.preservesPitch).toBe(true);
  Audio.instances[0].onended?.();
  await result;
  player.dispose();
});
