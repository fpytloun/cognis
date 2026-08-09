export type CachedFileTreeState = {
  query: string;
  statusFilter: string;
  expanded: string[];
  selectedId: string | null;
  treeScrollTop: number;
  diffScrollTop: number;
};

const MAX_STATES = 24;
const TTL_MS = 300_000;
type Entry = { value: CachedFileTreeState; expiresAt: number };
const states = new Map<string, Entry>();

export function getWorkFileTreeState(key: string): CachedFileTreeState | null {
  const entry = states.get(key);
  if (!entry) return null;
  if (entry.expiresAt <= Date.now()) {
    states.delete(key);
    return null;
  }
  states.delete(key);
  states.set(key, entry);
  return { ...entry.value, expanded: [...entry.value.expanded] };
}

export function setWorkFileTreeState(key: string, state: CachedFileTreeState): void {
  states.delete(key);
  states.set(key, {
    value: { ...state, expanded: [...state.expanded] },
    expiresAt: Date.now() + TTL_MS,
  });
  while (states.size > MAX_STATES) {
    const oldest = states.keys().next().value;
    if (typeof oldest !== 'string') break;
    states.delete(oldest);
  }
}

export function clearWorkFileTreeStates(scopeKey?: string): void {
  if (!scopeKey) {
    states.clear();
    return;
  }
  for (const key of states.keys()) {
    if (key.startsWith(`${scopeKey}:`)) states.delete(key);
  }
}
