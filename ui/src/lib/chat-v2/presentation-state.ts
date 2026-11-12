import type { TimelineRow } from './tool-groups';

export const timelineDisclosureContext = Symbol('timeline-disclosure');
export const DISCLOSURE_ITEM_LIMIT = 1_024;

/** Only identifiers and explicit choices, bounded independently of loaded history. */
export class TimelineDisclosureState {
  private choices = new Map<string, boolean>();
  private scopeKey: string | undefined;

  constructor(private readonly currentScope?: () => string) {}

  private ensureScope(): void {
    const key = this.currentScope?.();
    if (key !== this.scopeKey) {
      this.clear();
      this.scopeKey = key;
    }
  }

  get size(): number { return this.choices.size; }

  clear(): void { this.choices.clear(); }

  read(kind: string, ids: readonly string[]): boolean | undefined {
    this.ensureScope();
    for (const id of ids) {
      const choice = this.choices.get(`${kind}:${id}`);
      if (choice !== undefined) return choice;
    }
    // Backfill can wrap a previously standalone, explicitly opened group in a
    // new activity segment. Do not hide that reader's content behind a fold.
    if (kind === 'activity_segment' && ids.some(id =>
      this.choices.get(`tool_group:${id}`) === true
      || this.choices.get(`thinking_group:${id}`) === true)) return true;
    return undefined;
  }

  remember(kind: string, ids: readonly string[], expanded: boolean): void {
    this.ensureScope();
    for (const id of ids) {
      const key = `${kind}:${id}`;
      this.choices.delete(key);
      this.choices.set(key, expanded);
    }
    while (this.choices.size > DISCLOSURE_ITEM_LIMIT) {
      this.choices.delete(this.choices.keys().next().value!);
    }
  }
}

export function timelineRowItemIds(row: TimelineRow): string[] {
  if (row.kind === 'item') {
    return row.item.kind === 'tool_call'
      ? [row.item.id, ...(row.item.program_children ?? []).map(item => item.id)]
      : [row.item.id];
  }
  if (row.kind !== 'activity_segment') return row.items.map(item => item.id);
  return row.entries.flatMap(entry => entry.kind === 'tool_group'
    ? entry.group.items.map(item => item.id) : [entry.item.id]);
}
