import { describe, expect, it, vi } from 'vitest';
import { PublicFeedbackClient, type PublicFeedbackExchange } from './public-api';

const exchange: PublicFeedbackExchange = {
  share_id: 'share-guest-review', capability: 'bounded-share-capability', expires_at: '2099-01-01T00:00:00Z',
  source: { source_token: 'source-token', content_digest: 'a'.repeat(64),
    content_ref: 'art_87d6238b098f4798919dc4b299894379', format: 'html', title: 'Review', canonical_text: null },
  allow_comments: true, discussion: 'own_only', permitted_locator_kinds: ['whole', 'html'],
};
describe('public feedback capability transport', () => {
  it('uses only the share capability for public audio and downloads and closes both on teardown', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(exchange))
      .mockResolvedValueOnce(Response.json({}))
      .mockResolvedValueOnce(Response.json([]))
      .mockResolvedValueOnce(Response.json({}))
      .mockResolvedValueOnce(new Response('download bytes'));
    const client = new PublicFeedbackClient(fetcher);
    await client.open('share-review-token');
    const audio = client.audio();
    await audio.context(exchange.source.content_ref);
    await audio.estimates(exchange.source.content_ref);
    await audio.create({ content_ref: exchange.source.content_ref, mode: 'narration', idempotency_key: 'public-audio-test' });
    expect(await (await client.download('markdown')).text()).toBe('download bytes');
    expect(fetcher.mock.calls.slice(1).map(([path]) => path)).toEqual([
      '/api/v1/public/feedback/audio/source', '/api/v1/public/feedback/audio/estimates',
      '/api/v1/public/feedback/audio', '/api/v1/public/feedback/source/download?format=markdown',
    ]);
    for (const [, options] of fetcher.mock.calls.slice(1)) {
      expect(options).toMatchObject({ credentials: 'omit', cache: 'no-store', redirect: 'error' });
      expect(new Headers(options?.headers).get('X-Cognis-Feedback-Capability')).toBe(exchange.capability);
      expect(new Headers(options?.headers).has('Authorization')).toBe(false);
    }
    client.close();
    await expect(audio.context(exchange.source.content_ref)).rejects.toThrow('expired');
    await expect(client.download('pdf')).rejects.toThrow('expired');
    expect(fetcher).toHaveBeenCalledTimes(5);
  });
  it('never sends owner cookies, bearer tokens, or capability query parameters', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(exchange))
      .mockResolvedValueOnce(Response.json({ guest_session_id: 'guest-review', guest_capability: 'bounded-guest-capability', expires_at: exchange.expires_at }))
      .mockResolvedValueOnce(Response.json({ items: [], next_cursor: null }));
    const client = new PublicFeedbackClient(fetcher);
    await client.open('share-review-token');
    await client.join('Reviewer');
    await client.threads();
    expect(fetcher).toHaveBeenCalledTimes(3);
    for (const [url, options] of fetcher.mock.calls) {
      expect(String(url)).toMatch(/^\/api\/v1\/public\/feedback\//);
      expect(String(url)).not.toContain('capability');
      expect(options).toMatchObject({ credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' });
      expect(new Headers(options?.headers).get('Authorization')).toBeNull();
    }
    const headers = new Headers(fetcher.mock.calls[2][1]?.headers);
    expect(headers.get('X-Cognis-Feedback-Capability')).toBe(exchange.capability);
    expect(headers.get('X-Cognis-Feedback-Guest')).toBe('bounded-guest-capability');
  });
  it('fails closed after capability expiry or teardown, without a private API fallback', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ...exchange, expires_at: '2000-01-01T00:00:00Z' }));
    const client = new PublicFeedbackClient(fetcher);
    await client.open('share-review-token');
    await expect(client.threads()).rejects.toThrow('expired');
    client.close();
    await expect(client.threads()).rejects.toThrow('expired');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('adopts standalone bootstrap authority directly and sends the full scoped text DTO', async () => {
    const source = { kind: 'document' as const, content_ref: exchange.source.content_ref };
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({ subject_id: exchange.share_id, source, source_token: exchange.source.source_token,
        content_digest: exchange.source.content_digest, format: 'html', title: 'Review', canonical_text: null,
        capabilities: { comment: true }, permitted_locator_kinds: ['whole', 'html'] }))
      .mockResolvedValueOnce(Response.json({}));
    const client = new PublicFeedbackClient(fetcher);
    await client.adoptCapability(exchange.capability, source);
    const request = { source, context: { kind: 'standalone' as const }, scope: { kind: 'document' as const }, start: 0, limit: 100 };
    await client.text(request);
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/v1/public/feedback/sources/resolve', '/api/v1/public/feedback/sources/text']);
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual(request);
  });
  it('rejects broad or oversized screenshot uploads before creating a capture', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(exchange));
    const client = new PublicFeedbackClient(fetcher);
    await client.open('share-review-token');
    await expect(client.capture(exchange.source.source_token, exchange.source.content_digest, {
      blob: new Blob(['svg'], { type: 'image/svg+xml' }), width: 20, height: 20, method: 'observed_pixels',
      viewport: { width_css_px: 20, height_css_px: 20, scroll_x_css_px: 0, scroll_y_css_px: 0, device_pixel_ratio: 1 },
      render_state_digest: 'b'.repeat(64),
    })).rejects.toThrow('PNG or WebP');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
