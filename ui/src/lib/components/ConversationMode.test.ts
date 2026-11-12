import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Writable } from 'svelte/store';
import type { Agent } from '$lib/types/api';
import ConversationMode from './ConversationMode.svelte';
import type { RuntimeOverlaySnapshot } from '$lib/chat-v2/types';
import { speechRuntime } from '$lib/utils/conversation-speech.test-fixtures';
import type { SettledVoiceTurn } from '$lib/utils/conversation-speech';

const mocks = vi.hoisted(() => ({
  capabilities: vi.fn(), synthesize: vi.fn(), transcribe: vi.fn(), toast: vi.fn(),
  documentContext: vi.fn(), documentCreate: vi.fn(), documentGet: vi.fn(),
  queued: [] as { url: string }[],
  idle: null as (() => void) | null,
  disposed: vi.fn(),
  cuePlay: vi.fn(), cueUnlock: vi.fn(), cueStop: vi.fn(), cueDispose: vi.fn(),
  liveOpen: vi.fn(), liveCommit: vi.fn(), liveCancel: vi.fn(),
  calibration: vi.fn(),
}));
vi.mock('$lib/utils/voice-activity', async (importOriginal) => ({
  ...await importOriginal<typeof import('$lib/utils/voice-activity')>(),
  calibrateNoise: mocks.calibration
}));
vi.mock('$lib/utils/live-transcription', () => ({
  LiveTranscription: class {
    static supported() { return true; }
    static open = mocks.liveOpen;
  }
}));
vi.mock('$lib/utils/listening-cues', () => ({
  ListeningCues: class {
    play = mocks.cuePlay;
    unlock = mocks.cueUnlock;
    stop = mocks.cueStop;
    dispose = mocks.cueDispose;
  }
}));
vi.mock('$lib/api/client', () => ({
  api: {
    tts: { capabilities: mocks.capabilities, synthesize: mocks.synthesize },
    stt: { transcribe: mocks.transcribe },
    audioJobs: { context: mocks.documentContext, create: mocks.documentCreate, get: mocks.documentGet },
  }
}));
vi.mock('$lib/stores/toasts', () => ({ addToast: mocks.toast }));
vi.mock('$lib/stores/audio-player', () => ({
  audioPlayer: { stop: vi.fn(), unlock: vi.fn().mockResolvedValue(undefined) }
}));
vi.mock('$lib/utils/audio-queue', () => ({
  AudioQueue: class {
    play = () => {};
    enqueue(value: { url: string }) { mocks.queued.push(value); this.play(); }
    clear() { mocks.queued.length = 0; }
    dispose() { this.clear(); mocks.disposed(); }
    async unlock() { return true; }
    getIsPlaying() { return false; }
    isEmpty() { return mocks.queued.length === 0; }
    onPlayingChange(callback: (playing: boolean) => void) { this.play = () => callback(true); return () => {}; }
    onIdle(callback: () => void) { mocks.idle = callback; return () => { mocks.idle = null; }; }
    onPlaybackError() { return () => {}; }
  }
}));
vi.mock('$lib/stores/userPreferences', async () => {
  const { writable } = await import('svelte/store');
  return { userPreferences: writable({ chat: { adapt_tts: false } }) };
});
import { userPreferences } from '$lib/stores/userPreferences';
const preferences = userPreferences as unknown as Writable<{ chat: { adapt_tts: boolean } }>;

class Recorder {
  static instances: Recorder[] = [];
  static isTypeSupported() { return true; }
  state = 'inactive';
  mimeType = 'audio/webm';
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  constructor() { Recorder.instances.push(this); }
  start() { this.state = 'recording'; }
  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({ data: new Blob(['recording'], { type: this.mimeType }) });
    this.onstop?.();
  }
}

