# Cognis Work Feedback

This specification describes feedback interactions and ownership boundaries,
including context-bound chat review staging.

## 1. Recommendation

Build **one feedback system** for artifacts, deliverables, and Work diffs. Give each surface a format-specific annotation adapter, but share comments, review, submission, permissions, and history.

The interaction is:

**Annotate → review selected feedback → choose destination → send → follow work → inspect the result.**

Saving a comment never starts an agent. Sending creates a durable, immutable submission. A comment, a submission, an execution, and a resulting revision are different objects with different states.

The content remains the center of the interface. Use a Google Docs-style sidebar, anchored highlights, and a persistent feedback/work indicator—not another separate review application.

### Product invariants

- Every annotation identifies the exact revision or diff evidence reviewed.
- Excluding a comment from a submission does not delete or resolve it.
- Only explicitly selected entry versions go to the agent; later replies are not silently included.
- Comment access does not imply permission to run an agent, revise a task, or modify content.
- Starting or finishing agent work does not automatically resolve comments.
- The source always exposes feedback history and authorized links to its destinations and results.
- New revisions preserve old feedback; uncertain anchors are never silently relocated.

## 2. Workspace and interaction

### Desktop

The following fictional workspace sketch illustrates the interaction.

```text
  Daily brief            Revision 3 ▾   Viewing ▾   Comments 8   Share
 Follow-up: Daily actions · Working                          Open ↗
 ┌──────────────────────────────────────┬────────────────────────────┐
  │ Text | Element | Area | Draw         │ Comments      Work/History │
 │                                      │ Open · This revision ▾     │
 │ The document, wireframe, image,       │                            │
 │ PDF page, or code diff               │ ① Weather card             │
  │                                      │ Reviewer · Not sent        │
 │ Highlighted selection ①              │ “Add an hourly forecast.”  │
 │                                      │ Reply · Resolve · Locate   │
 │ Region annotation ②                 │                            │
 │                                      │ ② Deployment findings      │
 │                                      │ Guest · Not sent           │
 │                                      │ “Can this be retried?”      │
 │                                      │                            │
 │                                      │ Review & send              │
 └──────────────────────────────────────┴────────────────────────────┘
```

This is a layout sketch, not a proposed new visual theme. Reuse Cognis tokens and existing document/workspace chrome.

- `Viewing`/`Commenting` is separate from the `Comments` indicator and optional rail.
- A temporary target exposes `Add comment`; only that deliberate action accepts a candidate into the existing draft owner.
- Clicking a marker opens a compact preview; `Open thread` opens the rail. `Show target` restores the exact supported source context.
- Viewing preserves normal links and controls. The default Text tool observes native ranges; clicking or pressing Enter never creates a paragraph/block target. Element interception and area/draw pointer capture require explicit tools.
- Offer only relevant annotation tools for the format. Whole-document and whole-file comments are always available.
- Thread cards show author, guest/unverified identity when applicable, revision, anchor excerpt/thumbnail, replies, and dispatch history.
- The compact rail offers open/resolved threads, history, and permitted advanced actions. History is not erased when the rail is closed.
- Markers have accessible labels and a keyboard-navigable comment list. Color is not the sole state signal.
- On narrow screens a bounded rail/composer leaves the source visible. Positioning follows `visualViewport` during keyboard changes; controls have at least 44 CSS pixel touch targets. Support native selection handles and explicit non-drag area adjustment; never require hover.

### Comment creation

1. Select native text, an explicit element/line target, or an explicit area. Element targeting starts at a table cell, with deliberate Cell/Row/Table expansion. Whole-source commenting is a separate explicit action.
2. `Add comment` accepts the current validated target and opens the shared contextual composer. Selection movement alone changes no durable candidate or draft.
3. Enter the comment, then `Save comment` publishes it to the permitted audience. It does not enqueue work. Closing or switching to Viewing preserves the draft; explicit discard cannot erase an uncertain original save.
4. Add more comments or replies, then select `Review & send`.

