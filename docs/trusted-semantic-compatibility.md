# Trusted semantic compatibility

This contract requires Mnemory `8a1bfce0fec279f2057fca7342797f2e4cdc2bcd`.
Both signed routes accept the existing 400,000-character source envelope.
The paired request inherits that limit without truncation.

## Terminal budget rejection

Only HTTP 422 with the exact typed `rejected_before_write` detail is terminal
budget failure. It must carry all four safety flags, the canonical operation ID,
caller-queue retention, and one of these reasons:

- `input_budget_exceeded`
- `extraction_fact_budget_exceeded`
- `action_limit_exceeded`
- `plan_budget_exceeded`

Cognis retains the complete signed source body, without its JWT, in the existing
queue row as `rejected_source`. The structured decision is `trusted_rejection`.
The evidence row becomes `rejected`; its paired user row becomes `failed` after
the same canonical rejection replays through the paired route.
Neither outcome means that memory ingestion succeeded.
The failure notice contains only a fixed explanation and the safe reason code.

Terminal deterministic rows prevent reconciliation from recreating retry loops.
Derived rows do not inherit retained source fields. Each failed trusted request
retains its own source under the existing queue-row retention behavior.
There is no automatic TTL for these terminal queue rows in the current code.
This change does not create a new retention duration, store, or permanent-retention
guarantee. Existing authorized administrative deletion still applies.

Generic 422 responses retain their previous behavior. HTTP 503 and transport
timeouts remain uncertain. Retries preserve the canonical root so Mnemory resumes
sealed work or replays its immutable result. Ordinary fallback is never invoked.

## Deployment order

Do not run the new semantic server with active admission on old Cognis.
Old Cognis cannot retain the typed rejection source and caps paired input at
1,000 characters.

1. Disable new trusted-evidence admission while retaining the one-owner allowlist.
2. Let existing admitted evidence and paired rows finish on the old matched pair.
3. Verify that admitted active rows are zero on both controllers.
4. Deploy Mnemory, then this Cognis revision with admission still disabled.
5. Verify readiness and version convergence, then restore one-owner admission.

Keep at least one Ready endpoint throughout. Do not drain Services, scale to zero,
or replay historical events. If existing admitted rows cannot finish, pause the
transition rather than migrate uncertain operations implicitly.

## Local contract check

Run `tests/contract/check_trusted_semantic_response.py` with Mnemory's Python
environment. Put the Mnemory root, Cognis root, and Cognis `packages/common/src`
on `PYTHONPATH`. It exercises real canonical rejection journals across both
client methods at 1,000, 1,001, and 40,000 characters without production access.
