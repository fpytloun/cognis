import { primeAudio } from './audio-priming';
import { SpeechPlayback } from './speech-playback';
import type { SpeechStreamEvent } from '$lib/speech';

/**
 * Sequential audio queue for TTS conversation mode.
 *
 * Sentences arrive over the chat WebSocket as ``tts_sentence_ready`` frames.
 * For each sentence, the client kicks off a TTS synthesis call and enqueues
 * the resulting URL here. The queue plays sentences in order, exposes a
 * "did the queue go idle" callback for the conversation mode loop to
 * restart the mic, and supports interruption (``clear``) when the user
 * cancels mid-playback.
 */

export type AudioQueueEntry = {
  /** URL of the audio to play (signed URL or Blob URL). */
  url: string;
  /** Optional caller-provided correlation id for diagnostics. */
  id?: string;
} | {
  stream: (signal: AbortSignal) => AsyncIterable<SpeechStreamEvent>;
  id?: string;
};

export class AudioQueue {
  private queue: AudioQueueEntry[] = [];
  private current: HTMLAudioElement | null = null;
  private playing = false;
  private audible = false;
  private cancelled = false;
  private unlocked = false;
  private idleCallbacks: Array<() => void> = [];
  private playingCallbacks: Array<(playing: boolean) => void> = [];
  private playbackErrorCallbacks: Array<() => void> = [];
  private playbackTimeout: ReturnType<typeof setTimeout> | null = null;
  private generation = 0;
  private primeController: AbortController | null = null;
  private speechPlayback: SpeechPlayback | null = null;
  private streamController: AbortController | null = null;
  private streamTask: Promise<unknown> | null = null;

  private static readonly PLAYBACK_TIMEOUT_MS = 60_000;

  enqueue(entry: AudioQueueEntry): void {
    if (this.cancelled) {
      this.cancelled = false;
    }
    this.queue.push(entry);
    if (!this.playing) {
      void this.playNext();
    }
  }

  clear(): void {
    this.primeController?.abort();
    this.primeController = null;
    this.generation += 1;
    this.streamController?.abort();
    this.speechPlayback?.stop();
    this.cancelled = true;
    this.queue = [];
    if (this.current) {
      try {
        this.current.pause();
      } catch {
        // ignore
      }
      this.current.onended = null;
      this.current.onerror = null;
      this.current.onloadedmetadata = null;
      this.current.src = '';
    }
    this.clearPlaybackTimeout();
    // Keep the gesture-primed element across interruptions, including in PWAs.
    if (this.playing) {
      this.playing = false;
      this.notifyPlaying(false);
    }
    // We intentionally do NOT call idle callbacks on clear — the caller
    // is asking us to stop, not telling us to wait for the queue to drain.
  }

  isPlaying(): boolean {
    return this.playing;
  }

  isEmpty(): boolean {
    return this.queue.length === 0 && !this.playing;
  }

  /** Subscribe to "queue drained" events. Returns an unsubscribe fn. */
  onIdle(cb: () => void): () => void {
    this.idleCallbacks.push(cb);
    return () => {
      this.idleCallbacks = this.idleCallbacks.filter((entry) => entry !== cb);
    };
  }

  /** Subscribe to playing-state changes. */
  onPlayingChange(cb: (playing: boolean) => void): () => void {
    this.playingCallbacks.push(cb);
    return () => {
      this.playingCallbacks = this.playingCallbacks.filter((entry) => entry !== cb);
    };
  }

  /** Subscribe to playback failures, usually mobile autoplay blocking. */
  onPlaybackError(cb: () => void): () => void {
    this.playbackErrorCallbacks.push(cb);
    return () => {
      this.playbackErrorCallbacks = this.playbackErrorCallbacks.filter((entry) => entry !== cb);
    };
  }

  /** Prime the reusable audio element from a user gesture when possible. */
  async unlock(): Promise<boolean> {
    let speechUnlock: Promise<void> = Promise.resolve();
    if (SpeechPlayback.supported()) {
      this.speechPlayback ??= new SpeechPlayback((playing) => this.notifyPlaying(playing));
      speechUnlock = this.speechPlayback.unlock();
    }
    if (this.unlocked) {
      try { await speechUnlock; return true; } catch { return false; }
    }
    if (typeof Audio === 'undefined') return false;
    const audio = this.ensureAudio();
    this.primeController?.abort();
    const controller = new AbortController();
    this.primeController = controller;
    const priming = primeAudio(audio, controller.signal);
    let unlocked = false;
    try { [unlocked] = await Promise.all([priming, speechUnlock]); } catch { return false; }
    if (this.primeController !== controller) return false;
    this.primeController = null;
    this.unlocked = unlocked;
    if (unlocked && this.queue.length > 0 && !this.playing) {
      void this.playNext();
    }
    return unlocked;
  }

