# Cognis: Projects, Step Metadata Gating, and Human-as-Evaluator Revisions

## Status

PLANNED. This spec defines first-class **Projects** as a runtime context for
tasks, schedules, conversations, and workflows; **structured step-completion
metadata** with conditional gates; and a unified **human-as-evaluator**
revision flow that lets users re-attempt a completed/paused workflow by
choosing (or letting a classifier choose) a re-entry step.

The three concerns ship together because they share the same downstream
plumbing: workflow state, step run history, prompt assembly, and the comment
data model.

## Goals

1. Projects are durable, owned, optionally shareable objects describing a
   piece of work that may span multiple repositories.
2. A project can carry one or more **source repositories** with desired (but
   not necessarily existing) local paths, optional remotes, and optional
   credential **clues**. Path existence is **never** validated; on-demand
   executors may need to clone at runtime.
3. Workflows can be bound to one or more projects. Project-bound workflows
   are eligible only inside their projects. Generic workflows remain
   global. Auto-selection prefers project-bound workflows when both match.
4. Tasks, schedules, and conversations carry an optional `project_id`. New
   tasks created from a project-bound conversation inherit the project
   unless the caller overrides it.
5. The agent receives sanitized project metadata the first time it touches a
   project source path, alongside the existing repo-local instruction
   files (`AGENTS.md`, `CLAUDE.md`, `README.md`). The model never sees
   secret values; only credential references.
6. Workflow steps can declare a typed **completion metadata contract**.
   Gates can include declarative conditional expressions over prior step
   outputs and metadata.
7. Human comments on a task are first-class. With explicit intent, they
   trigger the same revision machinery that the evaluator uses, picking a
   re-entry step (user-supplied or classifier-selected) from steps marked
   as eligible by the workflow.
8. Re-attempts preserve the original execution context: prior step runs,
   deliverables, and comments remain available as history. Outputs from
   the target step onward on the active path are invalidated so downstream
   steps re-execute against the new approved output.

## Non-goals

- Group/team grantees beyond schema reservation (mirrors `28-agent-sharing.md`).
- Public project discovery or federation.
- Server-side validation of `local_path` existence.
- Storing repository credentials inside Project rows. Credentials remain
  per-agent; projects only reference labels/IDs as a clue.
- Replacing the workflow engine. This spec extends the existing engine, the
  pause/notification surface, and the comment surface.
- Replacing AGENTS.md or repo-local instruction loading. Project context
  augments these, never replaces them.

## Design Principles

### 1. Project is a controller-side context, not a data set

Projects live in Cognis. They describe what the system is working on and
how Cognis should behave when working on it (preferred workflows, branch
and PR policy, worktree convention, etc.). Repository content stays in the
filesystem of the executor; agent-local instructions stay in `AGENTS.md`.

### 2. Paths are user-maintained hints

A `project_sources.local_path` is a hint to the agent and the executor.
Cognis never asserts the path exists. Workflows, project instructions, or
the agent itself may clone, prepare, or refuse depending on the situation.
This makes ephemeral / on-demand executors first-class.

### 3. Credentials are referenced, never embedded

Project source rows carry a `credential_ref` clue (e.g. a label like
`github-readwrite`). Real credential ownership and access remain on the
agent + secrets/credentials provider per
[`15-browser-credentials.md`](15-browser-credentials.md) and the existing
secrets layer. Cognis logs and exports never contain the credential value.

### 4. Project-bound workflows are eligibility filters, not routes

A workflow with at least one row in `project_workflows` is project-bound.
It is eligible **only** within those projects. Generic workflows stay
globally eligible. Auto-selection ranks project-bound candidates above
generic ones for project tasks.

### 5. Human is just another evaluator

The existing evaluator-rejection path already supports retry feedback,
revision targeting via `OutcomeRoute(action="revise(<step>)", ...)`, and
gate-driven workflow rewinds. Human revisions reuse exactly that
machinery. The only delta is the source label (`human` instead of
`evaluator`) and the route classifier.

### 6. Re-attempt is a rewind, not a rerun

