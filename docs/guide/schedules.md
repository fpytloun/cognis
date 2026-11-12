# Schedules

Schedules create tasks automatically on a recurring cadence.

## What schedules are for

Use `Schedules` when work should start without waiting for a person to open chat, for example:

- a daily review workflow
- a recurring sync or reporting task
- a periodic maintenance or follow-up routine

Each schedule acts as a task factory: when it fires, Cognis creates a normal task and that task runs through the usual workflow, agent, approval, and delivery paths.

## Where schedules appear

- `Schedules` in the main navigation manages recurring jobs
- `Tasks` shows the tasks created by those schedules
- `Workflows` provides the reusable execution template a schedule runs

The Schedules page shows active and recently completed one-shot schedules by default. One-shot schedules with no future run move to `Expired` after a 24-hour grace period.

On the dashboard, open an upcoming schedule and select **Planned steps** to
understand its workflow before it runs. The preview shows the workflow purpose,
agent instructions, tool actions, approval choices, decision paths, and possible
completion outcomes, including silent versus delivered results. Authored phases
group related steps.

Steps appear in definition order, not a promise that every step will execute.
Conditions and outcome routes choose the next path. Expand **Instructions and
technical details** for full prompts, result messages, and supporting
configuration. Raw tool arguments are intentionally omitted from the preview;
inspect the workflow definition when you need the exact command or payload.
Agent work describes intent, not a prediction of every action the agent will take.

## Creating a schedule

Open `Schedules` and define:

- a name and description
- the target agent
- the workflow to run
- the cadence or cron-style timing
- any delivery or execution settings that should apply to created tasks

Keep the first schedule simple. It is easier to verify one small recurring task before expanding into many automated jobs.

## How schedule runs behave

When a schedule fires, Cognis:

1. creates a task tied to that schedule
2. queues the task like any other background work
3. records progress and step activity in the task detail view
4. delivers the result back through the normal conversation or task delivery model

This means scheduled work uses the same safety checks, workflow gates, and audit trail as manually created tasks.

Controllers claim an immutable occurrence and recheck its due time in the
database. A stale timer cannot consume the next occurrence early. Completion of
an older task does not replace the visible status of a newer occurrence; failure
counts and automatic disabling still apply to task outcomes.

## Tips

- Use workflows for anything more complex than a single direct response.
- Start with low frequency until you trust the result quality.
- Check the resulting task history to validate prompts, step order, and delivery behavior.
