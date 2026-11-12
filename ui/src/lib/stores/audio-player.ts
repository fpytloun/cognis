/**
 * Single-instance audio playback store.
 *
 * Ensures that only one assistant message TTS audio plays at a time across
 * the workspace. Starting a new playback stops any active one. Used by the
 * per-message speaker button and (indirectly) by conversation mode.
 */

import { derived, get, writable } from 'svelte/store';
import { autoplayDenied, primeAudio } from '$lib/utils/audio-priming';
import { SpeechPlayback } from '$lib/utils/speech-playback';
import { applyMediaPlaybackRate, validatePlaybackRate } from '$lib/utils/playback-rate';
import type { SpeechStreamEvent } from '$lib/speech';
import type { TtsSynthesizeResponse } from '$lib/types/api';

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
const playbackRate = writable(1);
const state = derived([internal, playbackRate], ([playing, rate]) => ({ ...playing, playbackRate: rate }));

let activeAudio: HTMLAudioElement | null = null;
let activeKey: string | null = null;
let componentOwned = false;
let primedAudio: HTMLAudioElement | null = null;
let cancelPending: (() => void) | null = null;
let activeSeek: ((seconds: number) => void) | null = null;
let activeToggle: (() => Promise<void>) | null = null;
let primedSpeech: SpeechPlayback | null = null;
let activeSpeech: SpeechPlayback | null = null;

function detachActive(): void {
  activeSeek = null;
  const cancel = cancelPending;
  cancelPending = null;
  cancel?.();
  activeSpeech?.dispose();
  activeSpeech = null;
  primedSpeech?.dispose();
  primedSpeech = null;
  activeToggle = null;
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
    activeAudio.onloadedmetadata = null;
    if (!componentOwned) activeAudio.src = '';
  }
  activeAudio = null;
  activeKey = null;
  componentOwned = false;
}

