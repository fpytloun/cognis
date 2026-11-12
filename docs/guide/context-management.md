# Context management

Cognis first reduces the model-facing prompt through **projection**. It uses
**compaction** when projection cannot make the prompt safe, or when you request
`/compact`. A large prompt does not by itself mean compaction is necessary.

## What the numbers mean

| Metric | Meaning |
|---|---|
| Effective prompt budget | Model input capacity after output reserves, input limits, and serialization safety margin. |
| Steady target | Soft goal for between-turn projection, not a compaction limit. |
| Within-turn burst | Working-evidence sizing allowance; not a switch that automatically selects critical mode. |
| Hard target | Projection policy ceiling. |
| Loop pressure | Additional safety boundary at 95% of the effective prompt budget. |
| Effective hard boundary | The lower of hard target and loop pressure. Both checks remain enforced. |

The runtime rejects a projected prompt above the hard target, or at/above loop
pressure. These boundaries are deliberately inside the provider's capacity.
The legacy 85% assembly recommendation does not independently trigger
pressure-driven compaction. Manual compaction, provider-reported overflow
recovery, and idle checkpoint compaction are separate entry points. Long-lived
chats can checkpoint after the configured idle interval and minimum event count,
even below these token boundaries; that lifecycle policy is unchanged.

Token usage includes tool-schema overhead. Local estimates are calibrated from
provider observations only when session, provider, model, and estimator identity
match. Raw and calibrated usage are not interchangeable. The displayed policy
must use the same resolved model budget as the usage snapshot, including when
the previous projection is reused.

### How targets are calculated

Let `A` be the effective prompt budget. Integer token counts are rounded down:

- Between-turn trigger: `0.75 × effective input limit`; reduction target:
  `0.35 × effective input limit`. These use the input limit before the
  serialization margin (204k and 95.2k for a 272k input limit).
- Within-turn steady: `min(0.88 × A, steady cap)`.
- Burst: at least steady, otherwise `min(0.95 × A, burst cap)`.
- Hard: capped at `A`, at least burst, and otherwise the lower of
  `0.98 × A` and `1.1 × burst cap`.

| Model context window | Steady cap | Burst cap |
|---|---:|---:|
| Up to 150k | 95k | 115k |
| Up to 300k | 180k | 245k |
| Up to 500k | 250k | 360k |
| Above 500k | 320k | 600k |

These are current implementation defaults, not provider pricing boundaries.

## Between turns

1. Assemble canonical history and required instructions.
2. At or below 75% of the effective input budget, do not re-project canonical history.
   Above 75%, shorten the oldest eligible tool groups toward 35%, keeping recent groups.
3. Preserve instructions and unrecoverable evidence even if steady cannot be met.
4. Before any model request, run the model-facing projection/budget gate.
5. Only unresolved hard pressure enters automatic compaction/recovery.

History comes from canonical per-message events in Intaris. Small
`context_projection` events record which tool results or arguments were shortened,
which complete tool groups were omitted, and which historical events were dropped.
They contain identifiers and rendering decisions, not copied message content.
Replay reapplies those decisions after a restart or Redis loss.

When compaction rotates to a new session, event-scoped decisions are rebased to
the copied events' new sequence numbers. Rules targeting events outside the
preserved tail are discarded; tool-call-scoped rules keep their stable call IDs.
Historical recap cards stay in their original session and are not copied into
the continuation as new recaps.

Controller notices append when their content changes; earlier notices are not
rewritten or removed. Executor assignment notices also retain their original
position across turns and cache loss. An unchanged pool adds no duplicate notice,
and removal appends a superseding notice.

Routing notices are recorded once per request and retained at their delivered
position, including after cold-cache recovery. Their text explicitly limits
them to the associated request; on later requests they are historical context,
not current operating instructions. Reassembling the same turn does not append
an identical notice again. Existing audit-only routing records are not replayed.

The `switch_executor` tool uses a stable schema whenever multiple executors are
assigned, even if some or all are temporarily offline. Current assignment and
availability are supplied in context and checked again at execution time; they
are not embedded as changing schema enums. Task-control and delegated-child
restrictions remain unchanged.

Per-call `target_executor` schemas likewise use assigned executors that advertise
and enable the tool, even during temporary outages or reconfiguration. Transient
availability changes therefore do not add/remove that parameter or rewrite its
enum. Unassigned, disabled, or unavailable execution targets are still rejected
by normal execution-time validation.

Cached projection retries retain the compact decisions associated with their
rendered view. If persistence fails, a cached retry must persist those decisions
before inference; successful decisions are deduplicated through canonical replay.
This does not trigger new projection below the normal pressure threshold.

Within a turn, crossing only the soft steady target first checks the accepted
rendering plus newly appended messages. If the complete source prefix is unchanged
and that candidate remains below the existing 95% loop-pressure gate and within
the hard and available budgets, Cognis reuses it rather than rewriting older
tool results. Current tool-schema overhead and token calibration are included.
Initial projection, oversized new results, pressure transitions, critical pressure,
and changed source prefixes retain the full projection path. The 75%/35%
between-turn policy and session-compaction thresholds do not change.
This reduces avoidable cache invalidation; it does not guarantee a provider cache hit.

