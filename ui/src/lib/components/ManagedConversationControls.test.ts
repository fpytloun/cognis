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

function conversationWithParent(
  controllerConversationId: string | null | undefined,
  rootControllerConversationId: string | null | undefined,
  conversationId = 'managed-conversation',
): Conversation {
  const value = conversation('completed');
  value.conversation_id = conversationId;
  value.root_controller_conversation_id = rootControllerConversationId;
  if (value.managed_agent) {
    value.managed_agent.controller_conversation_id = controllerConversationId;
  }
  return value;
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

  it('links to the immediate controller without invoking control actions', async () => {
    const onStop = vi.fn();
    const onSend = vi.fn();
    const onTakeControl = vi.fn();
    render(ManagedConversationControls, {
      conversation: conversationWithParent('immediate/parent', 'root-parent'),
      onStop,
      onSend,
      onTakeControl,
    });

    const link = screen.getByRole('link', { name: 'Parent conversation' });
    expect(link).toHaveAttribute('href', '/chat/immediate%2Fparent');
    expect(link).not.toHaveAttribute('target');
    await fireEvent.click(link);
    expect(onStop).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(onTakeControl).not.toHaveBeenCalled();
  });

  it('falls back to the root controller when the immediate controller is absent', () => {
    render(ManagedConversationControls, {
      conversation: conversationWithParent(null, 'root-parent'),
      onStop: vi.fn(),
      onSend: vi.fn(),
      onTakeControl: vi.fn(),
    });

    expect(screen.getByRole('link', { name: 'Parent conversation' }))
      .toHaveAttribute('href', '/chat/root-parent');
  });

  it.each([
    ['both parent identifiers are absent', undefined, null],
    ['the immediate controller is the current conversation', 'managed-conversation', 'root-parent'],
    ['the root controller is the current conversation', null, 'managed-conversation'],
  ])('hides the parent link when %s', (_label, immediate, root) => {
    render(ManagedConversationControls, {
      conversation: conversationWithParent(immediate, root),
      onStop: vi.fn(),
      onSend: vi.fn(),
      onTakeControl: vi.fn(),
    });

    expect(screen.queryByRole('link', { name: 'Parent conversation' })).toBeNull();
  });
});
