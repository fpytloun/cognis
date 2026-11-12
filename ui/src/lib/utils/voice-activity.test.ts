import { afterEach, describe, expect, it, vi } from 'vitest';
import { calibrateNoise, VoiceActivity } from './voice-activity';

afterEach(() => vi.useRealTimers());

describe('pre-listening noise calibration', () => {
  it('measures room noise separately from the opening words', async () => {
    vi.useFakeTimers();
    const read = vi.fn(() => 0.02);
    const floor = calibrateNoise(read, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(700);
    expect(await floor).toBeCloseTo(0.02);
    const detector = new VoiceActivity((await floor)!);
    detector.sample(0.06, 100);
    expect(detector.sample(0.08, 200).speaking).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('cancels preparation without leaving a sampler behind', async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const read = vi.fn(() => 0.002);
    const floor = calibrateNoise(read, controller.signal);
    controller.abort();
    expect(await floor).toBeNull();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(read).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });
  it.each([() => Number.NaN, () => { throw new Error('Audio device unavailable'); }])(
    'leaves finish manual when microphone levels cannot be measured', async (read) => {
      vi.useFakeTimers();
      const floor = calibrateNoise(read, new AbortController().signal);
      await vi.advanceTimersByTimeAsync(700);
      expect(await floor).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
    }
  );
});

describe('conversation turn detection', () => {
  it('does not calibrate immediate opening words as room noise', () => {
    const vad = new VoiceActivity(0.002);
    expect(vad.sample(0.06, 100).speaking).toBe(false);
    expect(vad.sample(0.04, 200).speaking).toBe(true);
    for (let now = 300; now <= 1_000; now += 100) vad.sample(now % 200 ? 0.04 : 0.08, now);
    expect(vad.sample(0, 3_900).shouldFinish).toBe(false);
    expect(vad.sample(0, 4_000).shouldFinish).toBe(true);
  });
  it('keeps thinking pauses and resumed quiet speech in the same turn', () => {
    const vad = new VoiceActivity(0.002);
    vad.sample(0.04, 100);
    vad.sample(0.04, 200);
    expect(vad.sample(0, 2_900).shouldFinish).toBe(false);
    expect(vad.sample(0.012, 3_000).speaking).toBe(true);
    expect(vad.sample(0, 5_900).shouldFinish).toBe(false);
    expect(vad.sample(0, 6_000).shouldFinish).toBe(true);
  });
  it('never sends initial silence or a single noise spike', () => {
    const vad = new VoiceActivity(0.002);
    vad.sample(0.002, 100);
    vad.sample(0.08, 200);
    expect(vad.sample(0.002, 300).speaking).toBe(false);
    expect(vad.sample(0.002, 60_000).shouldFinish).toBe(false);
  });
  it('preserves the maximum utterance bound', () => {
    const vad = new VoiceActivity(0.01);
    vad.sample(0.06, 300);
    vad.sample(0.06, 400);
    expect(vad.sample(0.02, 45_299).shouldFinish).toBe(false);
    expect(vad.sample(0.02, 45_300).shouldFinish).toBe(true);
  });
  it.each([0.02, 0.06])('never submits sustained calibrated room noise at RMS %s', (rms) => {
    const vad = new VoiceActivity(rms);
    for (let now = 100; now <= 46_000; now += 100) {
      expect(vad.sample(rms, now)).toMatchObject({ speaking: false, shouldFinish: false });
    }
  });
  it('detects speech above a louder calibrated room and ignores that room during pauses', () => {
    const vad = new VoiceActivity(0.02);
    vad.sample(0.08, 800);
    expect(vad.sample(0.06, 900).speaking).toBe(true);
    expect(vad.sample(0.02, 3_800).shouldFinish).toBe(false);
    expect(vad.sample(0.02, 3_900).shouldFinish).toBe(true);
  });
});
