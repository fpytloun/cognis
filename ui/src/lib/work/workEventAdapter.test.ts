import { describe, expect, it } from 'vitest';

import type { WorkCommandEvent, WorkMutationEvent } from '$lib/chat-v2/types';
import { commandToToolCall, mutationToToolCall } from './workEventAdapter';

describe('workEventAdapter', () => {
  it('maps canonical complete status to the terminal tool renderer status', () => {
    const command: WorkCommandEvent = {
      id: 'command',
      call_id: 'call',
      sort_key: '1',
      command: 'npm test',
      status: 'complete',
      preview_truncated: false,
      has_full_output: false,
    };

    expect(commandToToolCall(command)).toMatchObject({
      status: 'completed',
      arguments: { command: 'npm test' },
    });
  });

  it('recursively redacts projected payloads before ToolCallBlock can render them', () => {
    const event: WorkMutationEvent = {
      id: 'event',
      call_id: 'call',
      sort_key: '1',
      tool_name: 'send',
      category: 'external',
      operation_kind: 'send',
      status: 'complete',
      arguments: {
        target: {
          access_token: 'unsafe-token',
          nested: [{ apiKey: 'unsafe-key' }],
          payload: '{"password":"unsafe value with spaces"}',
        },
        description: 'secret=["unsafe first","unsafe second"]',
      },
      result_preview: '{"password":"unsafe-password"}',
      evaluation: { decision: 'allow', reasoning: 'secret=unsafe-secret' },
      paths: [],
      file_diffs: [],
      diffs_truncated: false,
    };

    const adapted = mutationToToolCall(event);
    const serialized = JSON.stringify(adapted);
    expect(serialized).not.toContain('unsafe-token');
    expect(serialized).not.toContain('unsafe-key');
    expect(serialized).not.toContain('unsafe-password');
    expect(serialized).not.toContain('unsafe-secret');
    expect(serialized).not.toContain('unsafe value with spaces');
    expect(serialized).not.toContain('unsafe first');
    expect(serialized).not.toContain('unsafe second');
  });
});
