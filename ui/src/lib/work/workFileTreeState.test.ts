import { beforeEach, describe, expect, it, vi } from 'vitest';
import { clearWorkFileTreeStates, getWorkFileTreeState, setWorkFileTreeState } from './workFileTreeState';

function state(selectedId: string) {
  return { query: '', statusFilter: '', expanded: [], selectedId, treeScrollTop: 0, diffScrollTop: 0 };
}

describe('workFileTreeState', () => {
  beforeEach(() => clearWorkFileTreeStates());

  it('expires after five minutes', () => {
    vi.useFakeTimers();
    setWorkFileTreeState('scope:files', state('file'));
    vi.advanceTimersByTime(300_001);
    expect(getWorkFileTreeState('scope:files')).toBeNull();
    vi.useRealTimers();
  });

  it('keeps at most 24 LRU entries', () => {
    for (let index = 0; index < 25; index += 1) {
      setWorkFileTreeState(`scope:${index}`, state(`${index}`));
    }
    expect(getWorkFileTreeState('scope:0')).toBeNull();
    expect(getWorkFileTreeState('scope:24')?.selectedId).toBe('24');
  });
});
