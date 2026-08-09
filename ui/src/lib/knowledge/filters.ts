/**
 * Schema-aware filter builder helpers for knowledgebase Search/Ask, plus
 * URL state serialization shared by both modes.
 */
import type { KnowledgebaseFilter, KnowledgebaseFilterOp } from '$lib/types/api';

export type MetadataFieldType = 'string' | 'keyword' | 'number' | 'integer' | 'boolean' | 'date' | 'datetime' | 'array' | 'string[]';

export interface MetadataFieldSchema {
  type: MetadataFieldType | string;
  description?: string;
  enum?: (string | number)[];
  items?: { type?: string; enum?: (string | number)[] };
  filterable?: boolean;
}

export interface MetadataFieldOption {
  field: string;
  schema: MetadataFieldSchema;
}

const OPS_BY_TYPE: Record<string, KnowledgebaseFilterOp[]> = {
  string: ['eq', 'in', 'contains'],
  keyword: ['eq', 'in', 'contains'],
  number: ['eq', 'gte', 'lte', 'between'],
  integer: ['eq', 'gte', 'lte', 'between'],
  date: ['eq', 'gte', 'lte', 'between'],
  datetime: ['eq', 'gte', 'lte', 'between'],
  boolean: ['eq'],
  array: ['contains', 'overlap'],
  'string[]': ['contains', 'overlap']
};

export const ALL_FILTER_OPS: KnowledgebaseFilterOp[] = ['eq', 'in', 'contains', 'overlap', 'gte', 'lte', 'between'];

export function fieldOptionsFromSchema(metadataSchema: Record<string, unknown> | null | undefined): MetadataFieldOption[] {
  if (!metadataSchema) return [];
  const fields = metadataSchema.fields;
  if (!fields || typeof fields !== 'object' || Array.isArray(fields)) return [];
  return Object.entries(fields as Record<string, unknown>)
    .filter(([, value]) => value && typeof value === 'object' && (value as MetadataFieldSchema).filterable === true)
    .map(([field, value]) => ({ field, schema: value as MetadataFieldSchema }))
    .sort((a, b) => a.field.localeCompare(b.field));
}

export function operatorsForField(schema: MetadataFieldSchema | undefined): KnowledgebaseFilterOp[] {
  const type = schema?.type ?? 'string';
  if (type === 'array' && schema?.items?.type !== 'string') return [];
  return OPS_BY_TYPE[type] ?? [];
}

export function isValueOperator(op: KnowledgebaseFilterOp): boolean {
  return op !== 'between';
}

/** Coerces a raw string input from the filter builder into the value shape the operator expects. */
export function coerceFilterValue(op: KnowledgebaseFilterOp, raw: string, fieldType: string): unknown {
  const numeric = fieldType === 'number' || fieldType === 'integer';

  if (op === 'in' || op === 'overlap') {
    return raw
      .split(',')
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => (numeric ? Number(part) : part));
  }

  if (op === 'between') {
    const [low, high] = raw.split(',').map((part) => part.trim());
    return [numeric ? Number(low) : low, numeric ? Number(high) : high];
  }

  if (numeric) {
    const value = Number(raw);
    return Number.isNaN(value) ? raw : value;
  }

  if (fieldType === 'boolean') {
    return raw === 'true';
  }

  return raw;
}

export function isFilterComplete(filter: Partial<KnowledgebaseFilter>): filter is KnowledgebaseFilter {
  if (!filter.field || !filter.op) return false;
  if (filter.value === undefined || filter.value === null || filter.value === '') return false;
  if (Array.isArray(filter.value) && filter.value.length === 0) return false;
  return true;
}

/** Serializes filters into a single URL query param value (JSON, then URL-encoded by the caller). */
export function serializeFilters(filters: KnowledgebaseFilter[]): string {
  return JSON.stringify(filters);
}

export function parseFilters(raw: string | null | undefined): KnowledgebaseFilter[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item): item is KnowledgebaseFilter =>
        item && typeof item === 'object' && typeof item.field === 'string' && typeof item.op === 'string'
    );
  } catch {
    return [];
  }
}

export interface KnowledgeSearchUrlState {
  mode: 'search' | 'ask';
  query: string;
  limit: number;
  filters: KnowledgebaseFilter[];
}

export function searchStateToParams(state: KnowledgeSearchUrlState): URLSearchParams {
  const params = new URLSearchParams();
  params.set('mode', state.mode);
  if (state.query) params.set('q', state.query);
  if (state.limit) params.set('limit', String(state.mode === 'ask' ? Math.min(state.limit, 20) : state.limit));
  if (state.filters.length > 0) params.set('filters', serializeFilters(state.filters));
  return params;
}

export function searchStateFromParams(params: URLSearchParams): KnowledgeSearchUrlState {
  const mode = params.get('mode') === 'ask' ? 'ask' : 'search';
  const query = params.get('q') ?? '';
  const limitRaw = Number(params.get('limit'));
  const maxLimit = mode === 'ask' ? 20 : 50;
  const limit = Number.isFinite(limitRaw) && limitRaw > 0 ? Math.min(maxLimit, Math.round(limitRaw)) : 10;
  const filters = parseFilters(params.get('filters'));
  return { mode, query, limit, filters };
}