export const audioPlayer = {
  subscribe: state.subscribe,

  /** Keep one browser-session speed across messages and deliverable players. */
  setPlaybackRate(rate: number): void {
    validatePlaybackRate(rate);
    activeSpeech?.setPlaybackRate(rate);
    primedSpeech?.setPlaybackRate(rate);
    if (activeAudio) applyMediaPlaybackRate(activeAudio, rate);
    if (primedAudio) applyMediaPlaybackRate(primedAudio, rate);
    playbackRate.set(rate);
  },

  /** Adopt a component-owned player while preserving single-playback ownership. */
  attach(key: string, audio: HTMLAudioElement, options: { togglePause?: () => Promise<void> } = {}): void {
    if (activeAudio !== audio || activeKey !== key) {
      detachActive();
      activeAudio = audio;
      activeKey = key;
      componentOwned = true;
      audio.onplaying = () => {
        if (activeAudio === audio) internal.set({ currentKey: key, isPlaying: true, isLoading: false });
      };
      audio.onpause = () => {
        if (activeAudio === audio) internal.set({ currentKey: key, isPlaying: false, isLoading: false });
      };
    }
    activeToggle = options.togglePause ?? null;
    applyMediaPlaybackRate(audio, get(playbackRate));
    internal.set({ currentKey: key, isPlaying: true, isLoading: false });
  },

  /** Own a cancellable synthesis request before there is an audio element. */
  prepare(key: string, cancel: () => void): void {
    detachActive();
    activeKey = key;
    cancelPending = cancel;
    internal.set({ currentKey: key, isPlaying: false, isLoading: true });
  },

  /**
   * Begin playing ``url`` under ``key``. Stops any previous playback first.
   * Returns the audio element so callers can attach extra listeners
   * (e.g. for queue chaining in conversation mode).
   */
  async play(key: string, url: string | string[]): Promise<HTMLAudioElement> {
    const urls = typeof url === 'string' ? [url] : url;
    if (!urls.length) throw new Error('No audio to play');
    // Completion of our own synthesis is not cancellation of that request.
    if (activeKey === key) cancelPending = null;
    detachActive();
    const audio = primedAudio ?? new Audio();
    primedAudio = null;
    let part = 0;
    let offset = 0;
    let resumeAfterLoad = false;
    audio.src = urls[part];
    audio.preload = 'auto';
    applyMediaPlaybackRate(audio, get(playbackRate));
    activeAudio = audio;
    activeKey = key;

    internal.set({ currentKey: key, isPlaying: false, isLoading: true });
    const loadPart = (index: number, target: number, resume: boolean) => {
      part = index;
      offset = target;
      resumeAfterLoad = resume;
      audio.src = urls[part];
      // Consecutive cached parts may have the same URL; force a new media load.
      audio.load();
    };
    audio.onloadedmetadata = () => {
      if (activeAudio !== audio || !Number.isFinite(audio.duration)) return;
      const target = offset < 0 ? audio.duration + offset : offset;
      if (target < 0 && part > 0) { loadPart(part - 1, target, resumeAfterLoad); return; }
      if (target > audio.duration && part + 1 < urls.length) {
        loadPart(part + 1, target - audio.duration, resumeAfterLoad); return;
      }
      audio.currentTime = Math.max(0, Math.min(audio.duration, target));
      offset = 0;
      if (resumeAfterLoad) {
        resumeAfterLoad = false;
        void audio.play().catch(() => {
          if (activeAudio === audio) { detachActive(); internal.set(initialState); }
        });
      }
    };
    activeSeek = (seconds) => {
      if (!Number.isFinite(audio.duration)) return;
      const target = audio.currentTime + seconds;
      if (target < 0 && part > 0) loadPart(part - 1, target, !audio.paused);
      else if (target > audio.duration && part + 1 < urls.length) {
        loadPart(part + 1, target - audio.duration, !audio.paused);
      } else audio.currentTime = Math.max(0, Math.min(audio.duration, target));
    };

    audio.onplaying = () => {
      if (activeAudio === audio) {
        internal.set({ currentKey: key, isPlaying: true, isLoading: false });
      }
    };
    audio.onpause = () => {
      if (activeAudio === audio) {
        internal.update((s) => ({ ...s, isPlaying: false }));
      }
    };
    audio.onended = () => {
      if (activeAudio === audio) {
        if (part + 1 < urls.length) {
          loadPart(part + 1, 0, true);
          return;
        }
        detachActive();
        internal.set(initialState);
      }
    };
    audio.onerror = () => {
      if (activeAudio === audio) {
        detachActive();
        internal.set(initialState);
      }
    };

    try {
      await audio.play();
    } catch (err) {
      // Browser blocked auto-play or user navigated away.
      if (activeAudio === audio) {
        if (autoplayDenied(err)) {
          internal.set({ currentKey: key, isPlaying: false, isLoading: false });
        } else {
          detachActive();
          internal.set(initialState);
        }
      }
      throw err;
    }
    return audio;
  },

  async playSpeech(
    key: string, events: (signal: AbortSignal) => AsyncIterable<SpeechStreamEvent>,
    signal: AbortSignal,
  ): Promise<TtsSynthesizeResponse> {
    if (signal.aborted || activeKey !== key || !primedSpeech) throw new DOMException('Aborted', 'AbortError');
    const player = primedSpeech;
    player.setPlaybackRate(get(playbackRate));
    primedSpeech = null;
    activeSpeech = player;
    activeSeek = (seconds) => player.seek(seconds);
    try {
      return await player.play(events, signal);
    } finally {
      if (activeSpeech === player) {
        cancelPending = null;
        detachActive();
        internal.set(initialState);
      }
    }
  },

  /**
   * Prime an audio element from a user gesture so a later synthesis request
   * can start playback after its network round-trip on browsers with strict
   * autoplay policies.
   */
  async unlock(signal?: AbortSignal): Promise<boolean> {
    const key = activeKey;
    let speechUnlock: Promise<void> = Promise.resolve();
    if (SpeechPlayback.supported()) {
      primedSpeech?.dispose();
      primedSpeech = new SpeechPlayback((playing) => {
        if (activeKey === key && activeSpeech) {
          internal.set({ currentKey: key, isPlaying: playing, isLoading: false });
        }
      });
      speechUnlock = primedSpeech.unlock(signal);
    }
    if (typeof Audio === 'undefined') return false;
    if (primedAudio) { await speechUnlock; return true; }

    const audio = new Audio();
    audio.preload = 'auto';
    const unlocked = primeAudio(audio, signal);
    await speechUnlock;
    if (await unlocked) {
      primedAudio = audio;
      return true;
    }
    return false;
  },

  /** Stop and reset the current playback (no-op if nothing is playing). */
  stop(): void {
    detachActive();
    internal.set(initialState);
  },

  async togglePause(): Promise<void> {
    if (activeSpeech) { await activeSpeech.togglePause(); return; }
    if (activeToggle) { await activeToggle(); return; }
    const audio = activeAudio;
    if (!audio) return;
    if (!audio.paused) {
      audio.pause();
      internal.set({ currentKey: activeKey, isPlaying: false, isLoading: false });
    } else {
      await audio.play();
    }
  },

  seek(seconds: number): void {
    activeSeek?.(seconds);
  },

  buffering(key: string, audio: HTMLAudioElement, waiting: boolean): void {
    if (activeKey === key && activeAudio === audio) {
      internal.set({ currentKey: key, isLoading: waiting, isPlaying: !audio.paused });
    }
  },

  /**
   * Component-owned players also pass their element so a second viewer of the
   * same recording cannot release another viewer's current playback.
   */
  stopIfKey(key: string, owner?: HTMLAudioElement): void {
    if (activeKey === key && (!owner || activeAudio === owner)) {
      this.stop();
    }
  },

  isCurrent(key: string, owner?: HTMLAudioElement): boolean {
    return activeKey === key && (!owner || activeAudio === owner);
  }
};
