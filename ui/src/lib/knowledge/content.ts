/**
 * Resolves readable content through the authoritative document content API.
 */
import { api } from '$lib/api/client';
import type { KnowledgebaseDocumentModel } from '$lib/types/api';

export interface ResolvedDocumentContent {
  text: string;
  extractedText: boolean;
}

export async function resolveDocumentContent(
  knowledgebaseId: string,
  doc: KnowledgebaseDocumentModel,
  signal?: AbortSignal
): Promise<ResolvedDocumentContent | null> {
  const response = await api.knowledgebases.documents.content(
    knowledgebaseId,
    doc.doc_id,
    'extracted',
    { signal }
  );
  return { text: response.text, extractedText: response.content_mode === 'extracted' };
}

export async function resolveDownloadUrl(doc: KnowledgebaseDocumentModel): Promise<string | null> {
  if (!doc.artifact_id) return null;
  const signed = await api.artifacts.signedUrl(doc.artifact_id, 300, 'download');
  return signed.url;
}
