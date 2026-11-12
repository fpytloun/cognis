# Work Feedback runtime ownership

The generated [API handoff](work-feedback-api.md) defines wire contracts.
The [user guide](../guide/work-feedback.md) describes review and dispatch.

## State and lifetime

Feedback owns discussion metadata, immutable entry versions and submission
manifests, staging, capability grants, and causal references. Private feedback
payloads and observed capture bytes use the artifact store; SQL stores their
references. Intaris continues to own execution events and conversation content.
Feedback history is a projection, not a second execution log.

Transcript refs opt into assembled assistant evidence only with
`representation: "chat-item-as-of-event-v1"`. Missing/null representation retains
the exact legacy single-event source; serialization omits the null field so old
source keys, digests and tokens remain unchanged. The same discriminator must be
preserved in captures and manifests. It currently targets assistant `content`.

The selected contributor event is an immutable upper bound, not a latest-item
alias. `chat_v2.item_snapshot` reads through an already authority-bound cached
event reader and reuses the canonical normalizer/projector. It scans backward
only to a proven full/base event for that canonical item, never infers another
session's ancestry, and never concatenates independently. The request owns its
replay state and cancellation; DB sessions end before event I/O. Limits are 400
events/page, 5,000 examined events, 16 pages (including the exact read), and 8 MiB
of retained JSON/UTF-8 replay data, accounted before normalization/expansion.
Gaps, ambiguous identities, stalled pages, missing bases and limits fail closed.
No durable transcript cache, schema migration or new execution authority exists.

Discussion resolution, submission delivery, admitted execution state, and result
revisions are independent. Receipts observe the exact admitted turn or attempt,
not the latest activity of a conversation or task. Missing proof is unavailable.

Authored chat comments/replies and edits with explicit chat context prepare the
author's entry in the same metadata transaction as the immutable version and
payload adoption. `staging.stage_entry` owns first-item preparation, item CAS and
scope increments for both automatic and explicit staging. Entry fences precede
item/scope writes; there is no dispatch, access grant or external I/O here.
Creation-key replay does not stage again, and a changed head returns only its
explicit chat scope to prepared. Other scopes and submitted manifests remain
frozen. Contextless/standalone edits stay non-routing; the public edit DTO has no
context field. Transaction cancellation rolls back preparation with authoring.

Work projections preserve a helper-bound exact event/file locator only when
both the original evidence and the final displayed diff are eligible. Display
path/root normalization must not rewrite canonical source identity. Sensitive,
redacted, binary, omitted and truncated previews lose feedback authority;
projection budgets cannot upgrade partial evidence. Current-file aggregates
remain unbound. These are read-only projection rules: no new persistence,
event-sequence authority, source grant or runtime side effect is introduced.

## Admission and cancellation

`Submissions` prepares immutable payloads before admission. `DirectTurnStore` and
`TaskQueue` commit submission linkage with canonical admission metadata. Task
revision admission compares the reviewed safety generation and consequences.
The turn scheduler and task execution owners retain serialization, leases,
cancellation and retry ownership.

Child authority is selected explicitly with submission IDs and manifest hashes.
The child admission participant binds that authority to the actual target
session, turn or task attempt before creation/admission commits. Generic ancestry
does not grant packet access. Session reservations and grants commit before
guardrail provisioning; failed auto-reserved children become failed metadata,
without running. External provisioning is never inside the SQL transaction.

## Writes, replay and recovery

Canonical artifact and deliverable writers invoke a transaction participant.
It validates the current execution, disclosure and selected-source update mode,
then links the actual committed revision in the same transaction. Blob work and
packet loading happen outside that transaction. An inherited deliverable slot is
ownership context, not evidence of the producing execution.

Executor publication and recovered publication use the same materialization
boundary. Canonical task fences remain active even when no feedback packet is
bound. Replay must already have matching causal proof; it cannot adopt an old
document or silently broaden a child grant. Child-link identity includes the
concrete turn/attempt, so later work cannot overwrite earlier receipts.

## Disclosure and compatibility

Packet reads recheck current source permissions. Public capabilities remain
share-scoped, guest sessions remain bounded, and moderation reasons are visible
only to moderators. Capture readers must have source access and either own the
capture or be able to see a referencing thread.

Existing calls omit the optional admission/write participants. Native child
calls without `feedback_submissions` do not inherit authority. API contracts are
additive; there are no alternate public route or authentication aliases.
