import { afterEach, describe, expect, it, vi } from 'vitest';
import { readDocumentNarration, standaloneAudioClient, type DocumentAudioClient } from './document-audio';
import type { AudioJob } from '$lib/types/api';

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
describe('standalone document audio transport', () => {
  it('requests estimates with authenticated same-origin GET and never starts generation', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('[]', { status: 200 }));
    vi.stubGlobal('fetch', fetcher);
    await standaloneAudioClient().estimates('dlv_11111111111111111111111111111111');
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toContain('/audio/jobs/estimates?content_ref=');
    expect(fetcher.mock.calls[0][1].credentials).toBe('same-origin');
    expect(fetcher.mock.calls[0][1].method).toBeUndefined();
  });
  it.each([401, 403, 404])('does not use a public source capability for private audio (%s)', async status => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}', { status }));
    vi.stubGlobal('fetch', fetcher);
    const result = await standaloneAudioClient().context('dlv_11111111111111111111111111111111');
    expect(result.can_generate).toBe(false);
    expect(result.jobs).toEqual([]);
    expect(result.supported).toBe(false);
    expect(result.detail).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toContain('/audio/jobs/source?content_ref=');
    expect(fetcher.mock.calls[0][1].credentials).toBe('same-origin');
  });

  it('never automatically retries an uncertain paid submission', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('Connection lost'));
    vi.stubGlobal('fetch', fetcher);
    await expect(standaloneAudioClient().create({
      content_ref: 'art_22222222222222222222222222222222',
      mode: 'narration', idempotency_key: crypto.randomUUID()
    })).rejects.toThrow('Connection lost');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

function recording(overrides: Partial<AudioJob> = {}): AudioJob {
  return {
    job_id: 'report-recording', deliverable_id: 'dlv_11111111111111111111111111111111',
    mode: 'narration', status: 'completed', completed_parts: 1, total_parts: 1,
    parts: [{ artifact_id: 'report-audio', audio_url: '/report.mp3', content_type: 'audio/mpeg' }],
    script_artifact_id: null, script_url: null, error_code: null, created_at: '2026-11-07T09:00:00Z',
    ...overrides,
  };
}

function client(jobs: AudioJob[], canGenerate = true) {
  return {
    context: vi.fn().mockResolvedValue({ supported: true, can_generate: canGenerate, jobs }),
    create: vi.fn().mockResolvedValue(recording()), get: vi.fn(), cancel: vi.fn(), estimates: vi.fn(),
  } satisfies DocumentAudioClient;
}

describe('conversation document narration', () => {
  it('uses the same paid admission identity for simultaneous automatic reads of one revision', async () => {
    const transport = client([]);
    await Promise.all([1, 2].map(() => readDocumentNarration(
      transport, 'dlv_11111111111111111111111111111111', new AbortController().signal, vi.fn(),
    )));
    expect(transport.create.mock.calls.map(([payload]) => payload)).toEqual([
      {
        content_ref: 'dlv_11111111111111111111111111111111', mode: 'narration',
        idempotency_key: 'conversation-narration:dlv_11111111111111111111111111111111',
      },
      {
        content_ref: 'dlv_11111111111111111111111111111111', mode: 'narration',
        idempotency_key: 'conversation-narration:dlv_11111111111111111111111111111111',
      },
    ]);
  });
  it('reuses an accessible recording without requiring generation permission', async () => {
    const transport = client([recording()], false), part = vi.fn();
    await readDocumentNarration(transport, 'dlv_11111111111111111111111111111111', new AbortController().signal, part);
    expect(part.mock.calls.map(([value]) => value.audio_url)).toEqual(['/report.mp3']);
    expect(transport.create).not.toHaveBeenCalled();
  });
  it.each(['failed', 'cancelled', 'interrupted'] as const)('does not replay a %s paid attempt', async status => {
    const transport = client([recording({ status })]), part = vi.fn();
    await expect(readDocumentNarration(transport, 'dlv_11111111111111111111111111111111',
      new AbortController().signal, part)).rejects.toThrow('did not complete');
    expect(transport.create).not.toHaveBeenCalled();
    expect(part).not.toHaveBeenCalled();
  });
  it('does not generate when source access forbids it', async () => {
    const transport = client([], false);
    await expect(readDocumentNarration(transport, 'dlv_11111111111111111111111111111111',
      new AbortController().signal, vi.fn())).rejects.toThrow('not permitted');
    expect(transport.create).not.toHaveBeenCalled();
  });
  it('appends each completed part once and stops observing when interrupted', async () => {
    vi.useFakeTimers();
    const first = recording({ status: 'synthesizing' });
    const transport = client([first]), controller = new AbortController(), part = vi.fn();
    transport.get.mockResolvedValue({ ...first, parts: [...first.parts, {
      artifact_id: 'report-audio-2', audio_url: '/report-2.mp3', content_type: 'audio/mpeg',
    }] });
    const reading = readDocumentNarration(transport, first.deliverable_id, controller.signal, part);
    const cancelled = expect(reading).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(1000);
    expect(part.mock.calls.map(([value]) => value.audio_url)).toEqual(['/report.mp3', '/report-2.mp3']);
    controller.abort();
    await vi.advanceTimersByTimeAsync(1000);
    await cancelled;
    expect(transport.create).not.toHaveBeenCalled();
    expect(transport.cancel).not.toHaveBeenCalled();
  });
});
