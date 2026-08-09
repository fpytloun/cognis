import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import type { Conversation } from '$lib/types/api';
import ManagedConversationControls from './ManagedConversationControls.svelte';

function conversation(turnState: string, conversationState = 'open'): Conversation {
  return {
    conversation_id: 'managed-conversation',
    has_active_turn: turnState === 'running',
    managed_agent: {
      channel: 'agent_work',
      turn_state: turnState,
      conversation_state: conversationState,
    },
  } as Conversation;
}

describe('ManagedConversationControls', () => {
  it('offers Stop while active and disables continuation controls', async () => {
    const onStop = vi.fn();
    render(ManagedConversationControls, {
      conversation: conversation('running'),
      onStop,
      onSend: vi.fn(),
      onTakeControl: vi.fn(),
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(onStop).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send instruction' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Take control' })).toBeDisabled();
  });

  it('supports Continue, instruction, and Take control when idle', async () => {
    const onSend = vi.fn();
    const onTakeControl = vi.fn();
    render(ManagedConversationControls, {
      conversation: conversation('completed'),
      onStop: vi.fn(),
      onSend,
      onTakeControl,
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(onSend).toHaveBeenCalledWith('Continue');
    await fireEvent.click(screen.getByRole('button', { name: 'Send instruction' }));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Managed instruction' }), {
      target: { value: 'Check the result' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSend).toHaveBeenCalledWith('Check the result');
    await fireEvent.click(screen.getByRole('button', { name: 'Take control' }));
    expect(onTakeControl).toHaveBeenCalledOnce();
  });

  it('disables mutation controls after the managed conversation closes', () => {
    render(ManagedConversationControls, {
      conversation: conversation('completed', 'closed'),
      onStop: vi.fn(),
      onSend: vi.fn(),
      onTakeControl: vi.fn(),
    });
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send instruction' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Take control' })).toBeDisabled();
  });
});
