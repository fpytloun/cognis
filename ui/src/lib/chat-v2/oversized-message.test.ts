import { describe, expect, it } from 'vitest';

import {
  SERVER_OVERSIZED_MESSAGE_CANDIDATE_CHARS,
  shouldAwaitCanonicalUserMessage,
  shouldRenderOptimisticUserMessage
} from './oversized-message';

describe('shouldAwaitCanonicalUserMessage', () => {
  it('waits only when the server can normalize oversized content', () => {
    expect(shouldAwaitCanonicalUserMessage('x'.repeat(SERVER_OVERSIZED_MESSAGE_CANDIDATE_CHARS))).toBe(false);
    expect(shouldAwaitCanonicalUserMessage('x'.repeat(SERVER_OVERSIZED_MESSAGE_CANDIDATE_CHARS + 1))).toBe(true);
    expect(shouldAwaitCanonicalUserMessage('🙂'.repeat(3_073))).toBe(true);
  });
});

describe('shouldRenderOptimisticUserMessage', () => {
  it('renders a normal user message immediately', () => {
    expect(shouldRenderOptimisticUserMessage({
      isSlashCommand: false,
      isStepInputReply: false,
      awaitCanonicalUserMessage: false
    })).toBe(true);
  });

  it('waits for canonical rendering only for special message paths', () => {
    expect(shouldRenderOptimisticUserMessage({
      isSlashCommand: false,
      isStepInputReply: false,
      awaitCanonicalUserMessage: true
    })).toBe(false);
    expect(shouldRenderOptimisticUserMessage({
      isSlashCommand: true,
      isStepInputReply: false,
      awaitCanonicalUserMessage: false
    })).toBe(false);
  });
});
