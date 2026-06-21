# Cognis: Workflow Composer and Workflow-First Execution

## Purpose

This spec turns workflows from an advanced background-task feature into the
primary harness primitive for non-trivial work.

The goal is not to route every user turn through a controller classifier. The
goal is to let the **main agent already talking to the user** decide when a
request should stay inline, when it should use `system:general-task`, and when
it should compose a richer workflow with explicit intermediate steps,
evaluation, deliverables, and optional scheduling.

This spec defines:

- the workflow-first execution model for main-chat agents
- `compose_and_run_workflow` as the main authoring primitive
- hidden system agents for workflow composition and skill decomposition
- ephemeral workflow lifecycle and promotion to persistent workflows
- skill compatibility with official `SKILL.md` plus Cognis step extensions
- the initial coding workflow family used as the flagship composition domain
- deterministic workflow-step authoring guidance

## Related Specs

- [`02-agent-model.md`](02-agent-model.md)
- [`06-tool-system.md`](06-tool-system.md)
- [`09-ui-ux.md`](09-ui-ux.md)
- [`10-api-spec.md`](10-api-spec.md)
- [`14-workflow-engine.md`](14-workflow-engine.md)
- [`20-auto-routing-implementation-plan.md`](20-auto-routing-implementation-plan.md)
- [`21-workflow-deliverables.md`](21-workflow-deliverables.md)
- [`22-step-profiles.md`](22-step-profiles.md)
- [`34-deterministic-workflows.md`](34-deterministic-workflows.md)

## Motivation

Single-shot instruction following is not reliable enough for complex work.
When the agent is told to "do the whole thing" inside one generic step, the
controller can evaluate only the final claim, not whether the model skipped a
required sub-step, failed to inspect a second data source, or stopped early.

Workflows fix that class of failure because they provide:

1. explicit intermediate steps
2. step-local evaluation and retries
3. structured handoff between steps
4. a stable place to attach deliverables, profiles, and schedules

The existing workflow engine already provides the runtime contract. What is
missing is a practical authoring path from natural language to a workflow run.

## Design Principles

### 1. The main agent owns the decision to compose

The user's primary agent has the richest context: conversation history,
recalled memories, current intention, and the user's wording. It is better
positioned than a bounded classifier to decide whether the request should be
answered inline, wrapped in `system:general-task`, or expanded into a
multi-step workflow.

This spec does **not** add a new entrypoint agent and does **not** move this
decision into the controller's hot path.

### 2. `system:general-task` remains the unstructured execution envelope

`system:general-task` is still useful. It is the right shape for bounded work
that does not justify step decomposition but still benefits from semantic
evaluation.

Examples:

- explore recent GitHub activity and write a weekly report
- inspect a tool-heavy external system and summarize findings
- perform a one-step operational task that should be checked before delivery

It is **not** the preferred fallback for every coding request. Coding work may
instead compose into a shorter or longer workflow depending on scope.

### 3. Skills stay compatible with official `SKILL.md`

Cognis remains compatible with external harness skill formats. Official
`SKILL.md` instruction bundles continue to load and execute inline exactly as
they do today.

Cognis adds optional extensions so a skill may also declare reusable workflow
material:

- `steps:` — one or more workflow step fragments
- `workflow_templates:` — optional full workflow skeletons

If a skill does not declare steps, Cognis may still decompose it on demand via
the hidden `system:skill_decomposer` agent.

### 4. Ephemeral workflows are first-class

Most agent-composed workflows should be temporary. They need durable storage for
execution, audit, and promotion, but they should not pollute the user's regular
workflow library.

Ephemeral workflows therefore:

- are stored in the `workflows` table with `lifecycle="ephemeral"`
- are hidden from normal workflow library listings and classifier candidates
- are auto-archived after their task reaches a terminal state
- can be promoted into the workflow editor as a pre-populated draft

### 5. Coding is the flagship composition domain

Coding work benefits most clearly from workflow composition because the right
shape depends on the request:

