import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { documentCoordination } from '$lib/document-coordination';
import { attachmentUploads } from '$lib/chat-v2/attachment-uploads';
import { createIndexedDbChatV2Outbox, IndexedDbChatV2Outbox } from '$lib/chat-v2/outbox';
import CompactConversationChat from './CompactConversationChat.svelte';
import { DocumentDrafts } from '$lib/offline/document-drafts';

const mocks = vi.hoisted(() => ({ send: vi.fn() }));
vi.mock('$lib/api/client', () => ({
  api: { conversations: { sidebar: async () => ({ background_work: { items: [] } }) } },
}));
vi.mock('$lib/chat-v2/api', async (original) => {
  const module = await original<typeof import('$lib/chat-v2/api')>();
  return { ...module, chatV2Api: { ...module.chatV2Api, sendMessage: mocks.send } };
});
vi.mock('$lib/components/chat-v2/ScopedChatV2Timeline.svelte', async () => import('./CompactConversationChat.timeline-test-fixture.svelte'));
vi.mock('$lib/components/chat-v2/ConversationPendingInteractions.svelte', async () => import('./CompactConversationChat.interactions-test-fixture.svelte'));
vi.mock('$lib/components/chat-v2/QueuedMessagesPanel.svelte', async () => import('./CompactConversationChat.queue-test-fixture.svelte'));
vi.mock('$lib/components/chat-v2/ChatV2Composer.svelte', async () => import('./CompactConversationChat.composer-test-fixture.svelte'));

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe('compact durable submission', () => {
  beforeEach(async () => {
    localStorage.clear();
    await loginFixtureAccount();
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
    mocks.send.mockReset().mockImplementation(async (conversationId: string, id: string, body: { client_message_id: string }) => ({
      status: 'accepted', conversation_id: conversationId, client_txn_id: id, client_message_id: body.client_message_id,
      server_time: '2026-10-20T00:00:00Z',
    }));
    vi.spyOn(attachmentUploads, 'stage').mockReturnValue({
      artifact_id: 'art_synthetic', filename: 'synthetic.png', kind: 'image', mime_type: 'image/png', size_bytes: 9,
    });
  });
  afterEach(async () => { cleanup(); vi.restoreAllMocks(); await auth.clear(); });

  async function start() {
    const pending = deferred();
    const prepare = vi.spyOn(attachmentUploads, 'prepare').mockImplementation(() => pending.promise);
    const conversationId = crypto.randomUUID();
    const component = render(CompactConversationChat, { conversationId });
    await fireEvent.input(screen.getByLabelText('Composer text'), { target: { value: 'keep me' } });
    await fireEvent.click(screen.getByText('Attach fixture'));
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    await waitFor(() => expect(prepare).toHaveBeenCalledOnce());
    return { ...component, conversationId, pending, prepare };
  }

  it('persists each text-only edit with a distinct input intent', async () => {
    const conversationId = crypto.randomUUID(), drafts = new DocumentDrafts();
    const key = `cognis-chat-draft:${conversationId}`;
    render(CompactConversationChat, { conversationId });
    const input = screen.getByLabelText('Composer text');
    await fireEvent.input(input, { target: { value: 'first compact edit' } });
    await waitFor(() => expect(drafts.readComposite(key)?.text).toBe('first compact edit'));
    const revision = drafts.readComposite(key)!.input_revision;
    await fireEvent.input(input, { target: { value: 'newer compact edit' } });
    await waitFor(() => {
      const next = drafts.readComposite(key);
      expect(next?.text).toBe('newer compact edit');
      expect(next?.input_revision).not.toBe(revision);
    });
  });

  it('sends an attachment from dashboard with an untouched pinned composer open', async () => {
    const conversationId = crypto.randomUUID();
    const dashboard = render(CompactConversationChat, { conversationId });
    const pinned = render(CompactConversationChat, { conversationId });
    const inputs = screen.getAllByLabelText('Composer text');
    await fireEvent.click(screen.getAllByText('Attach fixture')[0]);
    await waitFor(() => {
      for (const refs of screen.getAllByTestId('fixture-attachments')) expect(refs).toHaveTextContent('synthetic.png');
    });
    vi.spyOn(attachmentUploads, 'prepare').mockResolvedValue();
    await fireEvent.click(screen.getAllByTestId('compact-composer-fixture')[0]);
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    for (const input of inputs) expect(input).toHaveValue('');
    for (const refs of screen.getAllByTestId('fixture-attachments')) expect(refs).toHaveTextContent('');
    expect(screen.queryByText(/Draft changed/)).toBeNull();
    expect(await createIndexedDbChatV2Outbox().list(conversationId)).toHaveLength(1);
    dashboard.unmount(); pinned.unmount();
  });

  it('sends local input despite a delayed empty draft from a sibling document', async () => {
    const conversationId = crypto.randomUUID();
    render(CompactConversationChat, { conversationId });
    await new DocumentDrafts().writeComposite(`cognis-chat-draft:${conversationId}`, {
      input_revision: crypto.randomUUID(), text: '', attachments: [], chat_mode: null,
    });
    await fireEvent.input(screen.getByLabelText('Composer text'), { target: { value: 'send my local input' } });
    vi.spyOn(attachmentUploads, 'prepare').mockResolvedValue();
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    expect(screen.queryByText(/Draft changed/)).toBeNull();
    expect(screen.getByLabelText('Composer text')).toHaveValue('');
    expect(await createIndexedDbChatV2Outbox().list(conversationId)).toHaveLength(1);
  });

  it('shares edits with a view opened during preparation and preserves newer input after send', async () => {
    const f = await start();
    const sibling = render(CompactConversationChat, { conversationId: f.conversationId });
    expect(screen.getAllByLabelText('Composer text')[1]).toHaveValue('keep me');
    await fireEvent.input(screen.getAllByLabelText('Composer text')[1], { target: { value: 'next message' } });
    f.pending.resolve();
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    for (const input of screen.getAllByLabelText('Composer text')) expect(input).toHaveValue('next message');
    sibling.unmount();
  });

  it('reserves one submission when both views send the same input concurrently', async () => {
    const conversationId = crypto.randomUUID();
    render(CompactConversationChat, { conversationId });
    render(CompactConversationChat, { conversationId });
    await fireEvent.input(screen.getAllByLabelText('Composer text')[0], { target: { value: 'send once' } });
    vi.spyOn(attachmentUploads, 'prepare').mockResolvedValue();
    const buttons = screen.getAllByTestId('compact-composer-fixture');
    await Promise.all(buttons.map((button) => fireEvent.click(button)));
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    expect(await createIndexedDbChatV2Outbox().list(conversationId)).toHaveLength(1);
    for (const input of screen.getAllByLabelText('Composer text')) expect(input).toHaveValue('');
  });

  it('retires A recovery before malformed B discovery and safely hydrates valid C', async () => {
    const a = crypto.randomUUID(), b = crypto.randomUUID(), c = crypto.randomUUID();
    const drafts = new DocumentDrafts();
    const aKey = `cognis-chat-draft:${a}`, bKey = `cognis:chat-v2-draft-attachments:${b}`;
    drafts.read(aKey); await drafts.write(aKey, 'private A recovery');
    drafts.read(bKey); await drafts.write(bKey, '{malformed');
    drafts.readComposite(`cognis-chat-draft:${c}`);
    await drafts.writeComposite(`cognis-chat-draft:${c}`, {
      input_revision: crypto.randomUUID(), text: 'valid C', attachments: [], chat_mode: 'plan',
    });
    const component = render(CompactConversationChat, { conversationId: a });
    const oldButton = await screen.findByRole('button', { name: 'Send as new' });
    await component.rerender({ conversationId: b });
    expect(screen.queryByRole('button', { name: 'Send as new' })).toBeNull();
    expect(screen.getByLabelText('Composer text')).toHaveValue('');
    await fireEvent.click(oldButton);
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    expect(mocks.send).not.toHaveBeenCalled();
    expect(new DocumentDrafts().readComposite(`cognis-chat-draft:${b}`)).toBeNull();
    expect(await createIndexedDbChatV2Outbox().list(b)).toEqual([]);
    expect(new DocumentDrafts().read(aKey)).toBe('private A recovery');
    expect(new DocumentDrafts().read(bKey)).toBe('{malformed');
    await component.rerender({ conversationId: c });
    await waitFor(() => expect(screen.getByLabelText('Composer text')).toHaveValue('valid C'));
    expect(screen.queryByRole('button', { name: 'Send as new' })).toBeNull();
  });

  for (const mode of [null, 'default', 'plan', 'build'] as const) {
    it(`sends a full-chat ${mode} draft through the actual compact caller without changing semantic mode`, async () => {
      const conversationId = crypto.randomUUID(), drafts = new DocumentDrafts();
      const input = { input_revision: crypto.randomUUID(), text: 'full input', attachments: [], chat_mode: mode };
      drafts.readComposite(`cognis-chat-draft:${conversationId}`);
      await drafts.writeComposite(`cognis-chat-draft:${conversationId}`, input);
      vi.spyOn(attachmentUploads, 'prepare').mockResolvedValue();
      render(CompactConversationChat, { conversationId });
      await waitFor(() => expect(screen.getByLabelText('Composer text')).toHaveValue('full input'));
      await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
      await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
      expect(mocks.send.mock.calls[0][2].chat_mode).toBe(mode ?? undefined);
      expect((await createIndexedDbChatV2Outbox().list(conversationId))[0].submission?.input_revision).toBe(input.input_revision);
    });
  }

  it('does not hide full-page legacy input and requires explicit choice when both sources exist', async () => {
    const conversationId = crypto.randomUUID(), drafts = new DocumentDrafts();
    const fullKey = `cognis-chat-draft:${conversationId}`, compactKey = `cognis:chat-v2-draft:${conversationId}`;
    for (const [key, text] of [[fullKey, '<b>full legacy</b>'], [compactKey, 'compact legacy']]) {
      drafts.read(key); await drafts.write(key, text);
    }
    vi.spyOn(attachmentUploads, 'prepare').mockResolvedValue();
    render(CompactConversationChat, { conversationId });
    await screen.findByRole('button', { name: 'Use full draft as new' });
    expect(screen.getByLabelText('Composer text')).toHaveValue('');
    expect(new DocumentDrafts().readComposite(fullKey)).toBeNull();
    expect(document.querySelector('b')).toBeNull();
    await fireEvent.change(screen.getByLabelText('Mode for new message'), { target: { value: 'build' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Use full draft as new' }));
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    expect(mocks.send.mock.calls[0][2]).toMatchObject({ content: '<b>full legacy</b>', chat_mode: 'build' });
    expect(new DocumentDrafts().read(fullKey)).toBe('<b>full legacy</b>');
    expect(new DocumentDrafts().read(compactKey)).toBe('compact legacy');
  });

  it('keeps legacy input unbound until explicit send-as-new confirmation', async () => {
    const conversationId = crypto.randomUUID(), key = `cognis:chat-v2-draft:${conversationId}`;
    const drafts = new DocumentDrafts();
    drafts.read(key);
    await drafts.write(key, 'legacy input');
    vi.spyOn(attachmentUploads, 'prepare').mockResolvedValue();
    render(CompactConversationChat, { conversationId });
    await screen.findByText('Recovered draft may already have been sent. Its original send identity is unavailable.');
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    expect(mocks.send).not.toHaveBeenCalled();
    expect(await createIndexedDbChatV2Outbox().list(conversationId)).toEqual([]);
    expect(new DocumentDrafts().read(key)).toBe('legacy input');
    await fireEvent.click(screen.getByRole('button', { name: 'Send as new' }));
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    const rows = await createIndexedDbChatV2Outbox().list(conversationId);
    expect(rows).toHaveLength(1);
    expect(rows[0].submission?.input.text).toBe('legacy input');
    expect(rows[0].submission?.input_revision).toBeTruthy();
    expect(new DocumentDrafts().read(key)).toBe('legacy input');
  });

  it('retains input and an inadmissible reservation during byte preparation failure', async () => {
    const f = await start();
    expect(screen.getByLabelText('Composer text')).toHaveValue('keep me');
    expect(screen.getByTestId('fixture-attachments')).toHaveTextContent('synthetic.png');
    f.pending.reject(new Error('byte commit failed'));
    await screen.findByText('byte commit failed');
    expect(screen.getByLabelText('Composer text')).toHaveValue('keep me');
    expect(screen.getByTestId('fixture-attachments')).toHaveTextContent('synthetic.png');
    expect(await createIndexedDbChatV2Outbox().list(f.conversationId)).toMatchObject([{ status: 'preparing' }]);
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it('clears committed input once and ignores duplicate clicks during preparation', async () => {
    const f = await start();
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    expect(f.prepare).toHaveBeenCalledOnce();
    f.pending.resolve();
    await waitFor(() => expect(screen.getByLabelText('Composer text')).toHaveValue(''));
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    expect(screen.getByTestId('fixture-attachments')).toHaveTextContent('');
    const rows = await createIndexedDbChatV2Outbox().list(f.conversationId);
    expect(rows).toHaveLength(1);
    expect(mocks.send).toHaveBeenCalledWith(f.conversationId, rows[0].client_txn_id,
      expect.objectContaining({ client_message_id: rows[0].client_message_id }));
    await waitFor(async () => expect((await createIndexedDbChatV2Outbox().list(f.conversationId))[0].admitted)
      .toMatchObject({ client_txn_id: rows[0].client_txn_id, client_message_id: rows[0].client_message_id }));
  });

  it('retains input if metadata commit fails after attachment preparation', async () => {
    const f = await start();
    vi.spyOn(IndexedDbChatV2Outbox.prototype, 'activate').mockRejectedValue(new Error('outbox commit failed'));
    f.pending.resolve();
    await screen.findByText('outbox commit failed');
    expect(screen.getByLabelText('Composer text')).toHaveValue('keep me');
    expect(screen.getByTestId('fixture-attachments')).toHaveTextContent('synthetic.png');
    expect(mocks.send).not.toHaveBeenCalled();
    expect(await createIndexedDbChatV2Outbox().list(f.conversationId)).toMatchObject([{ status: 'preparing' }]);
  });

  it('preserves newer edits while the captured submission commits', async () => {
    const f = await start();
    await fireEvent.input(screen.getByLabelText('Composer text'), { target: { value: 'new edits' } });
    f.pending.resolve();
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    expect(screen.getByLabelText('Composer text')).toHaveValue('new edits');
    expect(screen.getByTestId('fixture-attachments')).toHaveTextContent('synthetic.png');
  });

  it('does not clear a replacement conversation after preparation completes', async () => {
    const f = await start();
    await f.rerender({ conversationId: crypto.randomUUID() });
    await fireEvent.input(screen.getByLabelText('Composer text'), { target: { value: 'replacement' } });
    f.pending.resolve();
    await waitFor(async () => expect(await createIndexedDbChatV2Outbox().list(f.conversationId)).toHaveLength(1));
    expect(screen.getByLabelText('Composer text')).toHaveValue('replacement');
  });

  it('retains a committed row after close without projecting or admitting from the closed view', async () => {
    const f = await start();
    f.unmount();
    f.pending.resolve();
    await waitFor(async () => expect(await createIndexedDbChatV2Outbox().list(f.conversationId)).toHaveLength(1));
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it('reuses original IDs after cleanup failure and another click', async () => {
    const consume = vi.spyOn(DocumentDrafts.prototype, 'consumeComposite').mockRejectedValueOnce(new Error('cleanup failed'));
    const f = await start();
    f.pending.resolve();
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    const original = (await createIndexedDbChatV2Outbox().list(f.conversationId))[0];
    expect(screen.getByLabelText('Composer text')).toHaveValue('keep me');
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    await waitFor(() => expect(consume).toHaveBeenCalledTimes(2));
    expect(await createIndexedDbChatV2Outbox().list(f.conversationId)).toHaveLength(1);
    expect(mocks.send).toHaveBeenCalledOnce();
    expect((await createIndexedDbChatV2Outbox().list(f.conversationId))[0].client_txn_id).toBe(original.client_txn_id);
  });

  it('reopens a closed preparation with the same binding and admits once', async () => {
    const f = await start();
    const original = (await createIndexedDbChatV2Outbox().list(f.conversationId))[0];
    f.unmount();
    f.pending.resolve();
    await waitFor(async () => expect((await createIndexedDbChatV2Outbox().list(f.conversationId))[0].status).toBe('pending'));
    render(CompactConversationChat, { conversationId: f.conversationId });
    await waitFor(() => expect(screen.getByLabelText('Composer text')).toHaveValue('keep me'));
    await fireEvent.click(screen.getByTestId('compact-composer-fixture'));
    await waitFor(() => expect(screen.getByLabelText('Composer text')).toHaveValue(''));
    expect(await createIndexedDbChatV2Outbox().list(f.conversationId)).toHaveLength(1);
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    expect(mocks.send.mock.calls[0][1]).toBe(original.client_txn_id);
  });

  it('does not queue or admit after account revocation during preparation', async () => {
    const f = await start();
    await documentCoordination().invalidate();
    f.pending.resolve();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(mocks.send).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Composer text')).toHaveValue('keep me');
  });
});
