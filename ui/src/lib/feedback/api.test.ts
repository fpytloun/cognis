import { beforeEach, describe, expect, it, vi } from 'vitest';
import { feedbackApi } from './api';

const calls = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./transport', () => ({ request: calls.request }));

beforeEach(() => calls.request.mockReset().mockResolvedValue({}));

describe('authenticated edit context', () => {
  it('serializes the explicit chat context', async () => {
    const context = { kind: 'chat' as const, conversation_id: 'owned-chat' };
    await feedbackApi.edit('authored-entry', 2, 'New version', context);
    expect(JSON.parse(calls.request.mock.calls[0][1].body)).toEqual({
      expected_version: 2, body: 'New version', context
    });
  });

  it('keeps contextless callers non-routing', async () => {
    await feedbackApi.edit('authored-entry', 2, 'New version');
    expect(JSON.parse(calls.request.mock.calls[0][1].body)).toEqual({
      expected_version: 2, body: 'New version'
    });
  });
});
