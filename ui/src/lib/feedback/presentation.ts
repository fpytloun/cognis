import type { Readable } from 'svelte/store';
import type { FeedbackPresenterSession } from './presenter-session';
import type {
  FeedbackAnchorCandidate, FeedbackContext, FeedbackEntryVersion, FeedbackMode, FeedbackSurfaceBinding,
  FeedbackThread, FeedbackTargetGeometry, FeedbackVisibleArea, ResolvedFeedbackSource, FeedbackLocator,
} from './types';

/** A projection over the existing owner, not another data or persistence store. */
export interface FeedbackPresentationState {
  source: ResolvedFeedbackSource | null;
  threads: FeedbackThread[];
  mode: FeedbackMode;
  selected: string | null;
  candidate: FeedbackAnchorCandidate | null;
  draft: string;
  saving: boolean;
  pending_body: string | null;
  reply_draft?: { thread_id: string | null; body: string };
  error: string;
  loading: boolean;
  next_cursor: string | null;
  open: boolean;
  view: 'threads' | 'review' | 'history' | 'access';
}
export interface FeedbackPresentationPort {
  state: Readable<FeedbackPresentationState>;
  session: FeedbackPresenterSession;
  context: FeedbackContext;
  binding: () => FeedbackSurfaceBinding | null;
  /** Local owner/lease identity, never sent to an API. */
  authority?: () => string;
  accept: (candidate: FeedbackAnchorCandidate) => void;
  setMode: (mode: FeedbackMode) => void;
  setDraft: (body: string) => void;
  setReplyDraft?: (threadId: string, body: string) => void;
  canEdit?: (entry: FeedbackEntryVersion) => boolean;
  discard: () => Promise<void>;
  save: () => Promise<void>;
  closeRail: () => void;
  openRail: (view?: FeedbackPresentationState['view']) => void;
  /** Start a comment on the whole source when that target kind is permitted. */
  commentWhole?: () => void;
  select: (threadId: string) => void;
  geometry: (candidate: FeedbackAnchorCandidate) => FeedbackTargetGeometry | null;
  editorHost?: (locator?: FeedbackLocator | null) => HTMLElement | null;
  reveal: (candidate: FeedbackAnchorCandidate) => Promise<void>;
  expose: (candidate: FeedbackAnchorCandidate, visible: FeedbackVisibleArea) => Promise<void>;
  locate: (thread: FeedbackThread) => Promise<void>;
  more: () => Promise<void>;
  reload: () => Promise<void>;
  reply: (thread: FeedbackThread, body: string) => Promise<void>;
  edit: (entry: FeedbackEntryVersion, body: string) => Promise<void>;
  resolve?: (thread: FeedbackThread) => Promise<void>;
  moderate?: (thread: FeedbackThread, reason: string) => Promise<void>;
  reattach?: (thread: FeedbackThread, reason: string) => Promise<void>;
  capturePixels?: (id: string) => Promise<Blob>;
  loadHistory?: () => Promise<void>;
}

export function presentationAuthority(port: FeedbackPresentationPort, state: FeedbackPresentationState): string {
  try {
    return JSON.stringify([port.authority?.() ?? null, state.source?.source, state.source?.source_token,
      state.source?.content_digest, state.source?.availability, state.source?.capabilities, state.source?.permitted_locator_kinds, port.context]);
  } catch { return ''; }
}