Private drafts survive navigation/reload and stay account-scoped. Submission must wait for selected comments to be durably saved. Offline comments remain clearly pending; never pretend they are published.

Authors can edit their comments. Edits create entry versions; previously sent versions remain in submission history. Replies are separate entries. Resolution and reopening are explicit audited actions. An agent may mark feedback **Addressed**, **Needs clarification**, or **Not changed** with a reason and result reference; the human reviewer decides whether to resolve it.

### Review & send

Use the same sidebar, replacing the thread list with a review step:

- Checkboxes for individual comments/replies, grouped by thread and source.
- Selected count, quoted context, author, and revision on every group.
- Select all/none, plus an easy way to inspect each highlight.
- Optional overall message.
- An explicit destination and explanation of what will happen.
- A preview of the exact feedback packet the agent will receive.

Default to unsent comments authored by the submitter. Show other collaborators' and guest comments as available but unchecked. Previously sent comments require deliberate reselection and show where they were sent. Selecting a reply includes minimal parent context, visibly labelled as context—not an additional instruction.

The selected set is frozen for review. If an entry changes, an included revision becomes inaccessible, or task revision consequences change before sending, show the change and require a refreshed confirmation. Task confirmation binds the attempt, selected step and downstream scope, safety-relevant workflow state, and pending-interaction identity/version. Same-attempt progression or a new approval can invalidate confirmation too. Incoming comments do not appear in the outgoing packet automatically.

Use action-specific buttons: **Send feedback**, **Start follow-up**, or **Request task revision**. The UI shows what was accepted and a direct work link. Queue-full, permissions, and validation errors preserve the selection and message.

A single submission has one destination. A multi-file code review can submit together. If selected sources have different producers, require one explicit common destination or split the submission; never silently fan out.

### Chat context and the composer

Feedback created inside a conversation stays in that conversation. This includes
inline artifact/deliverable previews, code snippets, and Work inspector content.
The viewing context, not the source document's producer, determines this route.
Task-generated content reviewed from a normal conversation is feedback to that
conversation, not an implicit revision of the producing task.

All composers for the originating conversation show a compact review tray even
when the Work or feedback sidebar is closed:

- **Prepared, not included:** durable saved items waiting for review. Sending an
  ordinary message will not include these items.
- **Included with next message:** explicitly selected immutable entry versions.
  The tray states how many items accompany the next send.
- **Sending, queued, or sent:** the exact submitted selection and its receipt,
  separate from newer staged edits.

New comments start prepared, not included. Review permits include/exclude,
locate, and removal from next send without deleting the underlying feedback.
Explicitly included feedback can be sent without message text. A message with
feedback is one admitted turn, preserving attachments and chat mode. A sidebar
send control in chat stages/reviews the same composer selection; it must not
quietly create a second dispatch path or offer a different destination.

Staging is scoped to authenticated user and originating conversation. Other
conversations never consume or adopt it. Navigation, reload, multi-tab updates,
queueing, retry, and account changes preserve existing draft/outbox guarantees.
Only the accepted snapshot is consumed; new comments and later edits remain.
The user can inspect the immutable selected packet before sending.

Outside chat, retain the explicit destination flow below. A Library or standalone
viewer does not inherit whichever conversation happened to be opened last.

## 3. Annotation adapters

| Surface | Interaction | Durable anchor |
|---|---|---|
| Rich deliverable | Comment on block, card, metric, row, or text range | Exact deliverable revision/hash, stable block ID and item key, field path, text quote and offsets |
| Markdown/plaintext/code | Select text or a line range | Exact content revision/hash, canonical text offsets, exact quote with prefix/suffix; line/column for navigation |
| HTML | Inspect an element, select text, or annotate a region | Exact HTML revision/hash; stable element key when available, selector path and quote; layout/viewport snapshot and region as fallback |
| Image | Pin, rectangle, ellipse, arrow, freehand stroke | Exact image revision/hash; normalized intrinsic-image coordinates and orientation |
| PDF | Highlight text, pin or draw on a page | Exact PDF revision/hash, page number, page-space geometry with rotation transform; text quote when available |
| Work diff | File comment, hunk, line or multi-line range | Canonical activity/source evidence, file/root identity, diff hash, old/new side and range, contextual hunk |
| Code snippet | Select a range in its owning artifact/deliverable or immutable transcript item | Exact owning content/event identity plus range and quote |

