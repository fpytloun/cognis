import { describe, expect, it } from 'vitest';
import { resolveKnowledgeResource } from './resources';
import type { KnowledgebaseDocumentModel } from '$lib/types/api';

const doc = (id: string, path: string): KnowledgebaseDocumentModel => ({
  doc_id: id, knowledgebase_id: 'kb_1', artifact_id: `art_${id}`, display_name: path.split('/').at(-1)!,
  source_path: path, mime_type: 'text/markdown', size_bytes: 1, status: 'indexed',
  chunk_count: 1, metadata: {}, last_job_id: null, last_error: null, attached_at: null, indexed_at: null
});

describe('resolveKnowledgeResource', () => {
  const source = doc('kba_source', 'guides/setup/readme.md');
  const sibling = doc('kba_image', 'guides/images/diagram.md');
  it('opens an attached sibling document by stable doc id', () => {
    expect(resolveKnowledgeResource('../images/diagram.md', 'kb_1', source, [source, sibling])).toMatchObject({
      kind: 'document', docId: 'kba_image'
    });
  });
  it('matches document paths without query or fragment suffixes and preserves the fragment', () => {
    expect(resolveKnowledgeResource('../images/diagram.md?view=reader#installation', 'kb_1', source, [source, sibling])).toMatchObject({
      kind: 'document',
      docId: 'kba_image',
      fragment: 'installation',
      href: '/knowledge/kb_1?tab=browse&document=kba_image#installation'
    });
  });
  it('maps root aliases and binaries to the authenticated resource route', () => {
    expect(resolveKnowledgeResource('/knowledge/resources/assets/image.png', 'kb_1', source, [source])).toEqual({
      kind: 'resource', path: 'assets/image.png',
      href: '/api/v1/knowledgebases/kb_1/documents/kba_source/resources/knowledge/resources/assets/image.png'
    });
  });
  it('rejects traversal above the knowledgebase root', () => {
    expect(resolveKnowledgeResource('../../../secret.txt', 'kb_1', source, [source]).kind).toBe('unavailable');
  });
  it('leaves external links unchanged', () => {
    expect(resolveKnowledgeResource('https://example.com/a', 'kb_1', source, [source])).toEqual({
      kind: 'external', href: 'https://example.com/a'
    });
  });
});
