/**
 * Messages above this size can be rewritten by the server into attachments.
 * Wait for the canonical event rather than rendering raw optimistic content
 * that would immediately be replaced.
 */
export const SERVER_OVERSIZED_MESSAGE_CANDIDATE_CHARS = 12 * 1024;

export function shouldAwaitCanonicalUserMessage(content: string): boolean {
  return new TextEncoder().encode(content).byteLength > SERVER_OVERSIZED_MESSAGE_CANDIDATE_CHARS;
}

export function shouldRenderOptimisticUserMessage(input: {
  isSlashCommand: boolean;
  isStepInputReply: boolean;
  awaitCanonicalUserMessage: boolean;
}): boolean {
  return !input.isSlashCommand && !input.isStepInputReply && !input.awaitCanonicalUserMessage;
}
