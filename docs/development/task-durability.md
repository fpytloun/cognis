# Task durability boundaries

The scheduler owns occurrence selection. Its timer observation is advisory;
the database claim checks the expected occurrence and database due time under
the schedule lease and row lock. Manual fires remain separate occurrences.

`TaskExecutionFence` owns attempt-scoped dispatch metadata. Before transport
sends, it persists call, turn, session, executor and accepting-process identities
under the task lease. Arguments, prompts, result content and copied Intaris
snapshots are not stored in the Cognis task row. Result settlement precedes
descriptor removal. Ownership-only takeover claims do not require workflow
capacity. Infrastructure suspension requeues the same attempt and releases its
capacity; it does not cancel accepted executor work. Explicit user cancellation
retains physical cancellation.

Proven `not_sent` delivery is committed before infrastructure suspension, even
when transport failed before binding an executor instance. Recovery trusts only
that explicit state as non-execution; missing identities or receipts alone
remain ambiguous for effectful calls. A subsequent dispatch replaces the state
under the existing task fence before sending. No tool arguments or result
content are added to the durable descriptor.

`TaskQueue` receives its executor provider explicitly at application construction.
Its cancellation reconciler runs independently of workflow capacity, pages past
unresolved cancellations, and retains each descriptor until a terminal receipt
can be acknowledged, or proven `not_sent` evidence permits retirement without
an executor RPC. No database transaction spans its executor RPCs.

`task_tool_recovery` reads arguments and canonical pairing evidence from Intaris.
It shares the live agent loop's receipt interpretation and normal recovered
result finalization, including untrusted-output processing and controller-owned
audit metadata. It never invokes tools. Unavailable evidence is retryable;
unknown mutation effects are fenced. Shutdown leaves interrupted step evidence
intact, distinct from user cancellation.
The strict canonical event writer retires every settled task-owned call,
including controller calls such as `step_complete`, without retiring unresolved
siblings. Executor rejection after successful admission also commits a terminal
receipt through the identity-bound execution-result writer.

The executor owns its local SQLite receipt journal. Each admission transaction
commits a payload fingerprint and scope before effects. Each terminal envelope
is committed before transport. The original process identity remains in the
receipt after restart. An authenticated controller may trust a durable terminal
receipt from a replacement process only when its original accepting identity
matches the dispatch descriptor. Legacy executors must still match the live
process identity; this compatibility path is required during rolling upgrades.

Journal I/O is offloaded from the event loop. Transactions end before transport
and tool execution. Acceptance is immutable and duplicate payload conflicts
are rejected. Unacknowledged evidence is never evicted; admission applies
backpressure. Settlement hints are idempotent. Payload reclamation has a
fourteen-day grace and keeps non-replayable identity tombstones. This is not an
Intaris replacement or a generic exactly-once guarantee: an executor can die
between an external mutation and recording its result.

Fault tests distinguish controller shutdown, hard ownership loss, explicit
task cancellation, socket reconnect and executor process replacement. Assert
no early occurrence consumption, no mutation replay on uncertain evidence,
capacity-free infrastructure waiting, unchanged task/attempt identities,
settlement idempotency, scope binding and journal pressure behavior.

## Workflow-owned continuations

Background completions retain their originating session and turn. The existing
durable followup intent records the resolved task, attempt and StepRun identity.
Admission and restart recovery never submit a direct turn into a task-step
conversation. The workflow boundary consumes matching intents, appends the
canonical notice idempotently and only then acknowledges delivery. Rotation and
controller takeover preserve that ownership; a human pause is not auto-resumed.
Unprovable origins remain pending with a diagnostic instead of becoming direct
writers. Task-owned event append and delegation reservation reject conflicting
turn identities before side effects.

New delegation reservations retain task, attempt and StepRun provenance in the
existing recovery and child metadata. Missing attachments can be restored only
when that provenance, current authority, exact argument binding and canonical
call turn agree. Terminal children undergo receipt settlement without inference
or revival before the parent result is settled. Legacy cross-turn histories are
not reconstructed: they pause with the missing proof and an instruction to
cancel and start a fresh run after reconciling effects.

Infrastructure pauses update Task, WorkflowState and StepRun state under the
existing task fence. Context-preserving retries retain unsettled recovery
metadata from the claimed row. A fresh attempt cannot discard outstanding
calls. Explicit task cancellation remains available at a recovery pause.
If a failed task already closed its root session, retry first reconciles receipts
in that original session and turn without root or child inference. A proven
unfinished delegation keeps its descriptor and resumes its reserved child in a
separate recovery phase, still without root inference. The existing session owner
forks the settled history only after recovery succeeds.
The continuation retains its previous-session lineage so task-owned followups
remain deliverable after retry. Source metadata reads end before provisioning
the continuation or copying Intaris history.
Delegated runtime admission receives the task fence and cancellation owner, so
temporary executor unavailability takes the same capacity-free deferral path as
the root workflow, rather than the interactive readiness timeout.
