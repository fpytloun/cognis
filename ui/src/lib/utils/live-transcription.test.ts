import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/svelte';
import { LiveTranscription } from './live-transcription';
import { testDocumentLease } from '$lib/testing/document-lease';

let authority: ReturnType<typeof testDocumentLease>;
vi.mock('$lib/document-coordination', async (importOriginal) => ({
  ...await importOriginal<typeof import('$lib/document-coordination')>(),
  documentCoordination: () => ({ capture: () => authority.lease }),
}));

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1; bufferedAmount = 0;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  constructor() { Socket.instances.push(this); }
  receive(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
}
class Worklet {
  static instances: Worklet[] = [];
  port = {
    onmessage: null as ((event: { data: unknown }) => void) | null,
    postMessage: vi.fn(() => { this.port.onmessage?.({ data: { type: 'flushed' } }); }),
  };
  connect = vi.fn(); disconnect = vi.fn();
  constructor() { Worklet.instances.push(this); }
}
class Context {
  state: AudioContextState = 'running';
  audioWorklet = { addModule: vi.fn(async () => {}) };
  destination = {};
  resume = vi.fn(async () => {});
  close = vi.fn(async () => { this.state = 'closed'; });
  createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() });
}
beforeEach(() => {
  authority = testDocumentLease();
  Socket.instances = []; Worklet.instances = [];
  vi.stubGlobal('WebSocket', Socket); vi.stubGlobal('AudioContext', Context);
  vi.stubGlobal('AudioWorkletNode', Worklet);
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function open(signal: AbortSignal, failure = vi.fn()) {
  const partial = vi.fn();
  const pending = LiveTranscription.open({} as MediaStream, 16000, signal, partial, failure);
  await waitFor(() => expect(Socket.instances).toHaveLength(1));
  const socket = Socket.instances[0];
  socket.receive({ type: 'authenticated' });
  await waitFor(() => expect(socket.send).toHaveBeenCalledOnce());
  const start = JSON.parse(socket.send.mock.calls[0][0]);
  expect(start.type).toBe('speech_stt_start');
  socket.receive({ type: 'speech_stt_event', request_id: start.request_id, event: { type: 'start', sample_rate: 16000 } });
  return { session: await pending, socket, requestId: start.request_id, partial, failure };
}

it('streams microphone PCM and waits for the committed final transcript', async () => {
  const { session, socket, requestId, partial } = await open(new AbortController().signal);
  Worklet.instances[0].port.onmessage?.({ data: new Int16Array([1, 2]).buffer });
  await waitFor(() => expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({ type: 'speech_stt_audio', request_id: requestId }));
  socket.receive({ type: 'speech_stt_event', request_id: requestId, event: { type: 'partial', text: 'Some words' } });
  await waitFor(() => expect(partial).toHaveBeenCalledWith('Some words'));
  const result = session.commit();
  await waitFor(() => expect(JSON.parse(socket.send.mock.calls.at(-1)![0]).type).toBe('speech_stt_commit'));
  expect(socket.send.mock.calls.map(([raw]) => JSON.parse(raw).type)).toEqual([
    'speech_stt_start', 'speech_stt_audio', 'speech_stt_commit',
  ]);
  let settled = false; void result.then(() => { settled = true; });
  socket.receive({ type: 'speech_stt_event', request_id: requestId, event: { type: 'transcript', text: 'Some words.' } });
  await Promise.resolve(); expect(settled).toBe(false);
  socket.receive({ type: 'speech_stt_event', request_id: requestId, event: { type: 'done' } });
  socket.onclose?.();
  await expect(result).resolves.toEqual({ text: 'Some words.' });
  expect(socket.close).toHaveBeenCalledOnce();
});

it('does not reconnect or substitute batch transcription after cancellation', async () => {
  const controller = new AbortController();
  const { session, socket, failure } = await open(controller.signal);
  controller.abort();
  await expect(session.commit()).rejects.toMatchObject({ name: 'AbortError' });
  expect(Socket.instances).toHaveLength(1);
  expect(socket.close).toHaveBeenCalledOnce();
  expect(failure).not.toHaveBeenCalled();
});

it('closes live input when the authenticated document owner changes', async () => {
  const { session, socket, partial, failure } = await open(new AbortController().signal);
  const receive = socket.onmessage;
  const capture = Worklet.instances[0].port.onmessage;
  const sent = socket.send.mock.calls.length;
  authority.controller.abort();
  capture?.({ data: new Int16Array([1]).buffer });
  receive?.({ data: JSON.stringify({ type: 'speech_stt_event',
    request_id: JSON.parse(socket.send.mock.calls[0][0]).request_id,
    event: { type: 'transcript', text: 'Stale account text' } }) });
  await expect(session.commit()).rejects.toMatchObject({ name: 'AbortError' });
  expect(socket.send).toHaveBeenCalledTimes(sent);
  expect(socket.close).toHaveBeenCalledOnce();
  expect(partial).not.toHaveBeenCalled();
  expect(failure).not.toHaveBeenCalled();
});

it('does not open a paid speech transport after failed owner validation', async () => {
  vi.spyOn(authority.lease, 'validate').mockRejectedValue(new DOMException('Account changed', 'AbortError'));
  await expect(LiveTranscription.open({} as MediaStream, 16000,
    new AbortController().signal, vi.fn(), vi.fn())).rejects.toMatchObject({ name: 'AbortError' });
  expect(Socket.instances).toHaveLength(0);
});

it('commits behind a full accepted audio queue rather than abandoning the transport', async () => {
  const { session, socket, requestId, failure } = await open(new AbortController().signal);
  const port = Worklet.instances[0].port;
  port.postMessage.mockImplementation(() => {
    port.onmessage?.({ data: new Uint8Array([250, 0]).buffer });
    port.onmessage?.({ data: { type: 'flushed' } });
  });
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  vi.spyOn(authority.lease, 'validate').mockImplementation(() => held);
  for (let index = 0; index < 96; index++) {
    Worklet.instances[0].port.onmessage?.({ data: new Int16Array([index]).buffer });
  }
  const result = session.commit();
  await Promise.resolve();
  expect(socket.send).toHaveBeenCalledOnce();
  release();
  await waitFor(() => expect(JSON.parse(socket.send.mock.calls.at(-1)![0]).type).toBe('speech_stt_commit'));
  const messages = socket.send.mock.calls.map(([raw]) => JSON.parse(raw).type);
  expect(messages).toEqual(['speech_stt_start', ...Array(97).fill('speech_stt_audio'), 'speech_stt_commit']);
  expect(JSON.parse(socket.send.mock.calls.at(-2)![0]).audio_base64).toBe(btoa(String.fromCharCode(250, 0)));
  socket.receive({ type: 'speech_stt_event', request_id: requestId, event: { type: 'transcript', text: 'A complete utterance.' } });
  socket.receive({ type: 'speech_stt_event', request_id: requestId, event: { type: 'done' } });
  await expect(result).resolves.toEqual({ text: 'A complete utterance.' });
  expect(socket.close).toHaveBeenCalledOnce();
  expect(failure).not.toHaveBeenCalled();
});

it('closes the transport when the ordered commit send fails', async () => {
  const { session, socket, failure } = await open(new AbortController().signal);
  vi.spyOn(authority.lease, 'validate').mockRejectedValue(new Error('Commit authority lost'));
  await expect(session.commit()).rejects.toThrow('Commit authority lost');
  expect(socket.close).toHaveBeenCalledOnce();
  expect(failure).toHaveBeenCalledOnce();
});

it('settles a cancelled commit without waiting for held authority validation', async () => {
  const { session, socket } = await open(new AbortController().signal);
  let release!: () => void;
  let entered!: () => void;
  let returned = false;
  const held = new Promise<void>(resolve => { release = resolve; });
  const validating = new Promise<void>(resolve => { entered = resolve; });
  vi.spyOn(authority.lease, 'validate').mockImplementation(async () => {
    entered();
    await held; returned = true;
  });
  Worklet.instances[0].port.onmessage?.({ data: new Int16Array([1]).buffer });
  await validating;
  const result = session.commit();
  authority.controller.abort();
  try {
    await expect(result).rejects.toMatchObject({ name: 'AbortError' });
    expect(returned).toBe(false);
    expect(socket.close).toHaveBeenCalledOnce();
  } finally {
    release();
  }
});

it.each(['microphone', 'transcript'])('revalidates durable authority before %s delivery without an abort notification', async (direction) => {
  const { session, socket, requestId, partial, failure } = await open(new AbortController().signal);
  vi.spyOn(authority.lease, 'validate').mockRejectedValue(new Error('Durable owner changed'));
  const sent = socket.send.mock.calls.length;
  if (direction === 'microphone') {
    Worklet.instances[0].port.onmessage?.({ data: new Int16Array([1]).buffer });
  } else {
    socket.receive({ type: 'speech_stt_event', request_id: requestId,
      event: { type: 'transcript', text: 'Stale account text' } });
    socket.receive({ type: 'speech_stt_event', request_id: requestId, event: { type: 'done' } });
  }
  await waitFor(() => expect(socket.close).toHaveBeenCalledOnce());
  await expect(session.commit()).rejects.toThrow('Durable owner changed');
  expect(socket.send).toHaveBeenCalledTimes(sent);
  expect(partial).not.toHaveBeenCalled();
  expect(failure).toHaveBeenCalledOnce();
  expect(authority.lease.signal.aborted).toBe(false);
});
