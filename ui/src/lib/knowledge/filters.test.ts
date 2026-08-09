import { describe, expect, it } from 'vitest';

import {
  coerceFilterValue,
  fieldOptionsFromSchema,
  isFilterComplete,
  operatorsForField,
  parseFilters,
  searchStateFromParams,
  searchStateToParams,
  serializeFilters
} from './filters';

describe('fieldOptionsFromSchema', () => {
  it('extracts and sorts fields from a metadata schema', () => {
    const options = fieldOptionsFromSchema({
      fields: {
        category: { type: 'keyword', enum: ['guide', 'reference'], filterable: true },
        priority: { type: 'number', filterable: true },
        internal_note: { type: 'string', filterable: false }
      }
    });
    expect(options.map((o) => o.field)).toEqual(['category', 'priority']);
  });

  it('returns an empty list for a missing or empty schema', () => {
    expect(fieldOptionsFromSchema(undefined)).toEqual([]);
    expect(fieldOptionsFromSchema({})).toEqual([]);
  });
});

describe('operatorsForField', () => {
  it('restricts operators by declared type', () => {
    expect(operatorsForField({ type: 'boolean' })).toEqual(['eq']);
    expect(operatorsForField({ type: 'number' })).toContain('between');
    expect(operatorsForField({ type: 'number' })).not.toContain('in');
    expect(operatorsForField({ type: 'datetime' })).toEqual(['eq', 'gte', 'lte', 'between']);
    expect(operatorsForField({ type: 'array', items: { type: 'string' } })).toEqual(['contains', 'overlap']);
    expect(operatorsForField({ type: 'array', items: { type: 'number' } })).toEqual([]);
  });
});

describe('coerceFilterValue', () => {
  it('splits comma-separated values for in/overlap and coerces numerics', () => {
    expect(coerceFilterValue('in', 'a, b , c', 'string')).toEqual(['a', 'b', 'c']);
    expect(coerceFilterValue('overlap', '1,2', 'number')).toEqual([1, 2]);
  });

  it('splits a two-value range for between', () => {
    expect(coerceFilterValue('between', '10, 20', 'integer')).toEqual([10, 20]);
  });

  it('coerces booleans and plain numbers', () => {
    expect(coerceFilterValue('eq', 'true', 'boolean')).toBe(true);
    expect(coerceFilterValue('eq', '42', 'number')).toBe(42);
    expect(coerceFilterValue('eq', 'plain', 'string')).toBe('plain');
  });
});

describe('isFilterComplete', () => {
  it('rejects filters missing a field, op, or usable value', () => {
    expect(isFilterComplete({})).toBe(false);
    expect(isFilterComplete({ field: 'x', op: 'eq' })).toBe(false);
    expect(isFilterComplete({ field: 'x', op: 'eq', value: '' })).toBe(false);
    expect(isFilterComplete({ field: 'x', op: 'in', value: [] })).toBe(false);
    expect(isFilterComplete({ field: 'x', op: 'eq', value: 'ok' })).toBe(true);
  });
});

describe('filter serialization', () => {
  it('round-trips filters through JSON', () => {
    const filters = [{ field: 'category', op: 'eq' as const, value: 'guide' }];
    expect(parseFilters(serializeFilters(filters))).toEqual(filters);
  });

  it('parses defensively, dropping malformed entries', () => {
    expect(parseFilters('not json')).toEqual([]);
    expect(parseFilters('{}')).toEqual([]);
    expect(parseFilters('[{"field":"x"}]')).toEqual([]);
  });
});

describe('search URL state', () => {
  it('round-trips mode, query, limit, and filters through URL params', () => {
    const state = {
      mode: 'ask' as const,
      query: 'how does auth work',
      limit: 20,
      filters: [{ field: 'category', op: 'eq' as const, value: 'guide' }]
    };
    const params = searchStateToParams(state);
    expect(searchStateFromParams(params)).toEqual(state);
  });

  it('defaults to search mode, empty query, and limit 10 for empty params', () => {
    expect(searchStateFromParams(new URLSearchParams())).toEqual({
      mode: 'search',
      query: '',
      limit: 10,
      filters: []
    });
  });

  it('clamps an out-of-range limit', () => {
    const state = searchStateFromParams(new URLSearchParams('limit=500'));
    expect(state.limit).toBe(50);
  });

  it('clamps Ask limits to 20 when reading and writing URL state', () => {
    const parsed = searchStateFromParams(new URLSearchParams('mode=ask&limit=50'));
    expect(parsed.limit).toBe(20);
    expect(searchStateToParams({ mode: 'ask', query: 'q', limit: 50, filters: [] }).get('limit')).toBe('20');
  });
});
