import type { FeedbackLocator, FeedbackSurfaceBinding, FeedbackTargetGeometry, RichTableRange } from '../types';
import { targetSummary } from '../target-summary';
import { clearNativeSelection, targetView } from './native-selection';
import { renderedField, renderedTable, type RichTableGrid } from './rich';
import { surfaceIdentity } from './surface-identity';

type RichLocator = Extract<FeedbackLocator, { kind: 'rich' }>;
type Position = { row: number; column: number };
type Scope = RichTableRange['scope'];

/** Client preview eligibility only. The source owner repeats canonical validation on save. */
export function checkedTableRange(grid: RichTableGrid, range: RichTableRange): RichTableRange {
  const width = range.column_end - range.column_start;
  if (grid.error) throw new Error(grid.error);
  if (range.representation !== 'rich-table-v1' || !['cells', 'rows', 'columns'].includes(range.scope)
    || !range.rows.length || range.rows.length > 1000 || !Number.isInteger(range.column_start)
    || !Number.isInteger(range.column_end) || range.column_start < 0 || width <= 0
    || range.column_end > grid.columns.length || range.rows.length * width > 4096)
    throw new Error('Select at most 4,096 exact authored cells.');
  if (range.scope === 'rows' && (range.column_start !== 0 || range.column_end !== grid.columns.length))
    throw new Error('Whole rows require every authored data column.');
  if (range.scope === 'columns' && JSON.stringify(range.rows) !== JSON.stringify([...grid.rows.keys()].sort((a, b) => a - b)))
    throw new Error('Whole columns require all authored rows. Clear row filtering first.');
  for (let index = 0; index < range.rows.length; index++) {
    const ordinal = range.rows[index], row = grid.rows.get(ordinal);
    if (!Number.isInteger(ordinal) || ordinal < 0 || ordinal >= 1000 || index > 0 && ordinal <= range.rows[index - 1] || !row)
      throw new Error('Selected rows lack exact authored membership.');
    for (let column = range.column_start; column < range.column_end; column++)
      if (!Object.hasOwn(row, grid.columns[column]) || !Array.isArray(row[grid.columns[column]]) || !row[grid.columns[column]].length)
        throw new Error('This selection crosses a missing authored cell. Choose a complete range.');
  }
  return range;
}

export function draggedTableRange(grid: RichTableGrid, visibleRows: number[], start: Position, end: Position, scope: Scope): RichTableRange {
  const first = visibleRows.indexOf(start.row), last = visibleRows.indexOf(end.row);
  if (first < 0 || last < 0 || new Set(visibleRows).size !== visibleRows.length)
    throw new Error('Rendered row membership is missing or ambiguous.');
  const rows = (scope === 'columns' ? [...visibleRows] : visibleRows.slice(Math.min(first, last), Math.max(first, last) + 1)).sort((a, b) => a - b);
  return checkedTableRange(grid, { representation: 'rich-table-v1', scope, rows,
    column_start: scope === 'rows' ? 0 : Math.min(start.column, end.column),
    column_end: scope === 'rows' ? grid.columns.length : Math.max(start.column, end.column) + 1 });
}

interface Snapshot {
  table: HTMLTableElement; grid: RichTableGrid; rows: number[];
  cells: Map<string, HTMLTableCellElement>; headers: HTMLTableCellElement[];
  rowNodes: Map<number, HTMLTableRowElement>;
}
const cellKey = (row: number, column: number) => `${row}:${column}`;
const samePath = (a: string[], b: string[] | undefined) => Array.isArray(a) && Array.isArray(b) && JSON.stringify(a) === JSON.stringify(b);

/** Containment wins over center proximity; only gaps/outside positions snap to edges. */
export function intervalHit(coordinate: number, intervals: { start: number; end: number }[]): number | null {
  if (!Number.isFinite(coordinate)) return null;
  let nearest: number | null = null, distance = Infinity;
  for (let index = 0; index < intervals.length; index++) {
    const { start, end } = intervals[index];
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
    if (coordinate >= start && coordinate < end) return index;
    const gap = Math.max(start - coordinate, 0, coordinate - end);
    if (gap < distance) { nearest = index; distance = gap; }
  }
  return nearest;
}

function cellGeometry(nodes: HTMLTableCellElement[] | null): FeedbackTargetGeometry | null {
  if (!nodes) return null;
  const rects = nodes.map(node => {
    const rect = node.getBoundingClientRect();
    return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
  });
  return rects.every(rect => rect.width > 0 && rect.height > 0) ? { space: 'client', rects } : null;
}

