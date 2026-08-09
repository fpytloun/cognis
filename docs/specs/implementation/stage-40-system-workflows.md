# Stage 40: Primary-Session System Workflows

## Status

DONE — implemented and validated locally. Not merged, pushed, deployed, or released.

## Outcome

The bundled General Task, Research, and Software Development workflows now use
small responsibility contracts and deterministic completion boundaries.

- General Task requires completion metadata and a canonical deliverable. It has
  no semantic evaluator.
- Research keeps `plan`, `research`, and `synthesize` in one primary-agent
  session. The planning step defers evidence collection. The research step
  defers the final narrative.
- Software Development keeps its primary steps in one continued session.
  Architecture and code reviews remain isolated secondary-agent sessions.
- Reviewer decisions use required `decision=approved|revise` metadata.
  Deterministic condition steps route revisions and enforce a five-iteration
  route budget.
- Primary task steps can use joined delegates and a restricted set of
  task-owned managed-conversation controls. Task creation, workflow creation,
  profile changes, delivery changes, and unrelated conversation control remain
  unavailable.
- Task Control Chat guidance applies context-only comments at the next primary
  boundary. Substantive scope changes target the planning step.

## Compatibility

The changes do not add a session-lane abstraction or a new evaluator system.
User workflows retain their existing defaults and evaluator behavior. Persisted
task workflow snapshots remain pinned. The new condition fields are optional,
so older workflow definitions remain valid.

## Validation

The final local validation included:

- 667 focused backend workflow, agent-loop, scheduler, registry, recovery, and
  orchestration tests;
- 28 focused Workflow Manager, workflow-source, and Task Cockpit frontend tests;
- `svelte-check`;
- Ruff lint and task-owned format checks;
- an independent code review after two correction passes.

The independent review approved the final diff with no remaining findings.

## Parent-review correction

A follow-up correction preserves the Stage 40 commit and fixes five acceptance
boundaries:

- exhausted condition recovery ignores the rejected backward target and resumes
  from the durable post-exhaustion index;
- deterministic backward review routes mark the target as a compact routed
  revision and reopen terminal todos;
- source-output provenance accumulates across continued primary-session steps;
- `objective`, `responsibilities`, and `defer_to` survive API, tool, UI, and YAML
  round trips;
- reuse and retry queries select only the current task attempt and current
  non-superseded revision.