A re-attempt jumps `state.current_step_index` back to the chosen step,
invalidates that step and the active downstream path, and continues
forward with normal step execution, evaluation, and gating. Pre-target
steps remain authoritative. The fresh target step writes a new
deliverable and is subject to evaluator review like any other run.

### 7. History is preserved per attempt

Step runs and deliverables are never silently overwritten across human
revisions. New `step_runs` rows are inserted for re-attempts and chained
through `superseded_by_step_run_id`. UI exposes the history; workflow
state references only the currently active outputs.

### 8. Avatars reuse the agent image pipeline

Projects can carry an avatar via `avatar_image_id` or `avatar_url`. The
existing `images` route, generation flow, and agent avatar UI primitives
are reused. Avatars are display-only and do not influence routing.

## Domain Model

### Project

```python
class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Project(BaseModel):
    project_id: str
    owner_email: str
    name: str
    description: str | None = None
    instructions: str | None = None       # Cognis-side guidance (worktree/PR/branch policy, etc.)
    default_workflow_id: str | None = None
    avatar_image_id: str | None = None
    avatar_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: ProjectStatus = ProjectStatus.ACTIVE
    sources: list[ProjectSource] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    grants: list[ProjectGrant] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

`metadata` is a free-form JSON bag for things like:

```yaml
worktree:
  default: true
  base_dir: "~/work/worktrees"
git:
  branch_strategy: "feature/<task-id>"
  push_on_finish: true
  open_pr: true
  pr_template: ".github/PULL_REQUEST_TEMPLATE.md"
merge:
  policy: "wait_for_human_gate"
```

### ProjectSource

```python
class ProjectSource(BaseModel):
    source_id: str
    project_id: str
    name: str                              # short identifier ("cognis", "intaris")
    local_path: str | None = None          # user-maintained hint, may not exist
    remote_url: str | None = None
    default_branch: str | None = None
    credential_ref: str | None = None      # label/id only — clue, not a secret
    instructions: str | None = None        # repo-specific notes (clone depth, setup commands, etc.)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

### ProjectGrant

Mirrors `agent_grants` exactly so the two sharing mechanisms compose cleanly.

```python
class ProjectGrant(BaseModel):
    grant_id: str
    project_id: str
    grantee_type: Literal["user", "group"] = "user"   # group reserved
    grantee_user_email: str | None = None
    grantee_group_id: str | None = None               # reserved
    permission: Literal["use"] = "use"                # MVP single value
    granted_by: str
    granted_at: datetime
    revoked_at: datetime | None = None
    note: str | None = None
```

Two real-world cases this supports without further plumbing:

1. **Shared project, own agent.** Grantee has a `project_grants.use` and
   uses one of their own agents to operate on the project.
2. **Shared project + shared agents.** Grantee has both
   `project_grants.use` and `agent_grants.use` for the relevant agents.

There is no admin bypass for owner-owned projects, matching
[`28-agent-sharing.md`](28-agent-sharing.md).

### Step revision and metadata

```python
class StepRevisionConfig(BaseModel):
    allowed: bool = True
    use_when: str = ""                     # plain-language hint for the classifier


class StepCompletionMetadataField(BaseModel):
    name: str
    type: Literal["string", "number", "boolean", "array", "object"]
    required: bool = False
    description: str = ""
    enum: list[str] | None = None


class StepCompletionContract(BaseModel):
    fields: list[StepCompletionMetadataField] = Field(default_factory=list)


class StepDefinition(BaseModel):
    # existing fields ...
    revision: StepRevisionConfig = Field(default_factory=StepRevisionConfig)
    metadata_contract: StepCompletionContract | None = None
```

`StepOutput` gains an explicit `metadata: dict[str, Any] = {}` field for the
accepted typed metadata payload.

### Gate conditions

```python
class GateCondition(BaseModel):
    expression: str                        # DSL — see "Gate condition DSL"


class GateConfig(BaseModel):
    # existing fields ...
    conditions: list[GateCondition] = Field(default_factory=list)
```

When `conditions` is non-empty, the gate fires only if at least one
expression evaluates to true; otherwise the engine returns `"continue"`
without creating a notification or pausing.

### Task comments

