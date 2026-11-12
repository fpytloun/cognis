const ONSET_THRESHOLD = 0.018;
const SILENCE_MS = 3_000;
const MAX_SPEECH_MS = 45_000;

/** Measure the room before the ready cue, without starting recording or live transcription. */
export function calibrateNoise(readRms: () => number, signal: AbortSignal): Promise<number | null> {
  if (signal.aborted) return Promise.resolve(null);
  return new Promise(resolve => {
    const levels: number[] = [];
    const finish = (floor: number | null) => {
      clearInterval(interval);
      clearTimeout(deadline);
      signal.removeEventListener('abort', abort);
      resolve(floor);
    };
    const abort = () => finish(null);
    const sample = () => {
      try {
        const level = readRms();
        if (Number.isFinite(level) && level >= 0) levels.push(level);
      } catch {
        finish(null);
      }
    };
    const interval = setInterval(sample, 100);
    const deadline = setTimeout(() => {
      levels.sort((a, b) => a - b);
      finish(levels[Math.floor(levels.length * 0.2)] ?? null);
    }, 700);
    signal.addEventListener('abort', abort, { once: true });
    sample();
  });
}

/** Turn-owned microphone activity; silence is a grace period, not an immediate handoff. */
export class VoiceActivity {
  private onsetFrames = 0;
  private onsetAt = 0;
  private speechStartedAt: number | null = null;
  private lastVoiceAt = 0;

  constructor(private readonly noiseFloor: number) {
    if (!Number.isFinite(noiseFloor) || noiseFloor < 0) throw new RangeError('Invalid microphone noise floor');
  }

  sample(rms: number, now: number): {
    speaking: boolean; threshold: number; silenceMs: number; shouldFinish: boolean;
  } {
    this.observe(rms, now);
    const speaking = this.speechStartedAt !== null;
    const silenceMs = speaking ? Math.max(0, now - this.lastVoiceAt) : 0;
    const speechMs = this.speechStartedAt === null ? 0 : now - this.speechStartedAt;
    return { speaking, threshold: this.threshold(), silenceMs,
      shouldFinish: speaking && (silenceMs >= SILENCE_MS || speechMs >= MAX_SPEECH_MS) };
  }

  private threshold(): number {
    const onset = Math.max(ONSET_THRESHOLD, this.noiseFloor * 2.5);
    return this.speechStartedAt === null ? onset : onset * 0.55;
  }

  private observe(rms: number, now: number): void {
    if (Number.isFinite(rms) && rms > this.threshold()) {
      if (!this.onsetFrames) this.onsetAt = now;
      this.onsetFrames++;
      if (this.speechStartedAt === null && this.onsetFrames >= 2) this.speechStartedAt = this.onsetAt;
      this.lastVoiceAt = now;
    } else if (this.speechStartedAt === null) {
      this.onsetFrames = 0;
    }
  }
}