Regular executor calls and controller calls persist the same bounded native replay
fields. Original argument JSON (including whitespace and Unicode) is retained
when sanitization does not change its values; redacted arguments are reserialized.
The raw fallback is independently capped by its encoded JSON byte size, so extra
whitespace cannot bypass the native-envelope limit.
Existing events without native fields continue to replay from their recorded
arguments, but cannot retroactively recover discarded provider formatting.

`Context projection decision` logs contain only metadata: session/turn identifiers,
phase, trigger reason, pressure mode, before/after token estimates, separate entry
and deciding thresholds, available budget and steady/burst/hard targets. Cached
reuse and non-numeric reasons have no new deciding threshold. Cross-turn logs
distinguish the effective-input target from the actual savings target. A `normal` pressure mode alone
does not mean projection was unnecessary: the 75% projection trigger is separate
from the 92% within-turn pressure escalation band. Cached pending deltas are
identified as `cached_rendering`, not a new pressure transition. Reuse after a
soft-target decision is identified as `soft_target_cached_rendering`; it has no
new deciding threshold because the unchanged safety gates accepted the candidate.

Parallel tool batches can contain both compacted and retained results. Only a
batch whose results are all compacted is treated as a whole-batch demotion;
pruning one result does not authorize rewriting its siblings on later normal
cycles. Synthetic interruption markers remain protected evidence. Existing
persisted projection decisions still replay with their original rendering.

A textual tool result is replaced by a placeholder only when the placeholder is at
most half its size. Short outputs stay verbatim: replacing them would add tokens and
rewrite an early cache position for no saving. Fallback pruning counts savings net
of the placeholder.

New recoverable placeholders contain one executable `read_tool_output` call,
not repeated read/search/anchor instructions. Shared tool guidance explains how
to search or use anchors; markers retain concrete anchor names, source provenance
and authorized lazy artifact references. Missing saved handles remain explicitly
unrecoverable. Truncated previews use the same concise recovery formatter.

New result projections use rendering-policy version 2. Existing version-1
decisions retain their exact rendering, including after cold-cache recovery.
The version travels with the projected message and existing bounded projection
decisions; `SessionCache` reconstructs both versions from Intaris. This adds no
content storage, external writes, locks, or cancellation/retry owner. Tool
exchanges, projection/compaction thresholds and idle renewal policy are unchanged.

Tools from one model response are executed and recorded as call/result pairs, but
the live request sends them as one assistant turn followed by their results. Cold
replay regroups calls owned by the same newly recorded native Responses or Anthropic
response, so the first request of the next turn keeps the batch shape and native
call bytes. A version marker on the native assistant or tool-call event opts into this behavior.
Unmarked historical batches keep their original grouping and omission semantics,
without a migration. Unrelated messages between calls keep the separate replay.
Explicitly recorded notices and attachment context belonging to those calls retain
their live positions without splitting the native assistant batch.

Tool discovery and skill loading append results and instructions without promoting
new direct schemas. The search/describe/call kernel stays present even before any
hidden tools become eligible; its schema descriptions do not embed the changing
inventory. Sessions that can load skills use controller discovery from their first
request, including Anthropic sessions, because native deferred catalogs would still
rewrite that prefix as skill tools are activated. Sessions without dynamic skill
loading can retain native Anthropic search.

New skill instructions, activation notices, and artifact reminders are canonical
per-message events. Tool attachment context is reconstructed from the original
sanitized attachment references, not copied media payloads. Declared and
classifier-selected skill-tool eligibility both recover from Intaris after cache
loss; loading a skill never bypasses current permissions or hard step profiles.
UI-only skill-loaded notices do not become extra model instructions on replay.
Reloads append new instructions while retaining earlier exposed instructions;
only the latest skill state controls current eligibility. This also applies to
`/skill` reloads. The same discovery policy governs inference fallback candidates.
Project-exposure notices preserve their source and role before the first request,
so Anthropic's notice wrappers match canonical replay.

Native Responses assistant-message items retain their structured content, IDs,
phase, and other wire fields in the original per-message events. Cold replay and
same-model profile/effort changes therefore do not flatten those items into
different string-based input messages. This uses the existing bounded native
envelope, not transcript snapshots. If a message envelope exceeds that cap, normal
text fallback remains available without discarding reasoning/call items that fit.
Older events that never recorded a native message envelope cannot recover its
missing provider fields retroactively.

Output-limit continuations also preserve individual response
segments and their notices. The chat timeline combines these segments for display,
including when a sync window starts partway through a response.

New notices and projection decisions must be durable before inference. A failed
write stops delivery rather than sending a prompt that cannot be reconstructed.
Unchanged cycles add no projection events or additional event-store reads.
Crossing the projection threshold does not itself summarize or rotate the session.
Full-transcript snapshot events remain retained for audit but are ignored during
replay; new snapshots are never written.

