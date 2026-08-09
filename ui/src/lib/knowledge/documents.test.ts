import { describe, expect, it } from 'vitest';

import {
  collectAllKnowledgebaseDocuments,
  documentFromArtifact,
  documentsFromArtifacts,
  readSourcePath
} from './documents';
import type { KnowledgebaseArtifactModel } from '$lib/types/api';

function artifact(overrides: Partial<KnowledgebaseArtifactModel>): KnowledgebaseArtifactModel {
  return {
    kb_artifact_id: 'kba_1',
    knowledgebase_id: 'kb_1',
    source_path: null,
    artifact_id: 'art_1',
    pending_artifact_id: null,
    pending_source_hash: null,
    active_generation: 1,
    desired_generation: 1,
    status: 'indexed',
    source_hash: null,
    source_filename: 'file.md',
    source_mime_type: 'text/markdown',
    source_size_bytes: 123,
    metadata: {},
    chunk_count: 2,
    last_job_id: null,
    last_error: null,
    last_diagnostics: {},
    attached_at: null,
    indexed_at: null,
    stale_at: null,
    removed_at: null,
    ...overrides
  };
}

describe('readSourcePath', () => {
  it('reads a non-empty string source_path from metadata', () => {
    expect(readSourcePath({ source_path: 'a/b.md' })).toBe('a/b.md');
  });

  it('returns null for missing, blank, or non-string values', () => {
    expect(readSourcePath(null)).toBeNull();
    expect(readSourcePath({})).toBeNull();
    expect(readSourcePath({ source_path: '  ' })).toBeNull();
    expect(readSourcePath({ source_path: 42 })).toBeNull();
  });
});

describe('documentFromArtifact', () => {
  it('derives display name from the last source_path segment when present', () => {
    const doc = documentFromArtifact(artifact({ source_path: 'guides/setup.md' }));
    expect(doc.display_name).toBe('setup.md');
    expect(doc.source_path).toBe('guides/setup.md');
  });

  it('falls back to source_filename then artifact id when there is no source_path', () => {
    const withFilename = documentFromArtifact(artifact({ metadata: {} }));
    expect(withFilename.display_name).toBe('file.md');

    const withoutFilename = documentFromArtifact(artifact({ metadata: {}, source_filename: null }));
    expect(withoutFilename.display_name).toBe('art_1');
  });

  it('maps status, chunk_count, and error fields through unchanged', () => {
    const doc = documentFromArtifact(artifact({ status: 'failed', chunk_count: 0, last_error: 'boom' }));
    expect(doc.status).toBe('failed');
    expect(doc.chunk_count).toBe(0);
    expect(doc.last_error).toBe('boom');
  });
});

describe('documentsFromArtifacts', () => {
  it('excludes detached artifacts from the document list', () => {
    const artifacts = [artifact({ kb_artifact_id: 'a' }), artifact({ kb_artifact_id: 'b', status: 'detached' })];
    const docs = documentsFromArtifacts(artifacts);
    expect(docs.map((d) => d.doc_id)).toEqual(['a']);
  });
});

describe('collectAllKnowledgebaseDocuments', () => {
  it('collects more than one page until next_cursor is exhausted', async () => {
    const calls: Array<string | undefined> = [];
    const firstPage = Array.from({ length: 50 }, (_, index) =>
      artifact({ kb_artifact_id: `kba_${index}`, source_path: `folder/${index}.md` })
    );
    const secondPage = [artifact({ kb_artifact_id: 'kba_50', source_path: 'folder/50.md' })];

    const documents = await collectAllKnowledgebaseDocuments('kb_1', undefined, async (cursor) => {
      calls.push(cursor);
      return cursor
        ? { documents: secondPage, next_cursor: null }
        : { documents: firstPage, next_cursor: 'cursor_2' };
    });

    expect(calls).toEqual([undefined, 'cursor_2']);
    expect(documents).toHaveLength(51);
  });

  it('stops before fetching the next page when cancelled', async () => {
    const controller = new AbortController();
    let calls = 0;
    await expect(
      collectAllKnowledgebaseDocuments('kb_1', controller.signal, async () => {
        calls += 1;
        controller.abort();
        return { documents: [artifact({})], next_cursor: 'cursor_2' };
      })
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(calls).toBe(1);
  });
});