Text offsets refer to a versioned canonical text representation, not syntax-highlighter spans or prettified JSON. Preview-only/truncated content cannot pretend to anchor unseen text.

For rich blocks, preserve semantic IDs across compatible revisions. Existing blocks without IDs can receive revision-local render keys; these must not be mistaken for cross-revision identity. Table normalization and sorting carry private authored cell/row paths. Structural Cell/Row targets use those paths without a range; Table uses block identity. A row key is not a block item key. Only authored string fields support canonical ranges; numeric/generated cells remain structural.

### Presentation ownership

`FeedbackPresentationPort` projects existing private/public state and commands;
it is not a new draft, thread, staging or send store. The private controller and
guest host retain their clients, persistence, pending operations and fences.
Guests receive no execution/review commands. Private review remains the existing
immutable-version and destination flow; chat admission remains solely in the
normal composer.

One ephemeral presenter session owns only tool preference, temporary target,
geometry, composer/preview visibility and focus return. Existing owners retain
mode, accepted candidate, draft and selected thread. Native event observation is
shared across text, rich, PDF and opaque HTML, while each format proves its own
mapping. Client-viewport geometry is re-resolved on scroll, resize and keyboard
viewport changes and is never persisted. Async mapping is invalidated by source,
tool, permission, context or render-lifetime replacement.

The existing workspace owns source accommodation from ephemeral
measured presenter geometry: a constrained source viewport and corresponding
flow reservation keep the true source end reachable above the composer and
keyboard. Nested workspaces reuse the lexical owner; private, public and Work
hosts use the same typed layout projection. No global or unrelated-ancestor
padding is injected. Accommodation is released on close or authority teardown
without changing drafts or pending recovery.

Inline cards share exclusive temporary-target/composer/saved-preview ownership.
The existing portal action avoids source stacking contexts, while app-owned
floating-overlay CSS bounds are intersected with the live visual viewport.
Mobile navigation and keyboard occlusion constrain controls without changing
navigation visibility or inventing a public-page inset.

The shared presenter/rail remain deferred from private startup. Work uses its
existing per-event/file controllers behind shared chrome; neither a merged diff
nor the active UI tab can authorize another source.

For images and PDFs, geometry must survive zoom, responsive sizing, device pixel ratio, EXIF orientation, and page rotation. Freehand annotations are bounded vector shapes, not an editable graphics application. PDF text highlighting needs a selectable PDF.js text layer; scanned pages still support areas and drawing without requiring OCR.

### HTML safety and fidelity

Keep HTML inside its current opaque-origin sandbox. Do not add `allow-same-origin` or give artifact scripts application credentials.

Add a small annotation bridge to the preview renderer for selection and geometry. All bridge messages are untrusted: check the exact frame/window, preview-instance binding, schema, bounds, size, and rate. Artifact scripts share that realm; a nonce cannot make their statements authoritative. The bridge may report selection only—it cannot save comments, access APIs, dispatch work, or choose a destination. Trusted application chrome owns those actions.

For dynamic wireframes, preserve the observed viewport, scroll, element/text context, and render-state fingerprint at annotation time. Region/drawing feedback needs an associated visual snapshot when the observed state cannot be reconstructed from canonical HTML. Obtain observed pixels through explicit user-mediated browser capture, preview/crop confirmation, and source-bound raster adoption. When native capture is unavailable or denied, permit a bounded screenshot paste/file within that same source-bound confirmation flow. Label pixels as an unverified user observation, not authoritative render evidence. Never upload the whole screen or application chrome automatically. Decode/re-encode, quota, capability, and private-retrieval checks apply to authenticated and public captures. Freeze annotation on the confirmed image and retain exact HTML source context. Without a capture, keep geometry drafts private and offer element/text/whole-document feedback instead. Do not claim an iframe can be safely rasterized by the parent, or that a fresh server render reproduces the observed dynamic state.

