# Cross-client admission acceptance

These tests are authored against real public routes. Collection is not execution
evidence. Do not run them without the allocated isolated-stack/browser slot.

## Topology and lifetime

`e2e_stack` starts its existing mock LLM, Mnemory, Intaris, and Cognis processes.
`AppendProxyServer` owns a loopback-bound ephemeral socket, dedicated event-loop
thread, and one `AppendBarrierProxy`. Cognis uses its URL; the proxy forwards to
the actual isolated Intaris URL. The existing fixture-generated signing keys and
normal JWT headers remain unchanged. The proxy has no HTTP control routes,
does not follow redirects, and disables access logs.

Proxy readiness follows ASGI startup and socket listener creation. Tests control
the one gate on its owning loop using `arm`, `wait_entered`, `release`, and
`cancel`. The gate matches an actual session from `/sessions/{id}/intaris` and a
test-generated public admission ID. It never matches user text.

The testing-only mock LLM gates one scenario/assistant-turn index. Its loopback
control supports arm, entered-event wait, release, and cancel. Tests cancel gates
in `finally`; service shutdown also cancels. Timeouts fail rather than release.
The pytest finalizer cancels proxy work before stopping Cognis and backing
processes, including startup/authentication failures.

## Cases

- `test_append_barrier_early_read_recovers_on_real_invalidation[normal]`
- `test_append_barrier_early_read_recovers_on_real_invalidation[absorbed]`
- `test_append_barrier_early_read_recovers_on_real_invalidation[queued_after_ui_stop]`

These use real scheduler/provider writes and independent authenticated recipient
sockets. An actual append is held, an early cursor read completes, then release
allows Intaris persistence. Recovery reads follow server invalidations only.
Before release, a protocol ping/pong fences subscription hydration and drains
earlier socket hints. After release, only `event_store_session_invalidated`
triggers the successful read; admission/turn hints cannot satisfy that gate.
The absorbed case requires the leader's turn ID; the stopped-queue case requires
a different turn ID plus the actual snapshot's successor runtime state
(`starting` or `running`) observed while its durable append is held. No obsolete
legacy websocket lifecycle events are required.
The latter is explicitly **queued → started after UI stop**, not natural
no-stop terminal scheduling. Anonymous reads and an unrelated fixture user's
REST/WS subscription are rejected.

- `test_cross_client_browser_recipients` runs six Playwright cases from
  `ui/e2e/cross-client-admission.spec.ts`: main/scoped ×
  normal/absorbed/queued-after-UI-stop.

Each browser case uses separate browser contexts, real uploaded attachments,
and a held sender public admission response (`route.fetch` then deferred
`route.fulfill`). Recipient rendering must converge before ACK delivery.
Normal cases also hold the recipient's original snapshot across admission.
User-row selectors exclude queued text and require the attachment in the row.
The matching scoped queue entry must disappear before evidence is read. A
queue-only negative selector fixture is collected alongside the lifecycle cases.
ACK checks await request completion and stable canonical sender identity.
Reconnect waits for subscription hydration plus a completed canonical read
before requiring the same single rendered identity and attachment.
Active-leader modes establish the real inference-barrier precondition before
loading the recipient's live view, then subscribe the recipient before the
tested admission. This separates fixture precondition acquisition from concurrent
recipient metadata reads without injecting or refreshing recipient content.
The ten-second gate, case and outer subprocess budgets are unchanged. Normal-mode
cases and the separate first-session lineage test retain empty-session coverage.
The leader barrier must still be held after the tested admission. Terminal
barriers return a failing control status, and queued-after-stop cancellation
must confirm the barrier had not expired; a historical entry event is not enough.
The reconnect phase explicitly closes the original socket, creates a separate
post-ACK witness through sender admission while the recipient is offline, and
tracks request objects first initiated in the new online epoch. An older held
response is ineligible. The fresh response must contain the canonical witness,
which is still absent from the recipient while routing holds that response;
after delivery it must render stably. Fixture-local identity-gate tests reject
pre-epoch request objects independently of response arrival time.

