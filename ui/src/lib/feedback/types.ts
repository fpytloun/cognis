/** Feedback wire contracts. Python's strict feedback DTOs own validation. */
export type FeedbackContext = { kind: 'chat'; conversation_id: string } | { kind: 'standalone' };
export interface FeedbackEventRef {
  store: string; session_id: string; seq: number; event_id?: string; event_type: string;
}
export type FeedbackSourceRef =
  | { kind: 'document'; content_ref: string }
  | { kind: 'transcript'; conversation_id: string; source_event: FeedbackEventRef; source_item_id: string; field_path: string[]; representation?: 'chat-item-as-of-event-v1' | null }
  | { kind: 'work_diff'; activity_scope_id: string; source_event: FeedbackEventRef; source_item_id: string; file_ordinal: number; root_id: string | null; path: string; old_path: string | null };
export interface FeedbackTextRange {
  representation: 'feedback-text-v1'; text_digest: string; start: number; end: number;
  exact: string; prefix: string; suffix: string;
  start_line?: number; start_column?: number; end_line?: number; end_column?: number;
}
export type FeedbackTextScope =
  | { kind: 'document' }
  | { kind: 'rich_field'; block_id: string; identity: 'semantic' | 'revision_local'; item_key: string | null; field_path: string[] }
  | { kind: 'pdf_page'; page: number };
export interface CanonicalFeedbackText {
  representation: 'feedback-text-v1'; text_digest: string; length: number; available_ranges: [number, number][];
}
export interface CanonicalFeedbackTextPage {
  representation: 'feedback-text-v1'; text_digest: string; start: number; end: number; total_length: number; text: string;
}
export interface ScopedCanonicalFeedbackText {
  scope: FeedbackTextScope; content_digest: string; descriptor: CanonicalFeedbackText; text_page: CanonicalFeedbackTextPage;
}
export interface FeedbackTextRequest {
  source: FeedbackSourceRef; context: FeedbackContext; scope: FeedbackTextScope; start: number; limit: number;
}
export interface FeedbackPoint { x: number; y: number }
export type FeedbackShape =
  | { kind: 'pin'; point: FeedbackPoint }
  | { kind: 'rectangle' | 'ellipse'; x: number; y: number; width: number; height: number }
  | { kind: 'arrow'; from: FeedbackPoint; to: FeedbackPoint }
  | { kind: 'freehand'; points: FeedbackPoint[] };
export interface FeedbackViewport {
  width_css_px: number; height_css_px: number; scroll_x_css_px: number; scroll_y_css_px: number; device_pixel_ratio: number;
}
export interface RichTableRange {
  representation: 'rich-table-v1';
  scope: 'cells' | 'rows' | 'columns';
  rows: number[];
  column_start: number;
  column_end: number;
}
export type FeedbackLocator =
  | { kind: 'whole' }
  | { kind: 'text'; range: FeedbackTextRange }
  | { kind: 'rich'; block_id: string; identity: 'semantic' | 'revision_local'; item_key: string | null; field_path: string[]; range: FeedbackTextRange | null; table_range?: RichTableRange | null }
  | { kind: 'html'; element_key: string | null; selector_path: string | null; range: FeedbackTextRange | null; viewport: FeedbackViewport; render_state_digest: string; shapes: FeedbackShape[]; capture_id: string | null }
  | { kind: 'image'; intrinsic_width: number; intrinsic_height: number; orientation: number; shapes: FeedbackShape[] }
  | { kind: 'pdf'; page: number; page_width_pt: number; page_height_pt: number; rotation: 0 | 90 | 180 | 270; shapes: FeedbackShape[]; range: FeedbackTextRange | null }
  | { kind: 'diff'; scope: 'file' | 'lines'; side: 'old' | 'new' | null; start_line: number | null; end_line: number | null; hunk_digest: string | null; quote: string | null };
export interface FeedbackAnchor {
  schema_version: 1; source_token: string; content_digest: string; locator: FeedbackLocator;
}
export interface SelectedFeedbackVersion {
  entry_version_id: string; expected_entry_version: number; staging_version?: number;
}
export interface IncludedFeedback { creation_key: string; preflight_id: string; manifest_hash: string }
export type FeedbackDestination =
  | { kind: 'origin_chat' | 'producer_conversation' | 'existing_followup'; conversation_id: string }
  | { kind: 'new_followup'; agent_id: string; project_id: string | null; title: string | null }
  | { kind: 'task_revision'; task_id: string; expected_attempt: number; target_step: string };
