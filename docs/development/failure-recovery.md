# Failure recovery ownership

Inference recovery owns the physical request allowance and marks normalized
terminal failures with `recovery_exhausted`. The agent loop consumes that marker,
not the presence of a recovery service: admission may fail before the allowance
is used. No committed provider output is transparently replayed.

The turn scheduler retains ownership of admission, cancellation, durable retry
leases and execution fences. Exhaustion ends automatic recovery but preserves
manual recovery eligibility. Existing Intaris event append idempotency remains
unchanged. Retry-source reads follow canonical lineage and reject requests
superseded by a newer user message.

The workflow engine owns delayed task-step resumption. Recovery counters live
in the existing workflow state, separately from quality attempts; deferral uses
the current task fence and a running-to-ready compare-and-set in a short
transaction. The queue reclaims the task at `scheduled_for`. No database
transaction spans provider I/O or waits. Resumption reuses the step session and
saved tool results. Cancellation or a lost task fence prevents deferral.

Manual failed-task recovery retains the logical task attempt and explicitly
marks the failed step for resumption. Session discovery therefore stays scoped
to its original step-run lineage. An inactive source session is safely forked;
missing or unreadable history fails closed instead of starting the step with
an empty context. The separate re-run operation still creates a fresh task.

The UI derives actionable chat failures from the complete loaded canonical
timeline and current runtime identity, never from the viewport slice. Retry
lineage supersedes the source failure. Local suppression bridges accepted
admission until canonical recovery arrives. Historical errors remain evidence,
not controls. No schema migration, new persistent content store or cancellation
owner is introduced.
