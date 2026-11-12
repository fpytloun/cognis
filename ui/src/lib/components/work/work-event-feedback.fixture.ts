import { conversationTimelineScope, type FileDiffRef, type WorkFileHistoryResponse, type WorkProjectionResponse } from '$lib/chat-v2/types';
import type { FeedbackContext, FeedbackSourceRef, ResolvedFeedbackSource } from '$lib/feedback/types';

// Synthetic wire-contract fixtures, never runtime source discovery or production authority.
export const context: FeedbackContext = { kind: 'chat', conversation_id: 'work-activation-test' };
export const scope = conversationTimelineScope(context.conversation_id);
export const files = ['review.md', 'rollback.md'];
export function eventDiff(sequence: number, ordinal = 1): FileDiffRef {
  const path = files[ordinal];
  return {
    path, path_id: `path-${ordinal}`, path_generation_id: `generation-${ordinal}`,
    source_item_id: `tool-result-${sequence}`, diff: `--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-before ${sequence}\n+after ${sequence}\n`,
    additions: 1, deletions: 1,
    feedback_source: {
      kind: 'work_diff', activity_scope_id: scope.key,
      source_event: { store: 'intaris', session_id: 'work-activation-session', seq: sequence, event_id: `event-${sequence}`, event_type: 'tool_result' },
      source_item_id: `tool-result-${sequence}`, file_ordinal: ordinal, root_id: 'executor-workspace', path, old_path: null,
    },
  };
}
export function history(sequence = 20, more = false): WorkFileHistoryResponse {
  return { scope, path_generation_id: 'generation-1', items: [eventDiff(sequence)],
    has_more_before: more, before_cursor: more ? 'older-event-cursor' : null };
}
export function projection(): WorkProjectionResponse {
  return {
    schema_version: 2, projection_version: 'test', scope,
    mutations: [{ id: 'aggregate-event', call_id: 'aggregate-call', sort_key: '20', tool_name: 'apply_patch',
      category: 'filesystem', operation_kind: 'file_write', status: 'complete', arguments: {},
      paths: files, file_diffs: [], diffs_truncated: false,
      file_stats: files.map((path, ordinal) => ({ path, path_id: `path-${ordinal}`, path_generation_id: `generation-${ordinal}`,
        additions: 2, deletions: 2, preview_available: false })) }],
    commands: [], artifacts: [], summary: { mutations: 1, commands: 0, changed_files: 2, artifacts: 0 },
    has_more_before: false, server_time: '2026-10-27T00:00:00Z',
  };
}
export function resolved(source: FeedbackSourceRef, comment = true): ResolvedFeedbackSource {
  if (source.kind !== 'work_diff') throw new Error('Expected an exact event source');
  return { source, context, subject_id: `subject-${source.source_event.seq}-${source.file_ordinal}`,
    source_token: `token-${source.source_event.seq}-${source.file_ordinal}`, content_digest: 'a'.repeat(64),
    availability: 'available', permitted_locator_kinds: ['whole', 'diff'], lineage_id: null, revision: null,
    latest_content_ref: null, capabilities: { read: true, comment, moderate: false, dispatch: true, disclose: false },
    title: source.path, format: 'diff', canonical_text: null };
}