function mount(agent: Agent | null = null) {
  const runtime = (frame: RuntimeOverlaySnapshot) => component.rerender({ runtime: frame });
  let revision = 0;
  const sentences = new Map<number, string>();
  const submitText = vi.fn().mockResolvedValue('voice-input');
  const reconcileTurn = vi.fn<(clientMessageId: string) => Promise<SettledVoiceTurn | null>>(async () => ({
    turn_id: 'voice-turn',
    items: speechRuntime([...sentences.entries()].sort((a, b) => a[0] - b[0])
      .map(([, text]) => text).join(' '), { has_active_turn: false }).volatile_items,
  }));
  const stopAssistant = vi.fn().mockResolvedValue(true);
  const onclose = vi.fn();
  const props = {
    open: true, agent, conversationId: 'voice-conversation', onclose,
    submitText, stopAssistant, reconcileTurn,
    runtime: null,
  };
  const component = render(ConversationMode, props);
  return { component, submitText, stopAssistant, reconcileTurn, onclose,
    speak: async () => {
      await fireEvent.click(screen.getByRole('button', { name: 'Finish speaking' }));
      await waitFor(() => expect(submitText).toHaveBeenCalled());
    },
    runtime: (value: RuntimeOverlaySnapshot) => runtime(value),
    sentence: (index: number, text: string) => {
      sentences.set(index, text);
      // Runtime snapshots are cumulative; missing predecessors arrive together.
      let content = '';
      for (let i = 0; sentences.has(i); i++) content += sentences.get(i) + ' ';
      return runtime(speechRuntime(content, { runtime_revision: ++revision }));
    },
    complete: () => runtime(speechRuntime(
      [...sentences.entries()].sort((a, b) => a[0] - b[0]).map(([, text]) => text).join(' '),
      { has_active_turn: false, runtime_revision: ++revision }
    ))
  };
}

it('checks the selected agent speech configuration before opening the microphone', async () => {
  mount({ agent_id: 'voice-agent', name: 'Voice agent' } as Agent);
  await waitFor(() => expect(mocks.capabilities).toHaveBeenCalledWith('voice-agent'));
});

it('disposes each playback queue when conversation mode is unmounted', async () => {
  for (let attempt = 1; attempt <= 2; attempt++) {
    const { component } = mount();
    await screen.findByRole('button', { name: 'Finish speaking' });
    component.unmount();
    expect(mocks.disposed).toHaveBeenCalledTimes(attempt);
  }
});

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
beforeEach(() => {
  vi.clearAllMocks();
  mocks.documentContext.mockReset().mockResolvedValue({
    supported: true, can_generate: true, jobs: [],
  });
  mocks.documentCreate.mockReset().mockResolvedValue({
    job_id: 'document-recording', status: 'completed', parts: [{
      artifact_id: 'document-audio', audio_url: '/document.mp3', content_type: 'audio/mpeg',
    }],
  });
  mocks.queued.length = 0;
  mocks.cuePlay.mockReset().mockResolvedValue(undefined);
  mocks.calibration.mockReset().mockResolvedValue(0.002);
  mocks.liveCommit.mockReset().mockResolvedValue({ text: 'Explain the material.' });
  mocks.liveOpen.mockReset().mockResolvedValue({ commit: mocks.liveCommit, cancel: mocks.liveCancel });
  Recorder.instances.length = 0;
  mocks.capabilities.mockResolvedValue({ tts: true, stt: true, detail: null });
  mocks.synthesize.mockResolvedValue({
    audio_url: '/speech.mp3', parts: [{ audio_url: '/speech.mp3' }]
  });
  mocks.transcribe.mockResolvedValue({ text: 'Explain the material.' });
  preferences.set({ chat: { adapt_tts: false } });
  vi.stubGlobal('MediaRecorder', Recorder);
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn().mockImplementation(async () => {
      const track = { stop: vi.fn(), enabled: true, readyState: 'live' };
      return { getTracks: () => [track], getAudioTracks: () => [track] };
    }) }
  });
});

it('keeps the microphone off until a deliverable-only response has been read', async () => {
  const voice = mount();
  await screen.findByRole('button', { name: 'Finish speaking' });
  voice.reconcileTurn.mockResolvedValue({ turn_id: 'voice-turn', items: [{
    id: 'document-reference', kind: 'assistant_deliverable', deliverable_id: 'document-revision',
    turn_id: 'voice-turn', format: 'rich', sort_key: '003', stable: true, source_refs: [],
  }] });
  await voice.speak();
  await voice.complete();
  await waitFor(() => expect(mocks.queued.map(part => part.url)).toEqual(['/document.mp3']));
  expect(Recorder.instances).toHaveLength(1);
  expect(mocks.documentCreate.mock.calls[0][0]).toMatchObject({
    content_ref: 'document-revision', mode: 'narration',
  });
  mocks.queued.length = 0;
  mocks.idle?.();
  await waitFor(() => expect(Recorder.instances).toHaveLength(2));
});