export type FeedbackUpdateMode = 'follow_request' | 'revise_source' | 'answer_only' | 'separate_result';
export interface FeedbackPreflightRequest {
  context: FeedbackContext; selection: SelectedFeedbackVersion[]; reviewed_guest_entry_version_ids: string[];
  overall_message: string | null; destination: FeedbackDestination; update_mode: FeedbackUpdateMode;
}
export interface FeedbackCapabilities {
  read: boolean; comment: boolean; moderate: boolean; dispatch: boolean; disclose: boolean;
}
export interface ResolvedFeedbackSource {
  subject_id: string; source_token: string; source: FeedbackSourceRef; content_digest: string;
  availability: 'available' | 'partial' | 'unavailable'; permitted_locator_kinds: FeedbackLocator['kind'][];
  context: FeedbackContext; lineage_id: string | null; revision: number | null; latest_content_ref: string | null;
  capabilities: FeedbackCapabilities; title: string | null; format: string;
  canonical_text: CanonicalFeedbackText | null;
}
export interface FeedbackEntryVersion {
  entry_id: string; entry_version_id: string; version: number; thread_id: string; body: string;
  author: { kind: 'verified_user' | 'guest_unverified' | 'agent'; label: string; is_self: boolean }; audience: string;
  reply_to_entry_id: string | null; created_at: string;
  delivery: { submission_id: string; manifest_hash: string; delivery: SubmissionReceipt['delivery'] }[];
}
export interface FeedbackThread {
  thread_id: string; subject_id: string; version: number; state: 'open' | 'resolved';
  hidden: boolean; audience: string;
  anchor: { schema_version: 1; anchor_id: string; source: FeedbackSourceRef; locator: FeedbackLocator; content_digest: string };
  entries: FeedbackEntryVersion[];
}
export interface FeedbackThreadPage { items: FeedbackThread[]; next_cursor: string | null }
export interface FeedbackStagedItem {
  entry_id: string; entry_version_id: string; staging_version: number; inclusion: 'prepared' | 'included';
  subject_id: string; anchor_id: string;
}
export interface SubmissionReceipt {
  submission_id: string; creation_key: string; manifest_hash: string;
  delivery: 'pending' | 'accepted' | 'rejected' | 'uncertain';
  work: 'queued' | 'working' | 'waiting_for_input' | 'completed' | 'failed' | 'cancelled' | 'unavailable';
  reconciling: boolean; version: number;
  admission: { kind: 'conversation_turn'; request_id: string; conversation_id: string; turn_id: string }
    | { kind: 'task_attempt'; revision_request_id: string; task_id: string; attempt_number: number } | null;
  rejection: { code: string; recoverable: boolean } | null;
  check_delivery_available: boolean; review_again_available: boolean;
}
export interface FeedbackStagingSnapshot {
  conversation_id: string; version: number; items: FeedbackStagedItem[]; receipts: SubmissionReceipt[];
  /** Every subject commented on from this conversation, including resolved or removed comments. */
  subject_ids?: string[];
}
export interface FeedbackPreflight {
  preflight_id: string; manifest_hash: string; expires_at: string; selection: SelectedFeedbackVersion[];
  context_items: { entry: FeedbackEntryVersion; anchor: FeedbackThread['anchor']; role: 'instruction' | 'context' }[];
  source_summaries: ResolvedFeedbackSource[];
  destination: FeedbackDestination; update_mode: FeedbackUpdateMode; warnings: string[];
  task_consequences: { safety_token: string; expected_attempt: number; workflow_definition_digest: string;
    target_step: string; downstream_steps: string[]; side_effect_warning: string;
    pending_interactions: { notification_id: string; revision: number; notification_type: string; status: string }[] } | null;
}
export interface FeedbackDraftRequest {
  expected_version: number; context: FeedbackContext; source: FeedbackSourceRef | null;
  anchor: FeedbackAnchor | null; body: string; selection: SelectedFeedbackVersion[]; overall_message: string | null;
}
export interface FeedbackSubmissionDetail {
  receipt: SubmissionReceipt;
  manifest: {
    schema_version: 1; context: FeedbackContext; entries: FeedbackPreflight['context_items'];
    sources: Pick<ResolvedFeedbackSource, 'subject_id' | 'source' | 'content_digest' | 'lineage_id' | 'revision' | 'format' | 'title' | 'availability' | 'canonical_text'>[];
    destination: FeedbackDestination; update_mode: FeedbackUpdateMode; overall_message: string | null;
    task_consequences: FeedbackPreflight['task_consequences'];
  };
}
export interface FeedbackShare {
  can_copy?: boolean;
  link_mode?: 'view' | 'download';
  share_id: string; share_token: string | null; version: number; allow_comments: boolean;
  discussion: 'own_only' | 'shared'; expires_at: string; revoked: boolean;
}

