# Approve one specific action

Approval messages show the approval ID, tool, pre-redacted action arguments,
decision reason and expiry. Web prompts expose action scope and provide commands
for that particular approval.

Question requests and tool approvals use collapsible panels above web chat.
Select the panel heading to collapse or expand it without losing your draft.
Expanded details scroll within a bounded height, leaving the conversation visible.

Use `/approve --id <approval-id> [optional note]` or
`/deny --id <approval-id> [optional note]`.
A full UUID immediately after `/approve` or `/deny` is also an explicit target.
Other text remains a note for the sole pending approval.

Bare `/approve [note]` and `/deny [note]` work only when exactly one actionable
approval exists in that conversation, counting durable and local registrations
once per call ID. Multiple approvals produce a list and no
resolution. Replying to an older channel message does not select its approval;
use its ID.

Selection is bound to the authenticated owner and current conversation, including
controller-observed managed-conversation origin. Unknown, foreign or expired
targets are not resolved. The durable resolution owner retains per-call
submission, compare-and-set claims, idempotency and timeout behavior.
Bare-command ambiguity is rechecked in the atomic claim and against local
registrations immediately before submission. An already submitted action is not
stranded by another approval arriving afterward. Expiry blocks new claims and
stale reclaims, not completion of admitted submissions or identical terminal retries.
With the notification service present, local-only approvals cannot bypass durable
registration. Retry with the explicit ID after registration completes.

Legacy service-absent commands and REST resolutions share one local claimant.
Its state lives with the waiter until resolution or timeout cleanup; claiming is
synchronous and submission happens afterward. A definitive rejection permits
release or records a permanent denial. Transport failure or cancellation keeps
the claim closed because the upstream outcome may be uncertain. Expired or
removed waiters are never resumed, and stale claim tokens cannot resolve a
replacement. Existing gate and question resolution and durable synchronization
keep their original behavior.

The channel preview is bounded and uses the existing redacted projection.
If shortened, inspect the full action in Cognis before approving. This feature
does not grant broader scope or approve historical calls.
