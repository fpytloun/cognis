import { beforeEach, describe, expect, it } from 'vitest';

import {
  INSPECTOR_DEFAULT_WIDTH,
  conversationInfoDrawer,
} from './conversationInfo.svelte';

describe('conversationInfoDrawer', () => {
  beforeEach(() => {
    window.localStorage.clear();
    conversationInfoDrawer.open = false;
    conversationInfoDrawer.tab = 'overview';
    conversationInfoDrawer.contextOpen = false;
    conversationInfoDrawer.focus = false;
    conversationInfoDrawer.preferredPinned = true;
    conversationInfoDrawer.preferredWidth = INSPECTOR_DEFAULT_WIDTH;
  });

  it('derives pinned, overlay, and focus presentations without losing preferences', () => {
    conversationInfoDrawer.open = true;
    expect(conversationInfoDrawer.presentation(true)).toBe('pinned');
    expect(conversationInfoDrawer.presentation(false)).toBe('overlay');

    conversationInfoDrawer.focus = true;
    expect(conversationInfoDrawer.presentation(true)).toBe('focus');
    expect(conversationInfoDrawer.preferredPinned).toBe(true);
  });

  it('clamps and persists the accessible inspector width', () => {
    conversationInfoDrawer.setWidth(100);
    expect(conversationInfoDrawer.preferredWidth).toBe(384);
    expect(JSON.parse(window.localStorage.getItem('cognis.conversationInfo.v2') ?? '{}')).toMatchObject({
      preferredWidth: 384,
    });

    conversationInfoDrawer.setWidth(1200);
    expect(conversationInfoDrawer.preferredWidth).toBe(960);
  });

  it('persists open state and the Work tab immediately', () => {
    conversationInfoDrawer.setOpen(true);
    conversationInfoDrawer.mode = 'work';

    expect(JSON.parse(window.localStorage.getItem('cognis.conversationInfo.v2') ?? '{}')).toMatchObject({
      open: true,
      tab: 'work',
      preferredWidth: 512,
    });
  });

  it('keeps the selected Session or Work tab when the drawer closes', () => {
    conversationInfoDrawer.mode = 'work';
    conversationInfoDrawer.setOpen(true);

    conversationInfoDrawer.close();

    expect(conversationInfoDrawer.open).toBe(false);
    expect(conversationInfoDrawer.tab).toBe('work');
    expect(conversationInfoDrawer.mode).toBe('work');
  });

  it('returns from context details to the selected tab', () => {
    conversationInfoDrawer.mode = 'work';
    conversationInfoDrawer.mode = 'context';

    expect(conversationInfoDrawer.mode).toBe('context');

    conversationInfoDrawer.contextOpen = false;

    expect(conversationInfoDrawer.mode).toBe('work');
  });

  it('migrates the stored full tab to Overview', () => {
    window.localStorage.setItem('cognis.conversationInfo.v2', JSON.stringify({ tab: 'full' }));
    conversationInfoDrawer.hydrated = false;
    conversationInfoDrawer.hydrate();
    expect(conversationInfoDrawer.tab).toBe('overview');
  });
});
