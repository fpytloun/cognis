import type { WorkstreamRef } from '$lib/chat-v2/types';
import { isActiveExecutionState, isEffectiveSessionRunning } from '$lib/session-status';

export const HIDE_READ_ONLY_STORAGE_KEY = 'cognis:activity-tree:hide-read-only:v1';
export const HIDE_CLOSED_STORAGE_KEY = 'cognis:activity-tree:hide-closed:v1';

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

export function isClosedWorkstream(node: WorkstreamRef): boolean {
  return !isActiveExecutionState(node.execution_state) && node.activity_state === 'closed';
}

/**
 * A node represents the focused session when its own `session_id` matches,
 * or when the focused session was folded into this node as a rotated
 * `backing_session_id` (topology collapses rotated sessions into one
 * logical node, so the visible node's `session_id` alone is insufficient).
 */
export function nodeMatchesFocus(
  node: WorkstreamRef,
  focusedSessionId: string | null | undefined,
): boolean {
  if (!focusedSessionId) return false;
  if (node.session_id === focusedSessionId) return true;
  return Boolean(node.backing_session_ids?.includes(focusedSessionId));
}

/**
 * Ancestor keys (not including the focused node itself) that must stay
 * expanded so the focused/selected logical node remains reachable and
 * visible, even if the user manually collapsed one of those ancestors.
 */
export function focusedAncestorWorkstreamKeys(
  nodes: WorkstreamRef[],
  focusedSessionId: string | null | undefined,
): Set<string> {
  const expanded = new Set<string>();
  if (!focusedSessionId) return expanded;
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const focused = nodes.find((node) => nodeMatchesFocus(node, focusedSessionId));
  let parent = focused?.parent_key ? byKey.get(focused.parent_key) : undefined;
  while (parent) {
    expanded.add(parent.key);
    parent = parent.parent_key ? byKey.get(parent.parent_key) : undefined;
  }
  return expanded;
}

function keepWithAncestors(
  nodes: WorkstreamRef[],
  byKey: Map<string, WorkstreamRef>,
  predicate: (node: WorkstreamRef) => boolean,
): Set<string> {
  const visible = new Set<string>();
  for (const node of nodes) {
    if (!predicate(node)) continue;
    let cursor: WorkstreamRef | undefined = node;
    while (cursor) {
      visible.add(cursor.key);
      cursor = cursor.parent_key ? byKey.get(cursor.parent_key) : undefined;
    }
  }
  return visible;
}

export interface VisibleWorkstreamFilters {
  hideReadOnly?: boolean;
  hideClosed?: boolean;
  runtimeActiveSessionIds?: string[];
}

/**
 * Resolve the set of workstream keys visible under the "Hide read-only" and
 * "Hide closed" filters. Both filters always retain root nodes, the focused
 * node, and every ancestor required to keep a retained node reachable from
 * the root; they compose (a node must survive both to stay visible).
 */
export function visibleWorkstreamKeys(
  nodes: WorkstreamRef[],
  focusedSessionId: string | null,
  filters: VisibleWorkstreamFilters | boolean = {},
): Set<string> {
  const { hideReadOnly = false, hideClosed = false, runtimeActiveSessionIds = [] } = typeof filters === 'boolean'
    ? { hideReadOnly: filters, hideClosed: false, runtimeActiveSessionIds: [] }
    : filters;
  if (!hideReadOnly && !hideClosed) return new Set(nodes.map((node) => node.key));

  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const isRootOrFocused = (node: WorkstreamRef): boolean => (
    node.parent_key === null || nodeMatchesFocus(node, focusedSessionId)
  );
  const isRunning = (node: WorkstreamRef): boolean => (
    isEffectiveSessionRunning(
      node.status,
      node.activity_state,
      runtimeActiveSessionIds.includes(node.session_id),
      node.execution_state,
    )
  );

  let visible = new Set(nodes.map((node) => node.key));
  if (hideReadOnly) {
    visible = keepWithAncestors(
      nodes,
      byKey,
      (node) => isRootOrFocused(node) || isRunning(node) || hasDurableOutput(node),
    );
  }
  if (hideClosed) {
    const closedKeep = keepWithAncestors(
      nodes,
      byKey,
      // Current execution must stay visible even when physical-session metadata
      // still describes a closed predecessor.
      (node) => isRootOrFocused(node) || !isClosedWorkstream(node),
    );
    visible = new Set([...visible].filter((key) => closedKeep.has(key)));
  }
  return visible;
}

export function automaticExpandedWorkstreamKeys(
  nodes: WorkstreamRef[],
  runtimeActiveSessionIds: string[] = [],
): Set<string> {
  const expanded = new Set<string>();
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  for (const node of nodes) {
    if (!isEffectiveSessionRunning(
      node.status,
      node.activity_state,
      runtimeActiveSessionIds.includes(node.session_id),
      node.execution_state,
    )) continue;
    let parent = node.parent_key ? byKey.get(node.parent_key) : undefined;
    while (parent) {
      expanded.add(parent.key);
      parent = parent.parent_key ? byKey.get(parent.parent_key) : undefined;
    }
  }
  return expanded;
}