```python
class TaskCommentIntent(StrEnum):
    RECORD_ONLY = "record_only"
    CONTEXT_ONLY = "context_only"
    REQUEST_REVISION = "request_revision"
    ANSWER_PAUSE = "answer_pause"


class TaskComment(BaseModel):
    comment_id: str
    task_id: str
    author_email: str
    body: str
    intent: TaskCommentIntent = TaskCommentIntent.RECORD_ONLY
    noop: bool = True                      # UI shortcut for "record only"
    target_step: str | None = None         # explicit user choice, when present
    confidence: float | None = None        # populated by classifier when used
    applied: bool = False
    attempt_number: int = 1                # which task attempt this comment belongs to
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
```

### Task and StepRun additions

- `Task.project_id: str | None`
- `Task.attempt_number: int = 1` (incremented on each human revision)
- `Schedule.project_id: str | None`
- `Conversation.project_id: str | None`
- `StepRun.attempt_number: int = 1`
- `StepRun.superseded_by_step_run_id: str | None`
- `Deliverable.attempt_number: int = 1`

`StepRunStatus` gains `superseded` to mark prior attempts that have been
invalidated by a human revision.

## Database Schema

### New tables

```sql
CREATE TABLE projects (
    project_id            VARCHAR PRIMARY KEY,
    owner_email           VARCHAR NOT NULL REFERENCES users(email),
    name                  VARCHAR NOT NULL,
    description           TEXT NULL,
    instructions          TEXT NULL,
    default_workflow_id   VARCHAR NULL,
    avatar_image_id       VARCHAR NULL,
    avatar_url            VARCHAR NULL,
    metadata              JSONB NULL,
    status                VARCHAR NOT NULL DEFAULT 'active',
    created_at            TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at            TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX ix_projects_owner_email ON projects (owner_email);

CREATE TABLE project_sources (
    source_id             VARCHAR PRIMARY KEY,
    project_id            VARCHAR NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    name                  VARCHAR NOT NULL,
    local_path            TEXT NULL,
    remote_url            TEXT NULL,
    default_branch        VARCHAR NULL,
    credential_ref        VARCHAR NULL,
    instructions          TEXT NULL,
    metadata              JSONB NULL,
    created_at            TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at            TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX ix_project_sources_project_id ON project_sources (project_id);
CREATE INDEX ix_project_sources_local_path ON project_sources (local_path);

CREATE TABLE project_workflows (
    project_id            VARCHAR NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    workflow_id           VARCHAR NOT NULL,
    created_at            TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (project_id, workflow_id)
);

CREATE TABLE project_grants (
    grant_id              VARCHAR PRIMARY KEY,
    project_id            VARCHAR NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    grantee_type          VARCHAR NOT NULL DEFAULT 'user'
        CHECK (grantee_type IN ('user', 'group')),
    grantee_user_email    VARCHAR NULL REFERENCES users(email),
    grantee_group_id      VARCHAR NULL,
    permission            VARCHAR NOT NULL DEFAULT 'use',
    granted_by            VARCHAR NOT NULL REFERENCES users(email),
    granted_at            TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at            TIMESTAMP WITH TIME ZONE NULL,
    note                  TEXT NULL,
    CHECK (
        (grantee_type = 'user'  AND grantee_user_email IS NOT NULL AND grantee_group_id IS NULL)
     OR (grantee_type = 'group' AND grantee_group_id   IS NOT NULL AND grantee_user_email IS NULL)
    )
);
CREATE UNIQUE INDEX uq_project_grants_active_user ON project_grants (project_id, grantee_user_email)
    WHERE grantee_type = 'user' AND revoked_at IS NULL;

CREATE TABLE task_comments (
    comment_id            VARCHAR PRIMARY KEY,
    task_id               VARCHAR NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    author_email          VARCHAR NOT NULL REFERENCES users(email),
    body                  TEXT NOT NULL,
    intent                VARCHAR NOT NULL DEFAULT 'record_only',
    noop                  BOOLEAN NOT NULL DEFAULT TRUE,
    target_step           VARCHAR NULL,
    confidence            REAL NULL,
    applied               BOOLEAN NOT NULL DEFAULT FALSE,
    attempt_number        INTEGER NOT NULL DEFAULT 1,
    metadata              JSONB NULL,
    created_at            TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at            TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX ix_task_comments_task ON task_comments (task_id);
```

