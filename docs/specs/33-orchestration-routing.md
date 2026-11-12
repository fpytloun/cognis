# Orchestration routing by conversation surface

This document defines how Cognis should expose and enforce orchestration
choices for each conversation surface. The goal is to keep prompts
context-specific: invalid options should disappear from the model's
instructions and tool schemas where possible, with runtime validation as the
safety net.

## Routing table

| Conversation surface | Prompt should show | `delegate(wait=true)` | `delegate(wait=false)` | Managed conversation `wait=true` | Managed conversation `wait=false` | `create_task` |
|---|---|---:|---:|---:|---:|---:|
| Main / channel chat | Async-first routing options | Yes, when current answer needs result | Yes, only lightweight non-interactive background work | Yes, if caller must inspect result before continuing | Yes, for visible async interactive work; prefer `agent_conversation_send` when a relevant managed conversation already exists | Yes, preferred for heavier workflow-shaped work |
| Direct / topic chat | Inline + joined specialist work | Yes, common | No / not prompt-visible | Yes, by default, for planning/review/implementation needing interaction | Yes, when independent parent work can safely proceed | Yes, if durable/heavy |
| Managed agent conversation | Only valid synchronous/inline options | Yes, common | Forbidden, not prompt-visible | Only below the inherited managed-depth limit | Forbidden / not prompt-visible | No; descendants cannot bypass depth through durable tasks |
| Workflow task step | Workflow-step-safe options | Yes / forced sync | Forbidden / coerced or rejected | No, unless explicitly available and needed | No | Usually no; already inside task |
| Sub-session / delegated child | Finish assigned work | Avoid further fan-out unless necessary | No / not prompt-visible | No | No | No, unless explicitly requested and tool policy allows |

## Runtime invariants

### Delegation batching after sequential tools

The agent loop owns response-local delegation batching. A preceding sequential
tool settles before a following consecutive delegate run enters the existing
bounded fanout. Budget rejections, duplicate calls, and non-delegate calls end
that run. Results remain source-ordered and child creation retains its existing
side-effect, provisioning, and join contracts. The turn scheduler and existing
fanout own cancellation; there are no new locks, transactions, durable formats,
or automatic retries.

### Delegated configuration and managed independence

The controller snapshots only Cognis-owned capability configuration in delegated
session metadata, before external provisioning. The snapshot includes effective
parent memory profile options, not the parent's identity, model, permissions,
tools, executor grants, or Intaris-derived content. Child runtime construction
and policy refresh use that snapshot; specialist memory opt-outs remain in force.
Pre-snapshot delegates reconstruct configuration from their parent on admission.

Intaris durably owns session `analytics_enabled` and `guardrails_enabled` flags.
Creation explicitly sends and acknowledges both; policy refresh PATCHes them and
checks canonical readback. Rotation reads the source flags before taking locks.
These operations retain existing admission serialization, cancellation,
reservation identity and idempotent provisioning ownership; there is no new lock
or shared mutable provider preference cache.

WorkflowStep v1 declarations require an existing session. Flags are provisioned
or reconciled before declaration installation; the frozen declaration schema and
digest do not contain them. Declaration installation must preserve session flags.

Managed primary-agent conversations are independent configuration boundaries,
including same-agent targets. Only existing allow/deny policy clauses cross that
boundary, not the controller's evaluation selection. Access restrictions and
managed-depth enforcement are unchanged.

### Task outcomes

The workflow engine owns terminal routing; channel delivery owns external
outbox transport; Intaris owns app-chat content; the existing notification inbox
owns direct app notifications. Recipient identity remains separate from channel
identity. Current routing is task-owner-only, including shared/project tasks.

Default outcomes use a neutral `handle` follow-up with task/result references
and optional persisted finish instructions. They do not use a separate
notify-versus-integrate classifier. Direct delivery never admits an agent turn;
missing routes fall back to the recipient's app inbox.