/** Native browser objects never cross the JSON API boundary. */
export interface ObservedCaptureCandidate {
  blob: Blob; viewport: FeedbackViewport; render_state_digest: string;
  width: number; height: number; method: 'observed_pixels';
}
export interface FeedbackAnchorCandidate {
  locator: FeedbackLocator; capture?: ObservedCaptureCandidate;
}
export type FeedbackMode = 'view' | 'comment' | 'area' | 'draw';
export interface FeedbackHighlight {
  thread_id: string; anchor_id: string; locator: FeedbackLocator;
  category: 'exact' | 'remapped' | 'not_located'; label: string;
}
export interface FeedbackLocateResult {
  status: 'exact' | 'remapped' | 'not_located' | 'unavailable';
  historical_source?: FeedbackSourceRef; reason?: string;
}
export interface FeedbackAnnotationAdapter {
  locate: (anchor: FeedbackLocator) => FeedbackLocateResult | Promise<FeedbackLocateResult>;
  clearSelection: () => void;
  targetGeometry?: (anchor: FeedbackLocator) => FeedbackTargetGeometry | null;
  editorHost?: (locator?: FeedbackLocator | null) => HTMLElement | null;
  exposeTarget?: (anchor: FeedbackLocator, visible: FeedbackVisibleArea) => void | Promise<void>;
}
export interface FeedbackVisibleArea { left: number; top: number; width: number; height: number }
export type FeedbackTool = 'text' | 'element' | 'area' | 'draw';
/** Client-viewport CSS pixels, re-resolved after scroll/zoom; never persisted. */
export interface FeedbackTargetGeometry {
  space: 'client';
  rects: { left: number; top: number; width: number; height: number }[];
}
export interface FeedbackTargetView {
  candidate: FeedbackAnchorCandidate;
  label: string;
  surface_id: string;
  instance_id: string;
  geometry: () => FeedbackTargetGeometry | null;
  /** Optional native control slot, separate from the highlighted source (for example a diff gutter). */
  controlGeometry?: () => FeedbackTargetGeometry | null;
  validate?: () => Promise<void>;
  scopes?: { label: string; target: () => FeedbackTargetView }[];
}
export interface FeedbackSurfaceBinding {
  readonly source: ResolvedFeedbackSource;
  readonly context: FeedbackContext;
  readonly mode: FeedbackMode;
  readonly supported_locator_kinds: FeedbackLocator['kind'][];
  readonly completeness: ResolvedFeedbackSource['availability'];
  readonly highlights: FeedbackHighlight[];
  readonly selected_thread_id: string | null;
  readonly tool?: FeedbackTool;
  readonly accepted_candidate?: FeedbackAnchorCandidate | null;
  onTargetChange?: (target: FeedbackTargetView | null) => void;
  resolveTextScope: (scope: FeedbackTextScope, page?: { start: number; limit: number }) => Promise<ScopedCanonicalFeedbackText>;
  onAnchorCandidate: (candidate: FeedbackAnchorCandidate) => void;
  onSelectThread: (threadId: string) => void;
  onInteractionChange: (active: boolean) => void;
  registerAdapter: (instanceId: string, adapter: FeedbackAnnotationAdapter) => () => void;
}