  dispose(): void {
    this.clear();
    this.speechPlayback?.dispose();
    this.speechPlayback = null;
  }

  private notifyIdle(): void {
    for (const cb of [...this.idleCallbacks]) {
      try {
        cb();
      } catch {
        // ignore subscriber errors
      }
    }
  }

  private notifyPlaying(playing: boolean): void {
    if (this.audible === playing) return;
    this.audible = playing;
    for (const cb of [...this.playingCallbacks]) {
      try {
        cb(playing);
      } catch {
        // ignore
      }
    }
  }

  private notifyPlaybackError(): void {
    for (const cb of [...this.playbackErrorCallbacks]) {
      try {
        cb();
      } catch {
        // ignore
      }
    }
  }

  private clearPlaybackTimeout(): void {
    if (this.playbackTimeout !== null) {
      clearTimeout(this.playbackTimeout);
      this.playbackTimeout = null;
    }
  }

  private finishCurrent(audio: HTMLAudioElement): void {
    this.clearPlaybackTimeout();
    if (this.current === audio) {
      audio.onended = null;
      audio.onerror = null;
      audio.onloadedmetadata = null;
      audio.src = '';
      this.notifyPlaying(false);
    }
  }

  private ensureAudio(): HTMLAudioElement {
    if (this.current) return this.current;
    const audio = new Audio();
    audio.preload = 'auto';
    this.current = audio;
    return audio;
  }

  private async playNext(): Promise<void> {
    if (this.cancelled) {
      this.cancelled = false;
      return;
    }
    const entry = this.queue.shift();
    if (!entry) {
      if (this.playing) {
        this.playing = false;
        this.notifyPlaying(false);
      }
      this.notifyIdle();
      return;
    }
    const generation = ++this.generation;
    if (!this.playing) {
      this.playing = true;
    }
    if ('stream' in entry) {
      const controller = new AbortController();
      this.streamController = controller;
      try {
        await this.streamTask?.catch(() => {});
        if (generation !== this.generation) return;
        if (!this.speechPlayback) throw new Error('Streaming audio is not unlocked');
        const task = this.speechPlayback.play(entry.stream, controller.signal);
        this.streamTask = task;
        await task;
      } catch {
        if (generation === this.generation) this.notifyPlaybackError();
      } finally {
        if (generation === this.generation) {
          this.streamTask = null;
          this.streamController = null;
          void this.playNext();
        }
      }
      return;
    }
    const audio = this.ensureAudio();
    audio.onended = () => {
      if (generation !== this.generation) return;
      this.finishCurrent(audio);
      void this.playNext();
    };
    audio.onerror = () => {
      if (generation !== this.generation) return;
      this.finishCurrent(audio);
      this.notifyPlaybackError();
      void this.playNext();
    };
    this.clearPlaybackTimeout();
    const expire = () => {
      if (generation !== this.generation) return;
      this.finishCurrent(audio);
      this.notifyPlaybackError();
      void this.playNext();
    };
    this.playbackTimeout = setTimeout(expire, AudioQueue.PLAYBACK_TIMEOUT_MS);
    audio.onloadedmetadata = () => {
      if (generation !== this.generation || !Number.isFinite(audio.duration)) return;
      this.clearPlaybackTimeout();
      this.playbackTimeout = setTimeout(expire, Math.max(
        AudioQueue.PLAYBACK_TIMEOUT_MS, (audio.duration + 15) * 1000
      ));
    };
    audio.src = entry.url;
    try {
      await audio.play();
      if (generation === this.generation) this.notifyPlaying(true);
    } catch {
      if (generation !== this.generation) return;
      // Autoplay was blocked or playback failed. Drop this sentence and keep
      // draining so conversation mode never gets wedged in Speaking.
      this.unlocked = false;
      this.finishCurrent(audio);
      if (this.playing) {
        this.playing = false;
        this.notifyPlaying(false);
      }
      this.notifyPlaybackError();
      void this.playNext();
    }
  }
}
