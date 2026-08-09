import { render, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import DocumentReader from './DocumentReader.svelte';
import type { KnowledgebaseDocumentModel } from '$lib/types/api';

const documentModel: KnowledgebaseDocumentModel = {
  doc_id: 'kba_1',
  knowledgebase_id: 'kb_1',
  artifact_id: 'art_1',
  display_name: 'guide.md',
  source_path: 'guides/guide.md',
  mime_type: 'text/markdown',
  size_bytes: 100,
  status: 'indexed',
  chunk_count: 1,
  metadata: {},
  last_job_id: null,
  last_error: null,
  attached_at: null,
  indexed_at: null
};

describe('DocumentReader fragment navigation', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  it('maps a source fragment to the prefixed rendered heading after content loads', async () => {
    render(DocumentReader, {
      doc: documentModel,
      loading: false,
      error: null,
      content: { text: '# Guide\n\n## Installation\n\nSteps.', extractedText: false },
      downloadUrl: null,
      onDownload: null,
      knowledgebaseId: 'kb_1',
      documents: [documentModel],
      requestedFragment: 'installation'
    });

    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled());
  });
});