If reliable element inspection is unavailable—for example canvas, closed shadow DOM, or unsupported embedded frames—offer area/whole-document feedback and show the limitation. Never claim pixel coordinates alone identify an element across responsive layouts or revisions.

## 4. Destinations and the four use cases

For feedback created outside chat, offer destinations based on actual provenance
and permissions, not a guessed “owner conversation.” In-chat feedback follows
the originating-chat contract above.

| Destination | Behavior |
|---|---|
| Producer conversation | Queue a normal feedback turn through canonical turn admission; do not interrupt active work |
| New follow-up | Create one conversation with the producing agent by default, attach the feedback/source context, and start one turn |
| Existing follow-up | Reuse the explicitly selected linked conversation; useful for repeated dashboard actions |
| Revise producing task | Request a new attempt of the existing task through its revision owner, with an expected attempt and an explicit re-entry step |

Show unavailable destinations with a reason when useful: producer deleted, shared conversation read-only, task cancelled, inaccessible source, unavailable agent. Imported artifacts with no producer can still start a follow-up using an explicitly chosen eligible agent.

**Daily dashboard.** Start a new follow-up from the day's brief; optionally set it as the preferred follow-up for this document lineage. Later feedback defaults there. The conversation can dispatch managed children and publish refreshed revisions. Link the primary follow-up, submitted turns, explicit children working on that submission, and result revisions. Each day's brief is a distinct document unless the producing workflow deliberately maintains a rolling dashboard; do not accidentally attach tomorrow's comments to today's lineage.

**Weekly task result.** Default to `Revise producing task`, showing the relevant output-producing step, the current attempt, and the downstream steps that will rerun. Re-entry can repeat external side effects: show that consequence. Feedback does not answer an approval or bypass a safety gate. A cancelled task needs a separately confirmed follow-up/rerun, not a hidden resurrection. For a paused task, distinguish revision from answering the pending question; never infer that a comment approves an action.

**Design review.** Collect element/text comments and visual markup against the same revision. Submit them together to the producer or a follow-up. The agent receives visual context and exact content, not only coordinates or an unversioned screenshot.

**Human code review.** Stage comments across files and old/new diff sides, then send once with an overall instruction or question. This is local feedback, not an external GitHub approval or PR mutation. Route task work to task revision and conversation work to a queued turn/follow-up. Binary or incomplete diffs support file-level feedback; line-level feedback requires available exact evidence.

### Read access versus update authority

Following up must not transfer ownership implicitly. The default is the same producer agent, which can revise its private documents under existing rules. Another agent can update project content only if existing project-write policy permits it. Otherwise offer an explicit separate copy or send work back to the authorized producer; do not manufacture write access from a feedback reference.

Children receive only explicitly handed-off source and feedback references through existing entitlement rules. A dashboard coordinator can gather child results and own the final document update. Linking work does not require every child to have document-write permission.

“Keep dashboard updated” is an explicit follow-up instruction, not a new perpetual agent or schedule. Automatic updates happen only as authorized work produces revisions.

## 5. History, revisions, and status

The document header always shows a compact feedback indicator once feedback exists, including resolved/sent history. Library and Work lists expose a summary without loading all comments.

The sidebar's **Work/History** view interleaves:

- Feedback added or resolved.
- Submission: sender, selected entries, source revisions, destination, and time.
- Admission receipt and the exact queued/running turn or task attempt.
- Linked managed/delegated work where explicitly associated.
- Result revision(s), questions, or completion without a document change.

Use three independent state axes:

1. **Thread:** open / resolved.
2. **Delivery:** pending / accepted / rejected / uncertain.
3. **Work:** queued / working / waiting for input / completed / failed / cancelled / unavailable.

