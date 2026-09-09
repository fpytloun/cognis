import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import { WorkspaceManager } from '$lib/dashboard/workspace';
import WorkspaceDock from './WorkspaceDock.svelte';

const safe = { x: 12, y: 12, width: 1000, height: 700 };

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as typeof ResizeObserver;
});

function managerWithTwoWindows(): WorkspaceManager {
  const manager = new WorkspaceManager('desktop', safe);
  manager.open({
    kind: 'conversation',
    entityId: 'conversation-1',
    title: 'First conversation',
    canonicalHref: '/chat/conversation-1',
  });
  manager.open({
    kind: 'task',
    entityId: 'task-1',
    title: 'Second task',
    canonicalHref: '/tasks/task-1',
  });
  return manager;
}

describe('WorkspaceDock', () => {
  it('shows the active item and dashboard control for one ordinary visible window', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open({
      kind: 'conversation',
      entityId: 'conversation-1',
      title: 'Conversation',
      canonicalHref: '/chat/conversation-1',
    });

    const onTopChange = vi.fn();
    render(WorkspaceDock, { windows: get(manager), manager, onTopChange });

    expect(screen.getByTestId('workspace-dock-dashboard')).toHaveAttribute('title', 'Show dashboard');
    expect(screen.getByTestId('workspace-dock-item-conversation:conversation-1')).toHaveClass(
      'border-sky-400',
      'bg-sky-500/15',
    );
    expect(screen.getByTestId('workspace-dock-restore-conversation:conversation-1')).toHaveAttribute(
      'aria-current',
      'true',
    );
    expect(screen.getByTestId('workspace-dock-close-conversation:conversation-1')).toBeInTheDocument();
    expect(onTopChange).toHaveBeenCalledWith(0);
  });

  it('shows the switcher for one minimized window', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open({
      kind: 'conversation',
      entityId: 'conversation-1',
      title: 'Conversation',
      canonicalHref: '/chat/conversation-1',
    });
    manager.minimize('conversation:conversation-1');

    render(WorkspaceDock, { windows: get(manager), manager });

    expect(screen.getByTestId('workspace-dock')).toBeInTheDocument();
    expect(screen.getByTestId('workspace-dock-item-conversation:conversation-1')).toHaveAttribute('data-minimized', 'true');
  });

  it('shows all windows and highlights the active top visible window', () => {
    const manager = managerWithTwoWindows();
    const windows = get(manager);

    render(WorkspaceDock, { windows, manager });

    expect(screen.getByRole('navigation', { name: 'Workspace windows' })).toBeInTheDocument();
    expect(screen.getByTestId('workspace-dock-restore-conversation:conversation-1')).toBeInTheDocument();
    expect(screen.getByTestId('workspace-dock-restore-task:task-1')).toHaveAttribute('aria-current', 'true');
    expect(screen.getByTestId('workspace-dock-item-task:task-1')).toHaveClass('border-sky-400', 'bg-sky-500/15');
  });

  it('focuses a visible window, minimizes the focused window, and restores a minimized window', async () => {
    const manager = managerWithTwoWindows();
    const firstKey = 'conversation:conversation-1';
    const secondKey = 'task:task-1';
    const { rerender } = render(WorkspaceDock, { windows: get(manager), manager });

    await fireEvent.click(screen.getByTestId(`workspace-dock-restore-${firstKey}`));
    expect(manager.focusedTopVisibleKey()).toBe(firstKey);

    await rerender({ windows: get(manager), manager });
    await fireEvent.click(screen.getByTestId(`workspace-dock-restore-${firstKey}`));
    expect(get(manager).find((window) => window.key === firstKey)?.minimized).toBe(true);

    manager.minimize(secondKey);
    await rerender({ windows: get(manager), manager });
    expect(screen.getByTestId(`workspace-dock-item-${secondKey}`)).toHaveAttribute('data-minimized', 'true');
    expect(screen.getByTestId(`workspace-dock-item-${secondKey}`)).toHaveClass('opacity-60');

    await fireEvent.click(screen.getByTestId(`workspace-dock-restore-${secondKey}`));
    expect(get(manager).find((window) => window.key === secondKey)?.minimized).toBe(false);
    expect(manager.focusedTopVisibleKey()).toBe(secondKey);
  });

  it('keeps DOM item order stable when focus changes through switcher clicks', async () => {
    const manager = new WorkspaceManager('desktop', safe);
    for (const entityId of ['a', 'b', 'c']) {
      manager.open({
        kind: 'conversation',
        entityId,
        title: entityId.toUpperCase(),
        canonicalHref: `/chat/${entityId}`,
      });
    }
    const { rerender } = render(WorkspaceDock, { windows: get(manager), manager });
    const itemKeys = () => [...screen.getByTestId('workspace-dock').querySelectorAll('[data-testid^="workspace-dock-item-"]')]
      .map((element) => element.getAttribute('data-testid'));

    expect(itemKeys()).toEqual([
      'workspace-dock-item-conversation:a',
      'workspace-dock-item-conversation:b',
      'workspace-dock-item-conversation:c',
    ]);
    await fireEvent.click(screen.getByTestId('workspace-dock-restore-conversation:b'));
    await rerender({ windows: get(manager), manager });
    await fireEvent.click(screen.getByTestId('workspace-dock-restore-conversation:a'));
    await rerender({ windows: get(manager), manager });

    expect(itemKeys()).toEqual([
      'workspace-dock-item-conversation:a',
      'workspace-dock-item-conversation:b',
      'workspace-dock-item-conversation:c',
    ]);
    expect(screen.getByTestId('workspace-dock-restore-conversation:a')).toHaveAttribute('aria-current', 'true');
  });

  it('reorders with pointer drag and Alt-arrow without activating the item', async () => {
    const manager = new WorkspaceManager('desktop', safe);
    for (const entityId of ['a', 'b', 'c']) {
      manager.open({ kind: 'conversation', entityId, title: entityId.toUpperCase(), canonicalHref: `/chat/${entityId}` });
    }
    const { rerender } = render(WorkspaceDock, { windows: get(manager), manager });
    const reorder = vi.spyOn(manager, 'reorderSwitcher');
    const a = screen.getByTestId('workspace-dock-item-conversation:a');
    const b = screen.getByTestId('workspace-dock-restore-conversation:b');
    const bItem = screen.getByTestId('workspace-dock-item-conversation:b');
    const c = screen.getByTestId('workspace-dock-item-conversation:c');
    const originalRects = [a, bItem, c].map((item) => item.getBoundingClientRect);
    a.getBoundingClientRect = vi.fn(() => ({
      x: 20, y: 0, top: 0, left: 20, right: 120, bottom: 32, width: 100, height: 32, toJSON: () => ({}),
    }));
    bItem.getBoundingClientRect = vi.fn(() => ({
      x: 128, y: 0, top: 0, left: 128, right: 228, bottom: 32, width: 100, height: 32, toJSON: () => ({}),
    }));
    c.getBoundingClientRect = vi.fn(() => ({
      x: 236, y: 0, top: 0, left: 236, right: 336, bottom: 32, width: 100, height: 32, toJSON: () => ({}),
    }));

    try {
      await fireEvent.pointerDown(b, { button: 0, pointerId: 1, clientX: 180, clientY: 10 });
      await fireEvent.pointerMove(window, { pointerId: 1, clientX: 25, clientY: 10 });
      await fireEvent.pointerUp(window, { pointerId: 1, clientX: 25, clientY: 10 });
      await fireEvent.pointerUp(window, { pointerId: 1, clientX: 25, clientY: 10 });
      await fireEvent.click(b);
      await rerender({ windows: get(manager), manager });
      expect([...screen.getByTestId('workspace-dock').querySelectorAll('[data-workspace-switcher-key]')]
        .map((element) => element.getAttribute('data-workspace-switcher-key'))).toEqual([
        'conversation:b', 'conversation:a', 'conversation:c',
      ]);
      expect(manager.focusedTopVisibleKey()).toBe('conversation:c');
      expect(reorder).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('workspace-dock-announcement')).toHaveTextContent('B moved to position 1 of 3.');
      await new Promise((resolve) => setTimeout(resolve, 0));
      await fireEvent.click(screen.getByTestId('workspace-dock-restore-conversation:a'));
      await fireEvent.click(screen.getByTestId('workspace-dock-restore-conversation:b'));
      expect(manager.focusedTopVisibleKey()).toBe('conversation:b');

      await fireEvent.keyDown(screen.getByTestId('workspace-dock-restore-conversation:b'), {
        key: 'ArrowRight',
        altKey: true,
      });
      await rerender({ windows: get(manager), manager });
      expect([...get(manager)].sort((left, right) => left.switcherOrder - right.switcherOrder)
        .map((window) => window.key)).toEqual(['conversation:a', 'conversation:b', 'conversation:c']);
    } finally {
      [a, bItem, c].forEach((item, index) => {
        item.getBoundingClientRect = originalRects[index];
      });
    }
  });

  it('does not reorder when a pointer drag is cancelled', async () => {
    const manager = managerWithTwoWindows();
    const { rerender } = render(WorkspaceDock, { windows: get(manager), manager });
    const first = screen.getByTestId('workspace-dock-restore-conversation:conversation-1');

    await fireEvent.pointerDown(first, { button: 0, pointerId: 1, clientX: 20, clientY: 10 });
    await fireEvent.pointerMove(first, { pointerId: 1, clientX: 40, clientY: 10 });
    await fireEvent.pointerCancel(first, { pointerId: 1, clientX: 40, clientY: 10 });
    await rerender({ windows: get(manager), manager });

    expect([...get(manager)].sort((left, right) => left.switcherOrder - right.switcherOrder)
      .map((window) => window.key)).toEqual(['conversation:conversation-1', 'task:task-1']);
  });

  it('cleans up a drag when pointer capture is lost', async () => {
    const manager = managerWithTwoWindows();
    render(WorkspaceDock, { windows: get(manager), manager });
    const source = screen.getByTestId('workspace-dock-restore-conversation:conversation-1');

    await fireEvent.pointerDown(source, { button: 0, pointerId: 7, clientX: 20, clientY: 10 });
    await fireEvent.pointerMove(window, { pointerId: 7, clientX: 80, clientY: 10 });
    expect(screen.getByTestId('workspace-dock-drag-ghost')).toBeInTheDocument();

    await fireEvent(window, new PointerEvent('lostpointercapture', { pointerId: 7 }));

    expect(screen.queryByTestId('workspace-dock-drag-ghost')).toBeNull();
    expect(screen.getByTestId('workspace-dock')).toHaveAttribute('data-dragging', 'false');
    expect([...get(manager)].sort((left, right) => left.switcherOrder - right.switcherOrder)
      .map((item) => item.key)).toEqual(['conversation:conversation-1', 'task:task-1']);
  });

  it('shows the lifted item and provisional target after the drag threshold', async () => {
    const manager = new WorkspaceManager('desktop', safe);
    for (const entityId of ['a', 'b', 'c']) {
      manager.open({ kind: 'conversation', entityId, title: entityId.toUpperCase(), canonicalHref: `/chat/${entityId}` });
    }
    render(WorkspaceDock, { windows: get(manager), manager });

    const dock = screen.getByTestId('workspace-dock');
    const sourceItem = screen.getByTestId('workspace-dock-item-conversation:b');
    const source = screen.getByTestId('workspace-dock-restore-conversation:b');
    const target = screen.getByTestId('workspace-dock-item-conversation:a');
    const originalElementFromPoint = document.elementFromPoint;
    const originalElementsFromPoint = document.elementsFromPoint;
    const originalDockRect = dock.getBoundingClientRect;
    const originalMatchMedia = window.matchMedia;
    const itemRects: Map<string, { x: number; left: number; right: number; width: number }> = new Map([
      ['conversation:a', { x: 12, left: 12, right: 112, width: 100 }],
      ['conversation:b', { x: 120, left: 120, right: 260, width: 140 }],
      ['conversation:c', { x: 268, left: 268, right: 348, width: 80 }],
    ]);
    let elementAtPoint: Element | null = target;
    document.elementFromPoint = vi.fn(() => elementAtPoint);
    document.elementsFromPoint = vi.fn(() => elementAtPoint ? [sourceItem, elementAtPoint] : []);
    dock.getBoundingClientRect = vi.fn(() => ({
      x: 12, y: 0, top: 0, left: 12, right: 328, bottom: 40, width: 316, height: 40, toJSON: () => ({}),
    }));
    for (const item of [sourceItem, target, screen.getByTestId('workspace-dock-item-conversation:c')]) {
      const rect = itemRects.get(item.getAttribute('data-workspace-switcher-key')!)
        ?? { x: 0, left: 0, right: 0, width: 0 };
      item.getBoundingClientRect = vi.fn(() => ({
        ...rect,
        y: 0,
        top: 0,
        bottom: 32,
        height: 32,
        toJSON: () => ({}),
      }));
    }

    try {
      await fireEvent.pointerDown(source, { button: 0, pointerId: 4, clientX: 170, clientY: 10 });
      await fireEvent.pointerMove(source, { pointerId: 4, clientX: 174, clientY: 10 });
      expect(dock).toHaveAttribute('data-dragging', 'false');
      expect(screen.queryByTestId('workspace-dock-drop-indicator')).not.toBeInTheDocument();

      await fireEvent.pointerMove(source, { pointerId: 4, clientX: 40, clientY: 10 });

      expect(dock).toHaveAttribute('data-dragging', 'true');
      await waitFor(() => expect(dock).toHaveAttribute(
        'data-provisional-order',
        'conversation:b,conversation:a,conversation:c',
      ));
      expect(sourceItem).toHaveAttribute('data-dragged', 'true');
      expect(source).toHaveAttribute('aria-grabbed', 'true');
      expect(sourceItem).toHaveClass('workspace-dock-item--placeholder');
      expect(screen.getAllByTestId('workspace-dock-drag-ghost')).toHaveLength(1);
      expect(screen.getByTestId('workspace-dock-item-conversation:a')).toHaveStyle({
        transform: 'translate3d(148px, 0, 0)',
      });
      const indicator = screen.getByTestId('workspace-dock-drop-indicator');
      expect(indicator).toHaveAttribute('data-insertion-index', '0');
      expect(indicator).toHaveStyle({ left: '10px' });

      elementAtPoint = null;
      await fireEvent.pointerMove(source, { pointerId: 4, clientX: 60, clientY: 10 });
      expect(screen.getByTestId('workspace-dock-drag-ghost')).toHaveStyle({ left: '10px' });
      expect(screen.getByTestId('workspace-dock-drop-indicator')).toBeInTheDocument();

      Object.defineProperty(window, 'matchMedia', {
        configurable: true,
        value: vi.fn(() => ({ matches: true }) as unknown as MediaQueryList),
      });
      elementAtPoint = target;
      await fireEvent.pointerMove(source, { pointerId: 4, clientX: 50, clientY: 10 });
      expect(screen.getByTestId('workspace-dock-drag-ghost')).toHaveStyle({ transform: 'scale(1)' });
      expect(screen.getByTestId('workspace-dock-drop-indicator')).toBeInTheDocument();
    } finally {
      document.elementFromPoint = originalElementFromPoint;
      document.elementsFromPoint = originalElementsFromPoint;
      dock.getBoundingClientRect = originalDockRect;
      Object.defineProperty(window, 'matchMedia', {
        configurable: true,
        value: originalMatchMedia,
      });
    }
  });

  it('restores the exact visual and persisted order when Escape cancels a drag', async () => {
    const manager = managerWithTwoWindows();
    render(WorkspaceDock, { windows: get(manager), manager });
    const source = screen.getByTestId('workspace-dock-restore-conversation:conversation-1');
    const target = screen.getByTestId('workspace-dock-item-task:task-1');
    const originalElementFromPoint = document.elementFromPoint;
    document.elementFromPoint = vi.fn(() => target);

    try {
      await fireEvent.pointerDown(source, { button: 0, pointerId: 5, clientX: 20, clientY: 10 });
      await fireEvent.pointerMove(source, { pointerId: 5, clientX: 80, clientY: 10 });
      expect(screen.getByTestId('workspace-dock')).toHaveAttribute('data-dragging', 'true');

      await fireEvent.keyDown(window, { key: 'Escape' });
      await fireEvent.pointerMove(window, { pointerId: 5, clientX: 200, clientY: 10 });

      expect(screen.getByTestId('workspace-dock')).toHaveAttribute('data-dragging', 'false');
      expect(screen.queryByTestId('workspace-dock-drag-ghost')).toBeNull();
      expect(screen.queryByTestId('workspace-dock-drop-indicator')).toBeNull();
      expect([...get(manager)].sort((left, right) => left.switcherOrder - right.switcherOrder)
        .map((item) => item.key)).toEqual(['conversation:conversation-1', 'task:task-1']);
    } finally {
      document.elementFromPoint = originalElementFromPoint;
    }
  });

  it('toggles only the visible workspace windows through the fixed dashboard control', async () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open({ kind: 'conversation', entityId: 'a', title: 'A', canonicalHref: '/chat/a' });
    manager.open({ kind: 'task', entityId: 'b', title: 'B', canonicalHref: '/tasks/b' });
    manager.open({ kind: 'conversation', entityId: 'already-minimized', title: 'Hidden', canonicalHref: '/chat/hidden' });
    manager.minimize('conversation:already-minimized');
    const { rerender } = render(WorkspaceDock, { windows: get(manager), manager });
    const control = screen.getByTestId('workspace-dock-dashboard');

    expect(control).toHaveAttribute('title', 'Show dashboard');
    expect(control).toHaveAttribute('aria-pressed', 'false');
    await fireEvent.click(control);
    await rerender({ windows: get(manager), manager });
    expect(control).toHaveAttribute('title', 'Restore windows');
    expect(control).toHaveAttribute('aria-pressed', 'true');
    expect(get(manager).every((window) => window.minimized)).toBe(true);

    await fireEvent.click(control);
    await rerender({ windows: get(manager), manager });
    expect(control).toHaveAttribute('title', 'Show dashboard');
    expect(get(manager).find((window) => window.key === 'conversation:a')?.minimized).toBe(false);
    expect(get(manager).find((window) => window.key === 'task:b')?.minimized).toBe(false);
    expect(get(manager).find((window) => window.key === 'conversation:already-minimized')?.minimized).toBe(true);
  });

  it('uses compact controls and publishes the rendered dock top', async () => {
    const manager = managerWithTwoWindows();
    const onTopChange = vi.fn();
    const original = HTMLElement.prototype.getBoundingClientRect;
    HTMLElement.prototype.getBoundingClientRect = vi.fn(() => ({
      x: 12, y: 640, top: 640, left: 12, right: 800, bottom: 680,
      width: 788, height: 40, toJSON: () => ({}),
    }));

    try {
      render(WorkspaceDock, { windows: get(manager), manager, onTopChange });
      await waitFor(() => expect(onTopChange).toHaveBeenCalledWith(640));
      expect(screen.getByTestId('workspace-dock')).toHaveClass('gap-1.5', 'p-1.5', 'rounded-xl', 'overflow-hidden');
      expect(screen.getByTestId('workspace-dock-strip')).toHaveClass('overflow-x-auto');
      expect(screen.getByTestId('workspace-dock-dashboard')).toHaveClass('h-8', 'w-8', 'shrink-0');
      expect(screen.getByTestId('workspace-dock-close-task:task-1')).toHaveClass('h-8', 'w-8');
      expect(screen.getByTestId('workspace-dock-restore-task:task-1').querySelector('[data-testid="activity-avatar"]'))
        .toHaveClass('h-5', 'w-5');
    } finally {
      HTMLElement.prototype.getBoundingClientRect = original;
    }
  });

  it('closes a window from the compact close control', async () => {
    const manager = managerWithTwoWindows();
    render(WorkspaceDock, { windows: get(manager), manager });

    await fireEvent.click(screen.getByTestId('workspace-dock-close-task:task-1'));

    expect(get(manager).map((window) => window.key)).toEqual(['conversation:conversation-1']);
  });
});
