import { describe, expect, it } from 'vitest';

import { citationLabel, elementIdForMatch, mapCitationsToMatches } from './citations';
import type { KnowledgebaseSearchMatch, KnowledgebaseSourceCitation } from '$lib/types/api';

function locator(chunkId: string) {
  return {
    artifact_id: 'art_1',
    artifact_hash: null,
    chunk_id: chunkId,
    chunk_index: 0,
    char_start: null,
    char_end: null,
    byte_start: null,
    byte_end: null,
    line_start: null,
    line_end: null,
    page_start: null,
    page_end: null,
    paragraph_start: null,
    paragraph_end: null,
    timestamp_start_ms: null,
    timestamp_end_ms: null,
    extraction_method: 'text'
  };
}

function citation(chunkId: string, filename: string | null = 'doc.md'): KnowledgebaseSourceCitation {
  return { artifact_id: 'art_1', filename, mime_type: 'text/markdown', locator: locator(chunkId) };
}

function match(chunkId: string): KnowledgebaseSearchMatch {
  return {
    chunk_id: chunkId,
    kb_artifact_id: 'kba_1',
    artifact_id: 'art_1',
    snippet: 'snippet',
    score: 0.9,
    score_breakdown: {},
    metadata: {},
    citation: citation(chunkId)
  };
}

describe('mapCitationsToMatches', () => {
  it('resolves each citation to its matching raw evidence index', () => {
    const matches = [match('chunk_a'), match('chunk_b')];
    const citations = [citation('chunk_b'), citation('chunk_a')];

    const mapped = mapCitationsToMatches(citations, matches);

    expect(mapped[0].matchIndex).toBe(1);
    expect(mapped[1].matchIndex).toBe(0);
  });

  it('returns null for a citation with no corresponding raw match (synthesis referencing stale evidence)', () => {
    const mapped = mapCitationsToMatches([citation('chunk_missing')], [match('chunk_a')]);
    expect(mapped[0].matchIndex).toBeNull();
  });
});

describe('citationLabel / elementIdForMatch', () => {
  it('builds a 1-indexed label preferring filename over artifact id', () => {
    expect(citationLabel(citation('c1', 'report.pdf'), 0)).toBe('[1] report.pdf');
    expect(citationLabel(citation('c1', null), 2)).toBe('[3] art_1');
  });

  it('produces a stable, URL-safe element id per chunk', () => {
    expect(elementIdForMatch('chunk#1')).toBe('kb-raw-result-chunk%231');
  });
});
