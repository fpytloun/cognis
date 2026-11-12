import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
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

  it('does not invent actions when no gate options are returned', () => {
    render(AttentionPanel, {
      pause: { pause_type: 'gate', question: 'Review the task.', options: [] },
      onGate: vi.fn(),
      onQuestion: vi.fn()
    });

    expect(screen.queryByRole('button', { name: 'Approve & continue' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reject / revise' })).not.toBeInTheDocument();
  });

  it('confirms cancellation without sending it to a replaced or unmounted pause', async () => {
    render(ConfirmDialog);
    let delivered = '';
    const onGate = (action: string) => { delivered = action; };
    const pause = {
      pause_id: 'gate-a',
      pause_type: 'gate',
      step_name: 'review',
      options: [{ label: 'Cancel task', action: 'cancel' }]
    };
    const view = render(AttentionPanel, { pause, onGate, onQuestion: vi.fn() });
    const instruction = screen.getByLabelText('Optional instruction');
    await fireEvent.input(instruction, { target: { value: 'Keep this instruction' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));
    let dialog = await screen.findByRole('dialog', { name: 'Cancel task?' });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Keep task' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(delivered).toBe('');
    expect(instruction).toHaveValue('Keep this instruction');

    await fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));
    dialog = await screen.findByRole('dialog', { name: 'Cancel task?' });
    await view.rerender({ pause: { ...pause, pause_id: 'gate-b' }, onGate, onQuestion: vi.fn() });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel task' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(delivered).toBe('');

    await fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));
    dialog = await screen.findByRole('dialog', { name: 'Cancel task?' });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel task' }));
    await waitFor(() => expect(delivered).toBe('cancel'));

    delivered = '';
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));
    dialog = await screen.findByRole('dialog', { name: 'Cancel task?' });
    await view.unmount();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel task' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(delivered).toBe('');
  });
});
