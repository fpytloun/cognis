/**
 * Uploads one ingestion batch through the authoritative generic documents
 * endpoint. Attaching an existing artifact is a separate explicit operation.
 */
import { api } from '$lib/api/client';
import type { IngestionPlanItem } from '$lib/knowledge/ingestion';
import type { KnowledgebaseDocumentUploadOutcome } from '$lib/types/api';

export async function uploadIngestionBatch(
  knowledgebaseId: string,
  batch: IngestionPlanItem[],
  conflictPolicy: 'skip' | 'replace' | 'keep_both',
  signal?: AbortSignal
): Promise<KnowledgebaseDocumentUploadOutcome[]> {
  const response = await api.knowledgebases.documents.upload(
    knowledgebaseId,
    batch.map((item) => item.file),
    batch.map((item) => item.resolvedPath),
    conflictPolicy,
    { signal }
  );
  return response.outcomes;
}