## During a turn

The controller does not inject percentage-based instructions telling the model
to wind down its work. Usage telemetry, projection, hard-budget enforcement,
and compaction recovery remain active. Projection decisions use the existing
Intaris event append boundary and turn cancellation owner; they add no lock,
database content copy, or separate retry framework.

Context can grow above steady as the agent reads files, runs tools, and reasons.
The conditional projection pipeline can reconsider projection above steady,
when tool output is large, or when pressure changes. It may reuse the previous
projection when safe. Reuse still receives a model-facing budget check.

Normal, pressure, and critical modes progressively reduce retained tool detail.
Mode selection uses pressure bands and hysteresis; burst is not the mode switch.
The usage bands are 92% for pressure and 97% for critical, measured against the
effective prompt budget. Dropping to a lower mode requires two consecutive
cycles below its band. A failed hard-budget check can force stronger projection
before those percentages are reached, particularly for large context windows.
If the selected result violates the effective hard boundary, the loop tries
stronger projection before compaction. Being above steady can persist for a long
turn because useful protected evidence remains and the result still fits.

## What projection preserves

Evidence selection is rule-based, not semantic understanding of relevance:
current-turn/recent tool groups, explicit protection and recovery metadata, and
groups already committed to the model influence preservation. Critical pressure
can demote more evidence than routine projection. User/developer instructions
are not discarded merely to satisfy a soft target.

Projection replaces eligible bulky tool output with compact recovery references.
The durable source remains available, but reopening it costs another tool call
and requires the agent to recognize the need. Compaction instead summarizes
conversation history and continues through session rotation. A failed or
skipped attempt must terminate visibly rather than leave a running card.

### Compaction during one long turn

The recovery limit counts consecutive compaction rotations without a completed
model cycle, not every compaction during a long turn. A completed model cycle
resets the counter. The newly compacted session is projected and measured before
the controller decides whether another recovery is needed. Two recoveries are
allowed by default; if pressure still requires another, the turn stops with
`compaction_recursion_exhausted`. Provider overflow also retains its separate
one-replay limit.

A turn can run for hours through automatic continuations without a new user
message. Compaction does not need a whole earlier turn to summarize: when the
current turn alone is larger than the recent-history allowance (about 30% of the
prompt budget, measured on the full tool output that would be replayed), it
summarizes the older part of that turn. Your instruction for the turn, and any
retry link to it, stays word for word, followed by the most recent complete tool
steps. The card then reads "Earlier work in the current turn summarized".

Compaction is skipped, before anything is recorded, only when the older part
holds no conversation content to summarize.

## Worked examples

These are illustrative resolved budgets, not universal model constants.

### Codex: 250,240 available tokens

With the Responses 8% serialization margin:

- Steady: **220,211**
- Burst: **237,728**
- Hard target: **245,235**
- Loop pressure: **237,728** — the binding safety boundary

At **106,044**, no size-driven reduction is needed. At **216,478**, usage is
above the old 85% threshold but below steady: that alone must not compact.
At **230,000**, between-turn projection tries to return toward steady; if it
cannot and the model-facing prompt remains safe, work continues. At/above
**237,728**, stronger projection must make room before another model request;
otherwise compaction/recovery follows.

Values 229,785 / 248,064 / 255,897 correspond to a different, 4% margin budget
and must not be displayed alongside the 250,240 budget.

### Large-context Claude: 839,232 available tokens

- Steady: **320,000**
- Burst: **600,000**
- Hard target: **660,000** — the binding ceiling
- Loop pressure: **797,270**

At **350,800 calibrated tokens**, between-turn projection tries to reduce
usage toward 320,000. During a turn, that usage is allowed; retained evidence
may keep it above steady. Crossing 600,000 does not independently require
compaction or select critical mode. Above 660,000, stronger projection must
reduce usage enough; unresolved pressure falls back to compaction.

## Manual control and limits

### Compaction in the chat timeline

Automatic recovery appears as one compaction card: **Compacting**, then
**Compacted**, **Failed**, or **Skipped**. Mid-turn cards belong at the tool/model
boundary where recovery happened, not at the beginning of the turn. The started
event is durable, so reconnecting during recovery can reconstruct the card.
Session rotation keeps the same compaction occurrence ID.

For example, 668,585 tokens exceeds Claude's 660,000 hard target even though it
is below the 797,270 loop-pressure ceiling. The card's reason identifies the
binding hard limit. Successful recovery continues the turn without leaving a
separate "stopping this turn" warning. If recovery cannot resume, an explicit
failure notice remains. Historical pressure warnings are suppressed only when
the projected history contains a successful compaction with the same occurrence
ID; unrelated failures are not hidden.

Use `/compact` when you deliberately want a summary boundary. It should not be
routine maintenance required to keep the automatic budget checks working.
Provider overflow can occur despite local estimates and follows bounded
recovery. If projection and compaction cannot make the request safe, the
controller stops rather than retrying indefinitely.
