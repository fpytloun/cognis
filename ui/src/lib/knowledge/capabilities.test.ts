import { describe, expect, it } from 'vitest';

import { knowledgebaseLifecycleAccess, knowledgebaseReadiness } from './capabilities';
import type { KnowledgebaseCapabilities } from '$lib/types/api';

const capabilities: KnowledgebaseCapabilities = {
  enabled: true,
  vector_backend: 'qdrant',
  backend_ready: true,
  embedding_ready: true,
  indexer_ready: true,
  ask_ready: true,
  supported_mime_types: ['text/markdown'],
  supported_extensions: ['.md'],
  limits: {
    max_upload_bytes: 1_000,
    max_batch_files: 25,
    max_batch_upload_bytes: 10_000
  },
  notes: []
};

describe('knowledgebaseReadiness', () => {
  it('keeps reading and CRUD available while retrieval and indexing are degraded', () => {
    const readiness = knowledgebaseReadiness(
      { ...capabilities, backend_ready: false, embedding_ready: false, indexer_ready: false, ask_ready: false },
      false
    );
    expect(readiness).toMatchObject({
      canRead: true,
      canMutateCrud: true,
      canIngest: false,
      canSearch: false,
      canAsk: false,
      degraded: true
    });
  });

  it('keeps viewers read-only even when every backend capability is ready', () => {
    expect(knowledgebaseReadiness(capabilities, true)).toMatchObject({
      canRead: true,
      canMutateCrud: false,
      canIngest: false,
      canSearch: true,
      canAsk: true
    });
  });

  it('keeps only lifecycle management available for archived knowledgebases', () => {
    const readiness = knowledgebaseReadiness(capabilities, false);
    expect(knowledgebaseLifecycleAccess('archived', readiness)).toEqual({
      canManageLifecycle: true,
      canMutateContent: false,
      canIngest: false
    });
  });

  it('denies every mutation for a direct shared user', () => {
    const readiness = knowledgebaseReadiness(capabilities, false);
    expect(knowledgebaseLifecycleAccess('active', readiness, 'shared')).toEqual({
      canManageLifecycle: false,
      canMutateContent: false,
      canIngest: false
    });
  });

  it('keeps viewer-owned knowledgebases read-only', () => {
    const readiness = knowledgebaseReadiness(capabilities, true);
    expect(knowledgebaseLifecycleAccess('active', readiness, 'owner')).toEqual({
      canManageLifecycle: false,
      canMutateContent: false,
      canIngest: false
    });
  });
});
