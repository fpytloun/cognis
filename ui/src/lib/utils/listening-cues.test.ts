import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ListeningCues } from './listening-cues';

class Note {
  type = '';
  frequency = { setValueAtTime: vi.fn() };
  onended: (() => void) | null = null;
  connect = vi.fn(); disconnect = vi.fn(); start = vi.fn(); stop = vi.fn();
}
class Context {
  static instances: Context[] = [];
  state = 'running'; currentTime = 0; destination = {};
  baseLatency = 0.01; outputLatency = 0.02;
  notes: Note[] = [];
  gains: { gain: { setValueAtTime: ReturnType<typeof vi.fn>; exponentialRampToValueAtTime: ReturnType<typeof vi.fn> }; connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> }[] = [];
  constructor() { Context.instances.push(this); }
  resume = vi.fn(async () => { this.state = 'running'; });
  close = vi.fn(async () => { this.state = 'closed'; });
  createOscillator() { const note = new Note(); this.notes.push(note); return note; }
  createGain() {
    const gain = { gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
      connect: vi.fn(), disconnect: vi.fn() };
    this.gains.push(gain); return gain;
  }
}
beforeEach(() => {
  vi.useFakeTimers(); Context.instances = [];
  vi.stubGlobal('AudioContext', Context);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it('uses the same quiet envelope with opposite note order, waiting for output to settle', async () => {
  const cues = new ListeningCues();
  for (const [kind, frequencies] of [['start', [520, 660]], ['stop', [660, 520]]] as const) {
    const done = vi.fn();
    const playing = cues.play(kind).then(done);
    const context = Context.instances[0];
    const notes = context.notes.slice(-2);
    expect(notes.map(note => note.frequency.setValueAtTime.mock.calls[0][0])).toEqual(frequencies);
    expect(notes.every(note => note.type === 'sine')).toBe(true);
    for (const gain of context.gains.slice(-2)) {
      expect(gain.gain.exponentialRampToValueAtTime.mock.calls.map(call => call[0])).toEqual([0.12, 0.0001]);
    }
    notes[1].onended?.();
    await vi.advanceTimersByTimeAsync(54);
    expect(done).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await playing;
    expect(notes.every(note => note.disconnect.mock.calls.length === 1)).toBe(true);
  }
  expect(Context.instances).toHaveLength(1);
  cues.dispose();
  expect(Context.instances[0].close).toHaveBeenCalledOnce();
});

it('finishes the distinct waiting tone without leaving audio nodes or timers behind', async () => {
  const cues = new ListeningCues();
  const playing = cues.play('waiting');
  const context = Context.instances[0];
  expect(context.notes.map(note => note.frequency.setValueAtTime.mock.calls[0][0])).toEqual([440]);
  context.notes[0].onended?.();
  await vi.advanceTimersByTimeAsync(55);
  await playing;
  expect(context.notes[0].disconnect).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
  cues.dispose();
});

it('bounds blocked autoplay and never plays a stale cue after permission arrives', async () => {
  const cues = new ListeningCues();
  cues.unlock();
  const context = Context.instances[0];
  context.state = 'suspended';
  let resume!: () => void;
  context.resume.mockImplementation(() => new Promise<void>(resolve => { resume = resolve; }));
  const playing = cues.play('start');
  await vi.advanceTimersByTimeAsync(400);
  await playing;
  context.state = 'running';
  resume();
  await Promise.resolve();
  expect(context.notes).toHaveLength(0);
  cues.dispose();
});

it('cancels pending notes and resolves the capture gate when closed', async () => {
  const cues = new ListeningCues();
  const playing = cues.play('start');
  const context = Context.instances[0];
  cues.dispose();
  await playing;
  expect(context.notes.every(note => note.disconnect.mock.calls.length === 1)).toBe(true);
  expect(context.close).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});
