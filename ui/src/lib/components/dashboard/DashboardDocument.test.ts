import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkspaceManager } from '$lib/dashboard/workspace';
import type { LibraryItem } from '$lib/library';
import DashboardDocument from './DashboardDocument.svelte';
import WorkspaceLayer from './WorkspaceLayer.svelte';

const mocks = vi.hoisted(() => ({
  item: vi.fn(), content: vi.fn(), signedUrl: vi.fn(), textPreview: vi.fn(), revisions: vi.fn(),
  source: vi.fn(), sharing: vi.fn(),
}));
vi.mock('$app/navigation', () => ({ beforeNavigate: vi.fn() }));
vi.mock('./DashboardEntityModal.svelte', async () => import('./WorkspaceLayer.entity-test-fixture.svelte'));
vi.mock('./AgentQuickChatModal.svelte', async () => import('./WorkspaceLayer.entity-test-fixture.svelte'));
vi.mock('$lib/api/client', () => ({
  api: {
    library: { item: mocks.item, content: mocks.content },
    deliverables: { revisions: mocks.revisions },
    artifactDocuments: { source: mocks.source, sharing: mocks.sharing },
    artifacts: { signedUrl: mocks.signedUrl, textPreview: mocks.textPreview, revisions: mocks.revisions },
  },
}));

const item: LibraryItem = {
  id: 'art_document_revision', title: 'Canonical document.txt', kind: 'artifact', format: 'text/plain',
  owner_email: 'reader@cognis.test', agent_id: null, agent_name: null,
  project_id: null, project_name: null, conversation_id: null, conversation_title: null,
  task_id: null, task_title: null, lineage_id: 'art_document_lineage', revision: 2,
  created_at: '2026-10-17T06:00:00Z', updated_at: '2026-10-17T06:00:00Z',
  size_bytes: 128, status: 'attached', origin: 'user', is_readonly: true,
};

function setup(id = item.id) {
  const manager = new WorkspaceManager('desktop', { x: 0, y: 0, width: 1200, height: 800 });
  manager.open({ kind: 'artifact', entityId: id, title: 'Search title', canonicalHref: `/library/artifact/${id}` });
  const windowKey = manager.snapshot()[0].key;
  const view = render(DashboardDocument, { kind: 'artifact', id, manager, windowKey });
  return { manager, windowKey, view };
}

