import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { QueueMessage, QueueMutationResponse } from '$lib/chat-v2/types';
import QueuedMessagesPanel from './QueuedMessagesPanel.svelte';

const mocks = vi.hoisted(() => ({
  update: vi.fn(),
  remove: vi.fn(),
  confirm: vi.fn()
}));

vi.mock('$lib/chat-v2/api', () => ({
  chatV2Api: {
    updateQueuedMessage: mocks.update,
    deleteQueuedMessage: mocks.remove
  }
}));
vi.mock('$lib/stores/confirm', () => ({ confirmAction: mocks.confirm }));

function queued(overrides: Partial<QueueMessage> = {}): QueueMessage {
  return {
    queue_id: 'queue-1',
    client_message_id: 'client-1',
    content: 'Original message',
    attachments: [],
    created_at: '2026-08-26T00:00:00Z',
    position: 1,
    ...overrides
  };
}

function response(status: 'updated' | 'deleted', messages: QueueMessage[]): QueueMutationResponse {
  return {
    conversation_id: 'conv-1',
    client_txn_id: `${status}-txn`,
    status,
    queue: { messages, queued_count: messages.length },
    cursor: null,
    runtime: null,
    server_time: '2026-08-26T00:00:01Z'
  };
}

describe('QueuedMessagesPanel', () => {
  beforeEach(() => {
    mocks.update.mockReset();
    mocks.remove.mockReset();
    mocks.confirm.mockReset();
  });

  it('cancels an edit without mutating the queued message', async () => {
    render(QueuedMessagesPanel, {
      conversationId: 'conv-1',
      messages: [queued()],
      onMutation: vi.fn()
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Edit queued message: Original message, item 1' }));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Edit queued message: Original message, item 1' }), {
      target: { value: 'Changed locally' }
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(mocks.update).not.toHaveBeenCalled();
  });

  it('preserves the editor and original server item after a failed save', async () => {
    mocks.update.mockRejectedValueOnce(new Error('The queued message is stale.'));
    render(QueuedMessagesPanel, {
      conversationId: 'conv-1',
      messages: [queued()],
      onMutation: vi.fn()
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Edit queued message: Original message, item 1' }));
    const editor = screen.getByRole('textbox', { name: 'Edit queued message: Original message, item 1' });
    await fireEvent.input(editor, { target: { value: 'Edited message' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('The queued message is stale.');
    expect(editor).toHaveValue('Edited message');
    expect(screen.getByText('Original message')).toBeInTheDocument();
  });

  it('applies a successful edit response and preserves attachments', async () => {
    const original = queued({
      attachments: [{
        artifact_id: 'artifact-1',
        kind: 'file',
        mime_type: 'text/plain',
        filename: 'note.txt',
        size_bytes: 4
      }]
    });
    const updated = queued({ content: 'Edited message', attachments: original.attachments });
    const mutation = response('updated', [updated]);
    const onMutation = vi.fn();
    mocks.update.mockResolvedValueOnce(mutation);
    render(QueuedMessagesPanel, {
      conversationId: 'conv-1',
      messages: [original],
      onMutation
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Edit queued message: Original message, item 1' }));
    await fireEvent.input(screen.getByRole('textbox'), { target: { value: ' Edited message ' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onMutation).toHaveBeenCalledWith(mutation, {
      kind: 'update',
      message: original,
      content: 'Edited message'
    }));
    expect(mocks.update).toHaveBeenCalledWith('conv-1', 'queue-1', expect.objectContaining({
      content: 'Edited message'
    }));
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('requires confirmation before deletion and applies the server queue', async () => {
    const mutation = response('deleted', []);
    const onMutation = vi.fn();
    mocks.confirm.mockResolvedValueOnce(true);
    mocks.remove.mockResolvedValueOnce(mutation);
    render(QueuedMessagesPanel, {
      conversationId: 'conv-1',
      messages: [queued()],
      onMutation
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Delete queued message: Original message, item 1' }));

    await waitFor(() => expect(onMutation).toHaveBeenCalledWith(mutation, {
      kind: 'delete',
      message: expect.objectContaining({ queue_id: 'queue-1' })
    }));
    expect(mocks.confirm).toHaveBeenCalledOnce();
    expect(mocks.remove).toHaveBeenCalledWith('conv-1', 'queue-1', expect.objectContaining({
      client_txn_id: expect.any(String)
    }));
  });

  it('expands immutable automatic continuations without exposing mutations', async () => {
    render(QueuedMessagesPanel, {
      conversationId: 'conv-1',
      compact: true,
      messages: [queued({
        content: '',
        kind: 'automatic_continuation',
        continuation_reason: 'llm_cycle_ceiling_reached'
      })],
      onMutation: vi.fn()
    });

    expect(screen.queryByRole('button', { name: 'Edit queued message' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete queued message' })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: /^Expand queued message: Continuing automatically/ }));

    expect(screen.getAllByText('Continuing automatically after the LLM cycle limit.')).toHaveLength(2);
    expect(screen.getByRole('button', { name: /^Collapse queued message: Continuing automatically/ })).toHaveAttribute('aria-expanded', 'true');
  });

  it('expands committing messages while keeping them immutable', async () => {
    render(QueuedMessagesPanel, {
      conversationId: 'conv-1',
      messages: [queued({
        content: 'Full committing message',
        status: 'committing'
      })],
      onMutation: vi.fn()
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Expand queued message: Full committing message, item 1' }));

    expect(screen.getAllByText('Full committing message')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Edit queued message' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete queued message' })).not.toBeInTheDocument();
  });

  it('keeps identical queue actions uniquely named without visible position badges', () => {
    render(QueuedMessagesPanel, {
      conversationId: 'conv-1',
      messages: [
        queued({ queue_id: 'queue-1', content: 'Repeated queued request', position: 1 }),
        queued({ queue_id: 'queue-2', content: 'Repeated queued request', position: 2 }),
      ],
      onMutation: vi.fn(),
    });

    expect(screen.getByRole('button', { name: 'Edit queued message: Repeated queued request, item 1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit queued message: Repeated queued request, item 2' })).toBeInTheDocument();
    expect(screen.queryByText('#1')).not.toBeInTheDocument();
    expect(screen.queryByText('#2')).not.toBeInTheDocument();
  });
});
