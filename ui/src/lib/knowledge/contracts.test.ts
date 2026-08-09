import { describe, expect, it } from 'vitest';

import type {
  KnowledgebaseAskResponse,
  KnowledgebaseCapabilities,
  KnowledgebaseDocumentContentResponse,
  KnowledgebaseDocumentListResponse,
  KnowledgebaseDocumentUploadResponse,
  KnowledgebaseIndexJobModel,
  KnowledgebaseModel,
  KnowledgebaseShareCandidate,
  KnowledgebaseShareModel
} from '$lib/types/api';

describe('knowledgebase backend contract fixtures', () => {
  it('accepts exact owned/shared list and sharing shapes', () => {
    const shared = {
      knowledgebase_id: 'kb_shared', owner_email: 'owner@example.com', access_level: 'shared',
      name: 'Shared docs', description: null, status: 'active', metadata_schema: {}, settings: {},
      created_at: null, updated_at: null, archived_at: null
    } satisfies KnowledgebaseModel;
    const grant = {
      grant_id: 'kbg_1', user_email: 'reader@example.com', user_name: 'Reader',
      permission: 'view', granted_at: '2026-01-01T00:00:00Z', note: null
    } satisfies KnowledgebaseShareModel;
    const candidate = { email: 'reader@example.com', name: 'Reader' } satisfies KnowledgebaseShareCandidate;
    expect(shared.access_level).toBe('shared');
    expect(grant.permission).toBe('view');
    expect(candidate.email).toBe(grant.user_email);
  });
  it('accepts capabilities and document list/content serialized shapes', () => {
    const capabilities = {
      enabled: true,
      vector_backend: 'qdrant',
      backend_ready: true,
      embedding_ready: true,
      indexer_ready: true,
      ask_ready: true,
      supported_mime_types: ['text/markdown'],
      supported_extensions: ['.md'],
      limits: {
        max_upload_bytes: 52_428_800,
        max_batch_files: 100,
        max_batch_upload_bytes: 104_857_600
      },
      notes: []
    } satisfies KnowledgebaseCapabilities;
    const documents = {
      documents: [{
        kb_artifact_id: 'kba_1', knowledgebase_id: 'kb_1', source_path: 'guides/a.md',
        artifact_id: 'art_1', pending_artifact_id: null, pending_source_hash: null,
        active_generation: 1, desired_generation: 1, status: 'indexed', source_hash: 'sha256',
        source_filename: 'a.md', source_mime_type: 'text/markdown', source_size_bytes: 12,
        metadata: { category: 'guide' }, chunk_count: 1, last_job_id: 'job_1',
        last_error: null, last_diagnostics: {}, attached_at: '2026-01-01T00:00:00Z',
        indexed_at: '2026-01-01T00:00:01Z', stale_at: null, removed_at: null
      }],
      next_cursor: null
    } satisfies KnowledgebaseDocumentListResponse;
    const content = {
      kb_artifact_id: 'kba_1', artifact_id: 'art_1', source_path: 'guides/a.md',
      content_mode: 'extracted', mime_type: 'text/markdown', text: '# A', size_bytes: 3,
      extraction_method: 'markdown', diagnostics: {}
    } satisfies KnowledgebaseDocumentContentResponse;

    expect(capabilities.ask_ready).toBe(true);
    expect(documents.documents[0].source_path).toBe('guides/a.md');
    expect(content.content_mode).toBe('extracted');
  });

  it('accepts ingest, job, and cited Ask serialized shapes', () => {
    const ingest = {
      outcomes: [{
        filename: 'a.md', source_path: 'a.md', status: 'updated', artifact_id: 'art_2',
        kb_artifact_id: 'kba_1', job_id: 'job_2', error_code: null, message: null
      }]
    } satisfies KnowledgebaseDocumentUploadResponse;
    const job = {
      job_id: 'job_2', knowledgebase_id: 'kb_1', kb_artifact_id: 'kba_1',
      artifact_id: 'art_2', generation: 2, job_type: 'index_artifact', status: 'queued',
      attempts: 0, error: null, diagnostics: {}, chunks_indexed: 0, chunks_deleted: 0,
      queued_at: '2026-01-01T00:00:00Z', started_at: null, completed_at: null
    } satisfies KnowledgebaseIndexJobModel;
    const ask = {
      status: 'answered',
      answer: 'See the guide. [1]',
      cited_chunk_ids: ['chunk_1'],
      matches: [],
      error: null
    } satisfies KnowledgebaseAskResponse;

    expect(ingest.outcomes[0].status).toBe('updated');
    expect(job.generation).toBe(2);
    expect(ask.cited_chunk_ids).toEqual(['chunk_1']);
  });
});
