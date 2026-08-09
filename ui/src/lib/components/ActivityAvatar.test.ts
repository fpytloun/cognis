import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import ActivityAvatar from './ActivityAvatar.svelte';

describe('ActivityAvatar', () => {
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
