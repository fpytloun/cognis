# Stage 42 — Reconstructable Work cache

> **Superseded.** Migration `137_work_live_projection` removed this unreleased
> generation-cache design. See
> [`../42-work-live-projection.md`](../42-work-live-projection.md) for the
> active architecture. This document remains as historical design context.

## Status

Implemented.

## Purpose

Cognis SQL stores a disposable projection of Work activity.

Canonical Cognis metadata authorizes the activity topology. Intaris session
events provide the execution evidence. A Work cache can be deleted and rebuilt
without changing the visible result.

Foreground Work requests never reconstruct the cache. They authorize the
canonical root, enqueue one durable recovery request, and read one complete
cache generation.

## Source contract

Intaris event reads expose:

- `last_seq`: the highest sequence that Intaris durably assigned.
- `first_available_seq`: the first sequence that is still readable.
- `history_gap`: the first unavailable retained-history range.

These fields describe the unfiltered source stream. An empty filtered result
does not prove that source history is complete.

Cognis treats a reported history gap as permanent source unavailability.
Transport errors and old Intaris responses without availability metadata remain
retryable or unknown.

## Cache generations

Each Work scope has:

- one optional active generation,
- one optional recovery generation,
- durable recovery priority, retry, lease, and fence fields.

A worker builds candidate topology and evidence without changing the active
generation. It publishes a candidate only after all integrity checks pass.

Promotion performs one fenced transaction:

1. Lock every member projection in stable order.
2. Check topology, source targets, projector versions, and integrity counts.
3. Copy immutable summary and Current Files snapshots into the candidate epoch.
4. Mark the candidate ready.
5. Swap the active epoch.
6. Persist a Work invalidation outbox entry.

The worker publishes invalidation after commit. A failed publication remains in
the outbox and retries with backoff.

## Recovery states

The API exposes these states:

- `missing`: no complete cache exists.
- `recovering`: a candidate is in progress.
- `ready`: the active generation is complete.
- `stale`: complete evidence is available while refresh is in progress.
- `failed`: recovery failed and follows retry policy.
- `source_unavailable`: required Intaris history no longer exists.

The UI never presents an incomplete candidate as complete. If a prior active
generation exists, evidence refresh keeps that generation visible. A topology
revocation invalidates the affected active generation synchronously.

## Foreground behavior

Ready Work and Overview requests:

- make no Intaris request,
- do not resolve the canonical graph,
- do not create projection rows,
- do not reconcile watermarks,
- do not wait for the materializer,
- do not publish revisions.

Opening chat does not calculate Activity Overview. Chat snapshots return
`activity_overview: null`. The dedicated Work endpoints provide Work state.

Normal ready Overview uses at most five SQL statements, including root
authorization and artifact authorization.

## Files

The materializer stores normalized file facts and per-session current state.
Promotion copies immutable Current Files rows into the active cache epoch.

The Files view reads only these epoch snapshots. It does not run the historical
rename query.

Full file history is lazy:

```text
POST /api/v1/work/file-history
```

The request contains the canonical Work scope and `path_generation_id`.
Authorization checks the root, active generation membership, and every source
session. A permanent retained-history gap returns `410 history_unavailable`.
An invalid epoch or cursor returns `409 cursor_invalid`.

Source-derived diff previews are not persisted by default. The setting
`work.source_preview_max_lifetime_seconds` can enable bounded preview retention.
Its value must not exceed the Intaris retention period.

## Activity list

```text
GET /api/v1/work/activities
```

The activity list uses canonical Cognis metadata. It does not query Intaris or
start recovery. Each item returns a canonical `TimelineScope`. Clients must pass
this scope unchanged when they open Work.

Cached summaries are optional. An absent summary becomes available only after
the user opens the activity and recovery completes.

## Operations

Migrations are schema-only. They do not prefill Work rows.

Bootstrap is idempotent and has parity with Alembic on SQLite and PostgreSQL.

For rollback:

1. Stop all controllers.
2. Restore the previous application version.
3. Keep the previous active cache generation until rollback is complete.
4. Do not downgrade the database while a newer controller can write.

Work cache tables are disposable. Canonical conversation, session, task, step,
agent, project, and Intaris event data are not disposable.

## Acceptance checks

The implementation includes checks for:

- cache deletion and deterministic rebuild,
- conversation, session, and task-step roots,
- deep and wide topology,
- cycle detection,
- append during recovery,
- duplicate replay,
- lease takeover and stale-fence rejection,
- retained-history prefix and internal gaps,
- SQLite and PostgreSQL parity,
- ready-path query budgets,
- 50,000-file virtualization,
- cursor epoch and projector fencing,
- top-level canonical activity listing.
