# How to Customize Your Harness

Cognis separates runtime rules from reusable work guidance and user-owned agent configuration. This separation keeps the core small while supporting different work styles.

## The four layers

### 1. Core runtime guidance

The controller supplies generic safety, capability, and context rules. These rules describe available tools, permissions, isolated child contexts, and compact delegation contracts.

A child contract normally includes:

- the objective and confirmed context
- exact source references
- scope, ownership, and non-goals
- acceptance and validation requirements
- the required result and evidence

Reuse a child only when its role, responsibility, authority, tools, and output contract remain compatible. Use a fork only for an independent branch that needs inherited context.

Core guidance does not require named execution modes, a coding hierarchy, or a specific team structure.

### 2. Reusable skills

Skills supply optional procedures for a class of work. The built-in `Cognis Coding` skill defines generic software-delivery conventions.

Its default is direct delivery in one conversation. The implementer owns the assigned code, tests, corrections, and result. It can use bounded exploration, research, advice, or independent review.

Coordinated delivery is also supported. Independent implementation workers need bounded ownership, stable interfaces, separate workspaces where applicable, and clear acceptance criteria. One coordinator owns integration and acceptance.

Advisory work returns analysis, a plan, or a review. It does not take implementation ownership unless the assignment changes.

These conventions are prompt text, not runtime modes. You can replace or disable the skill. Safety rules, workflow contracts, permissions, and visible tool capabilities still apply.

### 3. Agent prompts and profiles

Most users customize this layer. An agent prompt defines its role, behavior, and assignment conventions. Profiles define variants and selection criteria for that agent.

Profile descriptions must explain the role and when to select it. Do not encode a provider, model name, or reasoning effort in descriptive prompt text. Keep model and reasoning settings in the structured profile configuration.

### 4. Project instructions

Projects can add repository-specific instructions from two separate sources.

#### Configured project instructions

A Cognis project has an `instructions` field. Each project source also has an
`instructions` field for source-specific guidance.

Open **Projects**, select a project, and edit **Project instructions for
agents**. Edit a source to set **Source-specific instructions**.

The project API exposes the same fields:

- `POST /api/v1/projects` creates a project and accepts `instructions`.
- `PATCH /api/v1/projects/{project_id}` updates project `instructions`.
- Project source create and update requests accept source `instructions`.

When a conversation, task, or touched local path resolves to a Cognis project,
the runtime adds project metadata to the session context. This metadata includes
the configured project instructions and instructions for all listed sources.
The resolved source also identifies the relevant local project root.

#### Repository instruction files

In a local source workspace, Cognis loads `AGENTS.md` or `CLAUDE.md` files from
the workspace root to the effective working directory.

Cognis combines these files from root to leaf. Instructions in a nested
directory can refine broader repository instructions for that subtree. In each
directory, Cognis reads `AGENTS.md` before `CLAUDE.md`.

Configured instructions can define the project's purpose, shared operating
rules, and source-specific context. Repository files can define local
architecture, commands, validation, and contributor rules.

For example, the Cognis repository uses its root `AGENTS.md` to define package
boundaries, canonical owners, safety contracts, and test requirements.

Configured instructions apply when Cognis resolves the project by its explicit
project ID or a matching local source path. Repository files apply when the
runtime can inspect the local workspace. Secondary specialist delegations
receive their bounded contract instead of the parent project context.

## How the layers combine

The layers have different scopes:

1. Runtime capabilities, permissions, safety gates, and workflow contracts set
   non-overridable boundaries.
2. The agent identity and system prompt define the agent's general role.
3. Loaded skills add reusable procedures for the current work.
4. Configured project instructions add project and source context. Repository
   files add rules for the selected workspace and directory.
5. The current assignment defines the requested outcome inside those
   boundaries.

Neither project source replaces core safety rules or grants capabilities.
Configured instructions and repository files are both added to context. Cognis
does not define one as an override for the other. Resolve conflicts through the
normal instruction hierarchy and the current assignment. More specific
repository files refine broader files because Cognis combines them from root to
leaf.

This combination is prompt context, not a new runtime policy engine. Cognis
does not classify project instructions into direct, coordinated, or advisory
modes.

## Supported configuration paths

### User interface

Open **Agents**, then create or edit an agent.

- Use **Personality** and **System prompt** for role and behavior instructions.
- Use the profile editor for profile descriptions and structured model settings.
- Use the skill selector to attach or detach `Cognis Coding`.
- Open **Tools** to create or edit a replacement database-backed skill.

The **System prompt preview** shows the combined agent identity message. Skills load separately through the skill runtime.

### API

Use the existing agent API:

- `POST /api/v1/agents` creates an agent.
- `GET /api/v1/agents/{agent_id}` reads the current definition.
- `PUT /api/v1/agents/{agent_id}` updates the definition.

The request supports `system_prompt`, `personality`, `skills`, `agent_profiles`, `default_agent_profile_id`, and `llm_config`. Read the current agent first. Then send a complete update that preserves unrelated fields.

Use the skill API or built-in skill management tools to create, update, attach, or detach database-backed skills. Do not edit controller files for per-user customization.

## Prompt examples

### Direct implementer

> Complete the assigned engineering change in this conversation. Inspect the repository, implement the bounded scope, run relevant tests, correct defects, and return evidence. Use other agents only for bounded research, advice, or independent review. Do not delegate the implementation scope.

### Team coordinator

> Own decomposition, integration, validation, and final delivery. Assign only independent workstreams with stable interfaces and separate ownership. Give each worker exact references and acceptance criteria. Workers must not delegate their implementation. Do not replace this coordinator with another coordinator.

### Advisor

> Analyze the engineering request and return a plan, design assessment, or findings. Do not edit code or assume implementation ownership unless a later assignment explicitly requests it.

### General assistant that assigns engineering coordination

> Handle ordinary requests directly. For a substantial engineering request that benefits from parallel work, assign one bounded coordinator. Give it the objective, confirmed context, scope, acceptance criteria, and required evidence. Keep responsibility for the user-facing result.

These examples use conventions, not reserved syntax. You can use different words if ownership and acceptance remain clear.

## Chat modes are separate

The one-turn `plan` and `build` chat modes affect how a turn approaches the work. They do not define team ownership and do not enforce prompt conventions.

Use `plan` for planning-only analysis. Use `build` for implementation-oriented execution when the assignment and safety constraints permit it.

## What customization cannot change

Prompt text and skills cannot grant tools, permissions, credentials, or asynchronous execution. They cannot override workflow completion contracts, guardrails, ownership checks, or safety gates.

Cognis does not classify these prompt conventions into a new schema. It does not persist or enforce a direct, coordinated, or advisory mode.
