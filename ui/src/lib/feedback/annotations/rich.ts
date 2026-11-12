import type { FeedbackTextScope } from '$lib/feedback/types';
export interface RichTableSource { row: string[]; cells: Record<string, string[]> }
// Authored provenance stays beside each normalized row, never inside it, so row values and
// equality stay exactly what the renderer and its consumers expect.
const tableSources = new WeakMap<object, RichTableSource>();
export function withTableSource<T extends object>(row: T, source: RichTableSource): T {
  tableSources.set(row, source);
  return row;
}
export function tableSource(row: Record<string, unknown>): RichTableSource | null {
  return tableSources.get(row) ?? null;
}
export interface RichRenderedField { cell: string[]; row: string[]; text: string[] | null; column?: number }
const renderedFields = new WeakMap<Element, RichRenderedField>();
/** DOM provenance is installed by the renderer, never read from authored attributes. */
export function richRenderedField(node: HTMLElement, value: RichRenderedField) {
  renderedFields.set(node, value);
  return { update(next: RichRenderedField) { renderedFields.set(node, next); }, destroy() { renderedFields.delete(node); } };
}
export function renderedField(node: Node | null, root: Element): { element: Element; field: RichRenderedField } | null {
  let element = node instanceof Element ? node : node?.parentElement;
  while (element && root.contains(element)) {
    const field = renderedFields.get(element);
    if (field) return { element, field };
    element = element.parentElement;
  }
  return null;
}
export const richAnnotationIdentity = Symbol('rich-annotation-source');
type RichIdentity = Omit<Extract<FeedbackTextScope, { kind: 'rich_field' }>, 'kind' | 'item_key' | 'field_path'>;
export function richIdentity(block: Record<string, unknown>): RichIdentity | null {
  return (block as { [richAnnotationIdentity]?: RichIdentity })[richAnnotationIdentity] ?? null;
}
export interface RichTableGrid {
  identity: RichIdentity | null;
  columns: string[];
  rows: Map<number, Record<string, string[]>>;
  error: string | null;
}
const renderedTables = new WeakMap<Element, RichTableGrid>();
export function richRenderedTable(node: HTMLTableElement, grid: RichTableGrid) {
  renderedTables.set(node, grid);
  return { update(next: RichTableGrid) { renderedTables.set(node, next); }, destroy() { renderedTables.delete(node); } };
}
export function renderedTable(node: Element): RichTableGrid | null { return renderedTables.get(node) ?? null; }
/** Carry original JSON paths through media/publication copies, never DOM order or authored __metadata. */
export function annotateRichPayload(payload: unknown): unknown {
  const counts = new Map<string, number>();
  const queue: unknown[] = [payload];
  let counted = 0;
  while (queue.length && counted++ < 100_000) {
    const value = queue.pop();
    if (!value || typeof value !== 'object') continue;
    if (!Array.isArray(value) && 'id' in value && typeof value.id === 'string') counts.set(value.id, (counts.get(value.id) ?? 0) + 1);
    for (const child of Object.values(value)) queue.push(child);
  }
  const complete = queue.length === 0;
  let copied = 0;
  function copy(value: unknown, path: string, depth: number): unknown {
    if (!value || typeof value !== 'object' || depth > 64 || copied++ >= 100_000) return value;
    if (Array.isArray(value)) return value.map((child, index) => copy(child, `${path}/${index}`, depth + 1));
    const record = value as Record<string, unknown>;
    const result = Object.fromEntries(Object.entries(record).map(([key, child]) => [key, copy(child, `${path}/${key}`, depth + 1)]));
    const semantic = complete && typeof record.id === 'string' && record.id.length > 0 && counts.get(record.id) === 1;
    return { ...result, [richAnnotationIdentity]: { block_id: semantic ? record.id : path, identity: semantic ? 'semantic' : 'revision_local' } };
  }
  return copy(payload, '', 0);
}

export function richTextFields(block: Record<string, unknown>) {
  const result: { path: string[]; text: string; itemKey: string | null }[] = [];
  function visit(value: unknown, path: string[], depth: number) {
    if (depth > 16 || result.length >= 1000) return;
    if (typeof value === 'string') {
      let itemKey: string | null = null;
      let fieldPath = path;
      if (path[0] === 'items' && Array.isArray(block.items)) {
        const item = block.items[Number(path[1])] as Record<string, unknown> | undefined;
        if (typeof item?.key === 'string' && block.items.filter(value => value && typeof value === 'object' && 'key' in value && value.key === item.key).length === 1) {
          itemKey = item.key; fieldPath = path.slice(2);
        }
      }
      result.push({ path: fieldPath, text: value, itemKey }); return;
    }
    if (value && typeof value === 'object') {
      for (const [key, child] of Object.entries(value)) {
        if (!key.startsWith('__') && key !== 'id' && key !== 'type' && key !== 'key') visit(child, [...path, key], depth + 1);
        if (result.length >= 1000) break;
      }
    }
  }
  visit(block, [], 0);
  return result;
}
