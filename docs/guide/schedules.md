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

## Tips

- Use workflows for anything more complex than a single direct response.
- Start with low frequency until you trust the result quality.
- Check the resulting task history to validate prompts, step order, and delivery behavior.
