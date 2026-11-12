import type { SpeechStreamEvent } from '$lib/speech';
import type { TtsSynthesizeResponse } from '$lib/types/api';
import { primeAudio } from './audio-priming';
import { applyMediaPlaybackRate, validatePlaybackRate } from './playback-rate';

const abortError = () => new DOMException('Speech playback stopped', 'AbortError');
const MAX_BUFFERED_BYTES = 25 * 1024 * 1024;

/** One gesture-primed owner for live PCM and completed cached media. */
export class SpeechPlayback {
  private context: AudioContext | null = null;
  private audio: HTMLAudioElement | null = null;
  private controller: AbortController | null = null;
  private nodes = new Set<AudioBufferSourceNode>();
  private buffers: Array<{ buffer: AudioBuffer; offset: number }> = [];
  private epoch = 0;
  private positionOffset = 0;
  private duration = 0;
  private drainWaiters: Array<() => void> = [];
  private media = false;
  private paused = false;
  private queuedBytes = 0;
  private pcmGeneration = 0;
  private playbackRate = 1;

  constructor(private onPlaying: (playing: boolean) => void = () => {}) {}

  static supported(): boolean {
    return typeof AudioContext !== 'undefined';
  }

  async unlock(signal?: AbortSignal): Promise<void> {
    if (!this.context || this.context.state === 'closed') this.context = new AudioContext();
    this.audio ??= new Audio();
    await Promise.all([this.context.resume(), primeAudio(this.audio, signal)]);
    if (signal?.aborted) throw abortError();
    if (this.context.state !== 'running') throw new Error('Enable audio playback first');
  }

  stop(notify = true): void {
    this.controller?.abort();
    this.controller = null;
    this.resetPcm();
    this.audio?.pause();
    if (this.audio) this.audio.src = '';
    this.queuedBytes = 0;
    if (notify) this.onPlaying(false);
  }

  dispose(): void {
    this.stop();
    void this.context?.close();
    this.context = null;
  }

  async togglePause(): Promise<void> {
    this.paused = !this.paused;
    if (this.media) {
      if (this.paused) this.audio?.pause();
      else await this.audio?.play();
    } else if (this.context) {
      if (this.paused) await this.context.suspend();
      else await this.context.resume();
    }
    this.onPlaying(!this.paused);
  }

  setPlaybackRate(rate: number): void {
    validatePlaybackRate(rate);
    if (this.playbackRate === rate) return;
    const position = this.position();
    this.playbackRate = rate;
    if (this.audio) applyMediaPlaybackRate(this.audio, rate);
    if (!this.media && this.context && this.buffers.length) this.scheduleFrom(position);
  }

  private position(): number {
    return this.context
      ? Math.min(this.duration, this.positionOffset +
          Math.max(0, this.context.currentTime - this.epoch) * this.playbackRate)
      : 0;
  }

  private scheduleFrom(position: number): void {
    this.stopNodes();
    this.epoch = this.context!.currentTime + 0.025;
    this.positionOffset = position;
    for (const entry of this.buffers) this.schedule(entry.buffer, entry.offset, position);
    this.resolveDrain();
  }

  seek(seconds: number): void {
    if (this.media && this.audio && Number.isFinite(this.audio.duration)) {
      this.audio.currentTime = Math.max(0, Math.min(this.audio.duration, this.audio.currentTime + seconds));
    } else if (this.context && this.buffers.length) {
      this.scheduleFrom(Math.max(0, Math.min(this.duration, this.position() + seconds)));
    }
  }

  private stopNodes(): void {
    this.pcmGeneration += 1;
    for (const node of this.nodes) { node.onended = null; node.stop(); node.disconnect(); }
    this.nodes.clear();
  }

  private resetPcm(): void {
    this.stopNodes();
    this.buffers = [];
    this.duration = 0;
    this.positionOffset = 0;
    this.resolveDrain();
  }

  private resolveDrain(): void {
    if (!this.nodes.size) for (const resolve of this.drainWaiters.splice(0)) resolve();
  }

  private drain(): Promise<void> {
    return this.nodes.size ? new Promise((resolve) => this.drainWaiters.push(resolve)) : Promise.resolve();
  }

  private schedule(buffer: AudioBuffer, offset: number, position = 0): void {
    const context = this.context!;
    const skip = Math.max(0, position - offset);
    if (skip >= buffer.duration) return;
    const node = context.createBufferSource();
    const generation = this.pcmGeneration;
    node.buffer = buffer;
    node.playbackRate.value = this.playbackRate;
    node.connect(context.destination);
    node.onended = () => {
      node.disconnect();
      if (generation !== this.pcmGeneration) return;
      this.nodes.delete(node);
      this.resolveDrain();
    };
    this.nodes.add(node);
    node.start(Math.max(context.currentTime + 0.005,
      this.epoch + (offset + skip - this.positionOffset) / this.playbackRate), skip);
  }

