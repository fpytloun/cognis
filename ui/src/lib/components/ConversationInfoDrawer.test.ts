import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ConversationInfoDrawerHarness from './ConversationInfoDrawerHarness.svelte';

const originalAnimate = Object.getOwnPropertyDescriptor(Element.prototype, 'animate');
beforeEach(() => {
  Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: vi.fn(() => ({
    cancel: vi.fn(), pause: vi.fn(), play: vi.fn(), finished: Promise.resolve(), currentTime: 0,
    effect: { getComputedTiming: () => ({ progress: 1 }) },
  })) });
});
afterEach(() => {
  cleanup();
  if (originalAnimate) Object.defineProperty(Element.prototype, 'animate', originalAnimate);
  else Reflect.deleteProperty(Element.prototype, 'animate');
});

describe('ConversationInfoDrawer', () => {
  it('preserves the same source subtree and draft across pinned/focus transitions', async () => {
    render(ConversationInfoDrawerHarness);
    const input = screen.getByRole('textbox', { name: 'Inspector draft' });
    await fireEvent.input(input, { target: { value: 'Keep the live source owner' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Expand inspector' }));
    expect(await screen.findByRole('textbox', { name: 'Inspector draft' })).toBe(input);
    await fireEvent.click(screen.getByRole('button', { name: 'Exit expanded inspector' }));
    expect(await screen.findByRole('textbox', { name: 'Inspector draft' })).toBe(input);
    expect(input).toHaveValue('Keep the live source owner');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('complementary', { name: 'Conversation information' })).toContainElement(input);
  });
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
    expect(screen.getByRole('complementary', { name: 'Conversation information' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Context' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Expand inspector' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Close conversation information' })).toBeNull();
    expect(screen.getByTestId('conversation-info-drawer')).toHaveClass('bg-transparent', 'border-l', 'border-separator');
    expect(screen.getByTestId('conversation-info-drawer')).not.toHaveClass('bg-elevated');
    expect(screen.getByRole('separator', { name: 'Resize conversation inspector' }))
      .toHaveClass('touch-resize-handle', 'touch-resize-handle--left');
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
    expect(document.querySelector('#conversation-info-drawer')).toHaveClass('bg-elevated');
    expect(document.querySelector('#conversation-info-drawer')).not.toHaveClass('bg-slate-900/95');
    expect(screen.getByTestId('sheet-header-surface')).toHaveClass('bg-elevated');
    expect(screen.getByTestId('sheet-content-surface')).toHaveClass('bg-elevated');
  });

  it('bounds the overlay to the managed viewport and keeps its content as the scroll owner', () => {
    render(ConversationInfoDrawerHarness, { presentation: 'overlay' });
    const overlay = screen.getByRole('dialog');
    const panel = screen.getByTestId('conversation-info-drawer');
    const content = screen.getByTestId('sheet-content-surface');
    const viewportStyle = overlay.getAttribute('style');
    expect(viewportStyle).toContain('--app-shell-notice-height');
    expect(viewportStyle).toContain('--app-shell-top-offset');
    expect(viewportStyle).toContain('--app-viewport-offset-top');
    expect(overlay.style.bottom).toBe('auto');
    expect(viewportStyle).toContain('--app-viewport-height');
    expect(viewportStyle).toContain('--app-shell-bottom-offset');
    expect(panel).toHaveClass('min-h-0', 'max-h-full', 'overflow-hidden');
    expect(content).toHaveClass('min-h-0', 'flex-1', 'overflow-y-auto', 'overscroll-contain');
    expect(content).toContainElement(screen.getByRole('textbox', { name: 'Inspector draft' }));
    expect(content).not.toContainElement(screen.getByTestId('sheet-header-surface'));
  });
});