it('discards late document audio when narration is interrupted', async () => {
  let resolve!: (value: unknown) => void;
  mocks.documentContext.mockReturnValueOnce(new Promise(done => { resolve = done; }));
  const voice = mount();
  await screen.findByRole('button', { name: 'Finish speaking' });
  voice.reconcileTurn.mockResolvedValue({ turn_id: 'voice-turn', items: [{
    id: 'document-reference', kind: 'assistant_deliverable', deliverable_id: 'document-revision',
    turn_id: 'voice-turn', format: 'rich', sort_key: '003', stable: true, source_refs: [],
  }] });
  await voice.speak();
  await voice.complete();
  await screen.findByText('Preparing deliverable audio…');
  await fireEvent.click(screen.getByRole('button', { name: 'Interrupt assistant and listen' }));
  resolve({ supported: true, can_generate: true, jobs: [] });
  await waitFor(() => expect(Recorder.instances).toHaveLength(2));
  expect(mocks.queued).toEqual([]);
  expect(mocks.documentCreate).not.toHaveBeenCalled();
});

describe('conversation speech integration', () => {
  function enableMeter() {
    vi.stubGlobal('AudioContext', class {
      state = 'running';
      resume = async () => {};
      close = async () => {};
      createAnalyser() { return { fftSize: 1024, getFloatTimeDomainData: (buffer: Float32Array) => buffer.fill(0.002) }; }
      createMediaStreamSource() { return { connect: () => {} }; }
    });
  }

  it.each([false, true])('calibrates before the ready cue without recording or live transcription (live: %s)', async (live) => {
    enableMeter();
    mocks.capabilities.mockResolvedValue({ tts: true, stt: true, streaming_stt: live, stt_sample_rate: 24000 });
    let calibrated!: (floor: number) => void;
    let ready!: () => void;
    mocks.calibration.mockImplementationOnce(() => new Promise<number>(resolve => { calibrated = resolve; }));
    mocks.cuePlay.mockImplementationOnce(() => new Promise<void>(resolve => { ready = resolve; }));
    mount();
    await waitFor(() => expect(mocks.calibration).toHaveBeenCalledOnce());
    const stream = await vi.mocked(navigator.mediaDevices.getUserMedia).mock.results[0].value;
    expect(stream.getAudioTracks()[0].enabled).toBe(true);
    expect(Recorder.instances[0].state).toBe('inactive');
    expect(mocks.liveOpen).not.toHaveBeenCalled();
    expect(mocks.cuePlay).not.toHaveBeenCalled();
    expect(screen.getByText('Getting microphone ready…')).toBeVisible();
    calibrated(0.002);
    await waitFor(() => expect(mocks.cuePlay).toHaveBeenCalledWith('start'));
    expect(stream.getAudioTracks()[0].enabled).toBe(false);
    expect(Recorder.instances[0].state).toBe('inactive');
    ready();
    await waitFor(() => expect(Recorder.instances[0].state).toBe('recording'));
    expect(stream.getAudioTracks()[0].enabled).toBe(true);
    expect(mocks.liveOpen).toHaveBeenCalledTimes(live ? 1 : 0);
  });

  it('aborts pre-cue calibration when closed, without starting capture', async () => {
    enableMeter();
    mocks.calibration.mockImplementationOnce((_read, signal: AbortSignal) =>
      new Promise(resolve => signal.addEventListener('abort', () => resolve(null), { once: true })));
    const view = mount();
    await waitFor(() => expect(mocks.calibration).toHaveBeenCalledOnce());
    view.component.unmount();
    await Promise.resolve();
    expect(mocks.calibration.mock.calls[0][1].aborted).toBe(true);
    expect(Recorder.instances[0].state).toBe('inactive');
    expect(mocks.liveOpen).not.toHaveBeenCalled();
    expect(mocks.cuePlay).not.toHaveBeenCalled();
    expect(mocks.transcribe).not.toHaveBeenCalled();
  });

  it.each(['resolve', 'reject'])('isolates stale native startup %s after mute/unmute', async (outcome) => {
    mocks.capabilities.mockResolvedValue({ tts: true, stt: true, streaming_stt: true, stt_sample_rate: 24000 });
    let resolve!: (capture: object) => void;
    let reject!: (error: Error) => void;
    mocks.liveOpen.mockImplementationOnce(() => new Promise((yes, no) => { resolve = yes; reject = no; }));
    const view = mount();
    await waitFor(() => expect(mocks.liveOpen).toHaveBeenCalledOnce());
    await fireEvent.click(screen.getByRole('button', { name: /^Mute$/ }));
    await fireEvent.click(screen.getByRole('button', { name: /^Unmute$/ }));
    await waitFor(() => expect(Recorder.instances[1]?.state).toBe('recording'));
    const oldCancel = vi.fn();
    if (outcome === 'resolve') resolve({ commit: vi.fn(), cancel: oldCancel });
    else reject(new Error('Old connection closed'));
    await Promise.resolve();
    await Promise.resolve();
    expect(oldCancel).toHaveBeenCalledTimes(outcome === 'resolve' ? 1 : 0);
    expect(mocks.liveCancel).not.toHaveBeenCalled();
    expect(Recorder.instances[1].state).toBe('recording');
    await view.speak();
    expect(mocks.liveCommit).toHaveBeenCalledOnce();
  });

  it.each([false, true])('gates microphone input around matched listening cues (live STT: %s)', async (live) => {
    mocks.capabilities.mockResolvedValue({ tts: true, stt: true, streaming_stt: live, stt_sample_rate: 24000 });
    let ready!: () => void;
    mocks.cuePlay.mockImplementationOnce(() => new Promise<void>(resolve => { ready = resolve; }));
    const view = mount();
    await waitFor(() => expect(mocks.cuePlay).toHaveBeenCalledWith('start'));
    const stream = await vi.mocked(navigator.mediaDevices.getUserMedia).mock.results[0].value;
    const track = stream.getAudioTracks()[0];
    expect(track.enabled).toBe(false);
    expect(Recorder.instances[0].state).toBe('inactive');
    expect(screen.getByRole('button', { name: 'Finish speaking' })).toBeDisabled();
    expect(screen.getByText('Getting microphone ready…')).toBeVisible();
    ready();
    await waitFor(() => expect(Recorder.instances[0].state).toBe('recording'));
    expect(track.enabled).toBe(true);
    mocks.cuePlay.mockImplementation(async (kind) => {
      if (kind === 'stop') expect(track.enabled).toBe(false);
    });
    await view.speak();
    expect(track.enabled).toBe(false);
    expect(mocks.cuePlay).toHaveBeenLastCalledWith('stop');
    expect(mocks.liveCommit).toHaveBeenCalledTimes(live ? 1 : 0);
    expect(mocks.transcribe).toHaveBeenCalledTimes(live ? 0 : 1);
  });

  it('does not open capture after an overlay is closed during the start cue', async () => {
    let ready!: () => void;
    mocks.cuePlay.mockImplementationOnce(() => new Promise<void>(resolve => { ready = resolve; }));
    const view = mount();
    await waitFor(() => expect(mocks.cuePlay).toHaveBeenCalledWith('start'));
    const stream = await vi.mocked(navigator.mediaDevices.getUserMedia).mock.results[0].value;
    view.component.unmount();
    ready();
    await Promise.resolve();
    expect(Recorder.instances[0].state).toBe('inactive');
    expect(stream.getAudioTracks()[0].enabled).toBe(false);
    expect(stream.getAudioTracks()[0].stop).toHaveBeenCalled();
    expect(mocks.cueDispose).toHaveBeenCalled();
    expect(mocks.transcribe).not.toHaveBeenCalled();
  });

  it.each([false, true])('does not revive stale capture after mute/unmute during a cue (live STT: %s)', async (live) => {
    mocks.capabilities.mockResolvedValue({ tts: true, stt: true, streaming_stt: live, stt_sample_rate: 24000 });
    let ready!: () => void;
    mocks.cuePlay.mockImplementationOnce(() => new Promise<void>(resolve => { ready = resolve; }));
    mount();
    await waitFor(() => expect(mocks.cuePlay).toHaveBeenCalledWith('start'));
    const first = await vi.mocked(navigator.mediaDevices.getUserMedia).mock.results[0].value;
    await fireEvent.click(screen.getByRole('button', { name: /^Mute$/ }));
    expect(first.getAudioTracks()[0].enabled).toBe(false);
    expect(first.getAudioTracks()[0].stop).toHaveBeenCalled();
    expect(mocks.liveCancel).toHaveBeenCalledTimes(live ? 1 : 0);
    await fireEvent.click(screen.getByRole('button', { name: /^Unmute$/ }));
    await waitFor(() => expect(Recorder.instances[1]?.state).toBe('recording'));
    ready();
    await Promise.resolve();
    const second = await vi.mocked(navigator.mediaDevices.getUserMedia).mock.results[1].value;
    expect(Recorder.instances[0].state).toBe('inactive');
    expect(Recorder.instances[1].state).toBe('recording');
    expect(second.getAudioTracks()[0].enabled).toBe(true);
    expect(second.getAudioTracks()[0].stop).not.toHaveBeenCalled();
    expect(mocks.transcribe).not.toHaveBeenCalled();
    expect(mocks.liveCommit).not.toHaveBeenCalled();
  });

  it.each([
    Object.assign(new Error('Interrupted; may have been billed.'), { code: 'speech_generation_interrupted', status: 409 }),
    new DOMException('Aborted', 'AbortError')
  ])('does not automatically repeat uncertain or aborted synthesis (%s)', async (failure) => {
    mocks.synthesize.mockRejectedValue(failure);
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await view.speak();
    await view.sentence(0, 'A paid attempt.');
    await waitFor(() => expect(mocks.toast).toHaveBeenCalled());
    expect(mocks.synthesize).toHaveBeenCalledOnce();
  });

  it('does not repeat a paid attempt when the sentence timeout aborts its request', async () => {
    mocks.synthesize.mockImplementation((_payload, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    }));
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await view.speak();
    vi.useFakeTimers();
    await view.sentence(0, 'A slow paid attempt.');
    await vi.advanceTimersByTimeAsync(15_001);
    expect(mocks.synthesize).toHaveBeenCalledOnce();
    expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('skipped'), 'warning', 3_000);
  });

  it.each([false, true])('shows and deduplicates fallback warnings with adaptation %s', async (adapt) => {
    preferences.set({ chat: { adapt_tts: adapt } });
    const warning = 'The configured voice is unavailable. Using alloy instead.';
    mocks.synthesize.mockResolvedValue({
      audio_url: '/speech.mp3', parts: [{ audio_url: '/speech.mp3' }],
      warnings: [warning, warning]
    });
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await view.speak();
    await view.sentence(0, 'First sentence.');
    await view.sentence(1, 'Second sentence.');
    if (adapt) await view.complete();
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith(warning, 'warning', 6_000));
    expect(mocks.toast.mock.calls.filter(call => call[0] === warning)).toHaveLength(1);
  });
  it('checks configuration before asking for microphone permission and closes with Escape', async () => {
    mocks.capabilities.mockResolvedValue({ tts: false, stt: false, detail: 'Configure speech routes.' });
    const view = mount();
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Configure speech'));
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(view.onclose).toHaveBeenCalledOnce();
  });

  it('synthesizes original sentences immediately by default', async () => {
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await view.speak();
    await view.sentence(0, '**Keep this text.**');
    await waitFor(() => expect(mocks.synthesize).toHaveBeenCalled());
    expect(mocks.synthesize.mock.calls[0][0]).toMatchObject({
      text: 'Keep this text.', low_latency: true
    });
    expect(mocks.synthesize.mock.calls[0][0].adaptation).toBeUndefined();
  });

  it('adapts a complete response once, preserving sentence order and duplicate suppression', async () => {
    preferences.set({ chat: { adapt_tts: true } });
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await view.speak();
    await view.sentence(1, 'Second sentence.');
    await view.sentence(0, 'First sentence.');
    await view.sentence(0, 'First sentence.');
    expect(mocks.synthesize).not.toHaveBeenCalled();
    await view.complete();
    await waitFor(() => expect(mocks.synthesize).toHaveBeenCalledOnce());
    expect(mocks.synthesize.mock.calls[0][0]).toMatchObject({
      text: 'First sentence. Second sentence.', adaptation: 'listenable'
    });
  });

  it('recovers listening immediately after a rejected text submission', async () => {
    const view = mount();
    view.submitText.mockRejectedValueOnce(new Error('Send failed'));
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await fireEvent.click(screen.getByRole('button', { name: 'Finish speaking' }));
    await waitFor(() => expect(view.submitText).toHaveBeenCalled());
    await waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2));
  });

  it('cancels the backend turn before resuming after interruption', async () => {
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await fireEvent.click(screen.getByRole('button', { name: 'Finish speaking' }));
    await waitFor(() => expect(view.submitText).toHaveBeenCalled());
    await view.sentence(0, 'An answer in progress.');
    await waitFor(() => expect(mocks.queued.length).toBe(1));
    await fireEvent.click(screen.getByRole('button', { name: 'Interrupt assistant and listen' }));
    await waitFor(() => expect(view.stopAssistant).toHaveBeenCalledOnce());
    await waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2));
  });
  it('shows waiting after transcription and resumes only after turn, synthesis, and playback drain', async () => {
    let finishSynthesis!: (result: unknown) => void;
    mocks.synthesize.mockImplementation(() => new Promise(resolve => { finishSynthesis = resolve; }));
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await view.speak();
    expect(screen.getByText('Waiting for the assistant…')).toBeTruthy();
    await view.sentence(0, 'The spoken response.');
    await waitFor(() => expect(mocks.synthesize).toHaveBeenCalledOnce());
    await view.complete();
    expect(Recorder.instances).toHaveLength(1);
    finishSynthesis({ audio_url: 'https://audio.test/final.mp3' });
    await waitFor(() => expect(mocks.queued).toHaveLength(1));
    expect(Recorder.instances).toHaveLength(1);
    mocks.queued.length = 0;
    mocks.idle?.();
    await waitFor(() => expect(Recorder.instances).toHaveLength(2));
  });

  it('does not submit transcribed audio after closing the call', async () => {
    let finishStt!: (result: { text: string }) => void;
    mocks.transcribe.mockImplementation(() => new Promise(resolve => { finishStt = resolve; }));
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await fireEvent.click(screen.getByRole('button', { name: 'Finish speaking' }));
    await waitFor(() => expect(mocks.transcribe).toHaveBeenCalledOnce());
    const signal = mocks.transcribe.mock.calls[0][1].signal as AbortSignal;
    await fireEvent.click(screen.getAllByRole('button', { name: 'End conversation' })[0]);
    expect(signal.aborted).toBe(true);
    finishStt({ text: 'This must not be sent.' });
    await Promise.resolve();
    expect(view.submitText).not.toHaveBeenCalled();
  });

  it('allows interrupting a silent waiting turn and fences late response audio', async () => {
    const view = mount();
    await waitFor(() => expect(Recorder.instances.length).toBe(1));
    await view.speak();
    await fireEvent.click(screen.getByRole('button', { name: 'Interrupt assistant and listen' }));
    await waitFor(() => expect(view.stopAssistant).toHaveBeenCalledOnce());
    await view.sentence(0, 'Too late to play.');
    expect(mocks.synthesize).not.toHaveBeenCalled();
    await waitFor(() => expect(Recorder.instances).toHaveLength(2));
  });

  it('drains a terminal observation arriving during the first admission lookup', async () => {
    let resolveFirst!: (value: SettledVoiceTurn | null) => void;
    const view = mount();
    view.reconcileTurn.mockReturnValueOnce(new Promise(resolve => { resolveFirst = resolve; }));
    await waitFor(() => expect(Recorder.instances).toHaveLength(1));
    await view.speak();
    await view.runtime(speechRuntime('', { runtime_revision: 1, has_active_turn: false }));
    await waitFor(() => expect(view.reconcileTurn).toHaveBeenCalledOnce());
    await view.runtime(speechRuntime('', { runtime_revision: 2 }));
    await view.runtime(speechRuntime('Recovered final response.', {
      runtime_revision: 3, has_active_turn: false,
    }));
    await waitFor(() => expect(mocks.queued).toHaveLength(1));
    expect(Recorder.instances).toHaveLength(1);
    resolveFirst(null);
    await waitFor(() => expect(view.reconcileTurn).toHaveBeenCalledTimes(2));
    expect(Recorder.instances).toHaveLength(1);
    mocks.queued.length = 0;
    mocks.idle?.();
    await waitFor(() => expect(Recorder.instances).toHaveLength(2));
  });

  it('keeps the microphone closed when accepted-turn recovery fails', async () => {
    const view = mount();
    view.reconcileTurn.mockRejectedValueOnce(new Error('Admission lookup unavailable'));
    await waitFor(() => expect(Recorder.instances).toHaveLength(1));
    await view.speak();
    await view.complete();
    await waitFor(() => expect(screen.getByText(/Waiting to recover/)).toBeTruthy());
    expect(Recorder.instances).toHaveLength(1);
    expect(view.submitText).toHaveBeenCalledOnce();
  });
});