Entry delivery labels distinguish **Not sent**, **Sending**, **Sent**, **Send rejected**, and **Checking delivery**. Merely creating a submission does not mean an entry was sent. An entry can participate in several submissions; show their outcomes rather than replacing them with one boolean. “Accepted” means durably admitted—not completed. “Completed” means the submitted execution ended—not that the latest conversation happens to be idle, or that every comment was addressed. A missed lifecycle event or history gap produces unavailable/reconciling status, not invented success.

After reload, recover delivery from the durable receipt using the original submission or client creation key. For uncertain delivery, offer **Check delivery**, never a new send; reconcile the existing intent before enabling another dispatch. Definitive pre-admission rejection offers **Review again**, restoring selection and message. After fresh validation and confirmation, this creates an explicitly new submission linked to the rejected one. Failed or cancelled work that was already accepted is not a send rejection: retrying that work is a separately confirmed lifecycle action.

On a new revision:

- Keep comments and submissions pinned to their original source.
- Show older unresolved feedback in an accessible older-feedback section; default the content view to current-revision threads.
- Carry a highlight forward only when mapping is unambiguous, retaining both original anchor and derived mapping.
- Label mappings as exact, remapped, or not located. Ambiguous text matches, reordered blocks, or altered geometry stay on the original revision.
- Allow an authorized reviewer to reattach a thread with audit history.
- Show `Addressed in revision …` only with an explicit relationship; publishing a revision alone is insufficient.

A live dashboard can follow the latest revision while idle. While the user is selecting, typing, or reviewing, pin the current view and show `New revision available`; never replace the content beneath an unfinished annotation. Exact-revision links remain exact.

Concurrent updates use the existing linear-head check. A stale writer must inspect the new head and reconcile, not overwrite it. Forking/copying content creates a new lineage with an explicit relationship, not a counterfeit revision.

## 6. Sharing and trust

Model permissions independently:

- **Read content**
- **Read/post feedback**
- **Moderate/resolve feedback**
- **Dispatch feedback**
- **Revise content or task / send to a specific conversation**

Dispatch requires the source-feedback permission and the relevant destination permission. It also requires authority to disclose selected context to that destination. A read-only grant is not a reshare grant. Do not expose private conversation titles, task metadata, authors, or hidden-revision excerpts merely because somebody can view a shared document.

For eligible signed-in collaborators, add an explicit commenting capability to the applicable document/project access policy; do not silently upgrade all existing readers. A reader without dispatch rights can leave feedback and see **Awaiting owner review**, not start work as the owner.

Human document sharing is a distinct **Share with people** flow. Direct grants
follow the whole lineage, including future revisions, with a view-only default
and an explicit comment permission. Versioned revocation is independent of public
links and project access. These grants do not confer agent runtime, conversation,
task, dispatch, editing, moderation or reshare authority. Managed outputs cannot
borrow global human access. Sharing eligibility comes from owned Library metadata,
not from whether the current revision supports annotations.

### Public commenting

Add owner-controlled **Allow comments** to public sharing, off by default. Keep ordinary existing public links read-only.

Use a revocable server-side share record for new managed links, including
read-only viewer and download links: exact authorized revision, expiry, capability,
revocation state, and separate feedback audience. Public-link management has
explicit copy and revoke actions and is separate from internal sharing.
Copying recovers the original live token without extending or reviving it;
legacy random tokens are not silently replaced. A public comment token cannot be
a conversation send token or a generic artifact access grant.

Recommended default: guests see their own submissions, while owner/collaborators see the public feedback inbox. Optionally enable a shared public discussion thread, warning that posted comments are visible to everyone with the link. Internal feedback is never automatically exposed.

Anonymous visitors may enter an optional unverified name and leave comments/markup. They cannot dispatch, choose internal destinations, query internal history, upload arbitrary attachments, or receive private work links. An opaque guest-session capability allows editing their own unsent comment; without it, offer no identity claims. Distinguish guest posts from verified users.