### Column additions on existing tables

- `tasks.project_id VARCHAR NULL` (index)
- `tasks.attempt_number INTEGER NOT NULL DEFAULT 1`
- `schedules.project_id VARCHAR NULL`
- `conversations.project_id VARCHAR NULL`
- `step_runs.attempt_number INTEGER NOT NULL DEFAULT 1`
- `step_runs.superseded_by_step_run_id VARCHAR NULL` (self-reference)
- `deliverables.attempt_number INTEGER NOT NULL DEFAULT 1`

### Migrations and bootstrap

Per the project rule
([`AGENTS.md`](../../AGENTS.md) / `Database migrations`), every schema
change ships as **both** an Alembic migration in
`cognis/store/migrations/versions/` **and** an idempotent `_ensure_*`
helper in `cognis/bootstrap.py` registered in `run_schema_bootstrap()`.

## REST API

`projects` router under `/api/v1/projects`:

- `GET /api/v1/projects` — list owned + shared (`?status=`, `?q=`)
- `POST /api/v1/projects` — create (owner)
- `GET /api/v1/projects/{project_id}` — read (owner or grantee)
- `PATCH /api/v1/projects/{project_id}` — update (owner only)
- `DELETE /api/v1/projects/{project_id}` — soft archive (owner only)
- `POST /api/v1/projects/{project_id}/sources` — owner only
- `PATCH /api/v1/projects/{project_id}/sources/{source_id}` — owner only
- `DELETE /api/v1/projects/{project_id}/sources/{source_id}` — owner only
- `POST /api/v1/projects/{project_id}/workflows/{workflow_id}` — attach
- `DELETE /api/v1/projects/{project_id}/workflows/{workflow_id}` — detach
- `GET /api/v1/projects/{project_id}/grants` — owner or grantee (read)
- `POST /api/v1/projects/{project_id}/grants` — owner only
- `DELETE /api/v1/projects/{project_id}/grants/{grant_id}` — owner only
- `POST /api/v1/projects/{project_id}/avatar/generate` — convenience wrapper
  around the existing image-generation pipeline; seeds prompt from
  `name`/`description`. Returns `{avatar_image_id, avatar_url}`.

Existing routes extended:

- `tasks`: `project_id` on create/update/list and on responses; new
  comment sub-routes `GET/POST /api/v1/tasks/{task_id}/comments`,
  `PATCH /api/v1/tasks/{task_id}/comments/{comment_id}`.
- `schedules`: `project_id` on create/update/list responses.
- `conversations`: `project_id` on create and patch.

Authorization:

- All endpoints require `require_current_user`.
- Mutation endpoints use `forbid_mutation_for_viewer`.
- Owner OR active grant gates read; only **owner** mutates project,
  sources, workflow links, and grants.
- **Admin role does not bypass** ownership for project resources.

## Builtin Tools

New tools (registered in `cognis/tools/builtin/projects.py`):

- `list_projects(query?, status?)`
- `get_project(project_id)`
- `create_project(name, description?, instructions?, default_workflow_id?)`
- `update_project(project_id, ...)`
- `delete_project(project_id)`
- `add_project_source(project_id, name, local_path?, remote_url?, default_branch?, credential_ref?, instructions?)`
- `update_project_source(source_id, ...)`
- `remove_project_source(source_id)`
- `attach_workflow_to_project(project_id, workflow_id)`
- `detach_workflow_from_project(project_id, workflow_id)`

Tool descriptions explicitly carry the restraint rule:

> *Do not invent projects. Do not assign a project to a task unless an exact
> match (project name alias, source path prefix, or remote URL) exists.
> Project assignment is optional.*

`CREATE_TASK_TOOL`, `UPDATE_TASK_TOOL`, `LIST_TASKS_TOOL` gain optional
`project_id` with the same caution wording.

Comment / revision tools:

- `list_task_comments(task_id)`
- `add_task_comment(task_id, body, intent, target_step?, noop?)`
- `apply_revision(task_id, comment_id, target_step?)`
  — owner of the task only; runs the revision flow described below.

## Path-Touch Project Context

