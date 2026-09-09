import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
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
    render(WorkspaceWindowFixture, { window: windowState, manager });
    return { manager, windowState };
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

  it('resizes through a border-corner handle without covering window content', async () => {
    const { manager, windowState } = setup();
    const windowElement = screen.getByTestId(`workspace-window-${windowState.key}`);
    const grip = screen.getByTestId(`workspace-window-resize-${windowState.key}`);
    expect(grip).toHaveClass('h-3', 'w-3');
    expect(grip).toHaveStyle({ width: '12px', height: '12px' });
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
    expect(layout).toHaveClass('rounded-lg', 'h-8', 'w-8', 'bg-emerald-400/5');
    expect(layout).not.toHaveClass('rounded-full');
    expect(minimize).toHaveClass('rounded-lg', 'bg-amber-400/5');
    expect(close).toHaveClass('rounded-lg', 'bg-rose-400/5');
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
