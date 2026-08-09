import type { WorkstreamRef } from '$lib/chat-v2/types';

export const HIDE_READ_ONLY_STORAGE_KEY = 'cognis:activity-tree:hide-read-only:v1';

function timestamp(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function compareWorkstreamActivity(left: WorkstreamRef, right: WorkstreamRef): number {
  return (
    timestamp(right.updated_at) - timestamp(left.updated_at)
    || timestamp(right.created_at) - timestamp(left.created_at)
    || left.ordinal - right.ordinal
  );
}

export function hasDurableOutput(node: WorkstreamRef): boolean {
  const summary = node.summary;
  return Boolean(summary && (
    (summary.changed_files ?? 0) > 0
    || (summary.additions ?? 0) > 0
    || (summary.deletions ?? 0) > 0
    || (summary.mutations ?? 0) > 0
    || (summary.artifacts ?? 0) > 0
    || (summary.deliverables ?? 0) > 0
  ));
}

export function hasFileChanges(node: WorkstreamRef): boolean {
  const summary = node.summary;
  return Boolean(summary && (
    (summary.changed_files ?? 0) > 0
    || (summary.additions ?? 0) > 0
    || (summary.deletions ?? 0) > 0
  ));
}

export function visibleWorkstreamKeys(
  nodes: WorkstreamRef[],
  focusedSessionId: string | null,
  hideReadOnly: boolean,
): Set<string> {
  const visible = new Set(nodes.map((node) => node.key));
  if (!hideReadOnly) return visible;

  visible.clear();
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const keepWithAncestors = (node: WorkstreamRef): void => {
    let cursor: WorkstreamRef | undefined = node;
    while (cursor) {
      visible.add(cursor.key);
      cursor = cursor.parent_key ? byKey.get(cursor.parent_key) : undefined;
    }
  };
  for (const node of nodes) {
    if (
      node.parent_key === null
      || node.session_id === focusedSessionId
      || hasDurableOutput(node)
    ) {
      keepWithAncestors(node);
    }
  }
  return visible;
}

export function automaticExpandedWorkstreamKeys(nodes: WorkstreamRef[]): Set<string> {
  const expanded = new Set<string>();
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  for (const node of nodes) {
    if (node.activity_state !== 'ongoing') continue;
    let parent = node.parent_key ? byKey.get(node.parent_key) : undefined;
    while (parent) {
      expanded.add(parent.key);
      parent = parent.parent_key ? byKey.get(parent.parent_key) : undefined;
    }
  }
  return expanded;
}
