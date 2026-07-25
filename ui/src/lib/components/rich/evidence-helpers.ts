import { safeUrl } from '$lib/rich-deliverable';
import { humanize, objectList, tableColumns, tableRows, valueText, type ColumnDef } from './block-helpers';
import type { RichBlock } from '$lib/rich-deliverable';

export interface NormalizedSource {
  key: string;
  title: string;
  url: string;
  publisher: string;
  date: string;
  snippet: string;
  raw: Record<string, unknown>;
}

export type SortDirection = 'asc' | 'desc';

export interface MatrixSort {
  key: string;
  direction: SortDirection;
}

export function normalizeSources(value: unknown): NormalizedSource[] {
  return objectList(value).map((source, index) => {
    const rawUrl = source.url ?? source.href;
    const key = String(source.id ?? source.key ?? source.citation_id ?? rawUrl ?? source.title ?? index + 1);
    return {
      key,
      title: String(source.title ?? source.name ?? rawUrl ?? `Source ${index + 1}`),
      url: safeUrl(rawUrl),
      publisher: String(source.publisher ?? source.site ?? source.domain ?? ''),
      date: String(source.date ?? source.published_at ?? source.updated_at ?? ''),
      snippet: String(source.snippet ?? source.quote ?? source.description ?? ''),
      raw: source,
    };
  });
}

export function citationLabel(source: NormalizedSource, index: number): string {
  const explicit = source.raw.index ?? source.raw.number ?? source.raw.label;
  return explicit ? String(explicit) : String(index + 1);
}

export function normalizeDoi(value: unknown): string {
  return String(value ?? '').trim().replace(/^(?:https?:\/\/doi\.org\/|doi:\s*)/i, '');
}

export function normalizedSourceIdentity(source: NormalizedSource): string {
  const explicit = source.raw.id ?? source.raw.key ?? source.raw.citation_id;
  if (explicit) return `id:${String(explicit).trim().toLowerCase()}`;
  const doi = normalizeDoi(source.raw.doi).toLowerCase();
  if (doi) return `doi:${doi}`;
  if (source.url) return `url:${source.url.replace(/\/$/, '').toLowerCase()}`;
  const authors = Array.isArray(source.raw.authors)
    ? source.raw.authors.map(String).join(', ')
    : String(source.raw.authors ?? source.raw.author ?? '');
  return `meta:${authors.toLowerCase()}|${source.title.toLowerCase()}|${String(source.raw.year ?? '')}`;
}

export function resolveSourceRefs(refs: unknown, sources: NormalizedSource[]): NormalizedSource[] {
  const values = Array.isArray(refs) ? refs : refs === undefined || refs === null ? [] : [refs];
  const resolved: NormalizedSource[] = [];
  const add = (source: NormalizedSource) => {
    const identity = normalizedSourceIdentity(source);
    if (!resolved.some((item) => normalizedSourceIdentity(item) === identity)) resolved.push(source);
  };
  const matchesRef = (source: NormalizedSource, reference: string): boolean => {
    const ref = reference.trim();
    return (
      source.key.trim() === ref ||
      source.title.trim() === ref ||
      source.url.trim() === ref ||
      String(source.raw.id ?? '').trim() === ref ||
      String(source.raw.key ?? '').trim() === ref ||
      String(source.raw.citation_id ?? '').trim() === ref ||
      String(source.raw.url ?? '').trim() === ref ||
      String(source.raw.href ?? '').trim() === ref
    );
  };
  for (const value of values) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const record = value as Record<string, unknown>;
      const reference = record.source_id ?? record.sourceId ?? record.ref;
      if (reference !== undefined && reference !== null) {
        const ref = String(reference).trim();
        const match = sources.find((source) => matchesRef(source, ref));
        if (match) {
          const label = String(record.label ?? '').trim();
          add(label ? normalizeSources([{ ...match.raw, title: label }])[0] : match);
          continue;
        }
      }
      for (const source of normalizeSources([value])) add(source);
      continue;
    }
    const ref = String(value).trim();
    const match = sources.find((source) => matchesRef(source, ref));
    if (match) add(match);
  }
  return resolved;
}

export function confidencePercent(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.max(0, Math.min(100, value <= 1 ? Math.round(value * 100) : Math.round(value)));
  if (typeof value !== 'string') return 0;
  const normalized = value.trim().toLowerCase();
  const numeric = Number(normalized.replace('%', ''));
  if (Number.isFinite(numeric)) return Math.max(0, Math.min(100, numeric <= 1 ? Math.round(numeric * 100) : Math.round(numeric)));
  if (normalized === 'high') return 86;
  if (normalized === 'medium') return 62;
  if (normalized === 'low') return 34;
  return 0;
}

export function confidenceLabel(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return humanize(value.trim());
  const percent = confidencePercent(value);
  if (percent >= 75) return 'High';
  if (percent >= 50) return 'Medium';
  if (percent > 0) return 'Low';
  return 'Unknown';
}

export function evidenceItems(value: unknown): Record<string, unknown>[] {
  return objectList(value);
}

export function claimItems(block: RichBlock): Record<string, unknown>[] {
  return objectList(block.claims ?? block.items ?? block.data);
}

export function matrixRows(block: RichBlock): Record<string, unknown>[] {
  return tableRows(block);
}

export function matrixColumns(block: RichBlock, rows: Record<string, unknown>[]): ColumnDef[] {
  return tableColumns(block, rows);
}

export function recommendedRow(row: Record<string, unknown>): boolean {
  const value = row.recommended ?? row.recommendation ?? row.winner ?? row.selected;
  return value === true || value === 'true' || value === 'yes' || value === 'recommended' || value === 'winner';
}

export function rowEvidence(row: Record<string, unknown>): Record<string, unknown>[] {
  return evidenceItems(row.evidence ?? row.sources ?? row.rationale);
}

export function sortableValue(value: unknown): string | number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const numeric = Number(value.replace(/[%,$\s]/g, ''));
    if (Number.isFinite(numeric) && /[0-9]/.test(value)) return numeric;
    return value.toLowerCase();
  }
  return valueText(value).toLowerCase();
}

export function sortMatrixRows(rows: Record<string, unknown>[], sort: MatrixSort | null): Record<string, unknown>[] {
  if (!sort?.key) return rows;
  const direction = sort.direction === 'desc' ? -1 : 1;
  return [...rows].sort((left, right) => {
    const a = sortableValue(left[sort.key]);
    const b = sortableValue(right[sort.key]);
    if (a === b) return 0;
    if (typeof a === 'number' && typeof b === 'number') return (a - b) * direction;
    return String(a).localeCompare(String(b)) * direction;
  });
}

export function sourceMeta(source: NormalizedSource): string {
  return [source.publisher, source.date].filter(Boolean).join(' · ');
}
