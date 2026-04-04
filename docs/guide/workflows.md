# Workflows

Workflows define how Cognis runs multi-step work. They are reusable templates that tasks and agents can use when a request should be broken into explicit stages instead of one direct response.

## Where workflows appear

- `Workflows` in the main navigation opens the workflow editor.
- `Tasks` use workflows to execute queued or background work.
- Agent settings can choose default workflows or limit which workflows an agent may use.

## What a workflow contains

Each workflow defines:

- workflow identity and description
- ordered steps
- step type and prompts
- optional evaluation or revision behavior
- loop and gate behavior
- optional agent overrides per step

The workflow editor is designed for operators and advanced users. Most end users can start with existing workflows and only change them when they need a more structured execution path.

## Common workflow patterns

- `direct` style flows for simple work
- plan -> execute -> review flows for larger tasks
- gated steps that pause for human approval
- loops that retry or revise a step until it satisfies the evaluator

## Creating a workflow

Open `Workflows` and either:

- create a new workflow from scratch
- duplicate an existing workflow
- import workflow YAML into the editor

You can then define metadata, edit steps, preview the pipeline diagram, and save the workflow.

## YAML import and export

The workflow editor supports YAML import and export so you can:

- review a workflow definition outside the UI
- keep workflow definitions in version control
- copy a workflow between environments

## Workflow execution in tasks

When a task runs with a workflow, Cognis records:

- the active step
- step attempts
- evaluator decisions
- pauses for gates or questions
- final completion or failure state

Task results are delivered back into the conversation flow rather than sent directly to external channels.

## Tips

- Start simple. Only add evaluation loops or gates when the work truly needs them.
- Prefer short, explicit step instructions over long multi-purpose prompts.
- Use the task detail view to learn how a workflow behaves before making it more complex.
