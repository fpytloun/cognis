import { get } from 'svelte/store';
import { describe, expect, it, vi } from 'vitest';
import {
  clampWorkspaceRect,
  activeWorkspaceWindowKey,
  dashboardWorkspaceStorageKey,
  dispatchWorkspaceEscape,
  parseWorkspaceWindows,
  serializeWorkspaceWindows,
  workspaceKeyboardOwner,
  defaultWorkspaceRect,
  WorkspaceManager,
  MAX_RETAINED_WORKSPACE_WINDOWS,
  WORKSPACE_DOCK_GAP,
  workspaceCapacity,
  workspaceDeviceClass,
  workspacePresetRect,
  workspaceSnapTarget,
  workspaceSafeRectAboveDock,
  workspaceSafeRectFromContent,
  workspaceSafeRectWithDock,
} from './workspace';

const safe = { x: 240, y: 64, width: 1200, height: 800 };

  function input(
  entityId: string,
  kind: 'conversation' | 'task' | 'agent' | 'schedule' | 'attention' = 'conversation',
) {
  return { kind, entityId, title: entityId, canonicalHref: `/chat/${entityId}` };
}

describe('workspace geometry', () => {
  it('classifies devices and enforces their caps', () => {
    expect(workspaceDeviceClass(767, false)).toBe('phone');
    expect(workspaceDeviceClass(1024, true)).toBe('tablet');
    expect(workspaceDeviceClass(1440, false)).toBe('desktop');
    expect((['phone', 'tablet', 'desktop'] as const).map(workspaceCapacity)).toEqual([1, 2, 5]);
  });

  it('detects magnetic snap targets inside the safe workspace threshold', () => {
    expect(workspaceSnapTarget(safe.x + 100, safe.y + 20, safe)).toBe('maximize');
    expect(workspaceSnapTarget(safe.x + 20, safe.y + 100, safe)).toBe('left');
    expect(workspaceSnapTarget(safe.x + safe.width - 20, safe.y + 100, safe)).toBe('right');
    expect(workspaceSnapTarget(safe.x + 100, safe.y + 100, safe)).toBeNull();
  });

  it('clamps position and min/max dimensions to the safe workspace', () => {
    expect(clampWorkspaceRect({
      x: -500, y: 9999, width: 100, height: 9999,
    }, safe)).toEqual({ x: 240, y: 64, width: 360, height: 800 });
  });

  it('produces centered and exact split/maximize presets', () => {
    expect(defaultWorkspaceRect(safe, 'desktop')).toMatchObject({ x: 360, width: 960 });
    expect(workspacePresetRect('left', safe, 'desktop')).toEqual({
      x: 240, y: 64, width: 600, height: 800,
    });
    expect(workspacePresetRect('right', safe, 'desktop')).toEqual({
      x: 840, y: 64, width: 600, height: 800,
    });
    expect(workspacePresetRect('maximize', safe, 'tablet')).toEqual(safe);
  });

  it('uses content bounds for the full vertical paint area without bottom or vertical workspace insets', () => {
    expect(workspaceSafeRectFromContent(
      { top: 24, right: 1210, bottom: 834, left: 56 },
      { top: 0, right: 0, bottom: 0, left: 0 },
      0,
      1210,
    )).toEqual({
      x: 68,
      y: 24,
      width: 1130,
      height: 810,
    });
  });

  it('subtracts the measured dock height and gap and restores the base safe rectangle', () => {
    expect(workspaceSafeRectWithDock(safe, 64)).toEqual({ ...safe, height: 724 });
    expect(workspaceSafeRectWithDock(safe, 0)).toEqual(safe);
    expect(workspaceSafeRectWithDock({ ...safe, height: 50 }, 64)).toEqual({
      ...safe,
      height: 0,
    });
  });

  it('uses the dock top edge so bottom safe-area offsets are fully reserved', () => {
    const dockHeight = 64;
    const bottomSafeInset = 28;
    const dockBottomGap = 12;
    const dockTop = safe.y + safe.height - bottomSafeInset - dockBottomGap - dockHeight;

    const dockSafe = workspaceSafeRectAboveDock(safe, dockTop);

    expect(dockSafe).toEqual({
      ...safe,
      height: safe.height - bottomSafeInset - dockBottomGap - dockHeight - WORKSPACE_DOCK_GAP,
    });
    expect(workspacePresetRect('maximize', dockSafe, 'tablet')).toEqual(dockSafe);
    expect(workspacePresetRect('left', dockSafe, 'tablet').height).toBe(dockSafe.height);
    expect(workspaceSafeRectAboveDock(safe, null)).toEqual(safe);
  });

  it('uses a user-scoped client storage key', () => {
    expect(dashboardWorkspaceStorageKey(' User@Example.COM ')).toBe(
      'cognis.dashboard.workspace.v1:user%40example.com',
    );
  });
});

