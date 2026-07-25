import { describe, expect, it } from 'vitest';

import { confidenceLabel, confidencePercent, normalizeSources, resolveSourceRefs, sortMatrixRows } from './evidence-helpers';

describe('rich evidence helpers', () => {
  it('normalizes sources and strips unsafe URLs', () => {
    const sources = normalizeSources([
      { id: 'good', title: 'Good', url: 'https://example.com/source', snippet: 'safe' },
      { id: 'bad', title: 'Bad', url: 'javascript:alert(1)' },
    ]);

    expect(sources[0]).toMatchObject({ key: 'good', title: 'Good', url: 'https://example.com/source' });
    expect(sources[1]).toMatchObject({ key: 'bad', title: 'Bad', url: '' });
  });

  it('resolves citation references from ids and inline source records', () => {
    const sources = normalizeSources([{ id: 's1', title: 'Source 1', url: 'https://example.com/1' }]);

    expect(resolveSourceRefs(['s1', { id: 'inline', title: 'Inline' }], sources).map((source) => source.key)).toEqual(['s1', 'inline']);
  });

  it('resolves structured source_id references and preserves an optional display label', () => {
    const sources = normalizeSources([
      { id: 'sweet', title: 'Original title', url: 'https://example.com/sweet' },
    ]);

    expect(resolveSourceRefs([{ source_id: ' sweet ', label: 'Why cats cannot taste sweetness' }], sources))
      .toMatchObject([
        {
          key: 'sweet',
          title: 'Why cats cannot taste sweetness',
          url: 'https://example.com/sweet',
        },
      ]);
  });

  it('resolves every accepted document-source identifier with surrounding whitespace', () => {
    const sources = normalizeSources([
      {
        id: 'source-id',
        key: 'source-key',
        citation_id: 'citation-key',
        title: 'Source title',
        url: 'https://example.com',
        href: 'https://example.com/fallback',
      },
    ]);

    for (const reference of [
      'source-id',
      'source-key',
      'citation-key',
      'Source title',
      'https://example.com',
      'https://example.com/fallback',
    ]) {
      expect(resolveSourceRefs([` ${reference} `], sources), reference).toHaveLength(1);
    }
  });

  it('maps confidence values to bounded percentages and labels', () => {
    expect(confidencePercent(0.86)).toBe(86);
    expect(confidencePercent('72%')).toBe(72);
    expect(confidencePercent(120)).toBe(100);
    expect(confidenceLabel(0.86)).toBe('High');
    expect(confidenceLabel('medium')).toBe('Medium');
  });

  it('sorts decision matrix rows numerically and textually', () => {
    const rows = [
      { option: 'B', score: 10 },
      { option: 'A', score: 2 },
    ];

    expect(sortMatrixRows(rows, { key: 'score', direction: 'asc' }).map((row) => row.option)).toEqual(['A', 'B']);
    expect(sortMatrixRows(rows, { key: 'option', direction: 'desc' }).map((row) => row.option)).toEqual(['B', 'A']);
  });
});
