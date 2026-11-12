# Conversation accounting and cost marginals

## Retained conversation population

`POST /api/v1/usage/session-summaries` accepts additive `direct` inclusion and
an `execution` selector. Legacy `self` keeps its original physical-session,
activity, or whole-conversation population.

- `conversation + direct`: every retained own root execution, including
  compaction, renew, reset, undo, and fork-origin executions. The active session
  pointer does not define this lifetime population.
- `conversation + inclusive`: that same own population plus authorized
  delegated, hook, and owned managed-agent conversation descendants. External
  channels, arbitrary forks, and task ownership are not accounting child edges.
- `execution + direct`: selected physical session's validated continuation
  lane, including compaction predecessors/successors. `inclusive` adds that
  lane's descendants, not its parent or siblings. Keep the same anchor.
- `session + direct` also uses its execution lane. An activity cannot use
  `direct`; an execution cannot use legacy `self`.

Own roots match the conversation owner/agent and have no parent or delegation
mode/task. Compaction preserves those discriminators. Source links do not
disqualify a new conversation's fork origin. Continuation lanes require actual
previous-session connectivity and equal owner, conversation, activity, agent,
parent, and delegation identity. A different activity is a boundary.
Malformed or ambiguous lineage is unavailable, never an active-session or
broader fallback. Persisted historical scopes remain authoritative; lost
pre-migration transition distinctions are not reconstructed.

Limits are 100 selectors, 10,000 batch membership entries, 1,000 physical
sessions per continuation lane, and 1,000 accounting strata.
Duplicate selectors preserve response cardinality without multiplying
contributions. `physical_sessions` counts distinct authorized physical sessions
represented in returned projected accounting; unavailable items return null.
No observations do not prove zero usage. Pending coverage is rejected explicitly.

Membership is provenance, not authority. Consumer, current project/resource,
and invoking-agent fences apply to every contribution. Personal historical
accounting survives completion, control closure, and target-agent use-grant
revocation; invoking-agent revocation still denies. Content grants confer no
accounting authority. Managed ancestry reuses the validated owner-bound
root/parent relation without granting content or secret access.

## Known priced subtotal

`POST /api/v1/usage/query`, `view: "cost_breakdowns"`, uses the usual scope,
window, filters, bucket, and pricing basis. It returns canonical `total` plus
`cost_breakdowns` with `measure: "known_priced_subtotal"`, `complete_groups: true`,
and four `dimensions` entries in order: `subject`, `agent`, `provider`, `model`.
Each entry contains bounded complete `groups` of ordinary `UsageGroup` values.

Label amounts **Known priced subtotal**, not complete spend or invoices.
Known components of partial valuations are included. Every `(currency, basis)`
band stays separate; missing money remains null and explicit measured zero
remains zero. Unknown dimension keys remain null groups. `group_by`, forecast,
and a non-cost metric are invalid. Ranking `limit` does not truncate marginals:
exceeding 1,000 combined total/marginal strata fails instead.

`partially_priced_attempts` is global and per band. Existing `unpriced_attempts`
means not fully priced and includes partial valuations.
`unallocatable_cost_attempts` counts attempts without a supported currency/basis
band. Show these cohorts as counts, not fabricated monetary slices. Complete
cost ranking and forecast requirements are unchanged.

## Query and transport boundary

One authorized summary CTE feeds total plus four marginal arms in one statement,
not a cross-product cube or per-group reads. Descendant summaries use fixed
batch metadata proofs and indexed neighbor probes, not per-selector database
loops or a materialized global physical-session graph.

These new operations share a request-owned read snapshot across authority,
lineage proofs, active generations, metadata and accounting. PostgreSQL uses a
read-only repeatable-read transaction and keeps its existing statement timeout;
SQLite explicitly starts a read transaction. Closure/cancellation rolls back.
No writes, external I/O, delivery retries, or pricing computations occur there.

Raw DTOs live in `ui/src/lib/usage/wire.ts`; summary aliases are in
`ui/src/lib/types/api.ts`. `fetchUsageSummaries(body, { signal })` in
`ui/src/lib/usage/conversation-api.ts` only transports typed data. It does not
select populations, build UI models, or replace failures with zeros.
Export exact schemas using `scripts/export_usage_query_contract.py`.