Require explicit owner review of guest entries before dispatch. Apply size/count/rate limits, abuse protection, safe text rendering, revocation, and moderation. Never trust author labels supplied by the browser. Avoid recording raw tokens or comment content in logs; do not treat IP addresses as stable identities.

Public viewers get only explicitly public, sanitized feedback/result status. Revocation blocks future access and guest posting, but cannot retract content already viewed or feedback already submitted to an authorized agent.

Submission content remains attributed feedback/context, not system instructions. The authenticated submitter's overall request authorizes considering selected comments, not bypassing tools, credentials, safety gates, or destination policy.

## 7. Backend model and ownership

Add a transport-neutral feedback service; do not put business logic separately in preview components, WebSocket handlers, and task routes.

### Proposed records

| Record | Responsibility |
|---|---|
| Feedback subject | Artifact/deliverable lineage, or canonical Work/activity evidence scope; owner/project identity |
| Anchor | Exact revision/hash or event/diff reference plus a versioned typed locator |
| Thread | Subject, anchor, audience, author, resolution metadata, concurrency version |
| Entry revision | Immutable comment/reply text reference, attribution, edit history, visibility |
| Submission | Sender, optional message reference, destination, immutable manifest/hash, idempotency key |
| Submission item | Selected entry revision and source/anchor references; no mutable “send all comments” query |
| Work link | Submission → admission receipt → conversation turn or task attempt → explicit children/results |
| Anchor mapping | Derived mapping to a later content revision, confidence/category, audit |
| Comment-enabled share | Revocable exact-revision capability and guest feedback audience |

Store feedback metadata/relationships in Cognis SQL. Store immutable comment/manifest payload bytes in a dedicated artifact-store namespace, referenced by key/hash, without exposing them as unprotected public artifacts. These are first-class feedback documents, not a duplicate conversation transcript. Session message and execution evidence remain owned by Intaris. Do not store copied diff bodies or lifecycle histories in new SQL feedback tables.

A Work anchor must survive cache reconstruction: use canonical source session/event/item identity, root/path, old/new range and diff digest—not a disposable projection row ID or the current aggregated file alone. Resolve through the existing authorized Work/event boundary. Small anchor quotes are locator data, not an archive of tool output. If evidence expires, preserve the feedback and a clear unavailable-source tombstone; never fabricate a full diff.

### Dispatch transaction and recovery

1. Authorize the source, selected entry versions, audience, destination, disclosure, and expected task attempt/content head as applicable.
2. Stage and hash the immutable manifest outside the SQL transaction.
3. In a short transaction, revalidate optimistic versions and create the submission plus one durable dispatch intent. Clean abandoned staged payloads using the existing storage lifecycle.
4. Dispatch through canonical conversation admission or task revision. Do not hold SQL transactions across Intaris writes or other external I/O.
5. Persist/reconcile the destination receipt and expose accepted work identity.

The client persists an opaque creation key in its account-scoped review draft before the first send request. Enforce uniqueness per authenticated sender and reject reuse with a different manifest or destination. The client can recover the receipt by this key if the creation response is lost before it learns the submission ID.

Retries reuse the same submission identity, manifest, and destination idempotency key. After an ambiguous response, reconcile before resending. Creation of a new follow-up must itself be idempotent so retry cannot create a second conversation. Task dispatch checks the confirmed safety-relevant workflow/pause version, not only the attempt; invalidated consequences reject admission and return the user to review.

Conversation admission already has durable idempotency and transaction-participation seams. Task revision currently has an expected-attempt guard but no submission-key receipt; extend its canonical owner so the task attempt transition and feedback receipt are recorded together. Do not wrap the current revision HTTP call in an unsafe retry loop. Use durable claims/fences for multi-controller dispatch and recover pending intents after restart.

Only the dispatcher owns retries of delivery. Turn/task owners retain cancellation, scheduling, queue capacity, workflow gates, and execution recovery. Cancelling an accepted execution is an explicit existing work action; hiding a comment or closing a sidebar does not cancel it. Removing unsent feedback from a review has no effect on an already accepted packet.

### Agent contract

