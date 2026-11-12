import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import ToolCallBlock from './ToolCallBlock.svelte';
import ToolCallDetails from './ToolCallDetails.svelte';
import type { ToolCallTimelineItem } from '$lib/timeline-render-model';

const loader = vi.hoisted(() => ({ load: vi.fn(), peek: vi.fn(() => null) }));
vi.mock('./tool-call-details-loader', () => ({
  loadToolCallDetails: loader.load,
  peekToolCallDetails: loader.peek,
}));

function item(overrides: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem {
  return {
    id: 'tool-loading-boundary',
    kind: 'tool_call',
    callId: 'call-loading-boundary',
    toolName: 'read',
    status: 'completed',
    timestamp: '2026-10-28T18:00:00Z',
    arguments: { path: 'first.txt' },
    result: 'First file result',
    ...overrides,
  };
}

function pendingDetails() {
  let resolve!: (component: typeof ToolCallDetails) => void;
  const promise = new Promise<typeof ToolCallDetails>(ready => { resolve = ready; });
  loader.load.mockReturnValue(promise);
  return () => resolve(ToolCallDetails);
}

beforeEach(() => { loader.load.mockReset(); loader.peek.mockReturnValue(null); });
afterEach(cleanup);

describe('deferred tool details', () => {
  it('renders the header without loading collapsed details', () => {
    render(ToolCallBlock, { item: item() });
    expect(screen.getByRole('button', { name: /first\.txt/ })).toBeTruthy();
    expect(loader.load).not.toHaveBeenCalled();
  });

  it('keeps loading visible, forwards current data, and restores header focus', async () => {
    const ready = pendingDetails();
    const { rerender, container } = render(ToolCallBlock, { item: item() });
    const header = screen.getByRole('button', { name: /first\.txt/ });
    header.focus();
    await fireEvent.click(header);
    expect(screen.getByText('Loading tool details…')).toBeTruthy();
    await rerender({ item: item({
      arguments: { path: 'current.txt' },
      result: 'Fresh file result',
    }) });
    ready();
    await waitFor(() => expect(container.textContent).toContain('Fresh file result'));
    expect(screen.queryByText('Loading tool details…')).toBeNull();
    expect(container.textContent).not.toContain('First file result');
    expect(document.activeElement?.textContent).toContain('current.txt');
  });

  it('does not take focus back after the user leaves the header', async () => {
    const ready = pendingDetails();
    render(ToolCallBlock, { item: item() });
    const header = screen.getByRole('button', { name: /first\.txt/ });
    header.focus();
    await fireEvent.click(header);
    const outside = document.createElement('button');
    document.body.append(outside);
    outside.focus();
    try {
      ready();
      await screen.findByText('First file result');
      expect(document.activeElement).toBe(outside);
    } finally {
      outside.remove();
    }
  });

  it('retries a failed module load without repeating a tool action', async () => {
    loader.load.mockRejectedValueOnce(new Error('Chunk unavailable'))
      .mockResolvedValueOnce(ToolCallDetails);
    render(ToolCallBlock, { item: item() });
    await fireEvent.click(screen.getByRole('button', { name: /first\.txt/ }));
    await screen.findByRole('alert');
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('First file result');
    expect(loader.load).toHaveBeenCalledTimes(2);
  });

  it('ignores readiness after unmount', async () => {
    const ready = pendingDetails();
    const { unmount, container } = render(ToolCallBlock, { item: item() });
    await fireEvent.click(screen.getByRole('button', { name: /first\.txt/ }));
    unmount();
    ready();
    await tick();
    await tick();
    expect(container.textContent).toBe('');
  });
});
