import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { emptyChatV2State } from '$lib/chat-v2/sync-engine';
import { conversationTimelineScope } from '$lib/chat-v2/types';
import Panel from './TimelineDiagnosticExportPanel.svelte';

const recovery = vi.hoisted(() => ({ refresh: vi.fn() }));
vi.mock('$lib/chat-v2/timeline-recovery', () => ({ forceRefreshTimeline: recovery.refresh }));
vi.mock('./diagnostic-download', () => ({ downloadChatDiagnostic: vi.fn() }));
const state = (id = 'diagnostic-fixture') => {
  const scope = conversationTimelineScope(id);
  return { ...emptyChatV2State(), scope, scopeKey: scope.key, conversationId: id };
};
beforeEach(() => { recovery.refresh.mockReset(); });
afterEach(cleanup);

it('offers one busy refresh beside download and announces completion', async () => {
  let resolve!: () => void;
  recovery.refresh.mockReturnValue(new Promise<void>(done => { resolve = done; }));
  const clientState = state();
  render(Panel, { clientState, visibleItems: [] });
  await fireEvent.click(screen.getByRole('button', { name: 'Force refresh' }));
  expect(screen.getByRole('button', { name: 'Refreshing…' })).toBeDisabled();
  await fireEvent.click(screen.getByRole('button', { name: 'Refreshing…' }));
  expect(recovery.refresh).toHaveBeenCalledOnce();
  expect(recovery.refresh).toHaveBeenCalledWith(clientState.scope, expect.any(AbortSignal));
  resolve();
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('History refreshed.'));
  expect(screen.getByRole('button', { name: 'Force refresh' })).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Download chat diagnostics' })).toBeInTheDocument();
});

it('keeps recovery retryable without exposing an error body', async () => {
  recovery.refresh.mockRejectedValue(new Error('Private internal error body'));
  render(Panel, { clientState: state(), visibleItems: [] });
  await fireEvent.click(screen.getByRole('button', { name: 'Force refresh' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Your input has been kept.'));
  expect(screen.queryByText('Private internal error body')).toBeNull();
  expect(screen.getByRole('button', { name: 'Force refresh' })).toBeEnabled();
});

it('cancels pending refresh when the displayed scope changes', async () => {
  let signal!: AbortSignal;
  recovery.refresh.mockImplementation((_scope, current) => {
    signal = current;
    return new Promise<void>(() => {});
  });
  const view = render(Panel, { clientState: state(), visibleItems: [] });
  await fireEvent.click(screen.getByRole('button', { name: 'Force refresh' }));
  await view.rerender({ clientState: state('another-diagnostic'), visibleItems: [] });
  expect(signal.aborted).toBe(true);
  expect(screen.getByRole('button', { name: 'Force refresh' })).toBeEnabled();
  expect(screen.queryByRole('status')).toBeNull();
});

it('does not cancel refresh for ordinary live updates in the same scope', async () => {
  let signal!: AbortSignal;
  recovery.refresh.mockImplementation((_scope, current) => {
    signal = current;
    return new Promise<void>(() => {});
  });
  const view = render(Panel, { clientState: state(), visibleItems: [] });
  await fireEvent.click(screen.getByRole('button', { name: 'Force refresh' }));
  await view.rerender({ clientState: { ...state(), cursor: 'new-live-cursor' }, visibleItems: [] });
  expect(signal.aborted).toBe(false);
  expect(screen.getByRole('button', { name: 'Refreshing…' })).toBeDisabled();
  view.unmount();
  expect(signal.aborted).toBe(true);
});
