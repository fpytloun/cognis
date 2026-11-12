import { describe, expect, it } from 'vitest';
import { annotateRichPayload, richIdentity, richTextFields, tableSource } from './rich';
import { tableRows } from '$lib/components/rich/block-helpers';

describe('canonical rich source identity', () => {
  it('retains authored array, values and object cell paths through sorting without exposing metadata', () => {
    const raw = { type: 'table', columns: ['name', 'count'], rows: [['Zulu', 3], { values: ['Alpha', 4] }, { name: 'Beta', count: 5 }] };
    const rows = tableRows(raw).sort((left, right) => String(left.name).localeCompare(String(right.name)));
    expect(tableSource(rows[0])).toMatchObject({ row: ['rows', '1'], cells: { name: ['rows', '1', 'values', '0'], count: ['rows', '1', 'values', '1'] } });
    expect(tableSource(rows[1])?.cells.count).toEqual(['rows', '2', 'count']);
    expect(tableSource(rows[2])?.cells.name).toEqual(['rows', '0', '0']);
    expect(JSON.stringify(rows)).not.toContain('rich-table-source');
  });
  it('keeps exact JSON paths through copies and globally ambiguous IDs', () => {
    const raw = { blocks: [{ id: 'shared', type: 'grid', children: [{ id: 'shared', type: 'markdown', content: 'Child' }] }, { id: 'unique', type: 'table', rows: [{ value: 'A' }] }] };
    const annotated = annotateRichPayload(raw) as typeof raw;
    expect(richIdentity({ ...annotated.blocks[0] })).toEqual({ block_id: '/blocks/0', identity: 'revision_local' });
    expect(richIdentity(annotated.blocks[0].children![0])).toEqual({ block_id: '/blocks/0/children/0', identity: 'revision_local' });
    expect(richIdentity(annotated.blocks[1])).toEqual({ block_id: 'unique', identity: 'semantic' });
    expect(richIdentity(raw.blocks[0])).toBeNull();
    expect(JSON.stringify(annotated)).toBe(JSON.stringify(raw));
  });
  it('uses item keys only for unique canonical direct items and paths relative to that item', () => {
    const fields = richTextFields({ items: [{ key: 'stable', title: 'First' }, { key: 'duplicate', title: 'Second' }, { key: 'duplicate', title: 'Third' }],
      rows: [{ key: 'row-key', value: 'Cell' }] });
    expect(fields.find(value => value.text === 'First')).toMatchObject({ path: ['title'], itemKey: 'stable' });
    expect(fields.find(value => value.text === 'Second')).toMatchObject({ path: ['items', '1', 'title'], itemKey: null });
    expect(fields.find(value => value.text === 'Cell')).toMatchObject({ path: ['rows', '0', 'value'], itemKey: null });
  });
  it('bounds field enumeration without inventing identities', () => {
    expect(richTextFields({ rows: Array.from({ length: 1001 }, (_, i) => `Row ${i}`) })).toHaveLength(1000);
    expect(richIdentity({ __feedback_path: '/blocks/0', id: 'authored' })).toBeNull();
  });
});
