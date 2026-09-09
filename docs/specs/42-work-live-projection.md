# Work live projection

Work Activity uses mutable, reconstructable data for each physical session.
The database stores:

- `work_live_revisions` as the durable owner invalidation watermark
- `work_session_projections`
- `work_records`
- `work_record_files`
- `work_current_files`

The materializer updates these tables in one transaction for each Intaris
append. Its unique record key is the session, source sequence, and item
ordinal. A projection watermark advances only after the normalized records and
current-file state are durable.

Repair is session-local. A history gap changes only the affected projection to
`partial` or `failed`. Other sessions continue to materialize and remain
readable.

Overview and Work resolve the authorized recursive session tree from Cognis
SQL metadata for every request. They do not read Intaris or mutate foreground
state. The repository reads normalized rows for all physical members and
aggregates them into logical nodes. A non-null `activity_scope_id` collapses
session rotations.

Managed-conversation traversal keeps terminal history within the current
activity scope. It also preserves live descendants that still reference an
earlier physical generation of the same managed lineage. The graph maps the
physical parent to the current logical managed node. It does not admit
unrelated sessions from another scope.

The bounded graph prioritizes current backing sessions before rotation
history. Selected descendants retain their ancestor paths. A truncated graph
does not present only a completed predecessor of an omitted managed owner.
The response retains its truncation flag when the graph exceeds its budget.

Overview and Work use the same batched lifecycle enrichment after topology
resolution. Durable turn IDs and backing-session membership identify the
current execution owner. A queued successor cannot hide an earlier running
turn. A previous completion cannot hide an open managed conversation that is
idle or waiting for its controller. Delegated sessions retain their own
execution lifecycle.

The agent loop records the resolved model, provider, profile, and reasoning
selection in session metadata before execution. Activity reads this durable
snapshot without a controller-local cache. The snapshot includes its physical
session ID, so copied retry or fork metadata cannot claim a successor's
execution. Later profile selections replace the snapshot.

Historical delegates without recorded runtime identity remain unavailable.
Mutable agent defaults are not evidence of their previous execution.

The UI uses `execution_state` for execution labels and active indicators.
The Hide closed filter retains nodes with active execution. Descendant
execution does not make the parent's own turn active.

Recent Activity preserves evidence timestamp order across sessions.
Transcript order remains separate. A command result updates the existing
command identity without creating a second recent command.

Pagination cursors bind the authorized tree fingerprint, query filters,
ordering frontier, and immutable per-session watermarks. Later appends do not
change an existing pagination snapshot.

Append and topology changes publish best-effort owner-visible invalidations.
The database remains authoritative. A client that misses an invalidation
reloads on its next refresh or reconnect.

The Overview inspector tracks root and focused reads separately. It retains
same-scope data during refresh and shows an error only after an actual failed
read. An explicitly opened owned session can load local Cognis identity and
context telemetry before root topology catches up. This fallback does not
invent a structural parent.

The conversation sidebar reuses the owner Work revision as its durable
reconciliation watermark. A revision mismatch causes a filtered, paginated
full sidebar read. It is not a per-conversation delta. This keeps REST and push
lifecycle state coherent without a second revision policy. The tradeoff is
that owner Work changes that do not alter a sidebar row can cause an additional
sidebar read.

The UI applies these revision rules:

- Only an applied REST response advances the durable REST cursor.
- A push can advance the observed owner watermark, but it cannot advance the REST cursor.
- Each conversation has a durable push revision. Removal events use the same revision domain.
- An older push for another conversation remains eligible and causes a coalesced REST reconciliation.
- A revision gap causes a coalesced REST reconciliation.
- Legacy timestamp or unversioned pushes cannot replace durable state for the same conversation.
- Each legacy push causes a coalesced REST reconciliation.

Migration `137_work_live_projection` removes the unreleased scope-generation
tables. The cutover has no compatibility read and no SQL downgrade.
