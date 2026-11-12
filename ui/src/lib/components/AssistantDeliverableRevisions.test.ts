import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';
import { observedLineageRevisions, observeLineageRevision } from '$lib/deliverable-revisions';
import type { Agent, Deliverable, DeliverableRevisions } from '$lib/types/api';
import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
import { get } from 'svelte/store';
import { now } from '$lib/stores/now';
import AssistantDeliverableBlock from './AssistantDeliverableBlock.svelte';
// Lazily imported by the component under test; load it during collection.
import '$lib/components/rich/RichDeliverable.svelte';

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  getForStepRun: vi.fn(),
  revisions: vi.fn(),
}));

vi.mock('$lib/api/client', () => ({
  api: {
    deliverables: {
      get: mocks.get,
      getForStepRun: mocks.getForStepRun,
      revisions: mocks.revisions,
    },
  },
}));

function item(deliverableId: string): AssistantDeliverableTimelineItem {
  return {
    id: `assistant-deliverable:${deliverableId}`,
    kind: 'assistant_deliverable',
    sort_key: `0001:${deliverableId}`,
    source_refs: [],
    stable: true,
    status: 'complete',
    deliverable_id: deliverableId,
    format: 'markdown',
    title: 'Report',
  };
}

function revision(id: string, revisionNumber: number, overrides: Partial<Deliverable> = {}): Deliverable {
  return {
    deliverable_id: id,
    step_run_id: null,
    version: 1,
    attempt_number: 1,
    lineage_id: 'dlv-1',
    revision: revisionNumber,
    supersedes_deliverable_id: revisionNumber > 1 ? `dlv-${revisionNumber - 1}` : null,
    latest_deliverable_id: 'dlv-3',
    latest_revision: 3,
    revision_count: 3,
    is_latest: id === 'dlv-3',
    content: `# Body of revision ${revisionNumber}`,
    format: 'markdown',
    title: 'Report',
    target: null,
    outputs: {},
    rich_payload: null,
    validation_warnings: [],
    render_metadata: {},
    export_metadata: {},
    status: 'buffered',
    evaluator_feedback: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

const history: DeliverableRevisions = {
  lineage_id: 'dlv-1',
  latest_deliverable_id: 'dlv-3',
  revisions: [
    { deliverable_id: 'dlv-3', revision: 3, title: 'Report', format: 'markdown', status: 'approved', step_run_id: 'sr-1', task_id: 'task-1', conversation_id: null, created_at: null, is_latest: true },
    { deliverable_id: 'dlv-2', revision: 2, title: 'Report', format: 'markdown', status: 'buffered', step_run_id: null, task_id: null, conversation_id: 'conv', created_at: null, is_latest: false },
    { deliverable_id: 'dlv-1', revision: 1, title: 'Report', format: 'markdown', status: 'buffered', step_run_id: null, task_id: null, conversation_id: 'conv', created_at: null, is_latest: false },
  ],
};

describe('AssistantDeliverableBlock revisions', () => {
  beforeEach(() => {
    mocks.get.mockReset();
    mocks.getForStepRun.mockReset();
    mocks.revisions.mockReset();
    mocks.revisions.mockResolvedValue(history);
    mocks.get.mockImplementation(async (id: string) => revision(id, Number(id.split('-')[1])));
    mocks.getForStepRun.mockImplementation(async (_stepRunId: string, id: string) => revision(id, 3, { step_run_id: 'sr-1' }));
    observedLineageRevisions.set({});
  });

  it('shows chat identity beside controls and retains the message date when browsing revisions', async () => {
    const timestamp = '2026-11-03T10:00:00Z';
    render(AssistantDeliverableBlock, {
      item: { ...item('dlv-3'), created_at: timestamp },
      agent: { name: 'laforge', display_name: 'LaForge', avatar_url: '/agent-avatar.png' } as Agent,
      showMessageIdentity: true,
    });
    const footer = await screen.findByTestId('chat-document-footer');
    const identity = within(footer).getByTestId('deliverable-message-identity');
    expect(within(identity).getByRole('img', { name: 'LaForge' })).toHaveAttribute('src', '/agent-avatar.png');
    expect(within(identity).getByText('LaForge')).toBeVisible();
    expect(within(identity).getByTitle(formatAbsoluteTime(timestamp))).toHaveTextContent(formatCompactTime(timestamp, new Date(get(now))));
    expect(within(footer).getByRole('navigation', { name: 'Document actions' })).toBeInTheDocument();
    await fireEvent.click(await screen.findByTestId('deliverable-revision-toggle'));
    const options = await screen.findAllByTestId('deliverable-revision-option');
    await fireEvent.click(options[1]);
    await waitFor(() => expect(screen.getByTestId('deliverable-revision-toggle')).toHaveTextContent('Revision 2 of 3'));
    expect(within(screen.getByTestId('chat-document-footer')).getByTitle(formatAbsoluteTime(timestamp))).toBeVisible();
  });

  it('uses the render-model timestamp and assistant fallback without an agent', async () => {
    const timestamp = '2026-11-02T09:00:00Z';
    render(AssistantDeliverableBlock, {
      item: { id: 'render-card', kind: 'assistant_deliverable', deliverableId: 'dlv-3', format: 'markdown', createdAt: timestamp },
      showMessageIdentity: true,
    });
    const identity = await screen.findByTestId('deliverable-message-identity');
    expect(within(identity).getByText('Assistant')).toBeVisible();
    expect(within(identity).getByTitle(formatAbsoluteTime(timestamp))).toBeVisible();
  });

  it('does not add chat identity to non-chat document entry points', async () => {
    render(AssistantDeliverableBlock, { item: item('dlv-3') });
    await screen.findByTestId('chat-document-footer');
    expect(screen.queryByTestId('deliverable-message-identity')).not.toBeInTheDocument();
  });

  it('marks a mounted card superseded when a newer revision of its lineage appears', async () => {
    const solo = { lineage_id: 'dlv-1', latest_deliverable_id: 'dlv-1', latest_revision: 1, revision_count: 1, is_latest: true };
    mocks.get.mockResolvedValueOnce(revision('dlv-1', 1, solo));
    render(AssistantDeliverableBlock, { item: { ...item('dlv-1'), lineage_id: 'dlv-1', revision: 1 } });
    expect(await screen.findByText('Body of revision 1')).toBeInTheDocument();
    expect(screen.queryByTestId('deliverable-revision-bar')).not.toBeInTheDocument();

    // A revision-2 card for the same lineage is rendered later in the timeline.
    mocks.get.mockResolvedValueOnce(revision('dlv-1', 1, { latest_deliverable_id: 'dlv-2', latest_revision: 2, revision_count: 2, is_latest: false }));
    observeLineageRevision('dlv-1', 2);

    expect(await screen.findByTestId('deliverable-superseded-notice')).toHaveTextContent('Superseded by revision 2');
    expect(mocks.get).toHaveBeenCalledTimes(2);
    // The mounted original collapses once it becomes superseded.
    await waitFor(() => expect(screen.queryByTestId('rich-deliverable-inline-document')).toBeNull());
    expect(screen.getByRole('button', { name: 'Open fullscreen' })).toBeVisible();
  });

  it('marks an older card as superseded and opens the latest revision in place', async () => {
    render(AssistantDeliverableBlock, { item: item('dlv-1') });

    expect(await screen.findByTestId('deliverable-superseded-notice')).toHaveTextContent('Superseded by revision 3');
    expect(await screen.findByTestId('deliverable-revision-toggle')).toHaveTextContent('Revision 1 of 3');

    await fireEvent.click(screen.getByTestId('deliverable-view-latest'));

    // The latest revision is a task output and must load through its step run.
    await waitFor(() => expect(mocks.getForStepRun).toHaveBeenCalledWith('sr-1', 'dlv-3'));
    await waitFor(() => expect(screen.getByTestId('deliverable-revision-toggle')).toHaveTextContent('Revision 3 of 3'));
    expect(screen.queryByTestId('deliverable-superseded-notice')).not.toBeInTheDocument();
  });

  it('browses history and renders a selected older revision', async () => {
    render(AssistantDeliverableBlock, { item: item('dlv-3') });

    const toggle = await screen.findByTestId('deliverable-revision-toggle');
    expect(toggle).toHaveTextContent('Revision 3 of 3');
    expect(screen.queryByTestId('deliverable-superseded-notice')).not.toBeInTheDocument();

    await fireEvent.click(toggle);
    const options = await screen.findAllByTestId('deliverable-revision-option');
    expect(options.map((option) => option.dataset.deliverableId)).toEqual(['dlv-3', 'dlv-2', 'dlv-1']);
    expect(mocks.revisions).toHaveBeenCalledWith('dlv-3');

    await fireEvent.click(options[1]!);
    await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('dlv-2', expect.any(Object)));
    expect(await screen.findByText('Body of revision 2')).toBeInTheDocument();
    expect(screen.getByTestId('deliverable-superseded-notice')).toHaveTextContent('Superseded by revision 3');
  });

  it('shows no revision chrome for a single-revision deliverable', async () => {
    mocks.get.mockResolvedValueOnce(
      revision('dlv-solo', 1, { lineage_id: 'dlv-solo', latest_deliverable_id: 'dlv-solo', latest_revision: 1, revision_count: 1, is_latest: true }),
    );
    render(AssistantDeliverableBlock, { item: item('dlv-solo') });

    expect(await screen.findByText('Body of revision 1')).toBeInTheDocument();
    expect(screen.queryByTestId('deliverable-revision-bar')).not.toBeInTheDocument();
    expect(mocks.revisions).not.toHaveBeenCalled();
  });

  it.each([false, true])('keeps the picker and selected revision in chat fullscreen (fullscreenOnly=%s)', async fullscreenOnly => {
    render(AssistantDeliverableBlock, { item: item('dlv-3'), fullscreenOnly });
    if (!fullscreenOnly) {
      const open = await screen.findByRole('button', { name: 'Open fullscreen' });
      open.focus();
      await fireEvent.click(open);
    }
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Report' })).not.toHaveClass('fullscreen-pending'));
    const dialog = screen.getByRole('dialog', { name: 'Report' });
    const toggle = await within(dialog).findByTestId('deliverable-revision-toggle');
    expect(toggle.closest('.chat-document-more')).toBeNull();
    await fireEvent.click(toggle);
    const options = await within(dialog).findAllByTestId('deliverable-revision-option');
    await fireEvent.click(options[1]);
    await waitFor(() => expect(within(screen.getByRole('dialog', { name: 'Report' })).getByTestId('deliverable-revision-toggle')).toHaveTextContent('Revision 2 of 3'));
    expect(within(screen.getByRole('dialog', { name: 'Report' })).getByText('Body of revision 2')).toBeInTheDocument();
    await fireEvent.click(within(screen.getByRole('dialog', { name: 'Report' })).getByTestId('deliverable-revision-toggle'));
    await within(screen.getByRole('dialog', { name: 'Report' })).findAllByTestId('deliverable-revision-option');
    await fireEvent.keyDown(within(screen.getByRole('dialog', { name: 'Report' })).getByTestId('deliverable-revision-toggle'), { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: 'Report' })).toBeInTheDocument();
    if (!fullscreenOnly) {
      await fireEvent.click(within(screen.getByRole('dialog', { name: 'Report' })).getByRole('button', { name: 'Close' }));
      await waitFor(() => expect(screen.getByRole('button', { name: 'Open fullscreen' })).toHaveFocus());
    }
  });
});
