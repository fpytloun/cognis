import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import ActivityAvatar from './ActivityAvatar.svelte';
import { conversationActivityState } from '$lib/conversation-activity';

describe('ActivityAvatar', () => {
  it('renders the running orbit despite an informational completion notification', () => {
    render(ActivityAvatar, {
      name: 'Riker',
      state: conversationActivityState({
        conversation_id: 'conversation-1',
        has_active_turn: true,
        has_unread: false,
        last_message_at: null,
        last_read_at: null,
        active_session_status: 'active',
        active_session_completion_reason: null,
        active_session_updated_at: null,
        pending_notification_types: ['managed_conversation_completed'],
        context: { type: 'web', ref: null, platform_data: { kind: 'agent_direct' }, memory_labels: {} },
      }),
    });

    expect(screen.getByTestId('activity-avatar-orbit')).toBeInTheDocument();
    expect(screen.getByLabelText('Control chat is working')).toBeInTheDocument();
    expect(screen.queryByTestId('activity-avatar-attention')).not.toBeInTheDocument();
  });

  it('uses one circular shape for the avatar and rotating ring', () => {
    render(ActivityAvatar, {
      name: 'Forge',
      class: 'h-11 w-11 rounded-xl',
      state: {
        active: true,
        background: false,
        attention: false,
        unread: false,
        error: false,
        tone: 'default',
        label: 'Control chat is working',
      },
    });

    const avatar = screen.getByTestId('activity-avatar');
    const orbit = screen.getByTestId('activity-avatar-orbit');
    expect(avatar).toHaveAttribute('data-avatar-shape', 'circle');
    expect(orbit).toHaveAttribute('data-avatar-shape', 'circle');
    expect(avatar).toHaveClass('h-11', 'w-11', 'rounded-full');
    expect(avatar).not.toHaveClass('animate-spin');
    expect(orbit).toHaveClass('activity-orbit');
    expect(orbit).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByLabelText('Control chat is working')).toBe(avatar);
  });

  it('keeps the circular shape when a caller overrides only the size', () => {
    render(ActivityAvatar, {
      name: 'Forge',
      class: 'h-5 w-5',
      active: true,
    });

    const avatar = screen.getByTestId('activity-avatar');
    expect(avatar).toHaveClass('h-5', 'w-5', 'rounded-full');
    expect(screen.getByTestId('activity-avatar-orbit')).toHaveAttribute(
      'data-avatar-shape',
      'circle',
    );
  });

  it('renders critical activity with the rose conversation-sidebar tone', () => {
    render(ActivityAvatar, {
      name: 'Forge',
      state: {
        active: true,
        background: false,
        attention: false,
        unread: false,
        error: false,
        tone: 'rose',
        label: 'Control chat is working',
      },
    });

    expect(screen.getByTestId('activity-avatar-orbit')).toHaveClass('activity-orbit--rose');
    expect(screen.getByLabelText('Control chat is working')).toBeInTheDocument();
  });
});
