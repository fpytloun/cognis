import type { QueuedMessage } from '$lib/types/api';

export function isAutomaticContinuation(message: QueuedMessage): boolean {
  return message.kind === 'automatic_continuation';
}

export function queuedMessageLabel(message: QueuedMessage): string {
  if (!isAutomaticContinuation(message)) {
    const content = message.content.trim();
    if (content) return content;
    if (message.attachments?.length) {
      return `${message.attachments.length} queued attachment${message.attachments.length === 1 ? '' : 's'}`;
    }
    return '';
  }
  if (message.continuation_reason === 'llm_cycle_ceiling_reached') {
    return 'Continuing automatically after the LLM cycle limit.';
  }
  if (message.continuation_reason === 'tool_call_ceiling_reached') {
    return 'Continuing automatically after the tool-call limit.';
  }
  if (message.continuation_reason === 'step_timeout') {
    return 'Continuing automatically after the step timed out.';
  }
  return 'Continuing automatically.';
}

export function queuedMessageAccessibleLabel(message: QueuedMessage, maxLength = 48): string {
  const label = queuedMessageLabel(message).replace(/\s+/g, ' ').trim() || 'queued message';
  const bounded = label.length <= maxLength
    ? label
    : `${label.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
  return `${bounded}, item ${message.position}`;
}

export function visibleQueuedMessages(messages: QueuedMessage[]): QueuedMessage[] {
  return messages.filter((message) =>
    isAutomaticContinuation(message)
    || Boolean(message.content.trim())
    || Boolean(message.attachments?.length)
  );
}
