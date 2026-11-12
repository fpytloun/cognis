import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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
  afterEach(() => vi.restoreAllMocks());
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

  it('downloads the named original file in the app rather than opening an external tab', async () => {
    const open = vi.spyOn(window, 'open');
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe('guide.md');
      expect(this.href).toContain('/signed/guide.md');
      expect(this.target).toBe('');
    });
    render(DocumentReader, {
      doc: documentModel, loading: false, error: null, content: null,
      downloadUrl: '/signed/guide.md', onDownload: null, knowledgebaseId: 'kb_1',
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(click).toHaveBeenCalledOnce();
    expect(open).not.toHaveBeenCalled();
  });
});