Cognis already injects repo-local instruction files when a tool first
touches a project path (`agent_loop._maybe_load_project_context_before_tool`,
`tools/executor/project_context.py`). This spec adds a controller-side
enrichment layer alongside that flow.

`cognis/core/project_runtime.py` (new):

- `resolve_project_for_path(user_email, path) -> Project | None` — match
  the path against `project_sources.local_path` prefixes across owned and
  granted projects.
- `resolve_project_for_task(task) -> Project | None` — by `task.project_id`.
- `build_project_context_message(project, sources)` — sanitized markdown
  block: name, description, instructions, source list (name, remote_url,
  local_path, default_branch, credential_ref **as label only**), workflow
  IDs. Never embeds secret values.

Hooks:

- `agent_loop._ensure_known_project_context_loaded` extended: when
  `ctx.task_id` resolves to a project, inject a
  `<project_metadata>...</project_metadata>` developer message once per
  session, before the existing repo-local `<project_instructions>` block.
- `agent_loop._maybe_load_project_context_before_tool` extended: when a
  touched path matches a configured `project_sources.local_path`, fetch
  the project and inject the same block.
- `session_cache` extended with `project_metadata` per session and
  `get/store_project_metadata` helpers.
- **Path existence is not validated.** Missing checkouts still trigger
  injection so the agent can clone or set up per project instructions.

## Project-Aware Workflow Selection

Eligibility rules:

- A workflow is **generic** if it has no `project_workflows` rows.
- A workflow is **project-bound** if at least one row exists.
- Tasks with `project_id=None` see only generic workflows.
- Tasks with `project_id=X` see generic workflows ∪ workflows bound to X.

Auto-selection ranks project-bound candidates above generic ones with
otherwise comparable matching strength:

- `cognis/core/workflow_registry.py::list_all` accepts an optional
  `project_id` filter and applies eligibility.
- `cognis/core/decision.py::select_workflow` takes the project context,
  runs project-bound heuristic patterns first, and includes a
  `project_match: bool` flag on each classifier candidate so the LLM
  can break ties deterministically.
- `turn_scheduler._select_workflow` plumbs `project_id` through.
- `task_queue.submit`, the task API routes, and `update_task_fields`
  validate eligibility and return 400 on a mismatch.

A project's `default_workflow_id`, when set, is used as the fallback
default before falling back to the agent default and finally
`system:general-task`.

## Step Metadata Contract

`StepDefinition.metadata_contract` lists the metadata fields the step
must (or may) emit on `step_complete`.

The `step_complete` tool schema in
`cognis/tools/builtin/workflow.py` gains an optional `metadata` object.
The agent loop's per-step schema builder
(`_build_controller_tool_schemas`) injects step-specific required fields
so the LLM sees an accurate contract.

`_validate_controller_tool_arguments` enforces required fields and type/
enum constraints. On failure, return synthetic `is_error=True` tool
result so the model self-corrects (existing convention).

`StepOutput.metadata` is persisted in `step_runs.output`.

Common preset fields agents may use without inventing:

- `confidence` (number 0..1)
- `risk` (`low`|`medium`|`high`)
- `decisions` (string[])
- `open_questions` (string[])
- `assumptions` (string[])
- `evidence` (string[])

These are not hardcoded into the engine; workflow authors choose them
explicitly. The set is documented in the workflow editor as a starter
palette.

## Conditional Gate DSL

`cognis/core/gate_conditions.py` (new) implements a strict, declarative
expression DSL — not arbitrary code:

- References:
  - `metadata.<step>.<field>`
  - `outputs.<step>.<key>`
- Operators: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `and`,
  `or`, `not`, parentheses.
- Literals: numbers, single- or double-quoted strings, `true`, `false`,
  list literals.
- Whitelist-only tokenizer + parser; rejects on parse error at workflow
  validation time.

`workflow_engine._handle_gate_step` evaluates `gate.conditions` against
`state.step_outputs[*].outputs` and `state.step_outputs[*].metadata`.
Gate fires only if at least one expression is true; otherwise the engine
returns `"continue"` without creating a notification or pause.

`workflow_registry._validate_workflow` calls
`validate_gate_conditions(workflow)` to verify references resolve to
known earlier steps and declared metadata fields.