  private append(encoded: string, offset: number, sampleRate: number): void {
    if (sampleRate !== 24_000 || encoded.length > 43_696) throw new Error('Invalid speech PCM format');
    const raw = atob(encoded);
    if (!raw.length || raw.length % 2 || offset !== Math.round(this.duration * sampleRate)) {
      throw new Error('Speech audio is out of order');
    }
    const context = this.context!;
    const buffer = context.createBuffer(1, raw.length / 2, sampleRate);
    const floats = buffer.getChannelData(0);
    for (let i = 0; i < floats.length; i++) {
      const sample = raw.charCodeAt(i * 2) | raw.charCodeAt(i * 2 + 1) << 8;
      floats[i] = (sample >= 32768 ? sample - 65536 : sample) / 32768;
    }
    if (!this.buffers.length) this.epoch = context.currentTime + 0.025;
    // Rebase after an underrun; previously scheduled buffers must never overlap.
    if (this.epoch + (this.duration - this.positionOffset) / this.playbackRate < context.currentTime) {
      this.epoch = context.currentTime + 0.025;
      this.positionOffset = this.duration;
    }
    this.buffers.push({ buffer, offset: this.duration });
    this.schedule(buffer, this.duration);
    this.duration += buffer.duration;
    this.onPlaying(!this.paused);
  }

  private playUrl(url: string, signal: AbortSignal): Promise<void> {
    const audio = this.audio!;
    this.media = true;
    return new Promise((resolve, reject) => {
      const finish = (error?: unknown) => {
        audio.onended = audio.onerror = null;
        signal.removeEventListener('abort', abort);
        error ? reject(error) : resolve();
      };
      const abort = () => { audio.pause(); finish(abortError()); };
      audio.onended = () => finish();
      audio.onerror = () => finish(new Error('Cached speech playback failed'));
      signal.addEventListener('abort', abort, { once: true });
      audio.src = url;
      applyMediaPlaybackRate(audio, this.playbackRate);
      void audio.play().then(() => this.onPlaying(true)).catch(finish);
    });
  }

  async play(
    events: (signal: AbortSignal) => AsyncIterable<SpeechStreamEvent>,
    signal: AbortSignal,
  ): Promise<TtsSynthesizeResponse> {
    if (!this.context || this.context.state !== 'running') throw new Error('Enable audio playback first');
    this.stop(false);
    const controller = new AbortController();
    this.controller = controller;
    this.paused = false;
    const abort = () => controller.abort();
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) controller.abort();
    let tail = Promise.resolve();
    let failure: unknown;
    let result: TtsSynthesizeResponse | undefined;
    let current: {
      index: number; bytes: number; frames: Array<{ audio_base64: string; sample_offset: number; sample_rate: number }>;
      active: boolean; finish: () => void; finished: Promise<void>;
    } | null = null;
    let nextPart = 0;
    const enqueue = (work: () => Promise<void>) => {
      tail = tail.then(() => { if (controller.signal.aborted) throw abortError(); return work(); });
      void tail.catch((error) => { failure = error; controller.abort(); });
    };
    try {
      for await (const event of events(controller.signal)) {
        if (failure) throw failure;
        if (controller.signal.aborted) throw abortError();
        if (event.type === 'audio_start') {
          if (current || event.part_index !== nextPart || event.sample_rate !== 24_000) throw new Error('Invalid speech part');
          let finish!: () => void;
          const finished = new Promise<void>((resolve) => { finish = resolve; });
          const group = {
            index: event.part_index, bytes: 0,
            frames: [] as Array<Extract<SpeechStreamEvent, { type: 'audio' }>>,
            active: false, finish, finished,
          };
          current = group;
          enqueue(async () => {
            this.media = false;
            this.resetPcm();
            group.active = true;
            for (const frame of group.frames) this.append(frame.audio_base64, frame.sample_offset, frame.sample_rate);
            group.frames = [];
            await group.finished;
            await this.drain();
            this.queuedBytes -= group.bytes;
            this.resetPcm();
          });
        } else if (event.type === 'audio') {
          if (!current || current.index !== event.part_index) throw new Error('Unexpected speech audio');
          const bytes = Math.floor(event.audio_base64.length * 3 / 4);
          this.queuedBytes += bytes;
          current.bytes += bytes;
          if (this.queuedBytes > MAX_BUFFERED_BYTES) throw new Error('Speech playback buffer exceeded');
          if (current.active) this.append(event.audio_base64, event.sample_offset, event.sample_rate);
          else current.frames.push(event);
        } else if (event.type === 'audio_done') {
          if (!current || current.index !== event.part_index) throw new Error('Invalid speech completion');
          current.finish();
        } else if (event.type === 'part') {
          if (event.part_index !== nextPart || event.streamed !== Boolean(current)) throw new Error('Invalid speech receipt');
          if (!event.streamed) enqueue(() => this.playUrl(event.part.audio_url, controller.signal));
          current = null;
          nextPart += 1;
        } else if (event.type === 'complete') {
          if (current || !nextPart) throw new Error('Incomplete speech manifest');
          result = event.response;
        } else throw new Error('Speech streaming failed');
      }
      if (!result) throw new Error('Speech ended before completion');
      await tail;
      return result;
    } catch (error) {
      controller.abort();
      throw failure ?? error;
    } finally {
      current?.finish();
      signal.removeEventListener('abort', abort);
      this.stop();
      await tail.catch(() => {});
    }
  }
}
