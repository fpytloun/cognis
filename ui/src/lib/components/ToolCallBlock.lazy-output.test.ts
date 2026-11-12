import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';

import ToolCallBlock from './ToolCallBlock.svelte';
import { loadToolCallDetails } from './tool-call-details-loader';

const { toolOutputPage, backgroundToolCall, drawerModule } = vi.hoisted(() => ({
  toolOutputPage: vi.fn(),
  backgroundToolCall: vi.fn(),
  drawerModule: { loaded: false },
}));

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: { toolOutputPage, backgroundToolCall },
}));

vi.mock('$lib/components/ToolOutputDrawer.svelte', async (importOriginal) => {
  drawerModule.loaded = true;
  return await importOriginal<typeof import('$lib/components/ToolOutputDrawer.svelte')>();
});

afterEach(() => {
  toolOutputPage.mockReset();
  backgroundToolCall.mockReset();
});

beforeAll(async () => {
  await loadToolCallDetails();
});

it('loads the full-output drawer only after the user opens it', async () => {
  expect(drawerModule.loaded).toBe(false);
  const scope = {
    key: 'conversation:conv_1',
    kind: 'conversation',
    conversation_id: 'conv_1',
  } as const;
  toolOutputPage.mockResolvedValue({
    conversation_id: 'conv_1',
    session_id: null,
    call_id: 'call_full_output',
    status: 'completed',
    source: 'stored_output',
    content: 'canonical full output',
    chunks: [],
    offset: 0,
    limit: 200,
    next_offset: null,
    prev_offset: null,
    has_more_before: false,
    has_more_after: false,
    output_size: 21,
    recoverable: true,
    truncated: false,
    spool_truncated: false,
  });

  render(ToolCallBlock, {
    scope,
    item: {
      id: 'tool:call_full_output',
      kind: 'tool_call',
      callId: 'call_full_output',
      toolName: 'read',
      status: 'completed',
      timestamp: null,
      arguments: { file_path: 'report.csv' },
      result: 'short preview',
      truncated: true,
      hasFullOutput: true,
      outputSize: 5000,
    },
  });

  expect(toolOutputPage).not.toHaveBeenCalled();
  expect(drawerModule.loaded).toBe(false);
  await fireEvent.click(document.querySelector<HTMLButtonElement>(
    'button[aria-controls^="tool-call-content-call_full_output"]'
  )!);
  await fireEvent.click(await screen.findByRole('button', { name: 'Open full output' }));
  await waitFor(() => expect(drawerModule.loaded).toBe(true));
  await waitFor(() => expect(screen.getByText('canonical full output')).toBeTruthy());
  expect(toolOutputPage).toHaveBeenCalledWith(scope, 'call_full_output', {
    offset: undefined,
    limit: 200,
    latest: true,
  });
});