/** One renderer-local gesture owner. No draft, persistence or shared presentation state. */
export function createTableRanges(root: HTMLElement, instance: string, error: (message: string) => void) {
  let binding: FeedbackSurfaceBinding | undefined, fence = '', destroyed = false;
  let selected: RichLocator | null = null;
  let drag: { id: number; scope: Scope; start: Position } | null = null;
  let frame = 0;
  const marked = new Set<HTMLTableCellElement>();
  let observedTable: HTMLTableElement | null = null;

  function snapshot(): Snapshot | null {
    const table = [...root.querySelectorAll('table')].find(node =>
      node.closest('[data-feedback-rich-block]') === root && renderedTable(node));
    if (!table) return null;
    const grid = renderedTable(table)!;
    const cells = new Map<string, HTMLTableCellElement>(), rows: number[] = [];
    const rowNodes = new Map<number, HTMLTableRowElement>();
    for (const tr of table.tBodies[0]?.rows ?? []) {
      let ordinal: number | null = null;
      for (const td of tr.cells) {
        const field = renderedField(td, root)?.field, column = field?.column;
        if (!field || column === undefined || !Number.isInteger(column) || column < 0 || column >= grid.columns.length) continue;
        const row = Number(field.row[1]), authored = grid.rows.get(row);
        if (!samePath(field.cell, authored?.[grid.columns[column]])) continue;
        const key = cellKey(row, column);
        if (cells.has(key) || ordinal !== null && ordinal !== row) return null;
        cells.set(key, td); ordinal = row;
      }
      if (ordinal !== null) {
        if (rowNodes.has(ordinal)) return null;
        rows.push(ordinal); rowNodes.set(ordinal, tr);
      }
    }
    return { table, grid, rows, cells, rowNodes, headers: [...(table.tHead?.rows[0]?.cells ?? [])].slice(0, grid.columns.length) };
  }
  function nodesFor(locator: FeedbackLocator, view = snapshot()): HTMLTableCellElement[] | null {
    if (locator.kind !== 'rich' || !locator.table_range || locator.range || locator.item_key !== null || locator.field_path.length
      || !view || locator.block_id !== view.grid.identity?.block_id || locator.identity !== view.grid.identity.identity) return null;
    try { checkedTableRange(view.grid, locator.table_range); } catch { return null; }
    const nodes: HTMLTableCellElement[] = [];
    for (const row of locator.table_range.rows) for (let column = locator.table_range.column_start; column < locator.table_range.column_end; column++) {
      const node = view.cells.get(cellKey(row, column));
      if (!node) return null;
      nodes.push(node);
    }
    return nodes;
  }
  function geometry(locator: FeedbackLocator): FeedbackTargetGeometry | null {
    return cellGeometry(nodesFor(locator));
  }
  function paint(view = snapshot()) {
    for (const node of marked) node.removeAttribute('data-feedback-table-selected');
    marked.clear();
    const values = binding?.highlights.filter(value => value.category !== 'not_located').map(value => value.locator) ?? [];
    if (selected) values.push(selected);
    for (const value of values) for (const node of nodesFor(value, view) ?? []) {
      node.setAttribute('data-feedback-table-selected', ''); marked.add(node);
    }
  }
  function enabled() {
    return binding?.mode === 'comment' && binding.source.capabilities.read && binding.source.capabilities.comment
      && binding.source.availability !== 'unavailable' && binding.completeness !== 'unavailable'
      && binding.supported_locator_kinds.includes('rich') && binding.source.permitted_locator_kinds.includes('rich');
  }
  function stopDrag() {
    const previous = drag; drag = null;
    if (previous && root.hasPointerCapture?.(previous.id)) root.releasePointerCapture(previous.id);
    if (previous) binding?.onInteractionChange(false);
  }
  function clear(notify = true) {
    stopDrag(); selected = null;
    if (notify) binding?.onTargetChange?.(null);
    paint(); schedule();
  }
  function publish(view: Snapshot, start: Position, end: Position, scope: Scope) {
    const table_range = draggedTableRange(view.grid, view.rows, start, end, scope);
    if (!view.grid.identity) throw new Error('This table has no authored identity.');
    const locator: RichLocator = { kind: 'rich', ...view.grid.identity, item_key: null, field_path: [], range: null, table_range };
    if (!cellGeometry(nodesFor(locator, view))) throw new Error('Some selected cells are hidden or cannot be located. Choose a visible complete range.');
    selected = locator; error('');
    paint(view);
    const current = binding!;
    const capturedFence = fence;
    const target = targetView(current, instance, { locator },
      targetSummary({ locator, source: null, sourceTitle: null }).label, () => capturedFence === fence ? geometry(locator) : null);
    target.validate = async () => {
      if (capturedFence !== fence || !enabled() || !geometry(locator))
        throw new Error('This exact table target is no longer available. Select it again.');
    };
    current.onTargetChange?.(target);
    schedule();
  }
  function attempt(view: Snapshot, start: Position, end: Position, scope: Scope) {
    try { publish(view, start, end, scope); } catch (reason) {
      clear(); error(reason instanceof Error ? reason.message : 'Exact table selection is unavailable.');
    }
  }
  function point(view: Snapshot, x: number, y: number): Position | null {
    const hit = root.ownerDocument.elementFromPoint?.(x, y)?.closest('td');
    for (const [key, node] of view.cells) if (node === hit) {
      const [row, column] = key.split(':').map(Number); return { row, column };
    }
    const row = intervalHit(y, view.rows.map(ordinal => {
      const rect = view.rowNodes.get(ordinal)!.getBoundingClientRect();
      return { start: rect.top, end: rect.bottom };
    }));
    const column = intervalHit(x, view.headers.map(header => {
      const rect = header.getBoundingClientRect(); return { start: rect.left, end: rect.right };
    }));
    return row === null || column === null ? null : { row: view.rows[row], column };
  }
  /** Comment mode is a crosshair: press on a cell and drag to the last cell, like a spreadsheet. */
  function cellDown(event: PointerEvent) {
    if (!enabled() || drag || event.button !== 0 || event.pointerType === 'touch') return;
    const node = (event.target as Element)?.closest?.('td'), view = snapshot();
    if (!view || !node || node.closest('[data-feedback-rich-block]') !== root) return;
    const start = point(view, event.clientX, event.clientY);
    if (!start) return;
    event.preventDefault(); event.stopPropagation();
    clearNativeSelection(root);
    drag = { id: event.pointerId, scope: 'cells', start };
    binding?.onInteractionChange(true);
    root.setPointerCapture?.(event.pointerId);
    attempt(view, start, start, 'cells');
  }
  function pointerMove(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.id) return;
    const view = snapshot(), end = view && point(view, event.clientX, event.clientY);
    if (!view || !end) return;
    event.preventDefault(); event.stopPropagation();
    attempt(view, drag.start, end, drag.scope);
  }
  function pointerUp(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.id) return;
    event.preventDefault(); event.stopPropagation(); stopDrag(); schedule();
  }
  function refresh() {
    frame = 0;
    if (destroyed) return;
    const view = snapshot();
    if (selected && !cellGeometry(nodesFor(selected, view))) clear();
    paint(view);
    if (!view) return;
    if (view.table !== observedTable) {
      mutations.disconnect(); observedTable = view.table;
      mutations.observe(view.table, { childList: true, subtree: true });
    }
  }
  function schedule() { if (!destroyed && !frame) frame = requestAnimationFrame(refresh); }
  const mutations = new MutationObserver(schedule), resize = new ResizeObserver(schedule);
  resize.observe(root);
  root.addEventListener('pointerdown', cellDown);
  root.addEventListener('pointermove', pointerMove);
  root.addEventListener('pointerup', pointerUp);
  root.addEventListener('pointercancel', pointerUp);
  window.addEventListener('scroll', schedule, true);
  window.addEventListener('resize', schedule);
  return {
    get hasSelection() { return selected !== null; },
    clear, geometry, nodesFor,
    update(next: FeedbackSurfaceBinding | undefined) {
      const nextFence = next ? JSON.stringify([surfaceIdentity(next), next.source.content_digest, next.mode,
        next.source.capabilities, next.source.availability, next.completeness, next.supported_locator_kinds, next.source.permitted_locator_kinds]) : '';
      if (nextFence !== fence) { clear(); fence = nextFence; }
      binding = next; schedule();
    },
    destroy() {
      clear(); destroyed = true; cancelAnimationFrame(frame); mutations.disconnect(); resize.disconnect();
      root.removeEventListener('pointerdown', cellDown); root.removeEventListener('pointermove', pointerMove);
      root.removeEventListener('pointerup', pointerUp); root.removeEventListener('pointercancel', pointerUp);
      window.removeEventListener('scroll', schedule, true); window.removeEventListener('resize', schedule);
    },
  };
}
export type TableRangeController = ReturnType<typeof createTableRanges>;
