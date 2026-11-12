import { describe, expect, it, vi } from 'vitest';
import { createScopedTextResolver } from './scoped-text';
import type { FeedbackTextIdentity } from './scoped-text';
import type { FeedbackTextRequest, FeedbackTextScope, ScopedCanonicalFeedbackText } from './types';

const identity: FeedbackTextIdentity = { source: { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
  context: { kind: 'standalone' }, content_digest: 'a'.repeat(64) };
const scope: FeedbackTextScope = { kind: 'rich_field', block_id: 'summary', identity: 'semantic', item_key: null, field_path: ['text'] };
function response(): ScopedCanonicalFeedbackText {
  return { scope, content_digest: identity.content_digest,
    descriptor: { representation: 'feedback-text-v1', text_digest: 'b'.repeat(64), length: 3, available_ranges: [[0, 3]] },
    text_page: { representation: 'feedback-text-v1', text_digest: 'b'.repeat(64), start: 0, end: 3, total_length: 3, text: 'A😀' } };
}
describe('scoped canonical text', () => {
  it('reads backend field text and caches only exact scope and page', async () => {
    const read = vi.fn(async (_request: FeedbackTextRequest) => response());
    const resolve = createScopedTextResolver(identity, () => identity, read, () => undefined);
    const first = await resolve(scope);
    first.text_page.text = 'mutated';
    expect((await resolve(scope)).text_page.text).toBe('A😀');
    expect(read).toHaveBeenCalledTimes(1);
    expect(read.mock.calls[0][0]).toMatchObject({ source: identity.source, context: identity.context, scope });
  });
  it('rejects a response after access context or revision changes', async () => {
    let live: FeedbackTextIdentity | null = identity;
    let finish!: (value: ScopedCanonicalFeedbackText) => void;
    const resolve = createScopedTextResolver(identity, () => live, () => new Promise(done => { finish = done; }), () => undefined);
    const pending = resolve(scope);
    live = { ...identity, context: { kind: 'chat', conversation_id: 'conv_7874ff16fa0b4a66867811dc5fe93097' } };
    finish(response());
    await expect(pending).rejects.toThrow('source changed');
  });
  it('rejects mismatched digest and never fabricates rich or PDF text', async () => {
    const resolve = createScopedTextResolver(identity, () => identity,
      async () => ({ ...response(), content_digest: 'c'.repeat(64) }), () => undefined);
    await expect(resolve(scope)).rejects.toThrow('no longer matches');
  });
});
