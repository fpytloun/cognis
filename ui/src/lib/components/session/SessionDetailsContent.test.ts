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

  it('separates the selected runtime from the last context runtime', () => {
    render(SessionDetailsContent, {
      detail: {
        ...detail,
        runtime_selection: {
          revision: 2,
          profile_id: 'developer-deep',
          profile_source: 'session',
          model: 'gpt-6-astra',
          provider_id: 'codex',
          model_source: 'session_override',
          reasoning_effort: 'high',
          reasoning_effort_source: 'session_override',
          fast_mode: false,
          fast_mode_source: 'session_override',
        },
        context_usage: {
          model: 'gpt-5.4',
          provider_id: 'openai',
          prompt_tokens: 100,
          max_context_tokens: 1000,
          percentage: 10,
          reasoning_effort: null,
        },
      },
    });

    expect(screen.getByTestId('session-selected-runtime')).toHaveTextContent('developer-deep');
    expect(screen.getByTestId('session-selected-runtime')).toHaveTextContent(
      'codex/gpt-6-astra'
    );
    expect(screen.getByText(/Last context:/)).toHaveTextContent('gpt-5.4');
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

  it('hides lifecycle actions without eligible callbacks', () => {
    const onArchive = vi.fn();
    const onDelete = vi.fn();
    render(SessionDetailsContent, { detail, onArchive, onDelete });
    expect(screen.queryByRole('button', { name: 'Archive' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(onArchive).not.toHaveBeenCalled();
    expect(onDelete).not.toHaveBeenCalled();
  });

  it('shows archive and delete and invokes their callbacks', async () => {
    const onArchive = vi.fn();
    const onDelete = vi.fn();
    render(SessionDetailsContent, {
      detail,
      canArchive: true,
      canDelete: true,
      onArchive,
      onDelete,
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Archive' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(onArchive).toHaveBeenCalledOnce();
    expect(onDelete).toHaveBeenCalledOnce();
  });

  it('shows restore for archived conversations', async () => {
    const onRestore = vi.fn();
    render(SessionDetailsContent, {
      detail,
      canArchive: true,
      archived: true,
      onRestore,
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
    expect(onRestore).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: 'Archive' })).not.toBeInTheDocument();
  });

  it('shows busy labels and disables both lifecycle actions', () => {
    render(SessionDetailsContent, {
      detail,
      canArchive: true,
      canDelete: true,
      archiveBusy: true,
      onArchive: vi.fn(),
      onDelete: vi.fn(),
    });

    expect(screen.getByRole('button', { name: 'Archiving…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();
  });
});