Delivery idempotency identifies the terminal occurrence using task ID, attempt,
status, and normalized completion timestamp. Retrying that occurrence reuses its
follow-up/outbox/inbox identity; a later occurrence does not. Task execution
fences and turn-scheduler admission retain cancellation and serialization
ownership. Database transactions end before Intaris/channel I/O. No new lock,
automatic repair loop, or transport retry owner is introduced. Existing
serialized `notify`/`integrate` follow-ups remain readable.

### Signal destination policy

Signal sends use local destination admission through the channel manager.
The key contains the owner, account and transport chat ID, not the reply thread.
This is an operational restriction, not a claim about Signal's platform block scope.
Other destinations remain independent. Group responses can contain partial success.

The database serializes admission before transport. No database lock spans RPC.
A successful receipt or complete structured provider failure settles admission.
Provider failure can still leave the delivery outcome uncertain.
Timeout, cancellation and missing receipts leave an unresolved transport fence.
Lease age and executor restart never prove external completion or non-delivery.
No current API can clear an unresolved transport fence.
A future evidence-backed operator protocol is necessary for that case.

Rate-limit observations extend a durable destination cooldown from the observation time.
Without a usable retry delay, the destination requires manual action.
`reconcile_signal_destination_policy` clears only this local policy after explicit reconciliation.
It preserves diagnostics and audits the action. It never resends or clears transport fences.
`search_channel_targets` and `get_channel_delivery` expose sanitized diagnostics.
Challenge tokens and CAPTCHA results are not diagnostic fields.
Historical failures without retained structured data cannot establish a cooldown retroactively.

Managed controllers can supply `reconciliation_evidence` to release an uncertain failed
route before expiry. The existing expiry-only call remains supported.
Route release preserves uncertain outcomes and does not clear destination policy or replay input.

### Managed-channel final delivery

The scheduler commits the final outbox row with the managed turn result and
binding version. The completion observer only repeats this idempotent enqueue
and wakes the delivery worker. It does not await transport I/O.

The application owns the tracked delivery worker from startup through shutdown.
The worker scans persisted work on startup, on wakeup, and every 30 seconds.
The periodic scan covers cancellation after commit but before wakeup.
An observer rollback cannot remove the scheduler's previously committed row.

Outbox claims and managed binding leases fence each send. Shutdown cancellation
leaves unacknowledged non-idempotent sends for uncertain-outcome recovery, not
automatic resend. Persisted outbox status owns uncertainty across projections
and route recovery. Error text does not determine delivery certainty.

1. Managed agent conversation + `delegate(wait=false)` is rejected.
2. Workflow task step + `delegate(wait=false)` is forced to synchronous joined
   execution by the step orchestration mode.
3. Prompts and tool schemas should be context-specific. Forbidden options
   should disappear instead of being described as available-but-prohibited.
   Agent delegation policy further restricts every target catalog. The master
   switch, shared target allowlist, primary/system switches, target-side
   controller allowlist, and inherited managed-depth budget are authoritative
   at runtime as well as during discovery.
4. `delegate(wait=false)` is a narrow live/main or channel-chat primitive for
    lightweight non-interactive background work. It is not a general
    orchestration primitive.
    Its direct-turn ownership guard lasts for the child's execution. It permits
    continuation after successful completion by the exact spawning owner, but
    cancels on lease loss, takeover, failure, or cancellation. Child checkpoints
    and tool dispatches check ownership without modifying the parent's recovery
    journal. Checks release database sessions before external I/O; explicit
    cancellation remains owned by the parent's tracked child task. This does
    not add durable child replay or restart recovery. Workflow-task fences and
    joined delegates retain their existing ownership.
5. Managed conversation `wait=false` is available from main/channel and
   direct/topic chat surfaces. Main/channel chats default omitted
   managed-conversation `wait` to `false` to remain responsive; direct/topic
   chats default it to joined execution, while allowing explicit `wait=false`
   when independent parent work can safely proceed.
6. Prompt/context guidance must distinguish web main chat from normal web
   topic chats. Topic chats must retain joined managed work as the default and
   use background managed conversations only for genuinely independent work.
