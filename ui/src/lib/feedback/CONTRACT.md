# Renderer integration

The shell owns this directory except `annotations/`. All wire values use the
backend's snake-case DTOs. `FeedbackContext` is `chat` with `conversation_id`
or `standalone`; public credentials belong to a separate client, not this DTO.

Caller-owned `FeedbackSourceProvider` supplies a reactive surface binding.
Renderers call `getFeedbackSurface()` once during component initialization,
then derive `surface?.binding`. Do not copy the initial binding: it changes
after resolution, selection, mode and revision changes.

`FeedbackContextProvider` establishes explicit originating-chat context for
descendants, including portaled/fullscreen descendants. Never inspect the
browser URL or content producer to choose a destination.

Adapters submit `FeedbackAnchorCandidate` through `onAnchorCandidate` and
register `locate`/`clearSelection` with a unique render-instance ID. The shell
adds source token/digest, composes/saves comments, uploads confirmed observed
captures and stages comments. Adapters never save or dispatch directly.
`onInteractionChange` pins the source while selecting, drawing or reviewing.

Adapters normally publish `FeedbackTargetView` through `onTargetChange` instead.
It contains an existing candidate, the exact surface/adapter identity, a label,
optional explicit structural scopes and a geometry resolver. Rectangles use
client-viewport CSS pixels. The shared floating Add action validates identity
and invokes candidate acceptance; selection movements never modify the draft.
Use the shared native-selection action for text and retain format-specific
canonical mapping. Text-tool clicks/Enter must not create structural targets.

`FeedbackPresenterSession` is ephemeral presentation only. Mode, draft, accepted
candidate, selected thread and pending operation remain in existing owners.
Private and public hosts expose the narrow `FeedbackPresentationPort`; guest
presentation has no private review or dispatch owner. Heavy inline/rail UI stays
behind existing deferred definitions.

The presenter publishes only measured `FeedbackSourceLayout` geometry while a
composer is open. The lexical `FeedbackWorkspace` owns a constrained,
scrollable source viewport and measured flow reservation, so the final source
line remains reachable above the composer even at maximum scroll. Nested
workspaces on the same surface reuse that owner. Private, public and Work
providers project the same contract. Renderers never pad arbitrary ancestors.
Closing, invalidation and disposal release accommodation, not draft state.
Adapters' `exposeTarget` only reveals within this usable viewport.

Inline overlays use the existing portal action to escape source stacking
contexts. A non-interactive CSS probe resolves the app-owned
`--app-floating-overlay-*` safe/chrome bounds; the shared presenter intersects
them with `visualViewport`, including keyboard and panning changes. It never
queries a renderer's navigation DOM or invents a standalone navigation inset.
Toolbar height is measured before positioning target/preview cards. Temporary
target, composer and saved preview share one exclusive card branch; a new
target suppresses the preview without changing the selected thread or history.
The shared surface-portal action retains an owner anchor and follows that
owner's hidden/inert/ARIA/CSS visibility. Suspended retained surfaces hide and
inert their portaled controls, release layout accommodation, and suspend global
Escape/autofocus without clearing the draft or composer session.

Mount `FeedbackIndicator` in renderer toolbars. It derives count/open from
the source provider. `FeedbackWorkspace` accepts `children`, optional `panel`
snippets and `open`; use it to preserve content visibility alongside the
shell-provided panel. The shell will provide the controller-backed panel and
chrome through context as implementation proceeds; renderers must not
duplicate thread/review logic.

The provider now supplies `panel` and `opened` reactively. Workspace defaults to
those values when explicit props are absent; fullscreen renderers must retain
the inherited provider rather than building a second thread controller.

`binding.resolveTextScope(scope, page?)` retrieves server-owned canonical text.
Scopes are `document`, `rich_field` (block identity/item/path), or `pdf_page`.
Optional `{start, limit}` selects a bounded absolute UTF-16 page (default
0/16384, maximum 32768). Its response is exactly
`{scope, content_digest, descriptor, text_page}`. Use the returned field/page
descriptor, never a global rich digest or browser PDF extraction digest.
Unavailable/unseen text cannot authorize a text anchor. The shell cache guards
exact source, digest and access context before and after every response.

`FileDiffRef.feedback_source` carries backend-owned exact per-event evidence.
Missing references remain unbound; do not reconstruct them from a combined diff.

Capture candidates contain a transient PNG/WebP Blob, dimensions, viewport and
render state. Browser objects are never serialized into anchors or drafts.
Only confirmed, cropped observations may be adopted; failed adoption retains
the comment and requires retry or a non-visual anchor.

Source resolution/response field declarations will be checked against the
backend's canonical schema before integration. Contract changes must be
reported to the architecture owner and the annotation owner.
