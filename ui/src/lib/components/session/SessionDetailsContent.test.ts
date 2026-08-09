import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SessionDetailsContent from './SessionDetailsContent.svelte';

vi.mock('$lib/api/client', () => ({
  api: { llmProviders: { codexUsage: vi.fn() } },
}));

const detail = {
  session_id: 'session-a',
  intaris_session_id: 'intaris-a',
  intention: null,
  summary: 'A long session summary that must stay on one compact line until requested.',
  status: 'active',
  total_calls: 4,
  approved_count: 3,
  denied_count: 0,
  escalated_count: 1,
};

afterEach(cleanup);

describe('SessionDetailsContent', () => {
  it('keeps the narrative collapsed until the user expands it', async () => {
    render(SessionDetailsContent, { detail });

    const narrative = screen.getByTestId('session-narrative');
    expect(narrative).not.toHaveAttribute('open');
    expect(narrative.querySelector('.truncate')).toHaveTextContent(detail.summary);

    await fireEvent.click(screen.getByText('Summary'));
    expect(narrative).toHaveAttribute('open');
    expect(narrative).toHaveTextContent(detail.summary);
  });

  it('renders Star as an icon-only action next to Open in Intaris', async () => {
    const onToggleStar = vi.fn();
    const onOpenIntaris = vi.fn();
    render(SessionDetailsContent, {
      detail,
      canStar: true,
      starred: false,
      onToggleStar,
      onOpenIntaris,
    });

    const star = screen.getByRole('button', { name: 'Star conversation' });
    expect(star).toHaveTextContent('');
    expect(screen.getByRole('button', { name: 'Open in Intaris' })).toBeTruthy();
    await fireEvent.click(star);
    expect(onToggleStar).toHaveBeenCalledOnce();
  });
});
