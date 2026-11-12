import { blockType, type RichBlock } from '$lib/rich-deliverable';
import { richIdentity, tableSource, withTableSource, type RichTableGrid } from '$lib/feedback/annotations/rich';

export type ColumnDef = { key: string; label: string; align?: string };

export function objectList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
    : [];
}

export function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

export function sourceRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/** Range eligibility consumes the existing normalization and its authored provenance. */
export function tableRangeGrid(block: RichBlock, rows: Record<string, unknown>[], columns: ColumnDef[]): RichTableGrid {
  const grid: RichTableGrid = { identity: richIdentity(block), columns: columns.map(col => col.key), rows: new Map(), error: null };
  const raw = block.rows ?? block.data;
  if (!grid.identity || !['table', 'comparison_matrix', 'decision_matrix'].includes(blockType(block)))
    grid.error = 'This derived table has no exact authored grid. Select its source text instead.';
  else if (!Array.isArray(raw) || !raw.length || raw.length > 1000 || !columns.length || columns.length > 128)
    grid.error = 'Table ranges support up to 1,000 authored rows and 128 columns.';
  else if (new Set(grid.columns).size !== columns.length || Array.isArray(block.columns) && block.columns.some(col => {
    const key = typeof col === 'string' ? col : col && typeof col === 'object' ? (col as Record<string, unknown>).key ?? (col as Record<string, unknown>).id ?? (col as Record<string, unknown>).label : null;
    return typeof key !== 'string' || !key;
  })) grid.error = 'Column identities are ambiguous. Select source text instead.';
  if (!grid.error && Array.isArray(raw)) for (const row of rows) {
    const source = tableSource(row), ordinal = Number(source?.row[1]);
    if (!source || !Number.isInteger(ordinal) || ordinal < 0 || ordinal >= 1000 || grid.rows.has(ordinal)) {
      grid.error = 'Row membership cannot be proven. Select source text instead.'; break;
    }
    const authored = raw[ordinal] as unknown;
    const values = Array.isArray(authored) ? authored : authored && typeof authored === 'object' && 'values' in authored && Array.isArray(authored.values) ? authored.values : null;
    if (values) {
      const keys = values.map((_: unknown, index: number) => columns[index]?.key ?? String(index));
      if (new Set(keys).size !== keys.length) { grid.error = 'Cell membership is ambiguous. Select source text instead.'; break; }
    }
    grid.rows.set(ordinal, source.cells);
  }
  return grid;
}

