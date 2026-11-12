# Startup agent profile selection

## Ownership and lifecycle

`core.initial_profile_selection` owns startup intent, candidate selection,
confidence validation, revalidation, and persistence. `SessionManager` records an
`initial_agent_profile_selection` marker in existing conversation context JSON
when a new conversation is created. Old conversations without the marker remain
untouched. Task/step roots retain default/explicit selection; managed creation
opts into target-agent defaults. Forks inherit their existing profile.

`TurnScheduler` calls the selector during serialized, cancellable preflight,
after user-message admission and before context assembly or main inference.
Auto does not spend the agent's within-turn profile-switch budget.
Only the first unresolved startup decision can write a pin. A stale queued model
refreshes the committed selection instead of invoking classification again.

The external side effect is one bounded typed classifier operation, using the
existing `LLMService.classify` recovery/dispatch policy and a 15-second outer
deadline. It always offers keep-default, and a non-default choice must meet the
agent's minimum confidence (0.55 by default). Only enabled, agent-switchable
profile IDs and descriptions enter the candidate request. The default's
description is included only if that profile is itself eligible. Empty/default-
only sets and oversized sets that exceed the typed question budget use the
configured default without inference.

No database transaction is held during inference. The selector rechecks startup
state, the active session, manual overrides, candidate configuration, eligibility,
the opt-out flag and the latest confidence threshold before committing. It
reuses the existing conversation/session row locks and `RuntimeSelectionPlan`:
profile pins, runtime revision and the resolved marker commit together, then
legacy process-local runtime mirrors are refreshed. Synthetic fallback remains
implicit: its resolved marker is persisted with a null profile ID, without
calling the real-ID runtime writer. A real profile named `default` therefore
cannot replace the synthetic baseline unless it is explicitly configured or
confidently selected as an eligible alternative. It adds no separate lock service,
background worker, retry framework, schema or content store.

Cancellation remains owned by the turn scheduler and its existing execution
fence. Cancellation before commit leaves startup pending; retry can finish an
uncommitted decision. After commit, retries and cold-cache reconstruction use
the stored result. Profile changes made during inference take precedence.
Only IDs, decision status/reason, classifier-use flag and confidence are stored
in the marker, never copied user requests, profile descriptions, prompts or responses.

## Task-fit policy

Classification compares the supplied profile descriptions without giving the
configured default preference merely because a task is ordinary. It selects the
least specialized profile sufficient for the initial responsibilities, rather
than predicting hypothetical later difficulties. Missing incidental details and
ordinary discovery are not evidence of unresolved ownership or specialist work.
Fully specified execution remains distinct from bounded implementation with
local choices. Concrete coupled ownership, shared invariants, difficult root
causes and coordination still require the corresponding responsibilities.

The default remains the fallback for genuinely unclear intent or responsibility,
provider/parser failure, or a non-default choice below the configured confidence
threshold. The existing shared classifier facade supports both native System One
and normal chat-model adaptation. Tied predictions retain the default.

When classification is attempted, the marker retains its prediction and confidence
separately from the final startup pin. Runtime-profile system context reports both,
including unknown confidence on failure, and advises switching to a better-fitting
currently eligible profile if task evidence warrants it. This is historical startup
evidence, not a new instruction to reinstate the original guess or override explicit
user choices. Both context-assembly paths use the existing runtime-profile renderer
and canonical preamble/audit delivery; no new content store, lock, transaction,
cancellation owner or profile-switch allowance is added.

## Configuration and compatibility

`execution.auto_profile_selection` is a strict settings group with `enabled`
(default true) and finite `min_confidence` in `[0.5, 1]` (default 0.55).
Agent REST contracts, CRUD settings tools and runtime loading share its model.
Agent CRUD patches preserve omitted group fields; null restores defaults.
Explicitly saved thresholds are not migrated; existing values such as 0.85
continue to apply until changed by the owner.

Startup modes are separate from profile IDs, so real profiles named `auto` or
`default` are not stolen by sentinel values. An omitted mode follows the target
agent's configuration; explicit IDs win. The new-chat picker opts into mode
selection, while task/workflow/channel-configuration pickers preserve their
existing explicit-only value format.

Existing permissions, agent identity, tool eligibility, executor routing,
explicit memory overrides, workflow step-agent selection and normal runtime
profile-switch enforcement remain authoritative.

## Validation

Run the owning tests through the shared validation lock:

```sh
PYTHONPATH=.:packages/common/src:packages/executor/src COGNIS_SKIP_UI_BUILD=1 \
  make test PYTEST_ARGS='tests/unit/test_initial_profile_selection.py tests/unit/test_initial_profile_lifecycle.py -q'
```

The lifecycle tests also accept `COGNIS_AUTO_PROFILE_TEST_POSTGRES_URL` for a
disposable test database. Each test creates and removes its own schema. This is
a test-only environment variable, not application configuration.

The opt-in semantic regression matrix uses the actual startup question and
confidence gate through the actual classifier facade and an isolated local routing
database. It supports native System One and normal chat-model inference:

```sh
# Supply an inference API URL, model and authorized API key through the
# environment: PROFILE_CLASSIFIER_URL, PROFILE_CLASSIFIER_MODEL,
# PROFILE_CLASSIFIER_API_KEY. These are test inputs, not application settings.
COGNIS_RUN_PROFILE_SELECTION_LIVE=1 \
  PROFILE_CLASSIFIER_NATIVE=1 \
  PYTHONPATH=.:packages/common/src:packages/executor/src \
  COGNIS_SKIP_UI_BUILD=1 \
  make validate COMMAND='uv run pytest tests/integration/test_initial_profile_selection_live.py -q'
```

For a normal chat model, omit `PROFILE_CLASSIFIER_NATIVE` and supply its model ID
in `PROFILE_CLASSIFIER_MODEL`; the fixture uses the OpenAI-compatible provider
instead of native System One. Live inference requires its own authorization.

The matrix covers short PR-conflict requests, rebasing, ordinary implementation,
fully specified work, coupled ownership, concurrency, coordination and unclear
intent, with a senior implementation profile as the configured default. It starts
no agents and executes no task work. Recorded properties contain only the prediction
ID, confidence and decision reason. A classifier failure is never accepted as a
successful default-choice case. Live facade acceptance is separate from durable
application startup lifecycle qualification.

UI acceptance covers settings save/reload at desktop/mobile widths and distinct
Auto/Default/manual creation requests in `ui/e2e/agent-detail-tabs.spec.ts`.
The mocked fixture's CSP handling is not deployment CSP qualification.