The queued message contains a concise human-readable request plus an authorized structured feedback reference:

- Submission identity and immutable manifest hash.
- Source IDs, lineage and exact revisions/hashes, and permitted latest-head reference.
- Selected comment/reply versions and author/trust labels.
- Quotes, block/element locators, PDF page geometry, visual markup/snapshots, or diff side/ranges and canonical evidence references.
- Producing conversation/task/step/attempt where authorized.
- Requested destination and update mode: revise same document, answer only, or create a separate result.
- A link back to the feedback review.

Do not pass expiring signed URLs as the durable context contract. Resolve authorized exact references at execution time; recheck access if queued work outlives a grant.

Provide agent operations to read an authorized submission and record per-entry dispositions/result links. Submission-scoped access returns only selected immutable entry versions and the version-pinned parent context explicitly shown during review. It does not grant access to a live thread, excluded entries, later replies, or unreviewed guest feedback. A thread ID cannot expand the packet. Broader feedback access requires its own independent authorization and is not automatically handed to a feedback worker.

For admitted comment turns, the runtime binds direct comment-tool schemas after
assigning the execution's turn identity, before the first model request. This
turn-owned availability follows the immutable packet, not the current unresolved
thread count; answering a comment does not remove a tool mid-turn. The loop
intercepts these tools as controller operations and validates their canonical
arguments before invoking the feedback service. The service retains the exact
execution/packet authorization and existing reply transaction/idempotency
boundary. No executor dispatch, new persistence, external side effect, lock,
or retry owner is introduced; turn cancellation remains scheduler-owned.

Reuse artifact/deliverable authoring for revisions, adding explicit feedback causality metadata rather than inventing a second document-write path. Propagate submission identity to explicitly dispatched child work. The server validates links and records actual producing execution; agent prose alone cannot mark work successful or attach unrelated results.

### API shape

Expose subject feedback listing, thread/entry creation and edits, explicit resolution, private composer/review drafts, submission preflight/creation/detail, work/result links, and share capability administration. Subject listing must apply per-revision/per-entry access before returning excerpts or counts. Public endpoints use only their bounded share capabilities.

Apply those checks to submission detail, manifests, history, counts, destination metadata, and result links too. A full mixed-source manifest requires access to every included source/entry and permission to see its destination metadata. Otherwise deny full detail and offer an authorized subject-local history projection; never disclose hidden sources, entries, aggregate counts, or private destination identities through a shared submission. Agent execution gets only the explicitly authorized packet. Public-safe outcome projections are separate from internal receipts and work links.

Use optimistic versions for edits and expected entry versions for submission. Push notifications invalidate authorized subject views; REST remains the durable recovery path after reconnect. Keep Python/TypeScript contracts synchronized, bound pagination/geometry/payload sizes, and use both Alembic and idempotent bootstrap migrations.

## 8. Fit with current Cognis

Inspected baseline: `faf2a46a1a0043bcb406d6f8f1692b19bb744a44`. This design extends current owners rather than treating older planning specs as shipped behavior.

- Deliverables already carry provenance and linear revision lineage: `cognis/store/models.py:2548–2644`.
- Artifact revision ownership and head-safe writes exist: `cognis/core/artifact_revisions.py:1–17,129–172`.
- Revision history UI already exists: `ui/src/lib/components/DeliverableRevisionBar.svelte:6–60`.
- Artifact format selection is centralized: `ui/src/lib/components/ArtifactPreviewContent.svelte:42–60`.
- HTML previews are already script-enabled but opaque-origin sandboxed: `ui/src/lib/attachments/html-preview.ts:120–129`.
- PDF rendering already uses PDF.js canvas; selectable text/annotation layers are additional work: `ui/src/lib/components/PdfAttachmentViewer.svelte:98–121,166–169`.
- Diff rendering exposes old/new line numbers but no annotation interaction: `ui/src/lib/components/FileDiffViewer.svelte:124–134`.
- Work diff references already include source item, path/root, and workstream context: `cognis/api/chat_v2/schemas.py:127–150`.
- Work projections are reconstructable and source events remain authoritative: `docs/specs/42-work-live-projection.md:3–24`.
- Task comments already invoke revision requests, but feedback dispatch should use the canonical revision owner: `cognis/api/routes/tasks.py:2106–2155`; `cognis/core/task_queue.py:748–871`.
- Conversation submission already serializes/queues turns and exposes durable admission hooks: `cognis/core/turn_scheduler.py:3056–3114`.
- Existing owner-minted public links cover both artifact and deliverable documents: `cognis/core/document_sharing.py:45–98`. Comment capability needs new revocable state; existing read access does not permit reshare: `cognis/core/artifact_sharing.py:1`.

