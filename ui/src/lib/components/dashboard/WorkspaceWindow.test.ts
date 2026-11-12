import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { WorkspaceManager } from '$lib/dashboard/workspace';
import WorkspaceWindowFixture from './WorkspaceWindow.test-fixture.svelte';

const safe = { x: 100, y: 50, width: 1000, height: 700 };

describe('WorkspaceWindow pointer controls', () => {
  beforeAll(() => {
    Object.defineProperty(HTMLElement.prototype, 'setPointerCapture', {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(HTMLElement.prototype, 'releasePointerCapture', {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(HTMLElement.prototype, 'hasPointerCapture', {
      configurable: true,
      value: () => true,
    });
  });

  function setup() {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open({
      kind: 'conversation',
      entityId: 'conversation-1',
      title: 'Conversation',
      canonicalHref: '/chat/conversation-1',
    });
    const windowState = get(manager)[0];
    const component = render(WorkspaceWindowFixture, { window: windowState, manager });
    return { manager, windowState, component };
  }

  it.each(['mouse', 'touch'])('drags with %s pointer capture and keeps page scroll unchanged', async (pointerType) => {
    const { manager, windowState } = setup();
    const handle = screen.getByTestId(`workspace-window-drag-${windowState.key}`);
    const pageScroll = window.scrollY;
    await fireEvent.pointerDown(handle, {
      pointerId: 7, pointerType, button: 0, clientX: 300, clientY: 200,
    });
    await fireEvent.pointerMove(handle, {
      pointerId: 7, pointerType, clientX: 360, clientY: 240,
    });
    await fireEvent.pointerUp(handle, { pointerId: 7, pointerType });
    expect(get(manager)[0].rect.x).toBe(windowState.rect.x + 60);
    expect(get(manager)[0].rect.y).toBe(windowState.rect.y + 40);
    expect(HTMLElement.prototype.setPointerCapture).toHaveBeenCalledWith(7);
    expect(window.scrollY).toBe(pageScroll);
  });

  it.each(['up', 'cancel', 'blur', 'lostcapture'] as const)(
    'flushes final pending movement on %s and releases capture without reentrant commit', async (ending) => {
      const frames = new Map<number, FrameRequestCallback>();
      const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
        frames.set(1, callback);
        return 1;
      });
      const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((id) => { frames.delete(id); });
      try {
        const { manager, windowState } = setup();
        const handle = screen.getByTestId(`workspace-window-drag-${windowState.key}`);
        const panel = screen.getByTestId(`workspace-window-${windowState.key}`);
        const preview = vi.spyOn(manager, 'finishInteraction');
        await fireEvent.pointerDown(handle, { pointerId: 17, button: 0, clientX: 400, clientY: 200 });
        const publication = vi.fn();
        const stop = manager.subscribe(publication);
        publication.mockClear();
        await fireEvent.pointerMove(handle, { pointerId: 17, clientX: 420, clientY: 210 });
        await fireEvent.pointerMove(handle, { pointerId: 17, clientX: 460, clientY: 240 });
        expect(raf).toHaveBeenCalledTimes(1);
        expect(publication).not.toHaveBeenCalled();
        expect(panel).toHaveStyle({ left: `${windowState.rect.x}px` });
        const release = vi.spyOn(HTMLElement.prototype, 'releasePointerCapture').mockImplementation(() => {
          handle.dispatchEvent(new PointerEvent('lostpointercapture', { pointerId: 17, bubbles: true }));
        });
        if (ending === 'up') await fireEvent.pointerUp(handle, { pointerId: 17 });
        if (ending === 'cancel') await fireEvent.pointerCancel(handle, { pointerId: 17 });
        if (ending === 'blur') await fireEvent(window, new Event('blur'));
        if (ending === 'lostcapture') await fireEvent(handle, new PointerEvent('lostpointercapture', { pointerId: 17 }));
        expect(get(manager)[0].rect.x).toBe(windowState.rect.x + 60);
        expect(get(manager)[0].rect.y).toBe(windowState.rect.y + 40);
        expect(publication).toHaveBeenCalledTimes(1);
        expect(preview).toHaveBeenCalledTimes(1);
        expect(frames.size).toBe(0);
        expect(panel).toHaveAttribute('data-interacting', 'false');
        release.mockRestore();
        stop();
      } finally {
        raf.mockRestore();
        cancel.mockRestore();
      }
    },
  );

  it('paints one coalesced preview frame without publication and reconciles safe bounds', async () => {
    let queued: FrameRequestCallback | undefined;
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      queued = callback;
      return 23;
    });
    const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
    try {
      const { manager, windowState } = setup();
      const handle = screen.getByTestId(`workspace-window-drag-${windowState.key}`);
      const panel = screen.getByTestId(`workspace-window-${windowState.key}`);
      await fireEvent.pointerDown(handle, { pointerId: 21, button: 0, clientX: 400, clientY: 200 });
      const publication = vi.fn();
      const stop = manager.subscribe(publication);
      publication.mockClear();
      await fireEvent.pointerMove(handle, { pointerId: 21, clientX: 430, clientY: 220 });
      await fireEvent.pointerMove(handle, { pointerId: 21, clientX: 450, clientY: 230 });
      expect(raf).toHaveBeenCalledTimes(1);
      queued!(0);
      await tick();
      expect(panel).toHaveStyle({ left: `${windowState.rect.x + 50}px`, top: `${windowState.rect.y + 30}px` });
      expect(publication).not.toHaveBeenCalled();
      manager.setSafeRect({ x: 100, y: 50, width: 400, height: 350 });
      await tick();
      publication.mockClear();
      queued!(1);
      await tick();
      expect(panel).toHaveStyle({ left: '100px', top: '50px', width: '400px', height: '350px' });
      expect(publication).not.toHaveBeenCalled();
      await fireEvent.pointerUp(handle, { pointerId: 21 });
      expect(publication).toHaveBeenCalledTimes(1);
      stop();
    } finally {
      raf.mockRestore();
      cancel.mockRestore();
    }
  });

  it.each(['unmount', 'minimize', 'close'] as const)('abandons pending frames on %s', async (action) => {
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(19);
    const cancel = vi.spyOn(window, 'cancelAnimationFrame');
    try {
      const { manager, windowState, component } = setup();
      const handle = screen.getByTestId(`workspace-window-drag-${windowState.key}`);
      await fireEvent.pointerDown(handle, { pointerId: 18, button: 0, clientX: 400, clientY: 200 });
      await fireEvent.pointerMove(handle, { pointerId: 18, clientX: 450, clientY: 230 });
      const finish = vi.spyOn(manager, 'finishInteraction');
      if (action === 'unmount') component.unmount();
      if (action === 'minimize') manager.minimize(windowState.key);
      if (action === 'close') await manager.close(windowState.key);
      await waitFor(() => expect(cancel).toHaveBeenCalledWith(19));
      expect(finish).not.toHaveBeenCalled();
      if (action !== 'close') expect(get(manager)[0].rect).toEqual(windowState.rect);
      else expect(get(manager)).toHaveLength(0);
    } finally {
      raf.mockRestore();
      cancel.mockRestore();
    }
  });

  it('resizes through a border-corner handle without covering window content', async () => {
    const { manager, windowState } = setup();
    const windowElement = screen.getByTestId(`workspace-window-${windowState.key}`);
    const grip = screen.getByTestId(`workspace-window-resize-${windowState.key}`);
    expect(grip).toHaveClass('h-6', 'w-6');
    expect(grip).toHaveStyle({ minWidth: '24px', minHeight: '24px' });
    await fireEvent.pointerDown(grip, {
      pointerId: 9, pointerType: 'mouse', button: 0, clientX: 900, clientY: 600,
    });
    expect(windowElement).toHaveAttribute('data-interacting', 'true');
    await fireEvent.pointerMove(grip, {
      pointerId: 9, pointerType: 'mouse', clientX: -1000, clientY: -1000,
    });
    await fireEvent.pointerUp(grip, { pointerId: 9, pointerType: 'mouse' });
    expect(windowElement).toHaveAttribute('data-interacting', 'false');
    expect(get(manager)[0].rect).toMatchObject({ width: 360, height: 320 });
  });

  it('raises z-order without stealing focus from interactive child content', async () => {
    setup();
    const input = screen.getByTestId('workspace-child-state');
    await fireEvent.pointerDown(input, { pointerId: 11, pointerType: 'mouse', button: 0 });
    input.focus();
    await waitFor(() => expect(document.activeElement).toBe(input));
    await Promise.resolve();
    expect(document.activeElement).toBe(input);
  });

  it('minimizes through the window control', async () => {
    const { manager, windowState } = setup();
    await fireEvent.click(screen.getByTestId(`workspace-window-minimize-${windowState.key}`));
    expect(get(manager)[0].minimized).toBe(true);
  });

  it('preserves child identity and paints an opaque transform-only restore', async () => {
    const { manager, windowState } = setup();
    const windowElement = screen.getByTestId(`workspace-window-${windowState.key}`);
    const child = screen.getByTestId('workspace-child-state');

    manager.minimize(windowState.key);
    await waitFor(() => expect(windowElement).toHaveAttribute('data-minimized', 'true'));
    manager.restore(windowState.key);

    await waitFor(() => {
      expect(windowElement).toHaveAttribute('data-minimized', 'false');
      expect(windowElement).toHaveAttribute('data-restoring', 'true');
    });
    expect(screen.getByTestId('workspace-child-state')).toBe(child);
    expect(windowElement).toHaveStyle({ visibility: 'visible' });
    expect(windowElement).toHaveAttribute('data-restoring', 'true');

    const animationEnd = new Event('animationend', { bubbles: true });
    Object.defineProperty(animationEnd, 'animationName', { value: 'workspace-window-restore' });
    windowElement.dispatchEvent(animationEnd);
    await waitFor(() => expect(windowElement).toHaveAttribute('data-restoring', 'false'));
    expect(screen.getByTestId('workspace-child-state')).toBe(child);
  });

  it('keeps a rapid restore then minimize hidden', async () => {
    const { manager, windowState } = setup();
    const windowElement = screen.getByTestId(`workspace-window-${windowState.key}`);

    manager.minimize(windowState.key);
    await waitFor(() => expect(windowElement).toHaveAttribute('data-minimized', 'true'));
    manager.restore(windowState.key);
    await waitFor(() => expect(windowElement).toHaveAttribute('data-restoring', 'true'));
    manager.minimize(windowState.key);

    await waitFor(() => {
      expect(windowElement).toHaveAttribute('data-minimized', 'true');
      expect(windowElement).toHaveAttribute('data-opening', 'false');
      expect(windowElement).toHaveAttribute('data-restoring', 'false');
    });
    expect(windowElement).toHaveStyle({ pointerEvents: 'none' });
  });

  it('keeps the full-page link before compact square controls with close at the edge', () => {
    const { windowState } = setup();
    const header = screen.getByTestId(`workspace-window-drag-${windowState.key}`);
    const external = screen.getByTestId(`workspace-window-external-${windowState.key}`);
    const layout = screen.getByTestId(`workspace-window-menu-${windowState.key}`);
    const minimize = screen.getByTestId(`workspace-window-minimize-${windowState.key}`);
    const close = screen.getByTestId(`workspace-window-close-${windowState.key}`);

    expect([...header.children].slice(-4)).toEqual([
      external,
      layout.parentElement,
      minimize,
      close,
    ]);
    expect(layout).toHaveClass('rounded-lg', 'h-8', 'w-8', 'bg-success-subtle');
    expect(layout).not.toHaveClass('rounded-full');
    expect(minimize).toHaveClass('rounded-lg', 'bg-warning-subtle');
    expect(close).toHaveClass('rounded-lg', 'bg-danger-subtle');
  });

  it('places the conversation inspector control before the full-page action', async () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open({
      kind: 'conversation',
      entityId: 'header-inspector',
      title: 'Conversation',
      canonicalHref: '/chat/header-inspector',
    });
    const windowState = get(manager)[0];
    const onToggleInspector = vi.fn();
    render(WorkspaceWindowFixture, {
      window: windowState,
      manager,
      inspectorExpanded: true,
      inspectorControlsId: 'dashboard-conversation-inspector-header-inspector',
      onToggleInspector,
    });

    const header = screen.getByTestId(`workspace-window-drag-${windowState.key}`);
    const inspector = screen.getByTestId(`workspace-window-inspector-${windowState.key}`);
    const external = screen.getByTestId(`workspace-window-external-${windowState.key}`);
    expect([...header.children].indexOf(inspector)).toBe([...header.children].indexOf(external) - 1);
    expect(inspector).toHaveAttribute('aria-expanded', 'true');
    expect(inspector).toHaveAttribute('aria-controls', 'dashboard-conversation-inspector-header-inspector');
    await fireEvent.click(inspector);
    expect(onToggleInspector).toHaveBeenCalledOnce();
  });

  it('does not render an inspector control without a conversation callback', () => {
    const { windowState } = setup();
    expect(screen.queryByTestId(`workspace-window-inspector-${windowState.key}`)).not.toBeInTheDocument();
  });

  it('uses compact 40px tablet window controls', () => {
    const manager = new WorkspaceManager('tablet', safe);
    manager.open({
      kind: 'conversation',
      entityId: 'tablet',
      title: 'Tablet',
      canonicalHref: '/chat/tablet',
    });
    const windowState = get(manager)[0];
    render(WorkspaceWindowFixture, { window: windowState, manager, largeControls: true });

    expect(screen.getByTestId(`workspace-window-menu-${windowState.key}`))
      .toHaveClass('h-[40px]', 'w-[40px]', 'rounded-lg');
    expect(screen.getByTestId(`workspace-window-close-${windowState.key}`))
      .toHaveClass('h-[40px]', 'w-[40px]', 'rounded-lg');
    expect(screen.getByTestId(`workspace-window-drag-${windowState.key}`))
      .toHaveClass('min-h-[40px]');
    expect(screen.getByTestId(`workspace-window-drag-${windowState.key}`))
      .not.toHaveClass('min-h-14');
  });

  it('applies an edge snap on pointer release', async () => {
    const { manager, windowState } = setup();
    const handle = screen.getByTestId(`workspace-window-drag-${windowState.key}`);
    await fireEvent.pointerDown(handle, {
      pointerId: 12, pointerType: 'mouse', button: 0, clientX: 400, clientY: 200,
    });
    await fireEvent.pointerMove(handle, {
      pointerId: 12, pointerType: 'mouse', clientX: safe.x + 10, clientY: safe.y + 100,
    });
    await fireEvent.pointerUp(handle, { pointerId: 12, pointerType: 'mouse' });
    expect(get(manager)[0].rect).toEqual({
      x: safe.x,
      y: safe.y,
      width: safe.width / 2,
      height: safe.height,
    });
    expect(get(manager)[0].restoreRect).toEqual({
      ...windowState.rect,
      x: safe.x,
      y: safe.y + 20,
    });

    await fireEvent.pointerDown(handle, {
      pointerId: 14, pointerType: 'mouse', button: 0, clientX: 250, clientY: 100,
    });
    await fireEvent.pointerMove(handle, {
      pointerId: 14, pointerType: 'mouse', clientX: 310, clientY: 160,
    });
    await fireEvent.pointerUp(handle, { pointerId: 14, pointerType: 'mouse' });
    expect(get(manager)[0].restoreRect).toBeNull();
    expect(get(manager)[0].rect.width).toBe(windowState.rect.width);
  });

  it('does not snap when the pointer interaction is cancelled', async () => {
    const { manager, windowState } = setup();
    const handle = screen.getByTestId(`workspace-window-drag-${windowState.key}`);
    await fireEvent.pointerDown(handle, {
      pointerId: 13, pointerType: 'mouse', button: 0, clientX: 400, clientY: 200,
    });
    await fireEvent.pointerMove(handle, {
      pointerId: 13, pointerType: 'mouse', clientX: safe.x + 10, clientY: safe.y + 100,
    });
    await fireEvent.pointerCancel(handle, { pointerId: 13, pointerType: 'mouse' });
    expect(get(manager)[0].restoreRect).toBeNull();
  });
});