The absorbed delivery-suppression variant holds an actual received canonical
notification at the test-side receiver until a protocol ping/pong fence completes.
No positive recovery read occurs during that bounded hold; release then permits
the normal real sync. This suppresses **delivery to the test reader**, not
origin publication. It makes no claim of permanent nonconvergence in the
presence of other legitimate recovery hints.
Canonical IDs/attachment refs come
from the actual submitted request and evidence snapshot, not fixture constants.
The evidence snapshot is read only after UI convergence; it cannot make the UI
pass. The original backend test in `test_timeline_streaming.py` delays response
**consumption**, not browser application ACK delivery.

## Readiness and diagnostic failure evidence

The recipient passively observes completed production-issued snapshot/sync/backfill
responses before admission. A bounded metadata buffer preserves Request identity,
scope, canonical/runtime identity, stability and source references without retaining
content, attachments or cursor tokens. Only a stable canonical item with an Intaris
user-message reference qualifies; visible runtime rows and DOM presentation stability
do not. Rendered identity and the existing content/attachment evidence check remain
required before ACK release. Backend submissions use independent transaction and
message IDs. Scoped recipients open the actual ID-scoped dashboard conversation row.

Additional isolated diagnostic selectors (unset for the full matrix):
`COGNIS_E2E_DIAGNOSTIC_CASE=main_queued_after_ui_stop` and
`COGNIS_E2E_DIAGNOSTIC_CASE=scoped_normal`, each with
`pytest tests/e2e/test_cross_client_admission.py::test_cross_client_browser_recipients -v`.

The fixture selects its own provider as the default through the public
`set-default` endpoint and reads it back for owner/default identity. This preserves
the existing admin-created system provider, used by the system health probe.
The active agent remains owned by the synthetic authenticated admin and its
model override must reference that provider. No sharing grants are added.
HTTP readiness requires status 200. The proxy forwards GET `/health`
unchanged to real Intaris; it never synthesizes a healthy result. Cognis
`/api/health` must report healthy LLM and guardrails providers, the same producer
used by the browser's send gate. Broader `/api/v1/system/diagnostics` readiness
flags are captured separately without imposing unrelated companion prerequisites.

Named test steps check textbox enabled before sending. Deferred captures and
reconnect barriers have named sub-60-second deadlines; no overall timeout is
increased. Independent cleanup errors are secondary annotations when a primary
test failure exists, preserving its original stack and last semantic boundary.
Cursor comparisons use the real producer schema and exclude only `issued_at`
and `expires_at`; signed-token renewal is not canonical position movement.

## Commands after allocation

Dependencies: existing locked Python/UI environments, `uvx` companion packages,
installed Playwright Chromium, and the real UI build served by Cognis. No
dependency upgrades or production credentials are needed.

```sh
# Narrow helper/static preparation (does not start services):
uv run pytest tests/unit/test_e2e_barriers.py tests/unit/test_e2e_cursor_evidence.py -q
uv run pytest tests/e2e/test_cross_client_admission.py --collect-only -q
cd ui && npx --no-install playwright test e2e/cross-client-admission.spec.ts --list

# FIRST diagnostic allocation, not the full matrix:
uv run pytest 'tests/e2e/test_cross_client_admission.py::test_append_barrier_early_read_recovers_on_real_invalidation[normal-False]' -v
COGNIS_E2E_DIAGNOSTIC_MAIN_NORMAL=1 uv run pytest tests/e2e/test_cross_client_admission.py::test_cross_client_browser_recipients -v

# Only after those pass and broader execution is allocated:
uv run pytest tests/e2e/test_cross_client_admission.py -v
uv run pytest tests/e2e/ -v
make e2e-events-replay
cd ui
npm run check
npm run build
npx --no-install playwright test e2e/ --workers=1
```

For the final full UI suite use only an explicitly allocated isolated stack URL
and its fixture credentials; never fall back to a potentially shared default
port. The Python browser wrapper supplies its own stack URL, credentials, and
mock-LLM URL and removes personal local-admin environment overrides.
