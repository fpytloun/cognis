import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import AttentionPanel from './AttentionPanel.svelte';

afterEach(cleanup);

describe('AttentionPanel', () => {
  it('renders one weighted gate decision and forwards the optional instruction', async () => {
    const onGate = vi.fn();
    render(AttentionPanel, {
      pause: {
        pause_type: 'gate',
        step_name: 'review',
        question: 'Approve this release?',
        options: [
          { action: 'approve', label: 'Approve' },
          { action: 'reject', label: 'Reject' }
        ]
      },
      onGate,
      onQuestion: vi.fn()
    });

    expect(screen.getAllByTestId('task-attention')).toHaveLength(1);
    await fireEvent.input(screen.getByPlaceholderText('Add guidance for the next attempt.'), {
      target: { value: 'Add the rollback note.' }
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(onGate).toHaveBeenCalledWith('approve', 'Add the rollback note.');
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument();
  });

  it('preserves revise-first gate labels and their action pairs', async () => {
    const onGate = vi.fn();
    render(AttentionPanel, {
      pause: {
        pause_type: 'gate',
        step_name: 'review',
        question: 'Choose the next action.',
        options: [
          { action: 'revise(plan)', label: 'Revise the plan' },
          { action: 'continue', label: 'Accept the evidence' }
        ]
      },
      onGate,
      onQuestion: vi.fn()
    });

    expect(screen.queryByRole('button', { name: 'Approve & continue' })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Revise the plan' }));
    expect(onGate).toHaveBeenCalledWith('revise(plan)', '');
    await fireEvent.click(screen.getByRole('button', { name: 'Accept the evidence' }));
    expect(onGate).toHaveBeenLastCalledWith('continue', '');
  });
});
