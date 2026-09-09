import { describe, expect, it } from 'vitest';

import { asTypedCell, blockSpan, blockSurface, progressPercent } from './block-helpers';

describe('generic block surface/span helpers', () => {
  it('accepts only recognized surface values and returns undefined otherwise', () => {
    expect(blockSurface({ surface: 'outlined' })).toBe('outlined');
    expect(blockSurface({ surface: 'accent' })).toBe('accent');
    expect(blockSurface({ surface: 'glassy' })).toBeUndefined();
    expect(blockSurface({})).toBeUndefined();
  });

  it('clamps span to the 1..4 grid range and ignores invalid values', () => {
    expect(blockSpan({ span: 2 })).toBe(2);
    expect(blockSpan({ span: 9 })).toBe(4);
    expect(blockSpan({ span: 0 })).toBeUndefined();
    expect(blockSpan({ span: -1 })).toBeUndefined();
    expect(blockSpan({ span: 'wide' })).toBeUndefined();
    expect(blockSpan({})).toBeUndefined();
  });
});

describe('progressPercent', () => {
  it('computes a bounded percent from value/max', () => {
    expect(progressPercent(61, 100)).toBe(61);
    expect(progressPercent(38.4, 63)).toBeCloseTo(60.95, 1);
    expect(progressPercent(150, 100)).toBe(100);
    expect(progressPercent(-10, 100)).toBe(0);
  });

  it('falls back to a plain percentage when max is missing or non-finite', () => {
    expect(progressPercent(42, undefined)).toBe(42);
    expect(progressPercent(42, 'n/a')).toBe(42);
  });

  it('returns 0 for a non-finite value', () => {
    expect(progressPercent('not a number', 100)).toBe(0);
  });
});

describe('asTypedCell', () => {
  it('recognizes every typed cell type with a value', () => {
    for (const type of ['text', 'number', 'code', 'badge', 'progress'] as const) {
      expect(asTypedCell({ type, value: 'x' })).toMatchObject({ type, value: 'x' });
    }
  });

  it('returns null for scalars and plain objects without a recognized type+value shape', () => {
    expect(asTypedCell('plain string')).toBeNull();
    expect(asTypedCell(42)).toBeNull();
    expect(asTypedCell(null)).toBeNull();
    expect(asTypedCell(['a', 'b'])).toBeNull();
    expect(asTypedCell({ type: 'text' })).toBeNull();
    expect(asTypedCell({ type: 'not-a-real-type', value: 'x' })).toBeNull();
  });

  it('normalizes optional label/tone/emphasis/align/max fields', () => {
    const cell = asTypedCell({
      type: 'progress',
      value: 61,
      max: 100,
      label: 'Disk used',
      tone: 'warning',
      emphasis: 'strong',
      align: 'end',
    });

    expect(cell).toMatchObject({
      type: 'progress',
      value: 61,
      max: 100,
      label: 'Disk used',
      tone: 'warning',
      emphasis: 'strong',
      align: 'end',
    });
  });

  it('defaults emphasis to normal and drops unrecognized align values', () => {
    const cell = asTypedCell({ type: 'text', value: 'x', align: 'sideways' });
    expect(cell?.emphasis).toBe('normal');
    expect(cell?.align).toBeUndefined();
  });
});
