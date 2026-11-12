import { describe, expect, it } from 'vitest';
import { checkedTableRange, draggedTableRange, intervalHit } from './table-range';
import { annotateRichPayload, tableSource } from './rich';
import { tableColumns, tableRangeGrid, tableRows } from '$lib/components/rich/block-helpers';
import type { RichBlock } from '$lib/rich-deliverable';
import type { RichTableRange } from '../types';
import { targetSummary } from '../target-summary';

const authored = (block: RichBlock) => (annotateRichPayload({ blocks: [block] }) as { blocks: RichBlock[] }).blocks[0];
function grid(extra: Partial<RichBlock> = {}) {
  const block = authored({ id: 'release-table', type: 'table', columns: ['name', 'count'],
    rows: [['Zulu', 3], { values: ['Alpha', 4] }, { name: 'Beta', count: 5 }], ...extra });
  const rows = tableRows(block);
  return tableRangeGrid(block, rows, tableColumns(block, rows));
}
const position = (row: number, column: number) => ({ row, column });
describe('exact authored table range preview eligibility', () => {
  it('resolves unequal-width header/row containment before nearest-edge fallback', () => {
    const intervals = [{ start: 0, end: 300 }, { start: 300, end: 400 }];
    expect([275, 299, 300, 325, -10, 410].map(value => intervalHit(value, intervals))).toEqual([0, 0, 1, 1, 0, 1]);
    expect(intervalHit(Infinity, intervals)).toBeNull();
    expect(intervalHit(20, [{ start: 0, end: 0 }])).toBeNull();
  });
  it('preserves nonconsecutive authored membership under sorting and reverse dragging', () => {
    const view = grid();
    expect(draggedTableRange(view, [1, 2, 0], position(2, 1), position(1, 0), 'cells')).toEqual({
      representation: 'rich-table-v1', scope: 'cells', rows: [1, 2], column_start: 0, column_end: 2,
    });
    expect(draggedTableRange(view, [2, 0, 1], position(0, 0), position(2, 0), 'rows').rows).toEqual([0, 2]);
    expect(draggedTableRange(view, [1, 2, 0], position(0, 1), position(0, 0), 'columns').rows).toEqual([0, 1, 2]);
  });
  it('gates whole columns when filtering hides canonical rows, without gating proven cells', () => {
    expect(() => draggedTableRange(grid(), [2, 0], position(2, 0), position(0, 1), 'columns')).toThrow('all authored rows');
    expect(draggedTableRange(grid(), [2, 0], position(2, 0), position(0, 0), 'cells').rows).toEqual([0, 2]);
  });
  it.each([
    { columns: ['name', 'name'] }, { columns: [1] }, { rows: [['Zulu'], ['Alpha', 4]] },
    { rows: [['Zulu', 3], null, ['Beta', 5]] }, { rows: [['Zulu', 3], { name: 'Alpha' }] },
    { columns: ['name', 'constructor'], rows: [{ name: 'Zulu' }, { name: 'Alpha' }] },
  ])('rejects ambiguous, missing and ragged membership: %j', extra => {
    expect(() => draggedTableRange(grid(extra), [0, 1], position(0, 0), position(1, 1), 'cells')).toThrow();
  });
  it.each([-1, 1.5, Infinity])('rejects signed or invalid column coordinates %s', column => {
    expect(() => draggedTableRange(grid(), [0, 1, 2], position(0, column), position(0, 1), 'cells')).toThrow();
  });
  it('checks budgets before enumerating products or accepts empty/duplicate/reversed row lists', () => {
    const range: RichTableRange = { representation: 'rich-table-v1', scope: 'cells', rows: [0, 2], column_start: 0, column_end: 2 };
    for (const rows of [[], [2, 0], [-1], [0, 0], Array.from({ length: 1001 }, (_, i) => i)])
      expect(() => checkedTableRange(grid(), { ...range, rows })).toThrow();
    expect(() => checkedTableRange(grid(), { ...range, column_end: 128 })).toThrow();
  });
  it('keeps numeric-key values provenance in declared order, not Object.keys enumeration order', () => {
    const block = authored({ type: 'table', columns: ['10', '2'], rows: [{ values: ['ten', 'two'] }] });
    expect(tableSource(tableRows(block)[0])?.cells['10']).toEqual(['rows', '0', 'values', '0']);
    expect(tableSource(tableRows(block)[0])?.cells['2']).toEqual(['rows', '0', 'values', '1']);
  });
  it('summarizes coordinates without pretending disjoint authored rows are an interval or text quote', () => {
    const result = targetSummary({ source: null, sourceTitle: null, locator: { kind: 'rich',
      block_id: 'release-table', identity: 'semantic', item_key: null, field_path: [], range: null,
      table_range: { representation: 'rich-table-v1', scope: 'cells', rows: [0, 2], column_start: 25, column_end: 28 } } });
    expect(result).toEqual({ label: 'Rows 1, 3 · Columns Z–AB', quote: null, quoteKind: null });
  });
});
