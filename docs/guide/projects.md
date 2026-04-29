# Projects

Projects group related work so agents can understand which repository, source, workflow, task, or conversation they are operating inside.

![Cognis iOS PWA conversation drawer](../assets/screenshots/pwa-conversations.png)

Use projects when work has a durable context: a codebase, customer environment, research area, operations domain, or recurring workflow family.

## What a project contains

A project can track:

- name, description, and generated or uploaded avatar
- source hints such as repository paths, URLs, or external systems
- workflow bindings that define which workflows are appropriate for the project
- grants that share project access with other users
- project-aware tasks, schedules, and conversations

Source hints are context, not authority. Cognis uses them to help agents understand where work belongs and when a task touches a relevant path. Actual file, browser, shell, and network access still depends on the executor and tool permissions.

## Project-aware tasks

When a task belongs to a project, Cognis can inject project context into workflow steps. This helps coding and research workflows avoid generic execution by telling the agent which project it is working for and which sources matter.

Project-aware tasks are useful for:

- coding tasks tied to one repository
- research tasks tied to one domain or source set
- recurring operations for the same environment
- workflows that should use project-specific defaults

## Workflow bindings

Project workflow bindings let you define which workflow templates are relevant for a project. This keeps task creation focused and reduces accidental use of an unrelated workflow.

Typical examples:

- bind a software-development workflow to a code project
- bind a research workflow to a monitoring or market-analysis project
- bind an operations workflow to an infrastructure project

## Conversations and schedules

Projects can also anchor conversations and schedules. A project conversation gives the agent project context during chat. A project schedule creates recurring tasks that already know which project they belong to.

This is the difference between asking an agent to “check the docs” and giving it a stable project context where “the docs” means a specific repository, source set, and workflow style.

## Sharing and grants

Project grants allow other users to work with a project without making them global administrators. Admin role does not bypass user-owned resource ownership; access comes from ownership or explicit grants.

When sharing a project, still review executor placement and tool access. A user may be allowed to see project metadata but should only run tools through executors they are allowed to use.

## Revisions and human evaluation

Projects work with task comments and revision flows. A human can comment on a task outcome, request a revision, and Cognis can route the workflow back to the most relevant step while preserving prior step history.

This is especially useful for project work because feedback often targets a specific deliverable, implementation detail, or research gap rather than the whole workflow.

## Practical advice

- Create a project before creating recurring tasks for a codebase or research area.
- Add source hints that help the agent reason, but do not rely on hints for access control.
- Bind only workflows that make sense for the project.
- Use task comments for review feedback instead of starting a new unrelated task.
- Keep executor permissions narrow; project context does not replace tool boundaries.
