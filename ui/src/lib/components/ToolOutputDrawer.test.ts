import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { TimelineScope } from '$lib/chat-v2/types';
import ToolOutputDrawer from './ToolOutputDrawer.svelte';

const { toolOutputPage } = vi.hoisted(() => ({ toolOutputPage: vi.fn() }));

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: { toolOutputPage },
}));

afterEach(() => {
  cleanup();
  toolOutputPage.mockReset();
});

describe('ToolOutputDrawer scoped ChatV2 loader', () => {
  it.each([
    { key: 'conversation:conv_1', kind: 'conversation', conversation_id: 'conv_1' },
    { key: 'session:sess_1', kind: 'session', session_id: 'sess_1', conversation_id: 'conv_1' },
    { key: 'task_step:run_1', kind: 'task_step', task_id: 'task_1', step_run_id: 'run_1' },
  ] satisfies TimelineScope[])('loads output through $kind scope', async (scope) => {
    toolOutputPage.mockResolvedValue({
      conversation_id: scope.conversation_id ?? null,
      session_id: scope.session_id ?? null,
      call_id: 'call_1',
      status: 'completed',
      source: 'stored_output',
      content: 'canonical scoped output',
      chunks: [],
      offset: 1,
      limit: 200,
      next_offset: null,
      prev_offset: null,
      has_more_before: false,
      has_more_after: false,
      output_size: 23,
      recoverable: true,
      truncated: false,
      spool_truncated: false,
    });

    render(ToolOutputDrawer, {
      open: true,
      scope,
      callId: 'call_1',
      toolName: 'grep',
      onClose: vi.fn(),
    });

    await waitFor(() => expect(screen.getByText('canonical scoped output')).toBeTruthy());
    expect(toolOutputPage).toHaveBeenCalledWith(scope, 'call_1', {
      offset: undefined,
      limit: 200,
      latest: true,
    });
  });
});