Example:

```yaml
- name: pre_implement_gate
  type: gate
  gate:
    message: "Plan confidence is low or risk is high. Approve to continue."
    options:
      - {label: Continue, action: continue}
      - {label: Revise plan, action: "revise(plan)"}
      - {label: Cancel, action: cancel}
    conditions:
      - expression: "metadata.plan.confidence < 0.6 or metadata.plan.risk == 'high'"
```

## Task Comments and Human-as-Evaluator

### Intent semantics

| Intent              | Behavior |
|---------------------|---------|
| `record_only`       | Persist only. Never injected. UI default with `noop` checkbox on. **Never** changes task status, including on terminal tasks. |
| `context_only`      | Persist + inject into the current step at the **next model boundary** (never mid-tool-batch); also prepended to subsequent steps as `<user-comments>`. No re-attempt. |
| `request_revision`  | Trigger the re-attempt flow described below. |
| `answer_pause`      | Resolve the current `step_input` or gate. Reuses `respond_task_input` / `resolve_task_pause_action`. |

Comments are allowed on draft, queued, running, paused, completed, failed,
and cancelled tasks.

### Active-step injection

`agent_loop._execute_step` before each LLM call:

1. Drain unapplied comments for `(task_id, step_run_id)` with intent
   `context_only` or `request_revision` queued during the step run.
2. For `context_only`, append a developer/system message containing a
   `<user-comments>` block to `messages` (prompt-time injection, not
   re-persisted as an Intaris event).
3. For `request_revision`, finish the current tool batch, then halt the
   step and dispatch to the revision flow.

`_build_step_prompt` renders unapplied `context_only` comments under a
`## User Comments` section for next-step prompts (sibling of the
existing `## Operator Instruction` rendering).

### Revision flow

`cognis/core/revision.py` (new):

```python
class RevisionTarget(BaseModel):
    step_name: str
    confidence: float
    reason: str
    source: Literal["explicit", "classifier", "gate"]


async def select_revision_target(
    task: TaskModel, workflow: Workflow, comment: TaskComment
) -> RevisionTarget: ...
```

Layered selection:

1. If `comment.target_step` is set and that step has
   `revision.allowed=True`, use it (`source="explicit"`).
2. Else build a candidate list of steps where `revision.allowed=True`.
3. Build a classifier prompt (low temperature, JSON mode) with each
   candidate's `name`, `description`, `prompt`, `revision.use_when`,
   plus prior step outputs (truncated), prior deliverables (titles +
   summaries), and the human comment text.
4. Classifier returns `{step_name, confidence, reason}`.
5. If `confidence >= settings["workflow.revision.min_confidence"]`
   (default `0.65`), accept (`source="classifier"`).
6. Otherwise create a synthetic gate listing eligible steps with
   `revise(<step>)` actions; user picks (`source="gate"`).

`workflow_engine.apply_human_revision(task_id, comment_id, target_step)`:

1. Increment `task.attempt_number`.
2. Resolve target step index.
3. Find the **active-path** step runs from the target onward (target +
   downstream steps that contributed to the current `state.step_outputs`).
4. Mark those step runs `superseded`; chain `superseded_by_step_run_id`
   when the new attempt creates new rows.
5. Mark their deliverables `superseded`.
6. Drop those keys from `state.step_outputs` so downstream steps cannot
   consume stale outputs.
7. Reset `state.loop_iterations` keys for those steps.
8. Set `state.last_evaluation_feedback = comment.body`,
   `state.last_revision_context` = a structured prior-attempt summary
   (deliverable IDs, prior outputs of target+downstream).
9. Set `state.current_step_index = target_index`,
   `state.status = "running"`, task back to `queued`/`running` from the
   completed/failed/paused state it was in.
10. Mark the comment `applied=True`, store the chosen `target_step` and
    the classifier `confidence` if used.

Crucial properties:

- Pre-target step runs and outputs are untouched.
- The target step gets a **fresh `step_runs` row** so its prior attempt
  remains in history.
- Original execution context — deliverables, prior outputs, comments —
  remains visible as history and is the source of `<previous-run>` in
  the new prompt.
