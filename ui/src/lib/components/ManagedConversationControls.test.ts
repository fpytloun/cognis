import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import type { Conversation } from '$lib/types/api';
import ManagedConversationControls from './ManagedConversationControls.svelte';

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

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
  function clickWithoutBrowserNavigation(target: HTMLElement, init: MouseEventInit = {}): boolean {
    let prevented = false;
    document.addEventListener('click', event => {
      prevented = event.defaultPrevented;
      event.preventDefault();
    }, { once: true });
    target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ...init }));
    return prevented;
  }

  it('handles parent navigation in the host without changing native link semantics', () => {
    const onNavigateParent = vi.fn(() => true);
    render(ManagedConversationControls, {
      conversation: conversationWithParent('controller-root', null), onNavigateParent,
      onStop: vi.fn(), onSend: vi.fn(), onTakeControl: vi.fn(),
    });
    const parent = screen.getByRole('link', { name: 'Parent conversation' });
    expect(clickWithoutBrowserNavigation(parent)).toBe(true);
    expect(onNavigateParent).toHaveBeenCalledWith('controller-root');
    for (const init of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { button: 1 }]) {
      expect(clickWithoutBrowserNavigation(parent, init)).toBe(false);
    }
    expect(onNavigateParent).toHaveBeenCalledOnce();
  });

  it('keeps native parent navigation when the host does not own the destination', () => {
    const onNavigateParent = vi.fn(() => false);
    render(ManagedConversationControls, {
      conversation: conversationWithParent('controller-root', null), onNavigateParent,
      onStop: vi.fn(), onSend: vi.fn(), onTakeControl: vi.fn(),
    });
    expect(clickWithoutBrowserNavigation(screen.getByRole('link', { name: 'Parent conversation' }))).toBe(false);
    expect(onNavigateParent).toHaveBeenCalledWith('controller-root');
  });

  it('keeps running progress beside the turn summary until the turn ends', async () => {
    const props = { conversation: conversation('running'), onStop: vi.fn(), onSend: vi.fn(), onTakeControl: vi.fn() };
    const view = render(ManagedConversationControls, props);
    expect(screen.getByRole('status')).toHaveTextContent(/^running$/);
    await view.rerender({ ...props, conversation: { ...conversation('running'), title: 'Streaming another message' } });
    expect(screen.getByRole('status')).toHaveTextContent(/^running$/);
    await view.rerender({ ...props, conversation: conversation('idle') });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('does not describe a queued turn as running', () => {
    render(ManagedConversationControls, {
      conversation: conversation('queued'), onStop: vi.fn(), onSend: vi.fn(), onTakeControl: vi.fn(),
    });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('offers Stop while active and disables continuation controls', async () => {
    const onStop = vi.fn();
    render(ManagedConversationControls, {
      conversation: conversation('running'),
      onStop,
      onSend: vi.fn(),
      onTakeControl: vi.fn(),
    });
    expect(screen.getByRole('button', { name: 'Actions' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
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
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
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

  it('keeps context recovery available after the managed conversation closes', async () => {
    const onTakeControl = vi.fn();
    render(ManagedConversationControls, {
      conversation: conversation('completed', 'closed'),
      onStop: vi.fn(),
      onSend: vi.fn(),
      onTakeControl,
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send instruction' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Take control' })).toBeEnabled();
    await fireEvent.click(screen.getByRole('button', { name: 'Take control' }));
    expect(onTakeControl).toHaveBeenCalledOnce();
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
    expect(screen.getByRole('button', { name: 'Actions' }).parentElement).toBe(link.parentElement);
    expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument();
    expect(link).toHaveAttribute('href', '/chat/immediate%2Fparent');
    expect(link).not.toHaveAttribute('target');
    await fireEvent.click(link);
    expect(onStop).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(onTakeControl).not.toHaveBeenCalled();
  });

  it('preserves an instruction across collapse and metadata refresh but resets it for another target', async () => {
    const props = {
      conversation: conversation('completed'), onStop: vi.fn(), onSend: vi.fn(), onTakeControl: vi.fn(),
    };
    const view = render(ManagedConversationControls, props);
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Send instruction' }));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Managed instruction' }), { target: { value: 'Inspect safely' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    expect(screen.getByRole('textbox', { name: 'Managed instruction' })).toHaveValue('Inspect safely');
    await view.rerender({ ...props, conversation: { ...props.conversation } });
    expect(screen.getByRole('textbox', { name: 'Managed instruction' })).toHaveValue('Inspect safely');
    await view.rerender({ ...props, conversation: { ...props.conversation, conversation_id: 'another-managed-conversation' } });
    expect(screen.getByRole('button', { name: 'Actions' })).toHaveAttribute('aria-expanded', 'false');
    await fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Send instruction' }));
    expect(screen.getByRole('textbox', { name: 'Managed instruction' })).toHaveValue('');
  });

  it('keeps action errors visible when collapsed', () => {
    render(ManagedConversationControls, {
      conversation: conversation('completed'), onStop: vi.fn(), onSend: vi.fn(), onTakeControl: vi.fn(),
      error: 'Unable to stop the current turn',
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Unable to stop the current turn');
    expect(screen.getByRole('button', { name: 'Actions' })).toHaveAttribute('aria-expanded', 'false');
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