describe('dashboard canonical document reader', () => {
  beforeEach(() => {
    vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    vi.stubGlobal('MutationObserver', class { observe() {} disconnect() {} });
    mocks.item.mockReset().mockResolvedValue(item);
    mocks.signedUrl.mockReset().mockResolvedValue({ url: '/signed-document' });
    mocks.textPreview.mockReset().mockResolvedValue({ content: 'Canonical revision body', truncated: false });
    mocks.revisions.mockReset().mockResolvedValue({ revisions: [] });
    mocks.source.mockReset().mockResolvedValue({ content: 'Canonical revision body', filename: item.title });
    mocks.sharing.mockReset().mockResolvedValue({ agents: [] });
    mocks.content.mockReset().mockResolvedValue({
      deliverable_id: 'dlv_document_revision', title: 'Deliverable revision',
      format: 'markdown', content: 'Canonical deliverable body', rich_payload: null,
    });
  });

  it.each(['artifact', 'deliverable'] as const)('routes %s windows through the canonical reader in WorkspaceLayer', async (kind) => {
    const id = kind === 'artifact' ? item.id : 'dlv_document_revision';
    mocks.item.mockResolvedValue({ ...item, id, kind, format: kind === 'artifact' ? 'text/plain' : 'markdown' });
    const manager = new WorkspaceManager('desktop', { x: 0, y: 0, width: 1200, height: 800 });
    const input = { kind, entityId: id, title: 'Document', canonicalHref: `/library/${kind}/${id}` };
    manager.open(input);
    render(WorkspaceLayer, { manager, deviceClass: 'desktop' });
    const reader = await screen.findByTestId('resource-reader');
    await screen.findByText(kind === 'artifact' ? 'Canonical revision body' : 'Canonical deliverable body');
    expect(mocks.item).toHaveBeenCalledWith(kind, id);
    await manager.requestOpen(input);
    expect(manager.snapshot()).toHaveLength(1);
    expect(screen.getByTestId('resource-reader')).toBe(reader);
    if (kind === 'deliverable') expect(mocks.content).toHaveBeenCalledWith(id);
    expect(mocks.item).toHaveBeenCalledTimes(1);
  });

  it('preserves dirty edits across phone transitions and guards header, dock and Escape close paths', async () => {
    mocks.item.mockResolvedValue({ ...item, is_readonly: false });
    const manager = new WorkspaceManager('desktop', { x: 0, y: 0, width: 1200, height: 800 });
    manager.open({ kind: 'artifact', entityId: item.id, title: 'Document', canonicalHref: `/library/artifact/${item.id}` });
    const key = manager.snapshot()[0].key;
    const view = render(WorkspaceLayer, { manager, deviceClass: 'desktop' });
    await fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const name = await screen.findByRole('textbox', { name: 'Artifact name' });
    await fireEvent.input(name, { target: { value: 'Unsaved name.txt' } });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      await fireEvent.click(screen.getByTestId(`workspace-window-close-${key}`));
      await fireEvent.click(screen.getByTestId(`workspace-dock-close-${key}`));
      await fireEvent.keyDown(window, { key: 'Escape' });
      expect(confirm).toHaveBeenCalledTimes(3);
      expect(manager.snapshot()).toHaveLength(1);
      await view.rerender({ manager, deviceClass: 'phone' });
      expect(screen.getByTestId('workspace-layer').inert).toBe(true);
      await view.rerender({ manager, deviceClass: 'desktop' });
      expect(screen.getByRole('textbox', { name: 'Artifact name' })).toHaveValue('Unsaved name.txt');
      confirm.mockReturnValue(true);
      await fireEvent.click(screen.getByTestId(`workspace-dock-close-${key}`));
      expect(manager.snapshot()).toHaveLength(0);
    } finally { confirm.mockRestore(); }
  });

  it('retargets the workspace identity when the Library reader selects a revision', async () => {
    mocks.revisions.mockResolvedValue({ revisions: [
      { artifact_id: item.id, revision: 2, is_latest: true },
      { artifact_id: 'art_document_older', revision: 1, is_latest: false },
    ] });
    mocks.item.mockImplementation(async (_kind, id) => ({ ...item, id }));
    const manager = new WorkspaceManager('desktop', { x: 0, y: 0, width: 1200, height: 800 });
    manager.open({ kind: 'artifact', entityId: item.id, title: 'Document', canonicalHref: `/library/artifact/${item.id}` });
    render(WorkspaceLayer, { manager, deviceClass: 'desktop' });
    await fireEvent.click(await screen.findByTestId('library-revision-toggle'));
    const older = (await screen.findAllByTestId('library-revision-option')).find(option => option.dataset.revisionId === 'art_document_older');
    await fireEvent.click(older!);
    await waitFor(() => expect(manager.snapshot()[0]).toMatchObject({
      key: 'artifact:art_document_older', entityId: 'art_document_older',
      canonicalHref: '/library/artifact/art_document_older',
    }));
    await screen.findByTestId('resource-reader');
    expect(manager.snapshot()).toHaveLength(1);
  });

  it('blocks close while the reader is busy loading editable source', async () => {
    mocks.item.mockResolvedValue({ ...item, is_readonly: false });
    let resolve!: (value: { content: string; filename: string }) => void;
    mocks.source.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const { manager, windowKey } = setup();
    await fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    await manager.close(windowKey);
    expect(manager.snapshot()).toHaveLength(1);
    resolve({ content: 'Editable', filename: item.title! });
    await screen.findByRole('textbox', { name: 'Artifact name' });
    await manager.close(windowKey);
    expect(manager.snapshot()).toHaveLength(0);
  });

  it('resolves metadata before rendering the real Library reader, preserving the exact ID', async () => {
    const { manager } = setup();
    await screen.findByTestId('resource-reader');
    expect(mocks.item).toHaveBeenCalledWith('artifact', item.id);
    await screen.findByText('Canonical revision body');
    expect(mocks.textPreview).toHaveBeenCalledWith(item.id);
    expect(manager.snapshot()[0]).toMatchObject({ entityId: item.id, title: item.title });
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
  });

  it('fails closed on denied metadata and supports retry without loading document content', async () => {
    mocks.item.mockRejectedValueOnce(new Error('Access denied'));
    setup();
    expect(await screen.findByRole('alert')).toHaveTextContent('Access denied');
    expect(screen.queryByTestId('resource-reader')).not.toBeInTheDocument();
    expect(mocks.signedUrl).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByTestId('resource-reader');
  });

  it('ignores obsolete metadata after changing the requested revision', async () => {
    let resolve!: (value: LibraryItem) => void;
    mocks.item.mockReturnValueOnce(new Promise<LibraryItem>((done) => { resolve = done; }));
    const { manager, windowKey, view } = setup('art_old');
    await waitFor(() => expect(mocks.item).toHaveBeenCalledWith('artifact', 'art_old'));
    await view.rerender({ kind: 'artifact', id: item.id, manager, windowKey });
    await screen.findByText(item.title!);
    resolve({ ...item, id: 'art_old', title: 'Obsolete document' });
    await waitFor(() => expect(manager.snapshot()[0].title).toBe(item.title));
    expect(screen.queryByText('Obsolete document')).not.toBeInTheDocument();
  });
});
