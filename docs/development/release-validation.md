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
Use the shared-lock Make targets described in `AGENTS.md`, or wrap a custom
check with `make validate COMMAND='command args'`. Tests, type checks, and builds
share one slot across worktrees for the same host/user. A waiting command reports
the lock owner PID immediately and every five minutes, and gives up after
`VALIDATION_LOCK_TIMEOUT` seconds (3600 by default, caller-overridable), without
starting validation. Selective tests use the same lock:
`make test PYTEST_ARGS='tests/unit/test_validation_lock.py -q -k deadline'` or
`make test-ui VITEST_ARGS='src/lib/chat-v2/outbox-clone.test.ts --maxWorkers=2'`.
`make test` sets `TMPDIR` to `/dev/shm/cognis-pytest-<uid>` when `/dev/shm`
exists: tests create many file-backed SQLite databases, and on a disk every DDL
statement syncs, which made runs several times slower. Passing tests' `tmp_path`
directories are deleted at teardown, so a full run stays small.
API tests that seed data through a running `TestClient` must use
`client.portal.call(...)`, not `asyncio.run(...)`: a second event loop sharing the
app's database pool can hang the app's shutdown.
Do not remove or age out a live lock; it releases automatically on process exit.
Keep launchers alive until their children finish. After cancellation, verify
task-owned descendants have stopped: orphan processes that closed inherited
lock descriptors are not covered.
Stop a stalled run deliberately, preserve its log, and report that it was
interrupted rather than treating partial progress as a pass.
After validation, clean up only resources you started (`docker compose down -v
--remove-orphans`, `--rm` for one-offs, and your processes/browsers); after merge
or push, remove your clean worktree and merged local branch without forcing removal.
`make e2e-down` deletes the selected project's volumes; never use a pre-existing
development stack as your disposable test project.

The focused release-browser acceptance test uses the real `/docs/changelog`
route with isolated API fixtures. It can run against either the compiled app or
Vite: `cd ui && npx playwright test e2e/changelog.spec.ts`. Set `COGNIS_E2E_URL`
when the app is not at the default `http://localhost:8080`. Browser tests in
`ui/e2e/` must not depend on development-only module endpoints.

### Explicit native Chromium acceptance variant

The Playwright configuration uses its normal bundled browser unless both
`COGNIS_E2E_CHROMIUM_EXECUTABLE_PATH` and `COGNIS_E2E_CHROMIUM_SHA256` are set.
When both are set, configuration verifies the absolute executable's file type,
read/execute access, and SHA-256 before passing it as the inherited browser
launch executable. A partial, malformed, unreadable, non-regular, symlink, or
hash-mismatched input fails configuration loading; it never falls back.

This opt-in is only for a separately identified native-browser acceptance
variant. It does not download or start a browser, alter viewport choices, or
make that browser equivalent to Playwright's bundled Chromium. Record the
executable hash and browser/image provenance with the acceptance result, and do
not claim browser tests passed unless they were actually run.

## Browser harness ownership

Use `make e2e-browser` for the complete browser gate. The default Playwright
configuration owns compose-app cases, not isolated fixture pages. The matrix
in `ui/browser-harnesses.json` routes every other case through its existing
fixture server, viewport and required browser projects. Selection-copy cases
retain both Chromium and iPad coverage and use `COGNIS_E2E_URL`.

The runner independently lists the complete test directory, verifies exactly
one logical owner and all required device executions, then runs each harness
sequentially under one unchanged 3600-second budget. Assertions, retry counts
and per-config timeouts remain owned by the existing configurations. Missing
cases, devices, execution results, `.only`, empty groups or failed commands
fail the gate. Skips and flaky attempts remain explicit in each group's report.
Reports and artifacts are retained separately per group. Set `--output-root`
when invoking `uv run python scripts/run_browser_matrix.py` to retain a new run.
`--inventory-only` checks routing but is not execution or release qualification.

`playwright.pinned.config.ts` is the pinned-conversation owner; the older
`playwright.compiled.config.ts` has identical tests, device, retry and preview
inputs with only a different port, and is not a separate matrix execution.
For interactive compose-only debugging, use `make e2e-browser-ui`; use an
explicit owning config for fixture-page debugging.

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
