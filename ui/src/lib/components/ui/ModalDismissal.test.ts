import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import { createRawSnippet } from 'svelte';
import BlockingDialog from './BlockingDialog.svelte';
import Sheet from './Sheet.svelte';
import CreateTaskModal from '../tasks/CreateTaskModal.svelte';
import type { Agent } from '$lib/types/api';
vi.mock('$lib/api/client', () => ({ api: { workflows: { listAll: vi.fn().mockResolvedValue([]) } } }));
const children = createRawSnippet(() => ({ render: () => '<div>Modal content</div>' }));

describe('Modal backdrop policy', () => {
  it('keeps an automatically selected agent pristine, then protects task input', async () => {
    const onclose = vi.fn();
    render(CreateTaskModal, {
      agents: [{ agent_id: 'agent_task_editor', agent_type: 'primary', status: 'active', name: 'Task editor' } as Agent],
      workflows: [], projects: [], skills: [], conversations: [], onclose, oncreate: vi.fn(),
    });
    await waitFor(() => expect(screen.getByLabelText('Agent')).toHaveTextContent('Task editor'));
    const backdrop = screen.getByRole('button', { name: 'Dismiss Create task' });
    await fireEvent.click(backdrop);
    expect(onclose).toHaveBeenCalledTimes(1);
    await fireEvent.input(screen.getByLabelText('Title'), { target: { value: 'Keep draft' } });
    await fireEvent.click(backdrop);
    expect(onclose).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('Title')).toHaveValue('Keep draft');
  });
  it('dismisses clean dialogs but ignores the backdrop when edits are unsaved', async () => {
    const onClose = vi.fn();
    const view = render(BlockingDialog, { label: 'Editor', onClose, children });
    const backdrop = screen.getByRole('button', { name: 'Dismiss Editor' });
    await fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
    await view.rerender({ label: 'Editor', onClose, backdropDismissible: false });
    await fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
    // Deliberate keyboard closing still reaches the editor's discard guard.
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
    await view.rerender({ label: 'Editor', onClose, dismissible: false });
    await fireEvent.click(backdrop);
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
  it('routes backdrop dismissal only to the topmost nested overlay', async () => {
    const parentClose = vi.fn(), childClose = vi.fn();
    render(BlockingDialog, { label: 'Parent', onClose: parentClose, children });
    const parentBackdrop = screen.getByRole('button', { name: 'Dismiss Parent' });
    render(BlockingDialog, { label: 'Child', onClose: childClose, children });
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Child' })).toBeVisible());
    await fireEvent.click(parentBackdrop);
    expect(parentClose).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Dismiss Child' }));
    expect(childClose).toHaveBeenCalledTimes(1);
  });
  it('applies the same dirty guard to mobile sheets', async () => {
    const onClose = vi.fn();
    const view = render(Sheet, { open: true, label: 'Sheet editor', onClose, children, backdropDismissible: false });
    await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(onClose).not.toHaveBeenCalled();
    await view.rerender({ open: true, label: 'Sheet editor', onClose, backdropDismissible: true });
    await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
