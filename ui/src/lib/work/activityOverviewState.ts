import type { ActivityOverviewResponse } from '$lib/chat-v2/types';
import { presentActivityLifecycle } from './activityLifecycle';

export function activityOverviewHasContent(overview: ActivityOverviewResponse): boolean {
  const summary = overview.summary;
  return Boolean(
    summary.changed_files
    || summary.commands
    || summary.mutations
    || summary.artifacts
    || summary.deliverables
    || overview.workstreams.length
    || Object.values(overview.recent).some((items) => (items?.length ?? 0) > 0)
    || (overview.recent_work && Object.values(overview.recent_work).some((items) => items.length > 0))
  );
}

export function classifyActivityOverviewPresentation(overview: ActivityOverviewResponse) {
  return {
    lifecycle: presentActivityLifecycle(overview.materialization),
    hasContent: activityOverviewHasContent(overview),
  };
}

export type ActivityOverviewReadSection = 'root' | 'focused';

interface ActivityOverviewReadSlot {
  scopeKey: string | null;
  generation: number;
  pending: boolean;
  hasData: boolean;
  error: string | null;
}

export interface ActivityOverviewReadState {
  root: ActivityOverviewReadSlot;
  focused: ActivityOverviewReadSlot;
}

export interface ActivityOverviewReadToken {
  section: ActivityOverviewReadSection;
  scopeKey: string;
  generation: number;
}

export function emptyActivityOverviewReadState(): ActivityOverviewReadState {
  const slot = (): ActivityOverviewReadSlot => ({
    scopeKey: null,
    generation: 0,
    pending: false,
    hasData: false,
    error: null,
  });
  return { root: slot(), focused: slot() };
}

export function beginActivityOverviewRead(
  state: ActivityOverviewReadState,
  section: ActivityOverviewReadSection,
  scopeKey: string,
  hasData: boolean,
): { state: ActivityOverviewReadState; token: ActivityOverviewReadToken } {
  const generation = state[section].generation + 1;
  return {
    state: {
      ...state,
      [section]: { scopeKey, generation, pending: true, hasData, error: null },
    },
    token: { section, scopeKey, generation },
  };
}

export function settleActivityOverviewRead(
  state: ActivityOverviewReadState,
  token: ActivityOverviewReadToken,
  result: { applied: boolean; hasData: boolean; error?: string | null },
): ActivityOverviewReadState {
  const current = state[token.section];
  if (current.scopeKey !== token.scopeKey || current.generation !== token.generation) {
    return state;
  }
  return {
    ...state,
    [token.section]: {
      ...current,
      pending: false,
      hasData: result.hasData,
      error: result.applied ? null : result.error ?? null,
    },
  };
}

export function cancelActivityOverviewRead(
  state: ActivityOverviewReadState,
  section: ActivityOverviewReadSection,
): ActivityOverviewReadState {
  const current = state[section];
  return {
    ...state,
    [section]: {
      ...current,
      generation: current.generation + 1,
      pending: false,
      error: null,
    },
  };
}

export function activityOverviewReadPresentation(
  state: ActivityOverviewReadState,
  requirements: Array<{
    section: ActivityOverviewReadSection;
    scopeKey: string;
    hasData: boolean;
  }>,
): {
  loading: boolean;
  refreshing: boolean;
  ready: boolean;
  error: string | null;
} {
  const slots = requirements.map(({ section, scopeKey, hasData }) => {
    const slot = state[section];
    return slot.scopeKey === scopeKey
      ? { ...slot, hasData: hasData || slot.hasData }
      : { scopeKey, generation: 0, pending: false, hasData, error: null };
  });
  const ready = slots.every((slot) => slot.hasData);
  const pending = slots.some((slot) => slot.pending);
  return {
    loading: pending && !ready,
    refreshing: pending && ready,
    ready,
    error: ready
      ? null
      : slots.find((slot) => !slot.hasData && slot.error)?.error ?? null,
  };
}
