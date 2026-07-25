import type { AttachmentRef } from '$lib/types/api';

const reactiveAttachments = $state<AttachmentRef[]>([
  {
    artifact_id: 'artifact-1',
    kind: 'file',
    filename: 'trace.txt',
    mime_type: 'text/plain',
    size_bytes: 42,
    url: '/api/v1/artifacts/artifact-1/content',
  },
]);

export function reactiveAttachmentRefs(): AttachmentRef[] {
  return reactiveAttachments;
}