it('excludes ephemeral attention windows from serialization and hydration', () => {
  const manager = new WorkspaceManager('desktop', safe);
  manager.open(input('conversation-1'));
  manager.open({ ...input('approval-1', 'attention'), ephemeral: true });

  const serialized = serializeWorkspaceWindows(get(manager));
  expect(serialized).not.toContain('approval-1');
  expect(parseWorkspaceWindows(serialized, safe)).toHaveLength(1);
});

describe('WorkspaceManager', () => {
  it('deduplicates, focuses, caps without eviction, and closes explicitly', () => {
    const closed = vi.fn();
    const manager = new WorkspaceManager('desktop', safe, closed);
    expect(manager.open(input('one')).status).toBe('opened');
    expect(manager.open(input('one')).status).toBe('focused');
    manager.open(input('two', 'task'));
    manager.open(input('three', 'agent'));
    manager.open(input('four'));
    manager.open(input('five'));
    expect(manager.open(input('six'))).toEqual({ status: 'capped', limit: 5 });
    expect(get(manager).map((window) => window.entityId)).toEqual(['one', 'two', 'three', 'four', 'five']);
    manager.close('task:two');
    expect(closed).toHaveBeenCalledWith(expect.objectContaining({ entityId: 'two' }));
    expect(get(manager)).toHaveLength(4);
  });

  it('publishes focus and restore as one atomic store update', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('one'));
    manager.open(input('two'));
    let emissions = 0;
    const unsubscribe = manager.subscribe(() => { emissions += 1; });
    emissions = 0;

    manager.focus('conversation:one');
    expect(emissions).toBe(1);
    expect(get(manager).find((window) => window.key === 'conversation:one')).toMatchObject({
      key: 'conversation:one',
      zOrder: 2,
      focusRevision: 2,
    });
    expect(get(manager).map((window) => window.key)).toEqual([
      'conversation:one',
      'conversation:two',
    ]);

    manager.minimize('conversation:one');
    emissions = 0;
    manager.restore('conversation:one');
    expect(emissions).toBe(1);
    expect(get(manager).find((window) => window.key === 'conversation:one')).toMatchObject({
      key: 'conversation:one',
      minimized: false,
      zOrder: 2,
      focusRevision: 3,
    });
    expect(get(manager).map((window) => window.key)).toEqual([
      'conversation:one',
      'conversation:two',
    ]);
    unsubscribe();
  });

  it('keeps switcher order stable through focus, visibility, metadata, geometry, and persistence', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('a'));
    manager.open(input('b'));
    manager.open(input('c'));
    manager.focus('conversation:b');
    manager.focus('conversation:a');
    manager.minimize('conversation:c');
    manager.restore('conversation:c');
    manager.updateMetadata('conversation:b', { title: 'Updated B', status: 'running' });
    manager.invalidate('conversation', 'b');
    manager.setSafeRect({ x: 0, y: 0, width: 900, height: 600 });

    expect([...get(manager)].sort((left, right) => left.switcherOrder - right.switcherOrder)
      .map((window) => window.key)).toEqual([
      'conversation:a',
      'conversation:b',
      'conversation:c',
    ]);
    expect(manager.focusedTopVisibleKey()).toBe('conversation:c');

    const restored = new WorkspaceManager('desktop', safe);
    restored.hydrate(serializeWorkspaceWindows(manager.snapshot()));
    expect([...get(restored)].sort((left, right) => left.switcherOrder - right.switcherOrder)
      .map((window) => window.key)).toEqual([
      'conversation:a',
      'conversation:b',
      'conversation:c',
    ]);
  });

  it('reorders only switcher order and ignores invalid or unchanged requests', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('a'));
    manager.open(input('b'));
    manager.open(input('c'));
    manager.focus('conversation:b');
    const before = get(manager).map((window) => ({
      key: window.key,
      zOrder: window.zOrder,
      minimized: window.minimized,
      rect: window.rect,
    }));

    expect(manager.reorderSwitcher('conversation:b', 'conversation:a', 'before')).toBe(true);
    expect([...get(manager)].sort((left, right) => left.switcherOrder - right.switcherOrder)
      .map((window) => window.key)).toEqual(['conversation:b', 'conversation:a', 'conversation:c']);
    expect(get(manager).map((window) => ({
      key: window.key,
      zOrder: window.zOrder,
      minimized: window.minimized,
      rect: window.rect,
    }))).toEqual(before);
    expect(manager.reorderSwitcher('missing', 'conversation:a', 'before')).toBe(false);
    expect(manager.reorderSwitcher('conversation:b', 'missing', 'before')).toBe(false);
    expect(manager.reorderSwitcher('conversation:b', 'conversation:b', 'after')).toBe(false);

    const restored = new WorkspaceManager('desktop', safe);
    restored.hydrate(serializeWorkspaceWindows(manager.snapshot()));
    expect([...get(restored)].sort((left, right) => left.switcherOrder - right.switcherOrder)
      .map((window) => window.key)).toEqual(['conversation:b', 'conversation:a', 'conversation:c']);
  });

  it('retains ten total windows while minimized windows do not use visible capacity', () => {
    const manager = new WorkspaceManager('desktop', safe);
    for (let index = 0; index < MAX_RETAINED_WORKSPACE_WINDOWS; index += 1) {
      expect(manager.open(input(String(index))).status).toBe('opened');
      manager.minimize(`conversation:${index}`);
    }
    expect(get(manager)).toHaveLength(10);
    expect(manager.open(input('overflow'))).toEqual({ status: 'capped', limit: 10 });
  });

  it('caps restore by visible capacity without changing minimization', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('minimized'));
    manager.minimize('conversation:minimized');
    for (let index = 0; index < 5; index += 1) manager.open(input(`visible-${index}`));

    expect(manager.restore('conversation:minimized')).toEqual({ status: 'capped', limit: 5 });
    expect(get(manager).find((window) => window.key === 'conversation:minimized')?.minimized).toBe(true);
  });

  it('keeps minimized windows mounted and preserves identity through restore', () => {
    const visibility = vi.fn();
    const manager = new WorkspaceManager('desktop', safe, undefined, visibility);
    manager.open(input('one'));
    const key = get(manager)[0].key;
    manager.minimize(key);
    expect(get(manager)[0]).toMatchObject({ key, minimized: true });
    manager.restore(key);
    expect(get(manager)[0]).toMatchObject({ key, minimized: false });
    expect(visibility.mock.calls).toEqual([
      [expect.objectContaining({ key }), false],
      [expect.objectContaining({ key }), true],
    ]);
  });

  it('minimizes and restores the exact visible set without changing persisted workspace state', () => {
    const visibility = vi.fn();
    const manager = new WorkspaceManager('desktop', safe, undefined, visibility);
    manager.open(input('a'));
    manager.open(input('b', 'task'));
    manager.open(input('c'));
    manager.focus('task:b');
    const before = manager.snapshot();
    const serializedBefore = serializeWorkspaceWindows(before);

    expect(manager.showDashboard()).toEqual({ status: 'minimized', count: 3 });
    expect(manager.hasDashboardSnapshot()).toBe(true);
    expect(get(manager).every((window) => window.minimized)).toBe(true);
    expect(manager.restoreDashboard()).toEqual({ status: 'restored', count: 3, capped: false, limit: 5 });
    expect(manager.hasDashboardSnapshot()).toBe(false);
    expect(manager.snapshot().map((window) => ({
      key: window.key,
      minimized: window.minimized,
      rect: window.rect,
      switcherOrder: window.switcherOrder,
    }))).toEqual(before.map((window) => ({
      key: window.key,
      minimized: window.minimized,
      rect: window.rect,
      switcherOrder: window.switcherOrder,
    })));
    expect(manager.focusedTopVisibleKey()).toBe('task:b');
    expect(serializeWorkspaceWindows(manager.snapshot())).toBe(serializedBefore);
    expect(visibility.mock.calls.map(([window, visible]) => [window.key, visible])).toEqual([
      ['conversation:a', false],
      ['task:b', false],
      ['conversation:c', false],
      ['task:b', true],
      ['conversation:c', true],
      ['conversation:a', true],
    ]);
  });

  it('excludes pre-minimized windows and clears the dashboard snapshot after an explicit action', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('a'));
    manager.open(input('b'));
    manager.open(input('already-minimized'));
    manager.minimize('conversation:already-minimized');

    manager.showDashboard();
    expect(manager.hasDashboardSnapshot()).toBe(true);
    expect(manager.restoreDashboard()).toEqual({ status: 'restored', count: 2, capped: false, limit: 5 });
    expect(get(manager).find((window) => window.key === 'conversation:already-minimized')?.minimized).toBe(true);

    manager.showDashboard();
    manager.reorderSwitcher('conversation:b', 'conversation:a', 'before');
    expect(manager.hasDashboardSnapshot()).toBe(false);
    expect(manager.restoreDashboard()).toEqual({ status: 'inactive', count: 0 });
    expect(get(manager).every((window) => window.minimized)).toBe(true);

    manager.restore('conversation:a');
    manager.restore('conversation:b');
    manager.showDashboard();
    manager.close('conversation:b');
    expect(manager.hasDashboardSnapshot()).toBe(false);
    expect(manager.restoreDashboard()).toEqual({ status: 'inactive', count: 0 });
    expect(get(manager).find((window) => window.key === 'conversation:b')).toBeUndefined();
  });

  it('restores the focused snapshot window first when capacity decreases', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('a'));
    manager.open(input('b'));
    manager.open(input('c'));
    manager.focus('conversation:b');
    manager.showDashboard();
    manager.setDevice('tablet');

    expect(manager.restoreDashboard()).toEqual({ status: 'restored', count: 2, capped: true, limit: 2 });
    expect(manager.focusedTopVisibleKey()).toBe('conversation:b');
  });

  it('is idempotent when no windows are visible', () => {
    const manager = new WorkspaceManager('desktop', safe);
    expect(manager.showDashboard()).toEqual({ status: 'empty', count: 0 });
    expect(manager.restoreDashboard()).toEqual({ status: 'inactive', count: 0 });
    manager.open(input('minimized'));
    manager.minimize('conversation:minimized');
    expect(manager.showDashboard()).toEqual({ status: 'empty', count: 0 });
    expect(manager.hasDashboardSnapshot()).toBe(false);
  });

  it('keeps dashboard-only minimization out of the persisted snapshot', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('a'));
    manager.open(input('b'));
    manager.showDashboard();

    expect(manager.snapshot().every((window) => window.minimized)).toBe(true);
    expect(manager.persistentSnapshot().every((window) => !window.minimized)).toBe(true);
    expect(parseWorkspaceWindows(
      serializeWorkspaceWindows(manager.persistentSnapshot()),
      safe,
    ).every((window) => !window.minimized)).toBe(true);
  });

  it('persists and hydrates open geometry, minimization, ordering, and resolved identity', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('one'));
    manager.open(input('agent-one', 'agent'));
    manager.move('conversation:one', 310, 120);
    manager.resize('conversation:one', 720, 510);
    manager.minimize('conversation:one');
    manager.setResolvedConversation('agent:agent-one', 'resolved-conversation');
    const serialized = serializeWorkspaceWindows(manager.snapshot());
    const visibility = vi.fn();
    const restored = new WorkspaceManager('desktop', safe, undefined, visibility);

    restored.hydrate(serialized);

    expect(get(restored)).toEqual([
      expect.objectContaining({
        key: 'conversation:one',
        minimized: true,
        rect: { x: 310, y: 120, width: 720, height: 510 },
        refreshToken: 0,
      }),
      expect.objectContaining({
        key: 'agent:agent-one',
        minimized: false,
        resolvedConversationId: 'resolved-conversation',
        canonicalHref: '/chat/resolved-conversation',
      }),
    ]);
    expect(visibility).toHaveBeenCalledOnce();
    expect(visibility).toHaveBeenCalledWith(
      expect.objectContaining({ key: 'agent:agent-one' }),
      true,
    );
  });

  it('keeps the highest-z visible windows when hydration exceeds device capacity', () => {
    const desktop = new WorkspaceManager('desktop', safe);
    for (let index = 0; index < 5; index += 1) desktop.open(input(String(index)));
    const tablet = new WorkspaceManager('tablet', safe);

    tablet.hydrate(serializeWorkspaceWindows(desktop.snapshot()));

    expect(get(tablet).filter((window) => !window.minimized).map((window) => window.key))
      .toEqual(['conversation:3', 'conversation:4']);
  });

  it('invalidates agent windows through their resolved conversation', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('agent-one', 'agent'));
    manager.setResolvedConversation('agent:agent-one', 'resolved-conversation');

    manager.invalidate('conversation', 'resolved-conversation');

    expect(manager.snapshot()[0].refreshToken).toBe(1);
  });

  it('rejects malformed, external, duplicate, and unsupported persisted windows', () => {
    const serialized = JSON.stringify({
      version: 1,
      windows: [
        {
          ...input('one'),
          key: 'forged',
          rect: { x: -1000, y: -1000, width: 9999, height: 9999 },
          restoreRect: null,
          minimized: false,
          zOrder: 8,
        },
        {
          ...input('one'),
          canonicalHref: 'javascript:alert(1)',
          rect: safe,
        },
        {
          ...input('external'),
          canonicalHref: '//example.com',
          rect: safe,
        },
        {
          ...input('backslash-external'),
          canonicalHref: '/\\evil.example',
          rect: safe,
        },
        {
          ...input('unknown'),
          kind: 'unknown',
          rect: safe,
        },
      ],
    });

    expect(parseWorkspaceWindows(serialized, safe)).toEqual([
      expect.objectContaining({
        key: 'conversation:one',
        rect: safe,
        zOrder: 1,
      }),
    ]);
    expect(parseWorkspaceWindows('{bad json', safe)).toEqual([]);
  });

  it('migrates persisted windows without switcher order from stored array order', () => {
    const serialized = JSON.stringify({
      version: 1,
      windows: [
        { ...input('second'), rect: safe, restoreRect: null, minimized: false, zOrder: 2 },
        { ...input('first'), rect: safe, restoreRect: null, minimized: false, zOrder: 1 },
      ],
    });

    const windows = parseWorkspaceWindows(serialized, safe);
    expect(windows.map((window) => window.key)).toEqual([
      'conversation:second',
      'conversation:first',
    ]);
    expect(activeWorkspaceWindowKey(windows)).toBe('conversation:second');
  });

  it('selects the highest visible window for active-surface keyboard handling', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('one', 'task'));
    manager.open(input('two', 'task'));
    const topKey = 'task:two';
    expect(activeWorkspaceWindowKey(get(manager))).toBe(topKey);
    manager.minimize(topKey);
    expect(activeWorkspaceWindowKey(get(manager))).toBe('task:one');
    manager.minimize('task:one');
    expect(activeWorkspaceWindowKey(get(manager))).toBeNull();
  });

  it('yields keyboard ownership to phone fallback and blocking overlays', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('one', 'task'));
    expect(workspaceKeyboardOwner(get(manager), true, false)).toBe('task:one');
    expect(workspaceKeyboardOwner(get(manager), false, false)).toBeNull();
    expect(workspaceKeyboardOwner(get(manager), true, true)).toBeNull();
  });

  it('dispatches Escape only to the focused top visible window without fanout', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('one', 'task'));
    manager.open(input('two', 'task'));
    manager.open(input('three', 'task'));
    const one = { handleEscape: vi.fn(() => false) };
    const two = { handleEscape: vi.fn(() => false) };
    const three = { handleEscape: vi.fn(() => true) };
    const targets = {
      'task:one': one,
      'task:two': two,
      'task:three': three,
    };

    expect(dispatchWorkspaceEscape(manager, targets, true, false)).toBe(true);
    expect(three.handleEscape).toHaveBeenCalledOnce();
    expect(two.handleEscape).not.toHaveBeenCalled();
    expect(one.handleEscape).not.toHaveBeenCalled();
    expect(get(manager)).toHaveLength(3);

    three.handleEscape.mockReturnValue(false);
    expect(dispatchWorkspaceEscape(manager, targets, true, false)).toBe(true);
    expect(get(manager).map((window) => window.key)).toEqual(['task:one', 'task:two']);
    expect(two.handleEscape).not.toHaveBeenCalled();
  });

  it('yields Escape to blocking overlays and skips minimized windows', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('one', 'task'));
    manager.open(input('two', 'task'));
    const one = { handleEscape: vi.fn(() => true) };
    const two = { handleEscape: vi.fn(() => true) };
    const targets = { 'task:one': one, 'task:two': two };

    expect(dispatchWorkspaceEscape(manager, targets, true, true)).toBe(false);
    expect(one.handleEscape).not.toHaveBeenCalled();
    expect(two.handleEscape).not.toHaveBeenCalled();

    manager.minimize('task:two');
    expect(dispatchWorkspaceEscape(manager, targets, true, false)).toBe(true);
    expect(one.handleEscape).toHaveBeenCalledOnce();
    expect(two.handleEscape).not.toHaveBeenCalled();
  });

  it('uses snap-first tablet geometry and restores prior geometry', () => {
    const manager = new WorkspaceManager('tablet', safe);
    manager.open(input('one'));
    expect(get(manager)[0].rect).toEqual(safe);
    manager.open(input('two'));
    expect(get(manager).map((window) => window.rect)).toEqual([
      workspacePresetRect('left', safe, 'tablet'),
      workspacePresetRect('right', safe, 'tablet'),
    ]);
    manager.applyPreset('conversation:one', 'maximize');
    manager.applyPreset('conversation:one', 'restore');
    expect(get(manager).find((window) => window.key === 'conversation:one')?.rect)
      .toEqual(workspacePresetRect('left', safe, 'tablet'));
  });

  it('moves, resizes, reclamps, updates metadata, and invalidates only matches', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('one'));
    manager.open(input('task', 'task'));
    manager.move('conversation:one', -100, -100);
    manager.resize('conversation:one', 9999, 100);
    expect(get(manager)[0].rect).toEqual({ x: 240, y: 64, width: 1200, height: 320 });
    manager.updateMetadata('conversation:one', { title: 'Updated', status: 'closed' });
    manager.invalidate('conversation', 'one');
    manager.invalidate('task', 'elsewhere');
    manager.open(input('schedule', 'schedule'));
    manager.invalidate('schedule', 'schedule');
    expect(get(manager)[0]).toMatchObject({
      title: 'Updated', status: 'closed', refreshToken: 1,
    });
    expect(get(manager)[1].refreshToken).toBe(0);
    expect(get(manager)[2].refreshToken).toBe(1);
    manager.setSafeRect({ x: 0, y: 0, width: 800, height: 600 });
    expect(get(manager)[0].rect.width).toBe(800);
  });

  it('resizes snapped windows when dock-safe geometry shrinks and restores', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('maximized'));
    manager.applyPreset('conversation:maximized', 'maximize');
    const dockSafe = workspaceSafeRectWithDock(safe, 64);

    manager.setSafeRect(dockSafe);
    expect(get(manager)[0].rect).toEqual(dockSafe);

    manager.setSafeRect(safe);
    expect(get(manager)[0].rect).toEqual(safe);
  });

  it('preserves floating restore geometry while the dock safe rectangle is temporary', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('restorable'));
    const floating = get(manager)[0].rect;
    manager.applyPreset('conversation:restorable', 'maximize');

    manager.setSafeRect(workspaceSafeRectWithDock(safe, 64));
    manager.setSafeRect(safe);
    manager.applyPreset('conversation:restorable', 'restore');

    expect(get(manager)[0].rect).toEqual(floating);
  });

  it('does not clamp restore geometry when the unchanged device is synchronized', () => {
    const manager = new WorkspaceManager('desktop', safe);
    manager.open(input('same-device'));
    const key = 'conversation:same-device';
    const floating = {
      x: safe.x + 100,
      y: safe.y + safe.height - 420,
      width: 700,
      height: 400,
    };
    manager.resize(key, floating.width, floating.height);
    manager.move(key, floating.x, floating.y);
    manager.applyPreset(key, 'maximize');

    manager.setSafeRect(workspaceSafeRectAboveDock(safe, safe.y + safe.height - 100));
    manager.setDevice('desktop');
    manager.setSafeRect(safe);
    manager.applyPreset(key, 'restore');

    expect(get(manager)[0].rect).toEqual(floating);
  });
});
