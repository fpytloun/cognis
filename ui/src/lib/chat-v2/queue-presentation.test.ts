import { describe, expect, it } from 'vitest';
import type { QueuedMessage } from '$lib/types/api';
import {
  isAutomaticContinuation,
  queuedMessageLabel,
  visibleQueuedMessages
} from './queue-presentation';

function queued(overrides: Partial<QueuedMessage> = {}): QueuedMessage {
  return {
    queue_id: 'queue-1',
    content: '',
    attachments: [],
    position: 1,
    ...overrides
  };
}

describe('queue presentation', () => {
  it('labels automatic continuations even when content is empty', () => {
    const message = queued({
      kind: 'automatic_continuation',
      continuation_reason: 'tool_call_ceiling_reached'
    });

    expect(isAutomaticContinuation(message)).toBe(true);
    expect(queuedMessageLabel(message)).toBe(
      'Continuing automatically after the tool-call limit.'
    );
  });

  it('labels attachment-only messages and hides truly empty ordinary rows', () => {
    const attachment = queued({
      attachments: [{
        artifact_id: 'artifact-1',
        kind: 'file',
        mime_type: 'text/plain',
        filename: 'note.txt',
        size_bytes: 12
      }]
    });
    const empty = queued({ queue_id: 'queue-empty' });

    expect(queuedMessageLabel(attachment)).toBe('1 queued attachment');
    expect(visibleQueuedMessages([attachment, empty])).toEqual([attachment]);
  });
});
