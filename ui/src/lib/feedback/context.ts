import { getContext, setContext, type Snippet } from 'svelte';
import type { FeedbackContext, FeedbackMode, FeedbackSurfaceBinding, FeedbackVisibleArea } from './types';
import type { FeedbackSourceLayout } from './presenter-session';

const originKey = Symbol('feedback-origin');
const surfaceKey = Symbol('feedback-surface');
const workspaceKey = Symbol('feedback-workspace');
export interface FeedbackOriginProvider { readonly context: FeedbackContext }
export interface FeedbackSurfaceProvider {
  readonly binding: FeedbackSurfaceBinding | null;
  readonly commentCount?: number | null;
  readonly opened?: boolean;
  readonly panel?: Snippet;
  readonly available?: boolean;
  readonly layout?: FeedbackSourceLayout | null;
  open: (view?: 'threads' | 'review' | 'history' | 'access') => void;
  commenting?: (enabled: boolean) => void;
  requestCommentAccess?: () => void;
  prepare?: () => void | Promise<void>;
  whole?: () => void;
  setMode?: (mode: FeedbackMode) => void;
  workspaceOwned?: boolean;
  resume?: () => void;
  registerWorkspace?: () => () => void;
  reportBounds?: (bounds: FeedbackVisibleArea | null) => void;
  readonly indicatorOwner?: symbol;
  registerIndicator?: (id: symbol) => () => void;
  /** The comment list is open (floating or docked), independent of transient composer/target state. */
  readonly railOpen?: boolean;
  /** Toggle the comment list together with comment mode: the single comment button. */
  show?: () => void;
  close?: () => void;
  /** A host-side sidebar renders `panel` while registered, replacing the floating list. */
  registerDock?: () => () => void;
}

/** Explicit lexical origin survives portals; URL/provenance are never routing input. */
export function provideFeedbackContext(provider: FeedbackOriginProvider): void {
  setContext(originKey, provider);
}
export function getFeedbackContext(): FeedbackOriginProvider {
  return getContext<FeedbackOriginProvider | undefined>(originKey) ?? { context: { kind: 'standalone' } };
}
export function provideFeedbackSurface(provider: FeedbackSurfaceProvider): void {
  setContext(surfaceKey, provider);
}
export function getFeedbackSurface(): FeedbackSurfaceProvider | undefined {
  return getContext<FeedbackSurfaceProvider | undefined>(surfaceKey);
}
/** A nested renderer reuses its existing lexical surface's workspace. */
export function claimFeedbackWorkspace(surface: FeedbackSurfaceProvider | undefined): boolean {
  const parent = getContext<FeedbackSurfaceProvider | undefined>(workspaceKey);
  setContext(workspaceKey, surface);
  return Boolean(surface) && parent !== surface;
}