export function humanize(value: string): string {
  return value.replace(/[_-]/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}

export function valueText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

export function tableRows(block: RichBlock): Record<string, unknown>[] {
  const rows = block.rows ?? block.data;
  if (!Array.isArray(rows)) return [];
  const columns = tableColumns(block, []);
  const sourceKey = block.rows !== undefined && block.rows !== null ? 'rows' : 'data';
  return rows.flatMap((row, ordinal): Record<string, unknown>[] => {
    const path = [sourceKey, String(ordinal)];
    if (Array.isArray(row)) {
      return [withTableSource(Object.fromEntries(row.map((cell, index) => [columns[index]?.key ?? String(index), cell])),
        { row: path, cells: Object.fromEntries(row.map((_cell, index) => [columns[index]?.key ?? String(index), [...path, String(index)]])) })];
    }
    if (!row || typeof row !== 'object') return [];
    const record = row as Record<string, unknown>;
    const values = Array.isArray(record.values)
      ? Object.fromEntries(record.values.map((cell, index) => [columns[index]?.key ?? String(index), cell]))
      : {};
    return [withTableSource({ ...values, ...record }, { row: path, cells: {
      ...Object.fromEntries((Array.isArray(record.values) ? record.values : []).map((_, index) => [columns[index]?.key ?? String(index), [...path, 'values', String(index)]])),
      ...Object.fromEntries(Object.keys(record).map(key => [key, [...path, key]])),
    } })];
  });
}

export function tableColumns(block: RichBlock, currentRows: Record<string, unknown>[]): ColumnDef[] {
  const value = block.columns;
  if (Array.isArray(value)) {
    return value
      .map((col): ColumnDef => {
        if (typeof col === 'string') return { key: col, label: humanize(col) };
        const record = sourceRecord(col);
        const key = String(record.key ?? record.id ?? record.label ?? '');
        return {
          key,
          label: String(record.label ?? record.title ?? humanize(key)),
          align: String(record.align ?? ''),
        };
      })
      .filter((col) => col.key);
  }
  return currentRows[0] ? Object.keys(currentRows[0]).map((key) => ({ key, label: humanize(key) })) : [];
}

export function blockTone(block: RichBlock): string {
  return String(block.tone ?? block.variant ?? 'neutral');
}

const SURFACE_VALUES = new Set(['plain', 'subtle', 'outlined', 'raised', 'accent']);

/**
 * Generic block `surface`. Returns `undefined` when the author did not set
 * one so every existing block keeps its current default appearance
 * unchanged -- the CSS only overrides look for an explicitly authored,
 * recognized value (see `[data-rich-surface]` rules in rich-blocks.css).
 */
export function blockSurface(block: RichBlock): string | undefined {
  const value = String(block.surface ?? '');
  return SURFACE_VALUES.has(value) ? value : undefined;
}

/**
 * Generic block `span`, 1..4, for a block placed directly inside a grid
 * (`grid`/`columns`/`card_grid`). Returns `undefined` when unset or
 * out-of-range so the grid falls back to normal auto-placement.
 */
export function blockSpan(block: RichBlock): number | undefined {
  const raw = Number(block.span);
  return Number.isFinite(raw) && raw > 0 ? Math.max(1, Math.min(4, Math.round(raw))) : undefined;
}

/** Percent (0..100) for a value/max progress pair, used by both the metric
 * progress bar and typed `progress` table cells. Missing/non-finite max
 * falls back to 100 (a plain percentage). */
export function progressPercent(value: unknown, max: unknown): number {
  const numericValue = Number(value);
  if (!Number.isFinite(numericValue)) return 0;
  const numericMax = Number(max);
  const denominator = Number.isFinite(numericMax) && numericMax > 0 ? numericMax : 100;
  return Math.max(0, Math.min(100, (numericValue / denominator) * 100));
}

export interface TypedTableCell {
  type: 'text' | 'number' | 'code' | 'badge' | 'progress';
  value: unknown;
  label?: string;
  tone?: string;
  emphasis?: 'normal' | 'strong' | 'muted';
  align?: 'start' | 'center' | 'end';
  max?: number;
}

const TYPED_CELL_TYPES = new Set(['text', 'number', 'code', 'badge', 'progress']);

/** A typed table cell is an object with a recognized `type` and a `value`
 * key -- scalars (string/number/boolean/null) and plain arrays/objects
 * without that shape keep rendering through the existing `valueText` path
 * unchanged. */
export function asTypedCell(value: unknown): TypedTableCell | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (!TYPED_CELL_TYPES.has(String(record.type)) || !('value' in record)) return null;
  return {
    type: record.type as TypedTableCell['type'],
    value: record.value,
    label: typeof record.label === 'string' ? record.label : undefined,
    tone: typeof record.tone === 'string' ? record.tone : undefined,
    emphasis: record.emphasis === 'strong' || record.emphasis === 'muted' ? record.emphasis : 'normal',
    align: record.align === 'center' || record.align === 'end' ? record.align : record.align === 'start' ? 'start' : undefined,
    max: Number.isFinite(Number(record.max)) ? Number(record.max) : undefined,
  };
}

export function blockSources(block: RichBlock, fallbackSources: Record<string, unknown>[]): unknown[] {
  return Array.isArray(block.sources) ? block.sources : fallbackSources;
}

export function listBackedItems(block: RichBlock): Record<string, unknown>[] {
  return objectList(block.items ?? block.data ?? block.steps);
}

export function galleryBlocks(children: RichBlock[]): RichBlock[] {
  return children.map((child) => ({ ...child, type: String(child.type ?? child.kind ?? 'figure') }));
}

export function isGroupBlock(block: RichBlock): boolean {
  const type = blockType(block);
  return type === 'columns' || type === 'grid' || type === 'card_grid';
}