- Downstream steps re-execute because their inputs may now be stale.

### Step prompt for revisions

`_build_step_prompt` extended:

- If the current step is a re-attempt target, render:
  - `## Previous Attempt` — prior step run id, deliverable id/title,
    summary.
  - `## Human Evaluation` — comment body verbatim.
- Existing `Revision Feedback` / `Revision Context` rendering continues
  to feed downstream steps via `last_evaluation_feedback` /
  `last_revision_context`.

### Step run history

- `list_step_run_history(task_id, step_name)` returns ordered attempts
  including the supersede chain.
- `workflow_engine._run_step` reuses the latest `step_run` only when the
  reuse key is `(task_id, step_name, attempt_number)`. Human-driven
  revisions always insert new rows.
- UI consumes the history endpoint to render attempts inside the
  per-step view.

### Caps

There is **no cap** on human-driven revisions; only evaluator-driven
retries respect `max_attempts`.

## Workflow Authoring Examples

### Coding plan with metadata + conditional gate

```yaml
- name: plan
  type: run
  description: "Design approach and implementation plan."
  prompt: "..."
  revision:
    allowed: true
    use_when: "Feedback concerns scope, requirements, architecture, design, approach, or major missing direction."
  metadata_contract:
    fields:
      - {name: confidence, type: number, required: true,
         description: "0..1 plan confidence"}
      - {name: risk, type: string, required: true,
         enum: ["low", "medium", "high"]}
      - {name: decisions, type: array, required: false,
         description: "Key design decisions"}
      - {name: open_questions, type: array, required: false}
  completion: {evaluate: true, max_attempts: 5}

- name: pre_implement_gate
  type: gate
  gate:
    message: "Plan confidence is low or risk is high. Approve to continue."
    options:
      - {label: Continue, action: continue}
      - {label: Revise plan, action: "revise(plan)"}
      - {label: Cancel, action: cancel}
    conditions:
      - expression: "metadata.plan.confidence < 0.6 or metadata.plan.risk == 'high'"

- name: implement
  type: run
  description: "Apply code changes and verify behavior."
  revision:
    allowed: true
    use_when: "Feedback concerns bugs, missing behavior, tests, implementation defects, or code changes."
  ...

- name: final_summary
  type: run
  description: "Produce final report."
  revision:
    allowed: true
    use_when: "Feedback concerns wording, summary quality, or presentation only."
  ...
```

### Schedule + GitHub-issue scraper

```yaml
schedule:
  name: github-issues-cognis
  cron_expr: "*/15 * * * *"
  agent_id: agent:scraper
  workflow_id: system:scrape-issues
  project_id: prj_cognis
  task_template:
    title: "Triage GitHub issues for cognis"
    description: "Scrape issues with label 'agent-task' and create dev tasks."
```

A task materialized from this schedule inherits `project_id=prj_cognis`,
which makes the project's coding workflow the preferred candidate.

## UI

### Projects

- Top-level `Projects` nav (mobile bottom tab + desktop sidebar).
- `/projects` list with avatar, name, source count, status filter, search.
- `/projects/{projectId}` detail with sources, workflows, grants,
  instructions, avatar editor (reusing the agent avatar editor flow:
  upload, pick existing, generate via LLM seeded by name/description).

### Filters and assignment

- Tasks board gains a `Project` selector and URL-persisted filter.
- `CreateTaskModal` adds a `Project` selector; selecting a project
  filters the workflow picker to project-bound + generic.
- Task detail config modal adds a `Project` field and shows a project
  badge.
- Workflow editor adds a project-binding panel (attach/detach).
- Schedule editors add a `Project` field with eligibility-aware workflow
  picker.
- Conversation creation respects the optional `project_id`; tasks
  created from such conversations show the inherited project with an
  override.

### Comments and revisions

- `TaskComments` component on the task detail page (mobile + desktop).
- Composer:
  - Body textarea, `noop` checkbox (default ON).
  - Explicit intent buttons:
    `Save note` (record_only), `Add to context` (context_only),
    `Request revision` (request_revision),
    `Answer pause` (answer_pause; only when a pause exists).
  - Revision UI shows the classifier-recommended step with confidence
    and a manual override picker over eligible
    (`revision.allowed=true`) steps.
