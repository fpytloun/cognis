import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import type { Conversation } from '$lib/types/api';
import ManagedConversationControls from './ManagedConversationControls.svelte';
import { conversationFixture } from '$lib/testing/conversation-fixture';

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
});

function channel(turnState: string, state = 'open'): Conversation {
  return conversationFixture({
    conversation_id: 'channel-conversation',
    context: { type: 'agent_work', ref: null, memory_labels: {}, platform_data: {
      managed_conversation_kind: 'channel', channel_type: 'web', participant: 'Sofia Novak',
    } },
    managed_agent: { channel: 'agent_work', kind: 'channel', turn_state: turnState, conversation_state: state },
  });
}

describe('channel owner controls', () => {
  it.each(['idle', 'waiting_controller'])('offers only instructions and termination when %s', async turnState => {
    const onSend = vi.fn();
    const onTerminate = vi.fn();
    render(ManagedConversationControls, {
      conversation: channel(turnState), onSend, onTerminate, onStop: vi.fn(), onTakeControl: vi.fn(),
    });
    expect(screen.getByTestId('managed-channel-identity')).toHaveTextContent('Web channel · Sofia Novak');
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Take control' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Send instructions' }));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Managed instruction' }), { target: { value: 'Private instruction' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSend).toHaveBeenCalledWith('Private instruction');
    await fireEvent.click(screen.getByRole('button', { name: 'Terminate conversation' }));
    expect(onTerminate).toHaveBeenCalledOnce();
  });

  it('allows termination while running but waits before another private instruction', async () => {
    render(ManagedConversationControls, {
      conversation: channel('running'), onSend: vi.fn(), onTerminate: vi.fn(), onStop: vi.fn(), onTakeControl: vi.fn(),
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    expect(screen.getByRole('button', { name: 'Send instructions' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Terminate conversation' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
  });

  it('disables both mutations after termination', async () => {
    render(ManagedConversationControls, {
      conversation: channel('cancelled', 'cancelled'), onSend: vi.fn(), onTerminate: vi.fn(), onStop: vi.fn(), onTakeControl: vi.fn(),
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    expect(screen.getByRole('button', { name: 'Send instructions' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Terminate conversation' })).toBeDisabled();
  });
});
