import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ConversationInfoDrawerHarness from './ConversationInfoDrawerHarness.svelte';

afterEach(cleanup);

describe('ConversationInfoDrawer', () => {
  it('enters focus mode and restores focus to the equivalent expand control', async () => {
    render(ConversationInfoDrawerHarness);
    const expand = screen.getByRole('button', { name: 'Expand inspector' });
    await fireEvent.click(expand);
    const exit = await screen.findByRole('button', { name: 'Exit expanded inspector' });
    await fireEvent.click(exit);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Expand inspector' })).toHaveFocus());
  });

  it('keeps an accessible Context header and inspector controls in the pinned drawer', () => {
    render(ConversationInfoDrawerHarness);
    expect(screen.getByRole('heading', { name: 'Context' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Expand inspector' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Close conversation information' })).toBeNull();
    expect(screen.getByTestId('conversation-info-drawer').className).toContain('bg-transparent');
  });

  it('cleans pointer listeners and pending animation frames when destroyed during resize', async () => {
    const cancel = vi.spyOn(window, 'cancelAnimationFrame');
    const handle = HTMLElement.prototype as HTMLElement & {
      setPointerCapture: (id: number) => void;
      hasPointerCapture: (id: number) => boolean;
      releasePointerCapture: (id: number) => void;
    };
    Object.defineProperties(handle, {
      setPointerCapture: { configurable: true, value: vi.fn() },
      hasPointerCapture: { configurable: true, value: vi.fn(() => true) },
      releasePointerCapture: { configurable: true, value: vi.fn() },
    });
    const setCapture = vi.mocked(handle.setPointerCapture);
    const hasCapture = vi.mocked(handle.hasPointerCapture);
    const releaseCapture = vi.mocked(handle.releasePointerCapture);
    const view = render(ConversationInfoDrawerHarness);
    const separator = screen.getByRole('separator', { name: 'Resize conversation inspector' });
    await fireEvent.pointerDown(separator, { pointerId: 7, clientX: 700 });
    await fireEvent.pointerMove(separator, { pointerId: 7, clientX: 650 });
    view.unmount();
    expect(cancel).toHaveBeenCalled();
    expect(setCapture).toHaveBeenCalledWith(7);
    expect(hasCapture).toHaveBeenCalledWith(7);
    expect(releaseCapture).toHaveBeenCalledWith(7);
    cancel.mockRestore();
    delete (HTMLElement.prototype as Partial<HTMLElement>).setPointerCapture;
    delete (HTMLElement.prototype as Partial<HTMLElement>).hasPointerCapture;
    delete (HTMLElement.prototype as Partial<HTMLElement>).releasePointerCapture;
  });

  it('shows only Close in overlay presentation', () => {
    render(ConversationInfoDrawerHarness, { presentation: 'overlay' });
    expect(screen.getByRole('button', { name: 'Close conversation information' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Expand inspector' })).toBeNull();
    expect(screen.getByTestId('conversation-info-header')).toHaveClass('min-w-0');
  });
});
