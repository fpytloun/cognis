export const PLAYBACK_RATES: readonly number[] = [0.75, 1, 1.25, 1.5, 1.75, 2];

export function validatePlaybackRate(rate: number): void {
  if (!PLAYBACK_RATES.includes(rate)) throw new RangeError('Unsupported playback speed');
}

export function nextPlaybackRate(rate: number): number {
  return PLAYBACK_RATES[(PLAYBACK_RATES.indexOf(rate) + 1) % PLAYBACK_RATES.length];
}

export function applyMediaPlaybackRate(audio: HTMLMediaElement, rate: number): void {
  audio.defaultPlaybackRate = audio.playbackRate = rate;
  audio.preservesPitch = true;
}
