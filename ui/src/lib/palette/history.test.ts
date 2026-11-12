import { beforeEach, describe, expect, it } from 'vitest';
import { PaletteHistory } from './history';

let values: Map<string, string>;
const storage = () => ({
  getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => { values.set(key, value); },
  removeItem: (key: string) => { values.delete(key); },
});
const key = (owner: string) => `cognis.palette.history:${encodeURIComponent(owner)}`;
beforeEach(() => { values = new Map(); });

describe('bounded account-scoped search history', () => {
  it('stores only completed nonempty queries, de-duplicates case/space and retains 20', () => {
    const history = new PaletteHistory(storage);
    history.remember('not signed in');
    expect(values.size).toBe(0);
    history.open('owner@example.com');
    for (let i = 0; i < 30; i++) history.remember(`Search ${i}`);
    history.remember(' search 29 ');
    history.remember('  ');
    history.remember('x'.repeat(161));
    expect(history.size).toBe(20);
    expect(JSON.parse(values.get(key('owner@example.com'))!)).toEqual([
      'search 29', ...Array.from({ length: 19 }, (_, i) => `Search ${28 - i}`),
    ]);
  });

  it('walks older/newer queries, clamps at the oldest and restores the draft', () => {
    const history = new PaletteHistory(storage);
    history.open('owner@example.com');
    history.remember('first'); history.remember('second');
    expect(history.previous('unfinished draft')).toBe('second');
    expect(history.previous('second')).toBe('first');
    expect(history.previous('first')).toBe('first');
    expect(history.next()).toBe('second');
    expect(history.next()).toBe('unfinished draft');
    expect(history.browsing).toBe(false);
    expect(history.next()).toBeNull();
    history.previous('changed draft');
    history.reset();
    expect(history.next()).toBeNull();
  });

  it('persists across instances without exposing another account or signed-out history', () => {
    const history = new PaletteHistory(storage);
    history.open('first@example.com'); history.remember('private query');
    history.open('second@example.com');
    expect(history.previous('')).toBeNull();
    history.remember('another query');
    history.open(null);
    expect(history.size).toBe(0);
    const restored = new PaletteHistory(storage);
    restored.open('first@example.com');
    expect(restored.previous('')).toBe('private query');
    restored.open('second@example.com');
    expect(restored.previous('')).toBe('another query');
  });

  it('clears persisted history and respects browser-side removal', () => {
    const history = new PaletteHistory(storage);
    history.open('owner@example.com'); history.remember('coffee');
    history.clear();
    history.open('owner@example.com');
    expect(history.size).toBe(0);
    expect(values.has(key('owner@example.com'))).toBe(false);
    history.remember('new query');
    values.clear();
    history.open('owner@example.com');
    expect(history.previous('')).toBeNull();
  });

  it('rejects corrupt, oversized and structured stored data without hydrating it', () => {
    const history = new PaletteHistory(storage);
    for (const raw of ['{', '"query"', '{"items":["query"]}', 'x'.repeat(32769)]) {
      values.set(key('owner@example.com'), raw);
      history.open('owner@example.com');
      expect(history.size).toBe(0);
    }
    values.set(key('owner@example.com'), JSON.stringify([
      null, { title: 'private result' }, 42, '', 'x'.repeat(161), 'valid', 'VALID',
    ]));
    history.open('owner@example.com');
    expect(history.size).toBe(1);
    expect(history.previous('')).toBe('valid');
  });

  it('keeps functional in-memory history when storage is unavailable', () => {
    const history = new PaletteHistory(() => { throw new Error('Storage blocked'); });
    history.open('owner@example.com'); history.remember('coffee');
    history.open('owner@example.com');
    expect(history.previous('')).toBe('coffee');
    history.clear();
    expect(history.previous('')).toBeNull();
  });

  it('does not overwrite unsaved or cleared in-memory history with stale read-only storage', () => {
    values.set(key('owner@example.com'), JSON.stringify(['old query']));
    const history = new PaletteHistory(() => ({
      ...storage(),
      setItem() { throw new Error('Quota exceeded'); },
      removeItem() { throw new Error('Storage read-only'); },
    }));
    history.open('owner@example.com'); history.remember('new query');
    history.open('owner@example.com');
    expect(history.previous('')).toBe('new query');
    history.clear();
    history.open('owner@example.com');
    expect(history.size).toBe(0);
  });

  it('can reload the largest escaped queries it writes', () => {
    const history = new PaletteHistory(storage);
    history.open('owner@example.com');
    for (let i = 0; i < 20; i++) history.remember(`${i}${'\u0000'.repeat(150)}`);
    expect(values.get(key('owner@example.com'))!.length).toBeLessThan(32768);
    const restored = new PaletteHistory(storage);
    restored.open('owner@example.com');
    expect(restored.size).toBe(20);
  });
});
