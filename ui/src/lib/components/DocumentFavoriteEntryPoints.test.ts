import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Writable } from 'svelte/store';
import { auth } from '$lib/stores/auth';
import type { Deliverable, StepRun } from '$lib/types/api';
import AssistantDeliverableBlock from './AssistantDeliverableBlock.svelte';
import MessageAttachments from './MessageAttachments.svelte';
import AttachmentPreviewModal from './AttachmentPreviewModal.svelte';
import TaskStepOutcomeEpilogue from './tasks/TaskStepOutcomeEpilogue.svelte';
import './rich/RichDeliverable.svelte';
import DocumentFavoriteIndicator from './DocumentFavoriteIndicator.svelte';
import ProjectResources from './projects/ProjectResources.svelte';
import WorkView from './work/WorkView.svelte';
import { conversationTimelineScope } from '$lib/chat-v2';
import { saveFavorite } from '$lib/document-favorites';
import type { ProjectActivity } from '$lib/types/api';

const mocks = vi.hoisted(() => ({ item: vi.fn(), favorite: vi.fn(), step: vi.fn() }));
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({ user: null }) };
});
vi.mock('$lib/api/client', () => ({
  api: {
    library: { item: mocks.item, favorite: mocks.favorite },
    deliverables: { getForStepRun: mocks.step },
    artifacts: {
      signedUrl: vi.fn().mockResolvedValue({ url: '/signed-file' }),
      textPreview: vi.fn().mockResolvedValue({ content: '# Report', truncated: false }),
    },
  },
}));
const deliverable = {
  deliverable_id: 'dlv_favorite_report', step_run_id: 'step_favorite_report', title: 'Favorite report',
  format: 'markdown', content: '# Report\n\nVerified content.', rich_payload: null,
  status: 'delivered', version: 1, outputs: {}, validation_warnings: [],
} as unknown as Deliverable;
let account = 0;
describe('Favorites at task and chat integration boundaries', () => {
  afterEach(() => vi.unstubAllGlobals());
  beforeEach(() => {
    (auth as unknown as Writable<{ user: { email: string } }>).set({ user: { email: `reader-${++account}@test.local` } });
    mocks.item.mockImplementation(async (kind: string, id: string) => ({ kind, id, lineage_id: id, is_favorite: false }));
    mocks.favorite.mockImplementation(async (kind: string, id: string, favorite: boolean) => ({ kind, id, lineage_id: id, is_favorite: favorite }));
    mocks.step.mockResolvedValue(deliverable);
    mocks.favorite.mockClear();
    mocks.item.mockClear();
  });
  async function star(kind: string, id: string) {
    const button = await screen.findByRole('button', { name: 'Add to favorites' });
    await waitFor(() => expect(button).toBeEnabled());
    expect(button).toHaveAttribute('title', 'Add to favorites');
    expect(button.querySelector('svg')).toHaveAttribute('fill', 'none');
    await fireEvent.click(button);
    await waitFor(() => expect(mocks.favorite).toHaveBeenCalledWith(kind, id, true));
    expect(await screen.findByRole('button', { name: 'Remove from favorites' })).toHaveAttribute('aria-pressed', 'true');
    expect(button.querySelector('svg')).toHaveAttribute('fill', 'currentColor');
    expect(button).toHaveAttribute('title', 'Remove from favorites');
  }
  it('updates activity star indicators from the same favorite state as preview buttons', async () => {
    render(DocumentFavoriteIndicator, { kind: 'artifact', id: 'art_activity' });
    await waitFor(() => expect(mocks.item).toHaveBeenCalledWith('artifact', 'art_activity'));
    expect(screen.queryByRole('img', { name: 'Favorite' })).toBeNull();
    await saveFavorite('artifact', 'art_activity', true);
    expect(await screen.findByRole('img', { name: 'Favorite' })).toBeVisible();
    expect(screen.queryByRole('button')).toBeNull();
    await saveFavorite('artifact', 'art_activity', false);
    await waitFor(() => expect(screen.queryByRole('img', { name: 'Favorite' })).toBeNull());
  });

  it('shows saved stars beside artifact and deliverable titles in project activity', async () => {
    vi.stubGlobal('IntersectionObserver', class {
      observe() {}
      disconnect() {}
    });
    mocks.item.mockImplementation(async (kind: string, id: string) => ({ kind, id, lineage_id: id, is_favorite: true }));
    render(ProjectResources, {
      activity: {
        artifacts: [{ artifact_id: 'art_activity', filename: 'Activity report.csv', mime_type: 'text/csv', size_bytes: 42 }],
        deliverables: [{ deliverable_id: 'dlv_activity', title: 'Activity deliverable', format: 'markdown', revision: 4 }],
      } as unknown as ProjectActivity,
      onOpen: vi.fn(),
    });
    await waitFor(() => expect(screen.getAllByRole('img', { name: 'Favorite' })).toHaveLength(2));
    expect(within(screen.getByRole('button', { name: /Activity report.csv/ })).getByRole('img', { name: 'Favorite' })).toBeVisible();
    expect(within(screen.getByRole('button', { name: /Activity deliverable/ })).getByRole('img', { name: 'Favorite' })).toBeVisible();
  });

  it('shows favorite markers in the Work artifact and deliverable activity lists', async () => {
    mocks.item.mockImplementation(async (kind: string, id: string) => ({ kind, id, lineage_id: id, is_favorite: true }));
    const scope = conversationTimelineScope('conversation-activity');
    render(WorkView, {
      scope, initialTab: 'artifacts', forceInitialTab: true, refreshIntervalMs: 0,
      refreshWork: async () => ({ status: 'accepted', work_revision: 1, graph_revision: 1, admitted: [] }),
      loadWork: async () => ({
        schema_version: 2, scope, projection_version: 'chat-v2-test', work_revision: 1,
        server_time: '2026-01-01T00:00:00Z', has_more_before: false,
        summary: { mutations: 0, commands: 0, changed_files: 0, artifacts: 1, deliverables: 1 },
        mutations: [], changed_files: [], commands: [],
        artifacts: [{ artifact_id: 'art_activity', filename: 'Activity report.csv', mime_type: 'text/csv', size_bytes: 42 }],
        deliverables: [{ deliverable_id: 'dlv_activity', title: 'Activity deliverable', content: 'Preview', format: 'markdown', display_only: true }],
      }),
    });
    expect(await screen.findByText('Activity report.csv')).toBeVisible();
    expect(await screen.findByRole('img', { name: 'Favorite' })).toBeVisible();
    await fireEvent.click(screen.getByTestId('work-tab-results'));
    const row = await screen.findByTestId('work-deliverable-dlv_activity');
    expect(await within(row).findByRole('img', { name: 'Favorite' })).toBeVisible();
  });
  it('keeps fullscreen favorites labeled inside More rather than on the toolbar', async () => {
    render(AssistantDeliverableBlock, {
      item: { id: deliverable.deliverable_id, kind: 'assistant_deliverable', deliverableId: deliverable.deliverable_id, format: deliverable.format },
      loadDeliverable: async () => deliverable,
      conversational: true,
    });
    await fireEvent.click(await screen.findByRole('button', { name: 'Open fullscreen' }));
    const dialog = within(await screen.findByRole('dialog'));
    await fireEvent.click(dialog.getByTitle('More document actions'));
    const button = await dialog.findByRole('button', { name: 'Add to favorites' });
    await waitFor(() => expect(button).toBeEnabled());
    expect(button.textContent?.trim()).toBe('Add to favorites');
    expect(button.closest('.chat-document-menu')).not.toBeNull();
    expect(button.querySelector('svg')).not.toBeNull();
    await fireEvent.click(button);
    await waitFor(() => expect(button).toHaveAttribute('aria-pressed', 'true'));
    await fireEvent.click(button);
    await waitFor(() => expect(button).toHaveAttribute('aria-pressed', 'false'));
  });
  it('stars a chat deliverable through its shared preview header', async () => {
    render(AssistantDeliverableBlock, {
      item: { id: deliverable.deliverable_id, kind: 'assistant_deliverable', deliverableId: deliverable.deliverable_id, format: deliverable.format },
      loadDeliverable: async () => deliverable,
    });
    await star('deliverable', deliverable.deliverable_id);
  });
  it('stars a task outcome through the step-scoped loader', async () => {
    render(TaskStepOutcomeEpilogue, {
      stepRun: { step_run_id: 'step_favorite_report', status: 'completed', deliverables: [deliverable] } as StepRun,
    });
    await star('deliverable', deliverable.deliverable_id);
    expect(mocks.step).toHaveBeenCalledWith('step_favorite_report', deliverable.deliverable_id);
  });
  it('keeps favorite controls out of message file attachments', () => {
    render(MessageAttachments, { attachments: [{
      artifact_id: 'art_favorite_chat', filename: 'report.md', mime_type: 'text/markdown', size_bytes: 42, kind: 'file',
    }] });
    expect(screen.queryByRole('button', { name: 'Add to favorites' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Preview report.md' })).toBeVisible();
    expect(mocks.item).not.toHaveBeenCalled();
  });
  it('keeps favorite controls out of image thumbnails', () => {
    render(MessageAttachments, { attachments: [{
      artifact_id: 'art_favorite_image', filename: 'screenshot.png', mime_type: 'image/png',
      size_bytes: 42, kind: 'image', url: '/screenshot.png',
    }] });
    expect(screen.queryByRole('button', { name: 'Add to favorites' })).toBeNull();
    expect(screen.getByRole('button', { name: 'View screenshot.png' })).toBeVisible();
    expect(mocks.item).not.toHaveBeenCalled();
  });
  it('still stars a chat attachment from the opened preview', async () => {
    render(AttachmentPreviewModal, {
      attachment: { artifact_id: 'art_favorite_chat', filename: 'report.md', mime_type: 'text/markdown', kind: 'file', size_bytes: 42 },
      kind: 'markdown', onClose: vi.fn(), resolveDownloadUrl: async () => '/signed-file',
    });
    await star('artifact', 'art_favorite_chat');
  });
  it('still stars an image after opening its lightbox', async () => {
    render(MessageAttachments, { attachments: [{
      artifact_id: 'art_favorite_image', filename: 'screenshot.png', mime_type: 'image/png',
      size_bytes: 42, kind: 'image', url: '/screenshot.png',
    }] });
    expect(screen.queryByRole('button', { name: 'Add to favorites' })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'View screenshot.png' }));
    await star('artifact', 'art_favorite_image');
  });
  it('does not offer favorites for an upload that is still local', () => {
    render(MessageAttachments, { attachments: [{
      artifact_id: 'local:upload_favorite', local_id: 'upload_favorite', upload_state: 'uploading',
      filename: 'report.md', mime_type: 'text/markdown', size_bytes: 42, kind: 'file',
    }] });
    expect(screen.queryByRole('button', { name: 'Add to favorites' })).toBeNull();
  });
});
