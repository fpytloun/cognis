import 'fake-indexeddb/auto';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi, type MockInstance } from 'vitest';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { FeedbackController } from '$lib/feedback/state';
import { documentCoordination } from '$lib/document-coordination';
import { chatV2Api } from '$lib/chat-v2/api';
import { clearWorkViewStates } from '$lib/work/workViewState';
import type { FeedbackContext, FeedbackSourceRef, ResolvedFeedbackSource } from '$lib/feedback/types';
import { FeedbackDiffRegistry } from '$lib/feedback/diff-registry';
import { inlineChrome } from '$lib/components/feedback/panel-chrome';
import { loadFeedbackController } from '$lib/feedback/controller-loader';
import WorkEventFeedbackHarness from './WorkEventFeedbackHarness.svelte';
import { context, scope, eventDiff, history, projection, resolved } from './work-event-feedback.fixture';

const api = vi.hoisted(() => ({
  resolve: vi.fn(), threads: vi.fn(), draft: vi.fn(), saveDraft: vi.fn(), createThread: vi.fn(),
}));
vi.mock('$lib/feedback/api', () => ({ feedbackApi: api }));

let saves: MockInstance<FeedbackController['save']>;
beforeEach(async () => {
  saves = vi.spyOn(FeedbackController.prototype, 'save');
  await inlineChrome.load();
  await loadFeedbackController();
  vi.resetAllMocks();
  localStorage.clear();
  clearWorkViewStates();
  await documentCoordination().initialize();
  await documentCoordination().beginLogin();
  await documentCoordination().verified('work-event-feedback@cognis.test');
  api.resolve.mockImplementation(async source => resolved(source));
  api.threads.mockResolvedValue({ items: [], next_cursor: null });
  api.draft.mockRejectedValue(Object.assign(new Error('Not found'), { status: 404 }));
  api.saveDraft.mockResolvedValue({ version: 1 });
  api.createThread.mockResolvedValue({});
  vi.spyOn(chatV2Api, 'fileHistory').mockImplementation(async request => ({
    ...history(), path_generation_id: request.path_generation_id,
    items: [eventDiff(20, request.path_generation_id === 'generation-0' ? 0 : 1),
      eventDiff(10, request.path_generation_id === 'generation-0' ? 0 : 1)],
  }));
});
const registries: FeedbackDiffRegistry[] = [];
function registry(currentContext: () => FeedbackContext = () => context) {
  const value = new FeedbackDiffRegistry(context, vi.fn(), currentContext);
  registries.push(value);
  return value;
}
afterEach(() => { cleanup(); for (const value of registries.splice(0)) value.dispose(); vi.restoreAllMocks(); });

async function mount(count = 2) {
  const view = render(WorkEventFeedbackHarness, { props: { context, scope, loadWork: vi.fn().mockResolvedValue(projection()), refreshIntervalMs: 0 } });
  await screen.findByTestId('work-files-tree');
  await fireEvent.click(screen.getByTestId('work-file-path-1'));
  await waitFor(() => expect(screen.getAllByTestId('work-event-diff')).toHaveLength(count));
  return view;
}
function event(sequence: number) {
  const container = screen.getAllByTestId('work-event-diff').find(node => node.dataset.sourceItemId === `tool-result-${sequence}`);
  if (!container) throw new Error(`Missing rendered event ${sequence}`);
  return within(container);
}
async function activate(sequence: number) {
  // Diff lines are commentable without any mode; selecting a line only needs the event's binding.
  await waitFor(() => expect(event(sequence).getByRole('button', { name: 'Select new line 1' })).toBeEnabled());
}
async function send(label = 'Send') {
  const previous = saves.mock.calls.length;
  await fireEvent.click(screen.getByRole('button', { name: label }));
  expect(saves).toHaveBeenCalledTimes(previous + 1);
  await saves.mock.results[previous].value;
  await tick();
}

it('offers no comment mode toggle in the Work toolbar', async () => {
  await mount();
  const toolbar = within(screen.getByTestId('work-toolbar'));
  expect(toolbar.queryByRole('button', { name: 'Comment' })).toBeNull();
  expect(toolbar.queryByRole('button', { name: 'Comment options' })).toBeNull();
  await activate(10);
});

