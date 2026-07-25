import { blockType, type RichBlock } from '$lib/rich-deliverable';

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
  return objectList(block.rows ?? block.data);
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
