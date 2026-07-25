/**
 * Single-instance audio playback store.
 *
 * Ensures that only one assistant message TTS audio plays at a time across
 * the workspace. Starting a new playback stops any active one. Used by the
 * per-message speaker button and (indirectly) by conversation mode.
 */

import { writable } from 'svelte/store';

interface AudioPlayerState {
  /** A stable key identifying what is playing (e.g. message id). */
  currentKey: string | null;
  /** True while the underlying ``HTMLAudioElement`` is playing. */
  isPlaying: boolean;
  /** True while the audio is preparing (fetch/decode); UI shows a spinner. */
  isLoading: boolean;
}

const initialState: AudioPlayerState = {
  currentKey: null,
  isPlaying: false,
  isLoading: false
};

const internal = writable<AudioPlayerState>(initialState);

let activeAudio: HTMLAudioElement | null = null;
let activeKey: string | null = null;
let primedAudio: HTMLAudioElement | null = null;

const silentDataUrl =
  'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA=';

function detachActive(): void {
  if (activeAudio) {
    try {
      activeAudio.pause();
    } catch {
      // ignore
    }
    activeAudio.onended = null;
    activeAudio.onerror = null;
    activeAudio.onpause = null;
    activeAudio.onplaying = null;
    activeAudio.src = '';
  }
  activeAudio = null;
  activeKey = null;
}

export const audioPlayer = {
  subscribe: internal.subscribe,

  /**
   * Begin playing ``url`` under ``key``. Stops any previous playback first.
   * Returns the audio element so callers can attach extra listeners
   * (e.g. for queue chaining in conversation mode).
   */
  async play(key: string, url: string): Promise<HTMLAudioElement> {
    detachActive();
    const audio = primedAudio ?? new Audio();
    primedAudio = null;
    audio.src = url;
    audio.preload = 'auto';
    activeAudio = audio;
    activeKey = key;

    internal.set({ currentKey: key, isPlaying: false, isLoading: true });

    audio.onplaying = () => {
      if (activeKey === key) {
        internal.set({ currentKey: key, isPlaying: true, isLoading: false });
      }
    };
    audio.onpause = () => {
      if (activeKey === key) {
        internal.update((s) => ({ ...s, isPlaying: false }));
      }
    };
    audio.onended = () => {
      if (activeKey === key) {
        detachActive();
        internal.set(initialState);
      }
    };
    audio.onerror = () => {
      if (activeKey === key) {
        detachActive();
        internal.set(initialState);
      }
    };

    try {
      await audio.play();
    } catch (err) {
      // Browser blocked auto-play or user navigated away.
      if (activeKey === key) {
        detachActive();
        internal.set(initialState);
      }
      throw err;
    }
    return audio;
  },

  /**
   * Prime an audio element from a user gesture so a later synthesis request
   * can start playback after its network round-trip on browsers with strict
   * autoplay policies.
   */
  async unlock(): Promise<boolean> {
    if (typeof Audio === 'undefined') return false;
    if (primedAudio) return true;

    const audio = new Audio(silentDataUrl);
    audio.preload = 'auto';
    try {
      await audio.play();
      audio.pause();
      audio.currentTime = 0;
      primedAudio = audio;
      return true;
    } catch {
      audio.src = '';
      return false;
    }
  },

  /** Stop and reset the current playback (no-op if nothing is playing). */
  stop(): void {
    detachActive();
    internal.set(initialState);
  },

  /**
   * Stop only if the currently playing key matches ``key``. Useful when a
   * component is being unmounted and wants to clean up only its own audio.
   */
  stopIfKey(key: string): void {
    if (activeKey === key) {
      this.stop();
    }
  },

  isCurrent(key: string): boolean {
    return activeKey === key;
  }
};