it.each(['old', 'new'] as const)('activates exact paginated history events and saves %s line anchors with original ordinal', async side => {
  vi.mocked(chatV2Api.fileHistory).mockImplementation(async request => request.path_generation_id === 'generation-0'
    ? { ...history(), items: [eventDiff(20, 0)] }
    : request.before ? history(10) : history(20, true));
  await mount(1);
  await fireEvent.click(screen.getByTestId('work-file-history-more'));
  await waitFor(() => expect(screen.getAllByTestId('work-event-diff')).toHaveLength(2));
  expect(chatV2Api.fileHistory).toHaveBeenCalledWith(expect.objectContaining({ before: 'older-event-cursor' }), expect.anything());
  const sequence = side === 'old' ? 10 : 20;
  await activate(sequence);
  await fireEvent.click(event(sequence).getByRole('button', { name: `Select ${side} line 1` }));
   await fireEvent.click(await screen.findByRole('button', { name: 'Add comment' }));
   await screen.findByRole('region', { name: 'Add comment' });
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Comment' }), { target: { value: `${side} line comment` } });
  await send();
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Sending…' })).toBeNull());
  await waitFor(() => expect(api.createThread).toHaveBeenCalledWith(expect.objectContaining({
    anchor: expect.objectContaining({ source_token: `token-${sequence}-1`,
      locator: expect.objectContaining({ kind: 'diff', scope: 'lines', side, start_line: 1, end_line: 1, quote: `${side === 'old' ? 'before' : 'after'} ${sequence}` }) }),
  })));
  expect(api.resolve).toHaveBeenCalledWith(eventDiff(sequence).feedback_source, context);
});

it('saves a whole-event anchor without using combined file authority', async () => {
  await mount();
  await activate(10);
  await fireEvent.click(event(10).getByTestId('work-event-feedback'));
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Comment' }), { target: { value: 'Whole exact event' } });
  await send();
  await waitFor(() => expect(api.createThread).toHaveBeenCalledTimes(1));
  expect(api.createThread.mock.calls[0][0].anchor).toMatchObject({ source_token: 'token-10-1',
    locator: { kind: 'diff', scope: 'file', side: null } });
  for (const sequence of [10, 20]) {
    expect(api.resolve).toHaveBeenCalledWith(eventDiff(sequence).feedback_source, context);
  }
});

it('uses the other file’s canonical ordinal rather than its rendered event index', async () => {
  await mount();
  await fireEvent.click(screen.getByTestId('work-file-path-0'));
  await waitFor(() => expect(api.resolve).toHaveBeenCalledWith(eventDiff(10, 0).feedback_source, context));
  await activate(10);
  await fireEvent.click(event(10).getByTestId('work-event-feedback'));
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Comment' }), { target: { value: 'First file event' } });
  await send();
  await waitFor(() => expect(api.createThread).toHaveBeenCalledWith(expect.objectContaining({
    anchor: expect.objectContaining({ source_token: 'token-10-0' }),
  })));
});

it('never offers activation or resolves a source for display-only history without feedback_source', async () => {
  vi.mocked(chatV2Api.fileHistory).mockImplementation(async request => ({
    ...history(), path_generation_id: request.path_generation_id,
    items: [{ ...eventDiff(10), feedback_source: null }],
  }));
  render(WorkEventFeedbackHarness, { props: { context, scope,
    loadWork: vi.fn().mockResolvedValue(projection()), refreshIntervalMs: 0 } });
  await screen.findByTestId('work-event-diff');
  expect(screen.queryByTestId('work-event-feedback')).toBeNull();
  expect(api.resolve).not.toHaveBeenCalled();
});

it('uses shared comment mode and preserves exact event drafts and uncertain saves across switching', async () => {
  await mount();
  await waitFor(() => expect(api.resolve).toHaveBeenCalledWith(eventDiff(20).feedback_source, context));
  await activate(10);
  expect(event(20).getByTestId('work-event-feedback')).toBeEnabled();
  await fireEvent.click(event(10).getByTestId('work-event-feedback'));
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Comment' }), { target: { value: 'Retain original uncertain comment' } });
  api.createThread.mockRejectedValueOnce(new TypeError('Response lost'));
  await send();
  await screen.findByRole('button', { name: 'Retry original send' });
  const original = structuredClone(api.createThread.mock.calls[0][0]);
  await activate(20);
  await fireEvent.click(event(20).getByTestId('work-event-feedback'));
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Comment' }), { target: { value: 'Separate event draft' } });
  // Returning to the event reopens its unconfirmed send for an idempotent retry.
  await fireEvent.click(event(10).getByTestId('work-event-feedback'));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Comment' })).toHaveValue('Retain original uncertain comment'));
  await send('Retry original send');
  await waitFor(() => expect(api.createThread).toHaveBeenCalledTimes(2));
  expect(api.createThread.mock.calls[1][0]).toEqual(original);
  await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Comment' })).toBeNull());
  await fireEvent.click(event(20).getByTestId('work-event-feedback'));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Comment' })).toHaveValue('Separate event draft'));
});