- feature work may need planning and architecture review
- bug fixes may only need reproduce -> fix -> verify -> commit
- codebase questions may need explore -> synthesize with no code changes

The composer should therefore treat coding as a **workflow family**, not a
single monolithic workflow.

### 6. Deliverables and step profiles are prerequisites for best results

Typed deliverables and restrictive step profiles make workflow composition more
useful and more reliable. The composition stage therefore depends on the work in
[`21-workflow-deliverables.md`](21-workflow-deliverables.md) and
[`22-step-profiles.md`](22-step-profiles.md).

## Execution Model

Main-chat turns fall into three classes:

1. **Inline** — conversational, trivial, or naturally single-turn work.
2. **General-task** — bounded, tool-heavy, or exploratory work where explicit
   decomposition adds little value but evaluation still matters.
3. **Composed workflow** — multi-step, multi-source, recurring, or
   deliverable-sensitive work where the controller should enforce structure.

The primary agent makes this decision with guidance from the always-attached
`cognis-orchestrator` system skill.

## `compose_and_run_workflow`

### Tool contract

`compose_and_run_workflow` is a controller-intercepted orchestration tool
available to primary agents.

```python
class ComposeAndRunWorkflowArgs(BaseModel):
    intent: str
    context: str | None = None
    skill_hints: list[str] | None = None
    template_hints: list[str] | None = None
    schedule: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    persist: bool = False
    agent_id: str | None = None
    priority: int | None = None
```

Semantics:

- `intent` is the main agent's distilled statement of what the user wants
- `context` is a deliberate summary of the relevant conversation and memory
- `skill_hints` identifies relevant skills
- `template_hints` identifies useful workflow skeletons
- `persist=false` means the created workflow is ephemeral unless the request is
  being scheduled

### Controller flow

1. Resolve visible skill and workflow catalogs.
2. For hinted skills without declared `steps:`, call `system:skill_decomposer`.
3. Call `system:workflow_composer` with the full composition payload.
4. Validate the returned workflow definition.
5. Retry the composer once with validator feedback on malformed output.
6. On repeat failure, fall back to `system:general-task` rather than inventing
   controller-side structure.
7. Persist the composed workflow with `lifecycle="ephemeral"` unless the caller
   requested persistence or created a schedule.
8. Create a task or schedule referencing that workflow.

### Return shape

The tool returns ids plus a compact preview so the main agent can explain what
it built:

```json
{
  "task_id": "tsk_...",
  "workflow_id": "wf_...",
  "schedule_id": null,
  "workflow_preview": {
    "name": "Bug Fix",
    "steps": ["reproduce", "fix", "verify", "commit"]
  }
}
```

## Hidden System Agents

### `system:workflow_composer`

This hidden agent emits structured workflow definitions only. It has no tools.
It consumes:

- the main agent's distilled intent and context
- visible workflow templates
- visible skills and any declared step fragments
- decomposed skill fragments returned by `system:skill_decomposer`

It may:

- reuse an existing persistent workflow template
- adapt a template to a shorter or richer ephemeral shape
- assemble a bespoke workflow from fragments when no template fits

### `system:skill_decomposer`

This hidden agent converts skill instructions into `StepDefinition` fragments
when a skill is useful but does not declare `steps:` explicitly.

The decomposition result is advisory input to the composer, not a directly
executed workflow.

## Ephemeral Workflow Lifecycle

### Workflow fields

```python
class Workflow(BaseModel):
    workflow_id: str
    lifecycle: Literal["persistent", "ephemeral"] = "persistent"
    archived_at: datetime | None = None
```

Rules:

- system workflows are always `persistent`
- user-authored library workflows are normally `persistent`
- agent-composed one-shot workflows are normally `ephemeral`
- schedules force `persistent` storage because the workflow is reused

### Promotion

Promotion does not mutate the original ephemeral workflow in place. Instead the
UI opens the workflow editor with the ephemeral workflow pre-populated, and the
user saves a new persistent workflow. The original ephemeral workflow remains as
the audit artifact for the historical task run.

## Coding Workflow Family

The initial workflow family shipped with this spec is coding-focused.

