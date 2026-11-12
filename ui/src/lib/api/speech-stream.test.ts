import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { testDocumentLease } from '$lib/testing/document-lease';
import { api } from './client';

let authority: ReturnType<typeof testDocumentLease>;
vi.mock('$lib/document-coordination', async (importOriginal) => ({
  ...await importOriginal<typeof import('$lib/document-coordination')>(),
  documentCoordination: () => ({ capture: () => authority.lease }),
}));

const events = [
  { type: 'audio_start', part_index: 0, sample_rate: 24000 },
  { type: 'complete', response: {
    audio_url: '/speech.mp3', content_type: 'audio/mpeg', voice: 'alloy',
    model: 'gpt-4o-mini-tts', cached: false, parts: [], adapted: false,
    warnings: [], script_artifact_id: null, duration_seconds: null,
  } },
];
const encoded = (value: unknown) => new TextEncoder().encode(JSON.stringify(value) + '\n');

beforeEach(() => {
  authority = testDocumentLease();
  vi.spyOn(authority.lease, 'validate');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
    events.map(event => JSON.stringify(event) + '\n').join(''),
  )));
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it('streams an authorized response under the document owner', async () => {
  const result = [];
  for await (const event of api.tts.stream({ text: 'Spoken words.' })) result.push(event);
  expect(result).toEqual(events);
  expect(authority.lease.validate).toHaveBeenCalled();
});

it('refuses dispatch when document ownership is no longer valid', async () => {
  vi.mocked(authority.lease.validate).mockRejectedValue(new DOMException('Account changed', 'AbortError'));
  await expect(api.tts.stream({ text: 'Spoken words.' }).next()).rejects.toMatchObject({ name: 'AbortError' });
  expect(fetch).not.toHaveBeenCalled();
});

it.each(['document', 'caller'])('does not yield already-buffered data after %s cancellation', async (owner) => {
  const caller = new AbortController();
  const stream = api.tts.stream({ text: 'Spoken words.' }, { signal: caller.signal });
  expect((await stream.next()).value).toEqual(events[0]);
  (owner === 'document' ? authority.controller : caller).abort();
  await expect(stream.next()).rejects.toMatchObject({ name: owner === 'caller' ? 'AbortError' : 'Error' });
  expect(fetch).toHaveBeenCalledOnce();
});

it('cancels a blocked response reader when the account changes', async () => {
  const cancelled = vi.fn();
  vi.mocked(fetch).mockResolvedValue(new Response(new ReadableStream({
    start(controller) { controller.enqueue(encoded(events[0])); },
    cancel: cancelled,
  })));
  const stream = api.tts.stream({ text: 'Spoken words.' });
  expect((await stream.next()).value).toEqual(events[0]);
  const pending = stream.next();
  authority.controller.abort();
  await expect(pending).rejects.toThrow('The account changed.');
  expect(cancelled).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledOnce();
});

it('revalidates durable authority before buffered output without an abort notification', async () => {
  const stream = api.tts.stream({ text: 'Spoken words.' });
  expect((await stream.next()).value).toEqual(events[0]);
  vi.mocked(authority.lease.validate).mockRejectedValue(new Error('Durable owner changed'));
  await expect(stream.next()).rejects.toThrow('Durable owner changed');
  expect(authority.lease.signal.aborted).toBe(false);
  expect(fetch).toHaveBeenCalledOnce();
});
