# Typed classifier routing

`LLMService.classify` owns typed inference for the `classifier` route. Each
concrete recovery candidate is adapted independently: native System One models
use their typed transport; chat models generate the same validated answer
contract. The existing inference router owns retries, fallback, provider access,
credential resolution, and executor placement. Classification adds no separate
retry loop and holds no database session across inference.

Tool classification and skill-tool selection own their respective question
construction and interpretation in `cognis/tools/decisions.py`. Tool metadata
continues through the existing taxonomy, capability, fingerprint, cache, and
queue validation. Skill selection accepts only confident answers about the
already eligible candidates. Missing, extra, or malformed answers cannot grant
access. Failed skill classification is not cached as a successful result.

The agent loop owns only the current activation's candidate subset. Its
session cache lifetime is unchanged; cancellation remains owned by turn
scheduling. Classification performs inference, not tool execution, and adds
no durable event or permission mutation.

Text generation uses `auxiliary_generation`: recaps, skill decomposition, and
workflow composition retain their existing publication and persistence owners.
The route inherits default chat routing when unset. Native decision models
are ineligible for that route.

Tasks without an explicit workflow use `system:general-task` at submission and
for legacy unassigned tasks at execution. Automatic workflow/skill selection,
its hidden classifier agent, and the obsolete follow-up classifier are removed.
Agent execution JSON has no schema change: retired selection keys are ignored
on read and stripped on execution-setting writes; unrelated settings remain.

## Validation

Use a task-local Python 3.12 environment and the normal shared validation lock:

```sh
PYTHONPATH=.:packages/common/src:packages/executor/src COGNIS_SKIP_UI_BUILD=1 \
  make test PYTEST_ARGS='tests/unit/test_decision_engine.py tests/unit/test_followups.py tests/unit/test_task_queue.py tests/unit/test_tool_classification.py tests/unit/test_native_tool_classification.py tests/unit/test_workflow_composition.py tests/unit/test_recap.py tests/unit/test_recap_recovery.py tests/unit/test_model_routing_routes.py tests/unit/test_typed_classifier.py tests/unit/test_agent_sharing.py tests/unit/test_manage_agent_executors.py tests/unit/test_task_outcomes.py -q --tb=short'
```

Run relevant package typing, frontend checks and broader lifecycle tests when
their boundaries change. Keep actual commands, source provenance, outcomes and
unexecuted gates with the qualification evidence. A focused pass is not proof of
full startup, provider or deployment acceptance.
