import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { conversationTimelineScope } from '$lib/chat-v2/types';
import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
import ToolCallBlock from './ToolCallBlock.svelte';
import { loadToolCallDetails } from './tool-call-details-loader';

const { backgroundToolCall, addToast } = vi.hoisted(() => ({
  backgroundToolCall: vi.fn(),
  addToast: vi.fn()
}));

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: { backgroundToolCall }
}));

vi.mock('$lib/stores/toasts', () => ({ addToast }));

function runningBash(overrides: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem {
  return {
    id: 'tool-call:bash',
    kind: 'tool_call',
    callId: 'call-1',
    toolName: 'bash',
    status: 'started',
    timestamp: null,
    arguments: { command: 'make test', description: 'Run tests' },
    ...overrides
  };
}

async function renderExpanded(item: ToolCallTimelineItem, scope = conversationTimelineScope('conv-1')) {
  const view = render(ToolCallBlock, { item, scope });
  await fireEvent.click(screen.getAllByRole('button')[0]);
  return view;
}

describe('ToolCallBlock move to background', () => {
  beforeEach(async () => {
    backgroundToolCall.mockReset();
    addToast.mockReset();
    await loadToolCallDetails();
  });

  it('requests a background handoff for a running foreground bash command', async () => {
    backgroundToolCall.mockResolvedValue({ status: 'requested' });
    await renderExpanded(runningBash());

    const button = screen.getByTestId('tool-move-to-background');
    expect(button.textContent?.trim()).toBe('Move to background');
    await fireEvent.click(button);

    expect(backgroundToolCall).toHaveBeenCalledWith('conv-1', 'call-1');
    await waitFor(() => expect(button.textContent?.trim()).toBe('Moving to background\u2026'));
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(addToast).not.toHaveBeenCalled();
  });

  it('reports a rejected request and allows another attempt', async () => {
    backgroundToolCall.mockRejectedValue(new Error('The command can no longer be moved'));
    await renderExpanded(runningBash());

    const button = screen.getByTestId('tool-move-to-background');
    await fireEvent.click(button);

    await waitFor(() => expect(addToast).toHaveBeenCalledWith(
      'The command can no longer be moved',
      'error',
      6_000,
      'Could not move command to background'
    ));
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });

  it('hides the action once the command settles', async () => {
    backgroundToolCall.mockResolvedValue({ status: 'requested' });
    const view = await renderExpanded(runningBash());
    expect(screen.getByTestId('tool-move-to-background')).toBeTruthy();

    await view.rerender({ item: runningBash({ status: 'completed', result: 'done' }), scope: conversationTimelineScope('conv-1') });

    expect(screen.queryByTestId('tool-move-to-background')).toBeNull();
  });

  it.each([
    ['background command', runningBash({ arguments: { command: 'sleep 9', run_in_background: true } })],
    ['other tool', runningBash({ toolName: 'read' })],
    ['reconstructed call', runningBash({ reconstructed: true })]
  ])('does not offer the action for a %s', async (_label, item) => {
    await renderExpanded(item);
    expect(screen.queryByTestId('tool-move-to-background')).toBeNull();
  });

  it('does not offer the action without an owning conversation scope', async () => {
    const view = render(ToolCallBlock, { item: runningBash() });
    await fireEvent.click(screen.getAllByRole('button')[0]);
    expect(screen.queryByTestId('tool-move-to-background')).toBeNull();
    view.unmount();
  });
});
