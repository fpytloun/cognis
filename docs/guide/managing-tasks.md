# Managing Tasks

Tasks are how Cognis tracks durable work that should run through a workflow instead of a single immediate chat turn.

![Cognis task workflow view on iOS PWA](../assets/screenshots/pwa-task.png)

## What tasks are for

Use `Tasks` when work should be:

- queued for later execution
- reviewed step by step
- paused for human approval
- routed back into a conversation when complete

The main chat agent can also inspect and manage tasks directly. That includes:

- creating or updating tasks from chat
- inspecting paused task state and step outputs
- resolving workflow gates
- answering paused step questions

## Task board

The task board gives you a kanban-style view of work by status. From there you can:

- create draft tasks
- inspect queued or running work
- open task details
- understand which items are blocked, active, or completed

Tasks created through the UI, API, or agent `create_task` tool can be saved as
drafts. Draft tasks are durable but are not enqueued or executed until you submit
them. Creating a queued or ready task still starts through the normal queue path,
so existing automation can continue to create runnable work directly.

On mobile the board shows a single column at a time and a segmented-control
selector (Draft / Queued / Running / Paused / Done) so each column fills the
viewport. Drag-and-drop is desktop only; on mobile, open the task detail and
use the state buttons to move work.

## Task detail view

The task detail page shows the full workflow run, including:

- workflow and agent assignment
- current status
- timing and duration
- step runs and attempts
- evaluator feedback
- dependencies
- final result or failure information

This view is the best place to understand why a task is waiting, revising, or failing.

When a step runs more than once, the displayed accumulated duration includes all
attempts. Latest-attempt timing remains available as secondary detail where the
API/UI needs to distinguish it from total step time.

## Gates and step questions

Some workflows pause for human input. When that happens, Cognis records the pause and lets you respond from the task flow instead of losing the context in chat.

Step questions are stored as a question set: a paused step can ask one or more
questions, each with selectable options, multi-select when allowed, and an
optional custom answer. Web/API clients answer with structured per-question
answers. Plain-text channels such as Signal show the whole question set, but the
entire reply is forwarded as one free-form answer for the agent to interpret.

Gate pauses include evaluated condition details when available: referenced
values, expected or threshold values, comparison operator, actual result,
pass/fail outcome, branch or action taken, and any evaluation error. These
details help operators understand why a workflow paused, skipped, or followed a
specific branch.

You can also steer a paused task from chat through the main agent. When you do that, Cognis keeps the same task/workflow state and carries any operator instruction into the resumed step.

## Delivery model

Task results are delivered back into the target conversation instead of talking directly to external channels. This keeps user-facing communication inside the normal conversation model.

Task follow-ups are not all phrased the same way:

- if the result clearly belongs to the same conversation thread, Cognis can
  integrate it back into that thread
- if the result is scheduled, unrelated, paused, or delivered into another
  conversation, Cognis presents it as a separate notification-style update

If the controller is unsure whether a same-conversation task result still
belongs to the active thread, it falls back to a separate update.

## When to use tasks instead of chat

Prefer tasks when the work needs:

- multiple steps
- explicit review or evaluation
- persistence beyond the current turn
- dependency tracking
- later follow-up in the same conversation

If the same kind of task should run repeatedly, use `Schedules` to create those tasks automatically instead of creating them by hand each time.
