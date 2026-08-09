/**
 * Maps Ask citations back onto the raw evidence list so citation chips can
 * scroll to and highlight the matching `RawResultList` entry.
 */
import type { KnowledgebaseSearchMatch, KnowledgebaseSourceCitation } from '$lib/types/api';

export function matchKey(match: { chunk_id: string }): string {
  return match.chunk_id;
}

export function citationKey(citation: KnowledgebaseSourceCitation): string {
  return citation.locator.chunk_id;
}

export interface CitationMapping {
  citation: KnowledgebaseSourceCitation;
  matchIndex: number | null;
}

/** Resolves each citation to its index in `matches` (or null if not present in the raw evidence). */
export function mapCitationsToMatches(
  citations: KnowledgebaseSourceCitation[],
  matches: KnowledgebaseSearchMatch[]
): CitationMapping[] {
  const indexByKey = new Map(matches.map((match, index) => [matchKey(match), index]));
  return citations.map((citation) => ({
    citation,
    matchIndex: indexByKey.get(citationKey(citation)) ?? null
  }));
}

export function citationLabel(citation: KnowledgebaseSourceCitation, position: number): string {
  const name = citation.filename ?? citation.artifact_id;
  return `[${position + 1}] ${name}`;
}

export function elementIdForMatch(chunkId: string): string {
  return `kb-raw-result-${encodeURIComponent(chunkId)}`;
}
