import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import Harness from './DeferredViewTestHarness.svelte';

afterEach(cleanup);

function pending() {
  let ready!: (value: { ready: boolean }) => void;
  const promise = new Promise<{ ready: boolean }>(resolve => { ready = resolve; });
  return { load: vi.fn(() => promise), ready: () => ready({ ready: true }) };
}

describe('optional view loading', () => {
  it('keeps loading visible and renders the latest parent state when ready', async () => {
    const module = pending();
    const { rerender } = render(Harness, { load: module.load, message: 'Original' });
    expect(screen.getByRole('status')).toBeTruthy();
    await rerender({ load: module.load, message: 'Current' });
    module.ready();
    await screen.findByText('Current');
    expect(screen.queryByText('Original')).toBeNull();
    expect(module.load).toHaveBeenCalledTimes(1);
  });

  it('can cancel pending loading with Escape, but leaves ready view keys alone', async () => {
    const module = pending();
    const onCancel = vi.fn();
    render(Harness, { load: module.load, onCancel });
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
    module.ready();
    await screen.findByText('Ready');
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('exposes a retry after a failed import', async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error('Chunk unavailable'))
      .mockResolvedValueOnce({ ready: true });
    render(Harness, { load });
    await screen.findByRole('alert');
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('Ready');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('does not mount a resolved view after unmount', async () => {
    const module = pending();
    const { unmount, container } = render(Harness, { load: module.load });
    await waitFor(() => expect(module.load).toHaveBeenCalledTimes(1));
    unmount();
    module.ready();
    await tick();
    await tick();
    expect(container.textContent).toBe('');
  });
});
