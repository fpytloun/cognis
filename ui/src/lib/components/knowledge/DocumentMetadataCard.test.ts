import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import DocumentMetadataCard from './DocumentMetadataCard.svelte';
import type { KnowledgebaseDocumentModel } from '$lib/types/api';

const doc = {
  doc_id: 'kba_1', knowledgebase_id: 'kb_1', artifact_id: 'art_1', display_name: 'a.md',
  source_path: 'folder/a.md', mime_type: 'text/markdown', size_bytes: 12, status: 'indexed',
  chunk_count: 2, metadata: { source_path: 'duplicate', visible: false, zero: 0, tags: ['a', 'b'], hidden: 'secret' },
  last_job_id: null, last_error: null, attached_at: null, indexed_at: null
} satisfies KnowledgebaseDocumentModel;

describe('DocumentMetadataCard', () => {
  it('renders structured metadata outside prose while preserving false and zero', () => {
    const { container } = render(DocumentMetadataCard, {
      doc, metadataSchema: { fields: { hidden: { display: false } } }
    });
    expect(screen.getByText('false')).toBeInTheDocument();
    expect(screen.getByText('0')).toBeInTheDocument();
    expect(screen.queryByText('secret')).not.toBeInTheDocument();
    expect(screen.queryByText('duplicate')).not.toBeInTheDocument();
    expect(container.querySelector('.prose')).toBeNull();
  });
});
