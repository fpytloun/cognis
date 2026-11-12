import type { Snapshot } from '@sveltejs/kit';
import { getContext, setContext } from 'svelte';
import type { UsageHistoryState } from './query-state';

const context = Symbol('layout-usage-history');

/** The app layout exists when Kit restores, even while its authenticated children do not. */
export function createUsageSnapshot(current: () => UsageHistoryState): Snapshot<UsageHistoryState> & { readonly state: UsageHistoryState } {
  let restored = $state.raw<{ entry: UsageHistoryState; value: UsageHistoryState } | null>(null);
  const state = () => {
    const entry = current();
    const value = restored?.entry === entry ? { ...restored.value, ...entry } : entry;
    return { usage: value.usage };
  };
  return {
    get state() { return state(); },
    capture() { return { usage: state().usage }; },
    restore(value) {
      restored = { entry: current(), value: value && typeof value === 'object' ? value : {} };
    },
  };
}

export function provideUsageHistory(current: () => UsageHistoryState) {
  return setContext(context, createUsageSnapshot(current));
}
export function getUsageHistory(): ReturnType<typeof createUsageSnapshot> {
  return getContext(context);
}
