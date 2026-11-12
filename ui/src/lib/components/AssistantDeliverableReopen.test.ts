import { render, screen } from '@testing-library/svelte';
import { beforeAll, beforeEach, afterEach, expect, it, vi } from 'vitest';
import type { Deliverable } from '$lib/types/api';
import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';

const fixture = vi.hoisted(() => {
  const state = {
    epoch: 0, get: vi.fn(), listeners: new Set<() => void>(),
    coordination: {
      onInvalidated: (callback: () => void) => {
        state.listeners.add(callback);
        return () => state.listeners.delete(callback);
      },
      capture: () => {
        const epoch = state.epoch;
        const assertCurrent = () => { if (state.epoch !== epoch) throw new Error('Revoked'); };
        return { owner: 'reopen-account', epoch: String(epoch), signal: new AbortController().signal,
          assertCurrent, validate: async () => assertCurrent() };
      },
    },
  };
  return state;
});
vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/document-coordination', async importOriginal => ({
  ...await importOriginal<typeof import('$lib/document-coordination')>(),
  documentCoordination: () => fixture.coordination,
}));
vi.mock('$lib/api/client', () => ({ api: { deliverables: { get: fixture.get, revisions: vi.fn() } } }));
import AssistantDeliverableBlock from './AssistantDeliverableBlock.svelte';

function item(id = 'report-a'): AssistantDeliverableTimelineItem {
  return { id, kind: 'assistant_deliverable', sort_key: id, source_refs: [], stable: true,
    status: 'complete', deliverable_id: id, format: 'markdown' };
}
function body(id = 'report-a', content = '# Reopened report'): Deliverable {
  return { deliverable_id: id, content, format: 'markdown', outputs: {},
    step_run_id: null, version: 1, attempt_number: 1, title: null, target: null,
    status: 'approved', evaluator_feedback: null, created_at: null, updated_at: null,
    validation_warnings: [], render_metadata: {}, export_metadata: {} };
}
function deferred() {
  let resolve!: (value: Deliverable) => void;
  const promise = new Promise<Deliverable>(next => { resolve = next; });
  return { promise, resolve };
}
beforeAll(async () => {
  // Reopen assertions do not exercise cold feedback presentation loading.
  await import('./feedback/FeedbackIndicator.svelte');
});
beforeEach(() => {
  fixture.epoch++;
  fixture.listeners.forEach(callback => callback());
  fixture.get.mockReset().mockResolvedValue(body());
  history.replaceState({}, '', '/chat/conversation-a');
});
afterEach(() => history.replaceState({}, '', '/'));

it('shows an accessible animated status while a cold body is pending', () => {
  fixture.get.mockReturnValue(deferred().promise);
  render(AssistantDeliverableBlock, { item: item() });
  const status = screen.getByRole('status');
  expect(status).toHaveAttribute('aria-busy', 'true');
  expect(status).toHaveTextContent('Loading deliverable…');
  expect(status.querySelector('svg.animate-spin')).toBeTruthy();
});

it('renders immediately on A to B to A remount without a second body request or loading frame', async () => {
  const first = render(AssistantDeliverableBlock, { item: item() });
  await screen.findByRole('heading', { name: 'Reopened report' });
  first.unmount();
  history.replaceState({}, '', '/chat/conversation-b');
  fixture.get.mockResolvedValue(body('report-b', '# Other report'));
  const other = render(AssistantDeliverableBlock, { item: item('report-b') });
  await screen.findByRole('heading', { name: 'Other report' });
  other.unmount();
  history.replaceState({}, '', '/chat/conversation-a');
  render(AssistantDeliverableBlock, { item: item() });
  expect(screen.getByRole('heading', { name: 'Reopened report' })).toBeInTheDocument();
  expect(screen.queryByText('Loading deliverable…')).toBeNull();
  expect(fixture.get).toHaveBeenCalledTimes(2);
});

it('joins a read still pending when the original card was unmounted', async () => {
  const pending = deferred();
  fixture.get.mockReturnValue(pending.promise);
  const first = render(AssistantDeliverableBlock, { item: item() });
  first.unmount();
  render(AssistantDeliverableBlock, { item: item() });
  pending.resolve(body());
  await screen.findByRole('heading', { name: 'Reopened report' });
  expect(fixture.get).toHaveBeenCalledOnce();
});

it('does not reuse a body after account epoch invalidation', async () => {
  const first = render(AssistantDeliverableBlock, { item: item() });
  await screen.findByRole('heading', { name: 'Reopened report' });
  first.unmount();
  fixture.epoch++;
  fixture.listeners.forEach(callback => callback());
  fixture.get.mockReturnValue(deferred().promise);
  render(AssistantDeliverableBlock, { item: item() });
  expect(screen.queryByRole('heading', { name: 'Reopened report' })).toBeNull();
  expect(screen.getByRole('status')).toBeInTheDocument();
  expect(fixture.get).toHaveBeenCalledTimes(2);
});

it('never substitutes a conversation body for a task/custom loader authorization boundary', async () => {
  const first = render(AssistantDeliverableBlock, { item: item() });
  await screen.findByRole('heading', { name: 'Reopened report' });
  first.unmount();
  const loader = vi.fn().mockResolvedValue(body('report-a', '# Task authorized body'));
  render(AssistantDeliverableBlock, { item: item(), loadDeliverable: loader });
  await screen.findByRole('heading', { name: 'Task authorized body' });
  expect(screen.queryByRole('heading', { name: 'Reopened report' })).toBeNull();
  expect(loader).toHaveBeenCalledOnce();
});
