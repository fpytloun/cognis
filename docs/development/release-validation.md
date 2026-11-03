# Release validation

Release checks should find regressions, not turn implementation details into
permanent product requirements. A release is not the first time to run the broad
baseline: keep the ordinary CI unit, lint, type, and build jobs green during
development.

## Qualification matrix

1. Record the previous private release tag and the candidate commit/tree.
2. Audit the complete release range for affected boundaries. A documentation-only
   preparation commit does not excuse untested runtime changes earlier in that
   range.
3. Run the baseline in `AGENTS.md`: backend and frontend unit suites, Ruff checks,
   all package type checks, Svelte checks, production/standalone builds, and
   `uv run python scripts/changelog.py --check`.
4. Add service contracts for companion-service changes; runtime integration/E2E
   for orchestration, recovery, and hook changes; canonical replay for Chat v2;
   SQLite and PostgreSQL checks for schema/locking changes; isolated installs for
   package boundaries; and focused browser flows for affected rendering/state.
   Use the full scenario suites for broad protocol/runtime changes. Reuse
   evidence only when it identifies the exact tested tree and remains applicable.
5. Distinguish failures, intentional feature skips, and unavailable prerequisites.
   Do not report skipped service or device coverage as successful qualification.
   Unexplained regressions remain release blockers.

Use bounded parallel workers appropriate to the machine. Keep file-local
fixtures together when using pytest-xdist (`--dist loadfile`). Do not run
multiple overlapping full baselines while debugging one failing boundary.
Stop a stalled run deliberately, preserve its log, and report that it was
interrupted rather than treating partial progress as a pass.

The focused release-browser acceptance test uses the real `/docs/changelog`
route with isolated API fixtures. It can run against either the compiled app or
Vite: `cd ui && npx playwright test e2e/changelog.spec.ts`. Set `COGNIS_E2E_URL`
when the app is not at the default `http://localhost:8080`. Browser tests in
`ui/e2e/` must not depend on development-only module endpoints.

For frozen-source qualification, export `COGNIS_E2E_CAPTURE_ROOT` to an absolute
directory outside the checkout before running `uv run pytest tests/e2e/`. Raw captures go
to its `golden/` directory and promoted captures to `canonical/`, without
overwriting tracked fixtures. Keep the same variable exported for
`make e2e-events-replay`: replay checks both the committed corpus and the fresh
private captures, and rejects a missing or empty configured live corpus.
Ordinary runs without this variable retain the tracked output locations.
Review generated captures before explicitly promoting them into public source.

## Browser harness ownership

Use `make e2e-browser` for the complete browser gate. The default Playwright
configuration owns compose-app cases, not isolated fixture pages. The matrix
in `ui/browser-harnesses.json` routes every other case through its existing
fixture server, viewport and required browser projects. Selection-copy cases
retain both Chromium and iPad coverage and use `COGNIS_E2E_URL`.

The runner independently lists the complete test directory, verifies exactly
one logical owner and all required device executions, then runs each harness
sequentially under one unchanged 3600-second budget with one worker and zero
retries, regardless of interactive configuration defaults. Assertions and
per-config timeouts remain owned by the existing configurations. Missing
cases, devices, execution results, `.only`, empty groups or failed commands
fail the gate. Skips and flaky attempts remain explicit in each group's report.
Reports and artifacts are retained separately per group. Set `--output-root`
when invoking `uv run python scripts/run_browser_matrix.py` to retain a new run.
`--inventory-only` checks routing but is not execution or release qualification.

`make e2e-browser` binds the mock container through the same Compose project and
files used to start the stack. Only the Compose harness receives that identity.
Its barrier controls execute a fixed, deadline-bounded request on container
loopback; published Docker ports do not satisfy the mock's loopback-only control
gate. Missing or stale container identity fails without fallback or retry.
Host-started mock servers retain normal loopback HTTP control.

The component-runtime fixture compiles the real branding, streaming, scoped
timeline and session-log drawer components independently of the production
application. Scoped fixtures initialize fictional document-coordination
metadata before mounting while retaining principal-scoped dispatch fences.
This does not open development-only routes in production or replace application
authentication. Drawer cases belong to this harness, not the Compose app routes.

`playwright.pinned.config.ts` is the pinned-conversation owner; the older
`playwright.compiled.config.ts` has identical tests, device, retry and preview
inputs with only a different port, and is not a separate matrix execution.
For interactive compose-only debugging, use `make e2e-browser-ui`; use an
explicit owning config for fixture-page debugging.

The native attachment IndexedDB harness uses a fresh on-disk browser profile
under each test's output directory, matching the companion's persistent,
profile-scoped WebKit data store. WebKit's ephemeral context can reject Blob
records even when metadata-only transactions succeed. Both browser projects
retain their original device options and byte, reload, lease and abort checks.

## Failure triage

Classify each failure before editing:

| Failure | Correct response |
|---|---|
| Behavior, security, persistence, or recovery regression | Fix implementation and add/retain a reproducer |
| Fixture omits a newly required field or authorization provenance | Align the fixture with the real model; retain negative access tests |
| Test asserts obsolete UI or transport details | Verify the supported contract and test it through the owning boundary |
| Timing or shared-state flake | Isolate clocks, events, storage, and worker state; do not just increase limits |
| Missing service, key, database, browser, or device | Record the prerequisite and rerun in a qualified environment |

Prefer parsed query parameters over URL-string ordering, rendered accessible
controls over private component names, and shared style ownership over duplicate
pixel-string checks. Assert visible state transitions and deny paths. Avoid
source-text contracts when a behavioral test can establish the same invariant.
Do not replace one obsolete UI snapshot with another equally brittle snapshot.

## Evidence

Record:

- candidate and base revision, interpreter/runtime versions;
- exact commands and outcomes (counts, duration, skips);
- each original failure, verified cause, correction, and rerun result;
- unavailable prerequisites and the precise unverified boundary;
- reviewer identity/session, reviewed tree, findings, and their disposition.

Release notes are prepared independently of publishing. Follow
[`changelog.d/README.md`](../../changelog.d/README.md) to preview/export the
candidate notes. Finalizing fragments, bumping packages, tagging, pushing,
publishing a release, and deploying are separate explicit actions.

## Retrying controller publication

For an already-published immutable project tag, dispatch the current
`python-publish.yml` with its required `release_tag` input. The workflow keeps
its boundary verifier outside the application checkout, then checks out that
exact tag and verifies its canonical versions before building. This permits a
packaging-verifier correction without retagging or publishing newer application
source under an existing version. Existing common and executor distributions
are not republished by a manual controller retry.
