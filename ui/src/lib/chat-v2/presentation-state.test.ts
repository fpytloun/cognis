import { describe, expect, it } from 'vitest';
import { DISCLOSURE_ITEM_LIMIT, TimelineDisclosureState } from './presentation-state';

describe('bounded timeline disclosure choices', () => {
  it('survives a first-member change and remembers both explicit choices', () => {
    const state = new TimelineDisclosureState();
    state.remember('tool_group', ['call:100', 'call:101'], true);
    expect(state.read('tool_group', ['call:50', 'call:100'])).toBe(true);
    state.remember('tool_group', ['call:50', 'call:100'], false);
    expect(state.read('tool_group', ['call:50'])).toBe(false);
    expect(state.read('thinking_group', ['call:50'])).toBeUndefined();
  });

  it('never retains more choices than the fixed identifier budget', () => {
    const state = new TimelineDisclosureState();
    for (let index = 0; index < 10_000; index++) {
      state.remember('tool_group', [`call:${index}`], true);
    }
    expect(state.size).toBe(DISCLOSURE_ITEM_LIMIT);
    expect(state.read('tool_group', ['call:0'])).toBeUndefined();
    expect(state.read('tool_group', ['call:9999'])).toBe(true);
    state.clear();
    expect(state.size).toBe(0);
  });
});