## 9. Delivery plan and acceptance

### Stage A — Shared feedback and safe dispatch

Implement subject/anchor/thread/entry/submission contracts, sidebar, private drafts, review selection, lineage history, permissions, conversation/follow-up routing, and idempotent task revision integration. Ship whole-document, rich block/text, plaintext, and multi-file diff comments with status/result links.
Include the context-bound composer review tray and one-turn send integration.

### Stage B — Visual annotation

Add image and PDF region/freehand markup, PDF text selection, HTML element/text inspection and visual capture, responsive coordinate transforms, and conservative anchor mapping. All reuse Stage A submission/history, not separate comment systems.

### Stage C — Collaboration and dashboard continuity

Add explicit collaborator commenting grants, comment-enabled revocable public sharing, guest moderation, preferred dashboard follow-up, child work links, and revision refresh behavior. Signed-in commenting permissions and privacy contracts are designed in Stage A even where the UI ships later.

These are delivery slices, not optional omissions from the requested feature. Do not declare the feature complete before all requested formats, sharing modes, and task/conversation routes work.

### Required verification

- All four user scenarios end-to-end, including follow-up dashboard refresh and cross-file review.
- Selected/excluded comments, frozen edit versions, concurrent reviewers, new replies during confirmation, repeat submissions, and agent read attempts to expand a submission through a thread ID.
- Busy conversation, full queue, task-attempt race, same-attempt progression/new approval during review, active/paused task revision, cancelled/deleted producers, and source access revoked while queued.
- Double-click, timeout, restart, multiple replicas, response loss before the submission ID, and ambiguous admission: no duplicate conversation, turn, or task revision. Reload restores rejected/uncertain review and receipt recovery.
- Public/guest/collaborator permissions; mixed public/private-source manifest access; private history and destination-link leakage; revocation, expired links, malicious Markdown and bridge messages.
- HTML scripts cannot call annotation APIs or dispatch through the bridge; parent credentials and same-origin access stay unavailable.
- PDF rotation, zoom, image orientation, responsive HTML, ambiguous reanchors, old revision review, binary/truncated diffs, and unavailable source evidence.
- Keyboard/touch review, screen-reader navigation, narrow layouts, reconnect and account switching with unsaved drafts.
- In-chat versus standalone routing; composer prepared/included visibility with
  sidebar open and closed; feedback-only sends; attachments/chat mode preserved;
  multi-tab edits, conversation switching, and exact sent-snapshot consumption.
- Existing revision authorization/head-conflict tests and task/conversation lifecycle regressions; SQLite and PostgreSQL migrations and bootstrap-twice tests.

### Scope boundaries

Not included: collaborative document editing, arbitrary design-file editing, automatic code application, external GitHub review publication, silent guest-to-agent execution, or a new workflow engine.

Recommended defaults are deliberately conservative: public comments off; guest posts require review; no implicit write grants; no automatic thread resolution; source pinned while annotating; one explicit destination per submission. These are product choices for approval, not claims of existing behavior.

## 10. Design validation

The [user guide](../guide/work-feedback.md) describes the supported workflow.
Validation must cover submission-bound agent reads, mixed-source disclosure
protection, safety-state-bound task confirmation, durable rejected/uncertain
recovery and client creation-key idempotency. Component checks do not substitute
for integrated endpoint and browser acceptance.
