/**
 * Generic document projection helpers.
 *
 * The backend document list returns artifact records. This module is the
 * single adapter from that wire contract to the presentation model.
 */
import { api } from '$lib/api/client';
import type {
  KnowledgebaseArtifactModel,
  KnowledgebaseDocumentListResponse,
  KnowledgebaseDocumentModel
} from '$lib/types/api';

/** Metadata key used to carry a generic folder-relative path through ingestion. */
export const SOURCE_PATH_METADATA_KEY = 'source_path';

export function readSourcePath(metadata: Record<string, unknown> | null | undefined): string | null {
  const value = metadata?.[SOURCE_PATH_METADATA_KEY];
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

export function documentFromArtifact(artifact: KnowledgebaseArtifactModel): KnowledgebaseDocumentModel {
  const sourcePath = artifact.source_path ?? readSourcePath(artifact.metadata);
  const displayName =
    sourcePath?.split('/').filter(Boolean).pop() ?? artifact.source_filename ?? artifact.artifact_id ?? artifact.kb_artifact_id;

  return {
    doc_id: artifact.kb_artifact_id,
    knowledgebase_id: artifact.knowledgebase_id,
    artifact_id: artifact.artifact_id,
    display_name: displayName,
    source_path: sourcePath,
    mime_type: artifact.source_mime_type,
    size_bytes: artifact.source_size_bytes,
    status: artifact.status,
    chunk_count: artifact.chunk_count,
    metadata: artifact.metadata,
    last_job_id: artifact.last_job_id,
    last_error: artifact.last_error,
    attached_at: artifact.attached_at,
    indexed_at: artifact.indexed_at
  };
}

export function documentsFromArtifacts(artifacts: KnowledgebaseArtifactModel[]): KnowledgebaseDocumentModel[] {
  return artifacts.filter((artifact) => artifact.status !== 'detached').map(documentFromArtifact);
}

export const MAX_DOCUMENT_PAGES = 100;
export const DOCUMENT_PAGE_SIZE = 100;

export async function collectAllKnowledgebaseDocuments(
  knowledgebaseId: string,
  signal?: AbortSignal,
  fetchPage: (
    cursor: string | undefined,
    signal?: AbortSignal
  ) => Promise<KnowledgebaseDocumentListResponse> = (cursor, requestSignal) =>
    api.knowledgebases.documents.list(
      knowledgebaseId,
      { cursor, limit: DOCUMENT_PAGE_SIZE, sort: 'path', direction: 'asc' },
      { signal: requestSignal }
    )
): Promise<KnowledgebaseArtifactModel[]> {
  const documents: KnowledgebaseArtifactModel[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | undefined;

  for (let page = 0; page < MAX_DOCUMENT_PAGES; page += 1) {
    signal?.throwIfAborted();
    const response = await fetchPage(cursor, signal);
    documents.push(...response.documents);
    if (!response.next_cursor) return documents;
    if (seenCursors.has(response.next_cursor)) {
      throw new Error('Document pagination returned a repeated cursor');
    }
    seenCursors.add(response.next_cursor);
    cursor = response.next_cursor;
  }

  throw new Error(`Document pagination exceeded the ${MAX_DOCUMENT_PAGES}-page safety limit`);
}
