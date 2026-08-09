import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import AskAnswerCard from './AskAnswerCard.svelte';
import type { KnowledgebaseSearchMatch } from '$lib/types/api';

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

function match(chunkId: string): KnowledgebaseSearchMatch {
  return {
    chunk_id: chunkId,
    kb_artifact_id: 'kba_1',
    artifact_id: 'art_1',
    snippet: 'snippet',
    score: 0.9,
    score_breakdown: {},
    metadata: {},
    citation: { artifact_id: 'art_1', filename: 'doc.md', mime_type: 'text/markdown', locator: locator(chunkId) }
  };
}

describe('AskAnswerCard', () => {
  it('renders a cited answer with clickable citation chips', async () => {
    const onCitationClick = vi.fn();

    render(AskAnswerCard, {
      status: 'answered', answer: 'The answer is 42.', citedChunkIds: ['chunk_a'], error: null,
      matches: [match('chunk_a')], onCitationClick
    });

    expect(screen.getByText(/The answer is 42/)).toBeInTheDocument();
    const chip = screen.getByRole('button', { name: 'Citation 1: doc.md' });
    await fireEvent.click(chip);
    expect(onCitationClick).toHaveBeenCalledWith('chunk_a');
  });

  it('shows a no-matches state without a synthesized answer', () => {
    render(AskAnswerCard, {
      status: 'insufficient_evidence', answer: null, citedChunkIds: [], error: null,
      matches: [], onCitationClick: vi.fn()
    });
    expect(screen.getByText(/No relevant documents/)).toBeInTheDocument();
  });

  it('preserves raw evidence messaging when synthesis fails', async () => {
    const onRetry = vi.fn();
    const onRunAsSearch = vi.fn();
    render(AskAnswerCard, {
      status: 'error', answer: null, citedChunkIds: [],
      error: { code: 'provider_error', message: 'model unavailable', correlation_id: 'corr_1' },
      matches: [match('chunk_a')], onCitationClick: vi.fn(), onRetry, onRunAsSearch
    });
    expect(screen.getByText(/model unavailable/)).toBeInTheDocument();
    expect(screen.getByText('corr_1')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Run as Search' }));
    expect(onRetry).toHaveBeenCalled();
    expect(onRunAsSearch).toHaveBeenCalled();
  });
});
