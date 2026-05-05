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

export interface AudioQueueEntry {
  /** URL of the audio to play (signed URL or Blob URL). */
  url: string;
  /** Optional caller-provided correlation id for diagnostics. */
  id?: string;
}

export class AudioQueue {
  private queue: AudioQueueEntry[] = [];
  private current: HTMLAudioElement | null = null;
  private playing = false;
  private cancelled = false;
  private unlocked = false;
  private idleCallbacks: Array<() => void> = [];
  private playingCallbacks: Array<(playing: boolean) => void> = [];
  private playbackErrorCallbacks: Array<() => void> = [];

  private readonly silentDataUrl =
    'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA=';

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
      this.current.src = '';
    }
    this.current = null;
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
    if (this.unlocked) return true;
    if (typeof Audio === 'undefined') return false;
    const audio = this.ensureAudio();
    audio.src = this.silentDataUrl;
    try {
      await audio.play();
      audio.pause();
      audio.currentTime = 0;
      this.unlocked = true;
      if (this.queue.length > 0 && !this.playing) {
        void this.playNext();
      }
      return true;
    } catch {
      this.unlocked = false;
      return false;
    }
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
    const audio = this.ensureAudio();
    if (!this.playing) {
      this.playing = true;
      this.notifyPlaying(true);
    }
    audio.onended = () => {
      if (this.current === audio) {
        audio.src = '';
      }
      void this.playNext();
    };
    audio.onerror = () => {
      if (this.current === audio) {
        audio.src = '';
      }
      this.notifyPlaybackError();
      void this.playNext();
    };
    audio.src = entry.url;
    try {
      await audio.play();
    } catch {
      // Autoplay blocked: keep the sentence queued so a later user tap can
      // unlock playback and continue instead of losing the assistant reply.
      this.unlocked = false;
      if (this.current === audio) {
        audio.src = '';
      }
      this.queue.unshift(entry);
      if (this.playing) {
        this.playing = false;
        this.notifyPlaying(false);
      }
      this.notifyPlaybackError();
    }
  }
}