- History list with intent badges, applied/pending state, and attempt
  number.

### Step run history panel

- The task step group view shows attempts and the supersede chain.

## Operational Concerns

- **Logging redaction.** Project descriptions, source `local_path`,
  comment bodies, and revision feedback are user content. Logs include
  IDs, intents, decisions, and classifier confidence only — never the
  bodies/paths.
- **Credentials.** `credential_ref` is a clue/label. Resolution happens
  at the executor through the existing secrets/credentials provider per
  agent. Documented in spec and UI tooltip.
- **Concurrency.** The revision-target gate uses `pause_waiter` +
  `notification_service`. `WorkflowState.version` optimistic
  concurrency prevents simultaneous revisions on the same task.
- **Sharing.** Project authorization composes with agent authorization.
  The two real cases (shared project + own agent; shared project +
  shared agents) require no additional plumbing beyond the existing
  `agent_grants` resolver.
- **Avatars.** Stored as `avatar_image_id` referencing the existing
  images store. Avatars are display-only and never consulted by the
  routing engine. Prompts for LLM-generated project avatars are seeded
  from sanitized `name`/`description`.
- **No admin bypass** for project, source, workflow-link, grant, or
  comment mutations on user-owned projects, matching `28-agent-sharing.md`.

## Relationship to Other Specs

| Spec | Relationship |
|------|--------------|
| `01-architecture.md` | New tables and project_id columns are listed there. |
| `02-agent-model.md` | Agents remain owned per existing model; sharing composes. |
| `06-tool-system.md` | New project tools and revision tools are controller-injected, classified under `orchestration`. |
| `09-ui-ux.md` | New navigation surface, comment composer, workflow-editor revision/metadata fields. |
| `10-api-spec.md` | New `/api/v1/projects` and task-comment endpoints; existing routes get `project_id`. |
| `13-nfr-operations.md` | New metrics: project assignment rate, revision count per task, classifier confidence histogram. |
| `14-workflow-engine.md` | Step-completion metadata contract, conditional gates, human revision flow extend the engine. |
| `15-browser-credentials.md` | Project source `credential_ref` is a clue; real credential ownership stays per agent. |
| `21-workflow-deliverables.md` | Deliverables gain `attempt_number` and supersede status. |
| `22-step-profiles.md` | No change; step profiles remain orthogonal. |
| `28-agent-sharing.md` | `project_grants` mirrors `agent_grants`; both authorization paths compose. |

## Acceptance Criteria

1. Owners can CRUD projects, sources, workflow bindings, grants, and
   avatars. Grantees can read and use shared projects but cannot mutate
   them. Admin role does not bypass.
2. Project source paths are accepted without validation; missing
   checkouts still inject project metadata into the agent context.
3. Tasks, schedules, and conversations carry an optional `project_id`
   end-to-end through API, controller, agent loop, and UI. New tasks
   created in a project conversation inherit `project_id` unless
   overridden.
4. Workflows can be bound to projects. Project-bound workflows are
   ineligible for tasks outside their projects, and rank above generic
   workflows for project tasks in both heuristic and classifier
   selection.
5. `step_complete` accepts a typed `metadata` object validated against
   the step's `metadata_contract`. Rejection produces a structured
   tool error and the model can self-correct.
6. Conditional gates evaluate the DSL deterministically and skip pause
   creation when no expression is true.
7. Comments persist independently of attempts. `record_only` never
   changes task status, including on terminal tasks. `context_only`
   injects at the next model boundary, never mid-tool-batch.
8. `request_revision` reopens the same task: workflow state rewinds to
   the chosen step, target+downstream outputs are invalidated, prior
   step runs are preserved as `superseded`, and a new `step_runs` row
   is created for the target step. The new attempt receives previous
   run context and the human comment.
9. The revision-target classifier picks an eligible step or, when
   confidence is below threshold, creates a gate to ask the user.
10. Avatars on projects use the existing image pipeline; UI displays
    them on lists, detail pages, badges, and selectors.
11. `tests/unit/test_api_contracts.py` and
    `tests/unit/test_ui_contract_sync.py` pass for the new models and
    fields.
