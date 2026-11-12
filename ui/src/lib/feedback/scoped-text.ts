import type { FeedbackContext, FeedbackSourceRef, FeedbackTextRequest, FeedbackTextScope, ScopedCanonicalFeedbackText } from './types';

export interface FeedbackTextIdentity { source: FeedbackSourceRef; context: FeedbackContext; content_digest: string }
function identityKey(value: FeedbackTextIdentity): string { return JSON.stringify(value); }
/** An access-bound view cache, never a shared content-only authorization cache. */
export function createScopedTextResolver(
  identity: FeedbackTextIdentity,
  current: () => FeedbackTextIdentity | null,
  read: (request: FeedbackTextRequest) => Promise<ScopedCanonicalFeedbackText>,
  assertCurrent: () => void,
) {
  const key = identityKey(identity);
  const cache = new Map<string, Promise<ScopedCanonicalFeedbackText>>();
  function check(): void {
    assertCurrent();
    const live = current();
    if (!live || identityKey(live) !== key) throw new Error('The feedback source changed. Select the text again.');
  }
  return async (scope: FeedbackTextScope, page = { start: 0, limit: 16384 }): Promise<ScopedCanonicalFeedbackText> => {
    check();
    if (!Number.isInteger(page.start) || page.start < 0 || !Number.isInteger(page.limit) || page.limit < 1 || page.limit > 32768) {
      throw new Error('Invalid canonical text page.');
    }
    const request = structuredClone({ ...identity, scope, ...page });
    const cacheKey = JSON.stringify({ scope, ...page });
    let pending = cache.get(cacheKey);
    if (!pending) {
      pending = read({ source: request.source, context: request.context, scope: request.scope, start: request.start, limit: request.limit })
        .then(value => {
          check();
          if (value.content_digest !== identity.content_digest || JSON.stringify(value.scope) !== JSON.stringify(scope)
            || value.descriptor.text_digest !== value.text_page.text_digest
            || value.text_page.start !== page.start
            || value.text_page.end - value.text_page.start !== value.text_page.text.length) {
            throw new Error('Canonical text no longer matches this source.');
          }
          return structuredClone(value);
        }).catch(error => { cache.delete(cacheKey); throw error; });
      if (cache.size >= 32) cache.delete(cache.keys().next().value!);
      cache.set(cacheKey, pending);
    }
    const value = await pending;
    check();
    return structuredClone(value);
  };
}
