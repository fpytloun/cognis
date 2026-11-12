import type { LibraryItem } from '../../../src/lib/library';

export const deliverableId = 'dlv_revision_current';
export const artifactId = 'art_revision_current';
export const imageId = 'art_revision_image';
export function libraryItem(kind: 'deliverable' | 'artifact', id = kind === 'deliverable' ? deliverableId : artifactId): LibraryItem {
  return {
    id, kind, title: 'Release readiness report', format: kind === 'deliverable' ? 'markdown' : 'text/plain',
    owner_email: 'owner@cognis.test', agent_id: null, agent_name: null,
    project_id: null, project_name: null, conversation_id: null, conversation_title: null,
    task_id: null, task_title: null, lineage_id: kind, revision: id.endsWith('older') ? 1 : 2,
    created_at: '2026-11-03T10:00:00Z', updated_at: '2026-11-03T10:00:00Z',
    size_bytes: 120, status: 'delivered', origin: 'agent', is_readonly: false,
  };
}
export function deliverable(id = deliverableId) {
  return {
    deliverable_id: id, title: 'Release readiness report', format: 'markdown', content: id.endsWith('older') ? '# Earlier revision\n\nDraft readiness findings.' : '# Current revision\n\nFinal readiness findings.',
    lineage_id: deliverableId, revision: id.endsWith('older') ? 1 : 2,
    revision_count: 2, latest_revision: 2, latest_deliverable_id: deliverableId, is_latest: !id.endsWith('older'),
    version: 1, attempt_number: 1, supersedes_deliverable_id: id.endsWith('older') ? null : 'dlv_revision_older',
    step_run_id: null, target: null, outputs: {}, rich_payload: null, validation_warnings: [],
    render_metadata: {}, export_metadata: {}, status: 'delivered', evaluator_feedback: null,
    created_at: '2026-11-03T10:00:00Z', updated_at: null,
  };
}