7. Main/channel prompt and tool guidance must prefer reusing an existing
   relevant managed conversation with `agent_conversation_send` before
   creating a new one. `agent_conversation_create` is for new visible managed
   work loops or intentional separation, not same-problem continuation.
8. Moving the same issue from plan/debug to implementation should continue the
   existing managed conversation and set `chat_mode="build"` for that turn.
9. Lightweight delegate lineage is controller-owned RPC, not a managed
   conversation. `retry_subsession` reruns only a failed, interrupted, or
   cancelled source task; `follow_up_subsession` adds a new instruction;
   `fork_subsession` branches with a new instruction. All three preserve the
   source specialist/profile, create a new direct child, and copy the
   source child context because terminal delegate history and result fields are
   immutable. The source stays inspectable, lineage is explicit in
   `delegation_metadata`, and derived depth is limited to eight.
10. A lineage source must be a terminal direct child of the calling controller
     session. An active retry for the same source blocks another retry. Delegate
     children remain `OrchestrationMode.NONE`; async completion only notifies the
     parent and never continues the child.
    `delegate()` targets active system secondary agents only. They run bounded
    synchronous work and receive no delegation, managed-conversation, workflow,
    schedule, or task-assignment surface.
11. Start an initial independent review fresh. Keep fixes in the implementing
    agent. Re-review may continue the compatible reviewer only when its retained
    investigative context is materially useful and its role, criteria, findings,
    and dispositions remain preserved; otherwise start a fresh reviewer. Fork only
    an independent branch requiring inherited context.
12. Live capability guidance owns one proportional agent-briefing block for
    delegates and managed conversations on all orchestration-enabled surfaces.
    Brief a capable colleague with the outcome, relevant facts and references,
    and real boundaries, using readable sentences and Markdown rather than a
    mandatory form. Investigations receive questions and known facts, not steps
    based on unverified hypotheses; implementers receive ownership and expected
    behavior with room to choose the approach. Tool descriptions own field and
    lifecycle semantics; skills own delivery-specific procedures.
    Continue only the
    same bounded problem when role, responsibilities, tools, authority, and
    output remain compatible and retained context is materially useful; send
    only the context delta. Create a fresh isolated child for a changed
    responsibility or capability scope or an independent workstream, with
    relevant references, never the parent transcript. Fork
    only an independent branch requiring inherited context, not an ordinary
    handoff or review. Reviewer input includes objective, criteria, exact
    diff/artifact, verification, and invariants, not implementation reasoning.
    Re-review preserves reviewer role, criteria, findings, and dispositions and
    reuses context only when materially useful. Child returns contain the result,
    supporting evidence, and material uncertainty or blockers, proportional to the
    task; detailed logs remain inspectable outside the parent's active context.
13. In coordinate mode, the architect owns decomposition through integration,
    acceptance, and final delivery. Workers have bounded responsibilities; do
    not downgrade the architect into a worker by default. Preserve proportional
    review and the bounded one-correction rule.
