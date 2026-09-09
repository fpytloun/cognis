import { describe, expect, it } from 'vitest';

import {
  workspaceDockInsertion,
  workspaceDockOrderLefts,
  type WorkspaceDockSlot,
} from './workspace-dock-drag';

const slots: WorkspaceDockSlot[] = [
  { key: 'a', left: 0, width: 80 },
  { key: 'b', left: 88, width: 160 },
  { key: 'c', left: 256, width: 60 },
];

describe('workspace dock drag geometry', () => {
  it('uses stable variable-width slot centers', () => {
    expect(workspaceDockInsertion(slots, 'b', 30, 1)).toEqual({
      index: 0,
      order: ['b', 'a', 'c'],
    });
    expect(workspaceDockInsertion(slots, 'b', 300, 1)).toEqual({
      index: 2,
      order: ['a', 'c', 'b'],
    });
    expect(workspaceDockOrderLefts(slots, ['b', 'a', 'c'], 8)).toEqual({
      b: 0,
      a: 168,
      c: 256,
    });
  });

  it('does not oscillate while the pointer jitters inside hysteresis', () => {
    const moved = workspaceDockInsertion(slots, 'b', 45, 1);
    expect(moved.index).toBe(1);
    expect(workspaceDockInsertion(slots, 'b', 41, moved.index).index).toBe(1);
    expect(workspaceDockInsertion(slots, 'b', 33, moved.index).index).toBe(0);
    expect(workspaceDockInsertion(slots, 'b', 43, 0).index).toBe(0);
    expect(workspaceDockInsertion(slots, 'b', 47, 0).index).toBe(1);
  });

  it('updates deterministically when scrolling shifts stable bounds', () => {
    const shifted = slots.map((slot) => ({ ...slot, left: slot.left - 120 }));
    expect(workspaceDockInsertion(shifted, 'b', 180, 1)).toEqual({
      index: 2,
      order: ['a', 'c', 'b'],
    });
  });
});
