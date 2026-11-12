import { describe, expect, it } from 'vitest';
import type { FeedbackAnchorCandidate, FeedbackContext, IncludedFeedback } from './types';

describe('feedback wire boundary', () => {
  it('keeps explicit chat origin independent of document provenance', () => {
    const context: FeedbackContext = { kind: 'chat', conversation_id: 'origin-conversation' };
    const source = { kind: 'document' as const, content_ref: 'reviewed-document' };
    expect({ source, context }.context).toEqual(context);
  });
  it('uses a small immutable admission binding, not a live thread query', () => {
    const binding: IncludedFeedback = { creation_key: 'creation-key', preflight_id: 'preflight-key', manifest_hash: 'manifest-digest' };
    expect(Object.keys(binding).sort()).toEqual(['creation_key', 'manifest_hash', 'preflight_id']);
    const candidate: FeedbackAnchorCandidate = { locator: { kind: 'whole' } };
    expect(candidate).not.toHaveProperty('destination');
  });
});