14. A managed-conversation join is only a bounded parent wait (3600 seconds by
    default, with an explicit per-call override). Its timeout arms
    a completion handoff conditionally while the child is still queued/running;
    it never cancels the child or rewrites a terminal link. If completion wins
    the arming race, the join returns the persisted terminal state instead of
    reporting idle. Starting another turn clears the prior `completed_at`
    marker. Runtime reconciliation preserves live work and repairs impossible
    terminal combinations such as a completed conversation with a running turn.
    Timeout and cooperative reattachment return control to the same controller
    turn, including repeated timeouts. Fallback delivery ownership does not
    imply permission to end that turn. Reattached results omit completion
    content because the durable notification remains its sole delivery path.
    The agent loop owns this per-call continuation decision and its existing
    turn-local reattachment keys. Handoff transactions, scheduler cancellation,
    recovery, and notification deduplication remain unchanged. An unrelated
    asynchronous operation in the same batch retains its termination behavior.
 15. Durable follow-up intents use a deterministic observable turn ID derived
    from `(conversation_id, follow_up_id)`. Pending and pre-admission processing
    intents are claimed with a durable owner and expiry; another replica may
    reclaim only an expired lease. Admission fences the intent and dedupe row
    before workflow execution, so startup recovery does not replay admitted work
    or duplicate its lifecycle/tool events. Healthy owners renew admitted leases;
    an admitted lease abandoned by a crashed owner is failed rather than
    re-executed. Intent and dedupe finalization share one transaction and failed
    finalization is retried by the owner. Each claim, including the initial
    automatic continuation, consumes one of the configured total attempts.
    Explicit transient retries resume from persisted conversation state;
    external side effects still need tool/backend idempotency.
 16. `agent_conversation_set_profile` resolves a controller-owned managed link,
    accepts only an enabled agent-switchable target profile, and changes the
     target conversation and active session only while the per-conversation
     admission lock confirms there is no active or queued turn. The next managed
     send uses the persisted profile; the link's creation-time profile is not the
     current-profile authority.
 17. Managed depth counts primary-agent edges. The first managed target is depth
     one. New roots default to a limit of one; a per-agent limit of two supports
     an XO-to-coordinator-to-worker chain. The root limit is persisted on every
     link and inherited by descendants. At the limit, create and fork disappear,
     existing-link controls remain available, and eligible system `delegate()`
     targets remain available.
 18. The same primary target authorization applies to managed conversations and
     cross-agent task/workflow ownership. A managed descendant cannot create a
     task for another agent to reset its chain. Allowlists only restrict existing
     ownership, grant, lifecycle, and visibility checks; they never grant access.

## Generic question mediation

Clarification requests use one durable notification ID and the existing pause
waiter. Direct-chat questions remain user-facing `step_question` notifications.
Delegated and managed requests first become internal `agent_question` records
assigned to the responsible conversation. Agent-only questions are excluded from
user notification, attention, and pending-input surfaces.

`answer_question` validates structured answers against the stored question set.
`forward_question` promotes the same record to a user-facing question using a
revision-fenced update; it preserves the original execution, questions, and
deadline. Neither tool can resolve credentials, auth challenges, or approvals.
Respondents answer from context, task instructions, or user preferences, and
ask the user rather than guess when uncertain. The delivery destination of a
scheduled/API task is not itself decision authority.

Question follow-ups carry required controller-authored origin references:
conversation, session, agent, tool call, and tool name; task, step, and source
references accompany task questions. Task-created questions use the originating
conversation, or the pinned task delivery destination when there is no creator
conversation. Delegates inside a workflow use the task's responsible conversation
when their shared task conversation cannot admit a controller turn.

### State and delivery ownership

- Notification rows own question lifetime, routing, expiry, and resolution.
  Routing metadata uses existing JSON payloads; there is no schema migration.
- The scheduler owns durable follow-up admission, deduplication, safe-boundary
  absorption, and recovery. Persist the question before delivering its follow-up.
  A durable delivery-readiness flag prevents recovery and response tools from
  racing ahead of the origin's pause commit. Recovery activates an unready task
  question only after its matching internal pause is committed.
  Its recovery scan retries missed delivery and forwards unanswered questions to
  the user after a failed or completed mediation turn.
- Joined delegates run as tracked child tasks. A boundary event yields the join,
  not the child; an observer delivers completion through the existing follow-up
  path. Parent cancellation cancels the tracked child. Managed joins retain their
  existing boundary-input protocol. No second parent turn runs concurrently.
- Answer validation and routing revision fences reject stale or unauthorized
  mutations, including stale task attempts. Answered task questions preserve
  their structured reply and recover task admission if the local waiter was lost.
  Forwarding locks the task before its notification CAS, matching task lifecycle
  lock order. Database work ends before events and waits. Local waiters are only
  synchronization fast paths; the durable notification remains authoritative.
- Existing direct question IDs, user response APIs, interaction-mode limits,
  and question replay contracts remain supported.

## Fine-tuning point

The centralized policy lives in `cognis/core/orchestration_policy.py`. Adjust
surface classification and exposed affordances there before adding new ad-hoc
context checks elsewhere.
