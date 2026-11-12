import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LibraryItem } from '$lib/library';
import ResourceReader from './ResourceReader.svelte';

const mocks = vi.hoisted(() => ({
  content: vi.fn(), context: vi.fn(), estimates: vi.fn(), create: vi.fn(), get: vi.fn(), cancel: vi.fn(),
  revisions: vi.fn(), item: vi.fn(), signedUrl: vi.fn(), textPreview: vi.fn(),
}));
vi.mock('$app/navigation', () => ({ beforeNavigate: vi.fn() }));
vi.mock('$lib/app-document-audio', () => ({ appDocumentAudioClient: mocks }));
vi.mock('$lib/api/client', () => ({ api: {
  library: { content: mocks.content, item: mocks.item },
  deliverables: { revisions: mocks.revisions },
  artifacts: { revisions: mocks.revisions, signedUrl: mocks.signedUrl, textPreview: mocks.textPreview },
} }));

function item(format: string): LibraryItem {
  return {
    id: 'dlv_11111111111111111111111111111111', kind: 'deliverable',
    title: `Library ${format} document`, format, owner_email: 'reader@test.local',
    agent_id: null, agent_name: null, project_id: null, project_name: null,
    conversation_id: null, conversation_title: null, task_id: null, task_title: null,
    lineage_id: null, revision: 1, created_at: '2026-10-25T00:00:00Z',
    updated_at: '2026-10-25T00:00:00Z', size_bytes: 100, status: 'active',
    origin: 'agent', is_readonly: false
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.revisions.mockResolvedValue({ revisions: [] });
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({
    matches: false, media: '', onchange: null, addEventListener: vi.fn(),
    removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(),
    dispatchEvent: vi.fn()
  }));
  mocks.context.mockResolvedValue({
    supported: true, can_generate: true, can_cancel: true, detail: null, jobs: []
  });
  mocks.estimates.mockResolvedValue(['shorter', 'default', 'longer'].map(length => ({
    length, target_words: 100, seconds_min: 30, seconds_max: 60
  })));
});
afterEach(() => vi.unstubAllGlobals());

describe('Library reader audio toolbar', () => {
  it.each(['deliverable', 'artifact'] as const)('uses the visible shared revision picker for a Library %s', async kind => {
    const document = { ...item('markdown'), kind };
    mocks.content.mockResolvedValue({
      deliverable_id: document.id, title: document.title, format: 'markdown',
      content: '# Reader document', metadata: {},
    });
    mocks.signedUrl.mockResolvedValue({ url: '/file.md' });
    mocks.textPreview.mockResolvedValue({ content: '# Reader document', rich_payload: null, truncated: false });
    mocks.revisions.mockResolvedValue({ revisions: [
      { deliverable_id: document.id, artifact_id: document.id, revision: 2, is_latest: true, created_at: null },
      { deliverable_id: 'older-revision', artifact_id: 'older-revision', revision: 1, is_latest: false, created_at: null },
    ] });
    const onRevision = vi.fn();
    mocks.item.mockResolvedValue({ ...document, id: 'older-revision', revision: 1 });
    render(ResourceReader, { item: { ...document, revision: 2 }, onRevision });
    const toggle = await screen.findByTestId('library-revision-toggle');
    expect(toggle.closest('.chat-document-more, .compact-header-more')).toBeNull();
    expect(screen.queryByRole('combobox', { name: 'Revision history' })).toBeNull();
    await fireEvent.click(toggle);
    const options = await screen.findAllByTestId('library-revision-option');
    await fireEvent.click(options[1]);
    await waitFor(() => expect(onRevision).toHaveBeenCalledWith(expect.objectContaining({ id: 'older-revision' })));
    expect(mocks.item).toHaveBeenCalledWith(kind, 'older-revision');
  });
  it('offers the deliverable private link in the same picker as public sharing', async () => {
    const document = item('markdown');
    mocks.content.mockResolvedValue({
      deliverable_id: document.id, format: 'markdown',
      title: document.title, content: '# Reader document', metadata: {},
    });
    render(ResourceReader, { item: document });
    await vi.dynamicImportSettled();
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await vi.dynamicImportSettled();
    const dialog = within(await screen.findByRole('dialog', { name: 'Share document' }));
    expect(dialog.getByLabelText('Link access and expiration')).toHaveValue('private');
    expect(dialog.getByRole('button', { name: 'Copy private link' })).toBeEnabled();
    expect(dialog.getByRole('option', { name: '7 days' })).toBeTruthy();
  });
  it.each(['markdown', 'plain', 'html', 'rich'])('provides one Listen control for %s', async format => {
    const document = item(format);
    mocks.content.mockResolvedValue({
      deliverable_id: document.id, title: document.title, format,
      content: 'Readable document contents.', rich_payload: { blocks: [] }
    });
    render(ResourceReader, { item: document });
    expect(screen.getAllByRole('button', { name: 'Listen to this document' })).toHaveLength(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Listen to this document' }));
    await waitFor(() => expect(mocks.context).toHaveBeenCalledWith(document.id, expect.anything()));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Generate narration' })).toBeEnabled());
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    expect(screen.getByRole('button', { name: 'Generate podcast' })).toBeEnabled();
    expect(mocks.create).not.toHaveBeenCalled();
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('button', { name: 'Listen to this document' })).toHaveAttribute('aria-expanded', 'false');
  });
});