it('shows pending resolution and denied capabilities without entering Comment mode', async () => {
  let release!: (source: ResolvedFeedbackSource) => void;
  api.resolve.mockImplementation((source: FeedbackSourceRef) => source.kind === 'work_diff' && source.source_event.seq === 10
    ? new Promise<ResolvedFeedbackSource>(resolve => { release = resolve; }) : Promise.resolve(resolved(source)));
  const value = registry();
  const activation = value.activate(eventDiff(10).feedback_source!);
  await waitFor(() => expect(release).toBeTypeOf('function'));
  expect(get(value.activationStatus)).toBe('Loading feedback for this file change…');
  release(resolved(eventDiff(10).feedback_source!, false));
  await activation;
  expect(get(value.activationStatus)).toBe('Commenting is not permitted for this file change.');
  expect(value.binding(eventDiff(10).feedback_source!)?.mode).toBe('view');
  expect(api.createThread).not.toHaveBeenCalled();
});

it('does not let a deferred earlier activation replace the selected event', async () => {
  let release!: (source: ResolvedFeedbackSource) => void;
  api.resolve.mockImplementation((source: FeedbackSourceRef) => source.kind === 'work_diff' && source.source_event.seq === 10
    ? new Promise<ResolvedFeedbackSource>(resolve => { release = resolve; }) : Promise.resolve(resolved(source)));
  const value = registry();
  const earlier = value.activate(eventDiff(10).feedback_source!);
  await waitFor(() => expect(release).toBeTypeOf('function'));
  await value.activate(eventDiff(20).feedback_source!);
  release(resolved(eventDiff(10).feedback_source!));
  await earlier;
  await waitFor(() => expect(api.threads).toHaveBeenCalledWith('subject-10-1'));
  const active = get(value.active)!;
  expect(get(active.state).source?.subject_id).toBe('subject-20-1');
  expect(value.binding(eventDiff(10).feedback_source!)?.mode).toBe('view');
});

it.each(['account', 'context', 'dispose'])('fences deferred activation after %s replacement', async replacement => {
  let release!: (source: ResolvedFeedbackSource) => void;
  api.resolve.mockImplementation((source: FeedbackSourceRef) => source.kind === 'work_diff' && source.source_event.seq === 10
    ? new Promise<ResolvedFeedbackSource>(resolve => { release = resolve; }) : Promise.resolve(resolved(source)));
  let current: FeedbackContext = context;
  const value = registry(() => current);
  const onCommenting = vi.fn();
  const activation = value.activate(eventDiff(10).feedback_source!, onCommenting);
  await waitFor(() => expect(release).toBeTypeOf('function'));
  if (replacement === 'account') await documentCoordination().beginLogin();
  if (replacement === 'context') current = { kind: 'chat', conversation_id: 'replacement-context' };
  if (replacement === 'dispose') value.dispose();
  release(resolved(eventDiff(10).feedback_source!));
  await activation;
  expect(onCommenting).not.toHaveBeenCalled();
  expect(api.createThread).not.toHaveBeenCalled();
});

it('retains exact-event resolution failures and permits explicit retry', async () => {
  api.resolve.mockRejectedValue(new Error('Exact source unavailable'));
  const value = registry();
  await value.activate(eventDiff(10).feedback_source!);
  const active = get(value.active)!;
  expect(get(active.state).error).toBe('Exact source unavailable');
  expect(value.binding(eventDiff(10).feedback_source!)).toBeNull();
  api.resolve.mockImplementation(async source => resolved(source));
  await value.activate(eventDiff(10).feedback_source!);
  expect(value.binding(eventDiff(10).feedback_source!)?.source.subject_id).toBe('subject-10-1');
});

it('suppresses new binding and activation after account invalidation without throwing or resolving', async () => {
  const registry = new FeedbackDiffRegistry(context, vi.fn());
  await documentCoordination().beginLogin();
  expect(() => registry.binding(eventDiff(5).feedback_source)).not.toThrow();
  await expect(registry.activate(eventDiff(5).feedback_source!)).resolves.toBeUndefined();
  await Promise.resolve();
  expect(api.resolve).not.toHaveBeenCalled();
  expect(registry.binding(eventDiff(5).feedback_source)).toBeNull();
  registry.dispose();
});

it('renders new history after account invalidation without reconstructing comment authority', async () => {
  vi.mocked(chatV2Api.fileHistory).mockImplementation(async request => ({
    ...history(20, !request.before), path_generation_id: request.path_generation_id,
    items: request.before ? [eventDiff(5)] : [eventDiff(20), eventDiff(10)],
  }));
  await mount();
  const calls = api.resolve.mock.calls.length;
  await documentCoordination().beginLogin();
  await fireEvent.click(screen.getByTestId('work-file-history-more'));
  await waitFor(() => expect(event(5).getByText('after 5')).toBeInTheDocument());
  expect(event(5).queryByRole('button', { name: 'Select new line 1' })).toBeNull();
  expect(event(5).queryByRole('button', { name: 'Comment on file' })).toBeNull();
  expect(api.resolve).toHaveBeenCalledTimes(calls);
});
