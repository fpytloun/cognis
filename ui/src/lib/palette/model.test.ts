import { describe, expect, it } from 'vitest';
import { highlighted, localItems, matches, shouldOpenWindow, type PaletteItem } from './model';
import { ALL_SETTINGS_TABS, USER_SETTINGS_TABS } from '$lib/settings-navigation';
import { navigationItems } from '$lib/navigation';

const item: PaletteItem = { kind: 'conversation', id: 'chat-palette', title: 'Command palette',
  project_id: 'project-cognis', agent_id: 'laforge', status: 'active', format: null,
  updated_at: '', href: '/chat/chat-palette' };
describe('palette navigation policy', () => {
  it.each([
    [null, false, false],
    [{ projectId: null }, false, true],
    [{ projectId: 'project-cognis' }, false, true],
    [{ projectId: 'other-project' }, false, false],
    [{ projectId: null }, true, false],
    [{ projectId: 'project-cognis' }, true, false],
  ])('resolves dashboard context %j and redirect %s', (scope, force, expected) => {
    expect(shouldOpenWindow(item, scope, force)).toBe(expected);
  });
  it('never opens settings/actions in workspace windows', () => {
    for (const kind of ['setting', 'action', 'page', 'agent', 'project'] as const)
      expect(shouldOpenWindow({ ...item, kind }, { projectId: null }, false)).toBe(false);
  });
});
describe('local discovery and safe highlighting', () => {
  it('uses the shell navigation registry for app destinations without turning them into windows', () => {
    for (const destination of navigationItems) {
      const page = localItems(destination.label, false, false)
        .find(row => row.kind === 'page' && row.href === destination.href);
      expect(page?.title).toBe(destination.label);
    }
    expect(localItems('home', false).some(row => row.kind === 'page' && row.href === '/')).toBe(true);
    expect(localItems('', false).some(row => row.kind === 'page')).toBe(false);
  });
  it('reuses settings role visibility and supports aliases', () => {
    expect(localItems('usage', true, true, false).some((row) => row.href === '/usage')).toBe(false);
    expect(localItems('usage', true, true, true).some((row) => row.href === '/usage')).toBe(true);
    expect(localItems('routes', true).some((row) => row.href === '/settings?tab=routing')).toBe(true);
    expect(localItems('routes', false)).toHaveLength(0);
    expect(localItems('models', false).some((row) => row.id === 'providers')).toBe(true);
    expect(localItems('new chat', false)[0]?.kind).toBe('action');
    expect(localItems('new chat', false, false)).toHaveLength(0);
    for (const admin of [true, false]) {
      expect(localItems('', admin).some((row) => row.kind === 'setting')).toBe(false);
      const visible = ALL_SETTINGS_TABS.filter((tab) =>
        localItems(tab, admin).some((row) => row.kind === 'setting' && row.id === tab));
      expect(new Set(visible)).toEqual(new Set(admin ? ALL_SETTINGS_TABS : USER_SETTINGS_TABS));
    }
  });
  it('matches and highlights accent-insensitively without HTML injection', () => {
    expect(matches('Náklady — přístavba.xlsx', 'naklady pristavba')).toBe(true);
    expect(highlighted('Náklady <img>', 'naklady')).toEqual([
      { text: 'Náklady', match: true }, { text: ' <img>', match: false },
    ]);
    expect(highlighted('Budget budget', 'budget').filter((part) => part.match)).toHaveLength(2);
    expect(highlighted('😀 Náklady', 'naklady').map((part) => part.text).join('')).toBe('😀 Náklady');
  });
});