### `system:software-development`

Full feature pipeline:

- plan
- architect_review
- implement
- update_docs
- code_review
- commit
- update_memory

### `system:bug-fix`

Shorter coding workflow for narrow fixes:

- reproduce
- fix
- verify
- commit

### `system:code-research`

Read-heavy coding workflow for understanding or explaining a codebase area:

- explore
- synthesize

The composer may use these templates directly or adapt them into a bespoke
ephemeral workflow when the task needs a proportional variation.

## Skill Model Extension

Skills may support one or more execution shapes.

### Inline instructions

Official `SKILL.md` instructions remain valid and can be loaded into a direct
turn or a `system:general-task` execution as pure prompt guidance.

### Step fragments

Optional `steps:` blocks let a skill provide reusable workflow pieces. These are
consumed by the composer, not executed automatically.

### Full workflow templates

Optional `workflow_templates:` blocks let a skill publish one or more complete
workflow skeletons. This is useful when a skill genuinely represents a full
process, not just one step.

## Deterministic Workflow Authoring

Workflow composition should use deterministic steps when they make the workflow
cheaper, clearer, or safer without requiring judgment.

Preferred pattern:

```text
deterministic fetch/check
  → deterministic condition/skip
  → run step only if judgment or writing is needed
  → deterministic complete for silent no-op when appropriate
```

Composer guidance:

- use `tool_call` for one mechanical Cognis tool call, especially read-only
  fetches such as Slack history, Alertmanager alerts, Mimir/Loki queries, file
  existence checks, or deterministic render/validation tools;
- use `when` to skip any step whose precondition is mechanically false;
- use `condition` for explicit branching to named workflow steps;
- use `complete` for deterministic success/no-op endings, including
  `delivery_mode_override: silent`;
- reserve `run` for ambiguous interpretation, root-cause analysis, synthesis,
  natural-language writing, coding, research, and other judgment-heavy work;
- reserve `gate` for human approval or caller decision, not mechanical
  branching.

Initial deterministic-workflow v1 intentionally defers `notify` and
`transform`. Until those step types exist, composed workflows should either
route to a `run` step for user-facing interpretation/notification, or terminate
with deterministic `complete` when no user-facing response is needed.

The composer must not generate arbitrary scripts or templates that require
secret values. Deterministic workflow rendering exposes only the safe context
defined in [`34-deterministic-workflows.md`](34-deterministic-workflows.md).

## UI and API Surface

This spec introduces the following user-visible changes:

- chat timeline card when a workflow is composed
- workflow library filtering for persistent vs ephemeral workflows
- task detail action to open the workflow editor from an ephemeral workflow
- skill editor action to suggest step decomposition from an instruction-only
  skill
- workflow composer and workflow-management tool examples for deterministic
  `tool_call`, `condition`, `when`, and silent `complete` steps

The REST API remains centered on workflows, tasks, and schedules. There is no
requirement for a public REST endpoint that mirrors `compose_and_run_workflow`;
the primary interface is the agent tool.

## Relationship to Auto Routing

This spec is intentionally separate from
[`20-auto-routing-implementation-plan.md`](20-auto-routing-implementation-plan.md).

Auto routing remains about:

- `agent_id="auto"` and `agent_id="self"`
- delegation and task agent selection
- workflow-aware routing for already-existing tasks

Workflow composition remains main-agent-owned. The controller does not classify
inline vs compose on the main chat hot path.

## Relationship to Deliverables and Step Profiles

Composed workflows should use the same workflow contract as all other workflow
runs:

- typed deliverables from spec 21
- restrictive or permissive step profiles from spec 22
- `step_complete` and evaluator semantics from spec 14

The composer should prefer:

- `coding` profile for coding-family steps
- `research` profile for read-heavy exploration and synthesis
- `unrestricted` for direct and `system:general-task`-style work

## Implementation Order

This work should land after:

1. Stage 31 — workflow deliverables and step profiles

It does **not** require Stage 30 auto-routing because composition is initiated
by the main agent rather than by controller-owned routing.
