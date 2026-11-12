import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AppContactView } from '$lib/app-contacts';
import AppContactChat from './AppContactChat.svelte';

const mocks = vi.hoisted(() => ({
  get: vi.fn(), reply: vi.fn(), end: vi.fn(), update: vi.fn(), confirm: vi.fn(),
}));
vi.mock('$lib/api/client', () => ({
  api: {
    appContacts: { get: mocks.get, reply: mocks.reply, end: mocks.end },
    conversations: { update: mocks.update },
  },
}));
vi.mock('$lib/stores/confirm', () => ({ confirmAction: mocks.confirm }));

function contact(): AppContactView {
  return {
    id: 'contact-binding', conversation_id: 'recipient-conversation',
    agent_name: 'Incoming agent', sender_email: 'sender@cognis.test', title: 'Friday evening',
    state: 'waiting_external', expires_at: '2036-10-28T20:00:00Z', can_reply: true,
    blocked: false, main_conversation_id: null,
    messages: [{
      id: 'delivered-message', direction: 'incoming', content: 'Public assistant answer',
      occurred_at: '2026-10-28T20:00:00Z', attachments: [],
    }],
  };
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.get.mockResolvedValue(contact());
  mocks.reply.mockResolvedValue({ status: 'accepted' });
  mocks.update.mockResolvedValue({});
  mocks.confirm.mockResolvedValue(true);
});

describe('recipient chat', () => {
  it('uses the shared normal composer and only renders delivered messages', async () => {
    const initialLoaded = vi.fn();
    render(AppContactChat, { contactId: 'contact-binding', onInitialLoaded: initialLoaded });
    await screen.findByRole('heading', { name: 'Friday evening' });
    expect(screen.getByText('Public assistant answer')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveAttribute('rows', '1');
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveAttribute('maxlength', '32000');
    expect(screen.queryByTitle('Attach file')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open conversation mode' })).not.toBeInTheDocument();
    expect(initialLoaded).toHaveBeenCalledOnce();
  });

  it('retains the normal draft and idempotency key after an ambiguous send failure', async () => {
    mocks.reply.mockRejectedValueOnce(new Error('Reply not confirmed'));
    render(AppContactChat, { contactId: 'contact-binding' });
    const composer = await screen.findByRole('textbox', { name: 'Message' });
    await fireEvent.input(composer, { target: { value: 'Tuesday works' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findByRole('alert');
    expect(composer).toHaveValue('Tuesday works');
    await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(composer).toHaveValue(''));
    expect(mocks.reply).toHaveBeenCalledTimes(2);
    expect(mocks.reply.mock.calls[0]).toEqual(mocks.reply.mock.calls[1]);
  });

  it('sends slash-prefixed participant text literally without controller autocomplete', async () => {
    render(AppContactChat, { contactId: 'contact-binding' });
    const composer = await screen.findByRole('textbox', { name: 'Message' });
    await fireEvent.input(composer, { target: { value: '/executor is just my question' } });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(mocks.reply).toHaveBeenCalledWith(
      'contact-binding', '/executor is just my question', expect.any(String),
    ));
  });

  it('renames only the recipient-owned display conversation', async () => {
    render(AppContactChat, { contactId: 'contact-binding' });
    await screen.findByRole('heading', { name: 'Friday evening' });
    await fireEvent.click(screen.getByLabelText('Conversation actions'));
    await fireEvent.click(screen.getByRole('button', { name: 'Rename conversation' }));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Conversation title' }), { target: { value: 'My evening plan' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByRole('heading', { name: 'My evening plan' });
    expect(mocks.update).toHaveBeenCalledWith('recipient-conversation', { title: 'My evening plan' });
  });

  it('keeps ended history visible without offering another send', async () => {
    mocks.get.mockResolvedValue({ ...contact(), can_reply: false, state: 'cancelled' });
    render(AppContactChat, { contactId: 'contact-binding' });
    await screen.findByRole('heading', { name: 'Friday evening' });
    expect(screen.getByText('Public assistant answer')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Message' })).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('not accepting replies');
  });
});
