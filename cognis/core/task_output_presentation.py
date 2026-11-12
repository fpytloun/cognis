"""Read-only workflow-owned output freshness and task result selection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from cognis.core.workflow_output_contract import step_is_task_output
from cognis.models.deliverable import TaskDeliverableFreshness, TaskOutputPresentation
from cognis.models.task import TaskModel
from cognis.models.workflow import Workflow, WorkflowState
from cognis.store.models import DeliverableRow, StepRun, Task


@dataclass
class TaskOutputState:
    """One metadata observation shared by all task output surfaces."""

    freshness: dict[str, TaskDeliverableFreshness]
    step_freshness: dict[str, dict[str, TaskDeliverableFreshness]]
    output: TaskOutputPresentation


def resolve_task_outputs(
    task: Task | TaskModel, runs: Sequence[StepRun], documents: Sequence[DeliverableRow]
) -> TaskOutputState:
    """Resolve workflow freshness without changing publication or durable state."""
    state = (
        task.workflow_state
        if isinstance(task.workflow_state, WorkflowState)
        else WorkflowState.model_validate(task.workflow_state or {})
    )
    by_id = {run.step_run_id: run for run in runs}
    latest: dict[str, StepRun] = {}
    for run in runs:
        previous = latest.get(run.step_name)
        if previous is None or (
            run.attempt_number,
            run.attempt,
            run.started_at.isoformat() if run.started_at else "",
        ) > (
            previous.attempt_number,
            previous.attempt,
            previous.started_at.isoformat() if previous.started_at else "",
        ):
            latest[run.step_name] = run
    freshness: dict[str, TaskDeliverableFreshness] = {}
    for document in documents:
        producer = by_id.get(document.step_run_id or "")
        if producer is None:
            freshness[document.deliverable_id] = (
                TaskDeliverableFreshness(
                    state="stale",
                    replacement_status=task.status,
                    label=f"Partial output — task {task.status}",
                )
                if task.status in {"failed", "cancelled"}
                else TaskDeliverableFreshness()
            )
            continue
        replacement = latest[producer.step_name]
        invalidated = (
            producer.status in {"superseded", "rejected"}
            or producer.superseded_by_step_run_id is not None
            or replacement.step_run_id != producer.step_run_id
            or producer.deliverable_id != document.deliverable_id
        )
        if not invalidated:
            freshness[document.deliverable_id] = TaskDeliverableFreshness(
                state="stale" if task.status in {"failed", "cancelled"} else "current",
                replacement_status=task.status if task.status in {"failed", "cancelled"} else None,
                label=f"Partial output — task {task.status}"
                if task.status in {"failed", "cancelled"}
                else "Current output",
            )
            continue
        replacement_document = replacement.deliverable_id
        if replacement_document and replacement_document != document.deliverable_id:
            freshness[document.deliverable_id] = TaskDeliverableFreshness(
                state="historical", label=f"Historical — revision {document.revision}"
            )
            continue
        status = (
            task.status if task.status in {"paused", "failed", "cancelled"} else replacement.status
        )
        if replacement.status == "superseded":
            status = task.status
        text = {
            "running": "new revision in progress",
            "evaluating": "new revision in progress",
            "pending": "new revision pending",
            "queued": "new revision pending",
            "ready": "new revision pending",
            "paused": "revision paused",
            "failed": "revision failed",
            "cancelled": "revision cancelled",
            "rejected": "revision rejected",
        }.get(status, "replacement status unknown")
        freshness[document.deliverable_id] = TaskDeliverableFreshness(
            state="stale",
            replacement_status=status,
            label=f"Stale — revision {document.revision}, {text}",
        )

    selected = next(
        (
            doc
            for doc in documents
            if doc.deliverable_id == (task.result_data or {}).get("final_deliverable_id")
        ),
        None,
    )
    # A rewind clears result_data. Only a proven linear output owner may fill
    # that gap; document revision numbers never establish workflow ownership.
    if selected is None and state.effective_workflow_definition:
        workflow = Workflow.model_validate(state.effective_workflow_definition)
        owner = next(
            (
                step.name
                for index, step in enumerate(workflow.steps)
                if step_is_task_output(workflow.steps, step, index) is True
            ),
            None,
        )
        candidates = [
            (doc, by_id[doc.step_run_id])
            for doc in documents
            if doc.step_run_id is not None
            and doc.step_run_id in by_id
            and by_id[doc.step_run_id].step_name == owner
        ]
        if candidates:
            selected = max(
                candidates,
                key=lambda item: (
                    item[1].attempt_number,
                    item[1].attempt,
                    item[0].version,
                ),
            )[0]
    return TaskOutputState(
        freshness=freshness,
        step_freshness={
            run.step_run_id: {
                document.deliverable_id: freshness[document.deliverable_id]
                for document in documents
                if document.step_run_id == run.step_run_id
            }
            for run in runs
        },
        output=TaskOutputPresentation(
            deliverable_id=selected.deliverable_id,
            step_run_id=selected.step_run_id,
            freshness=freshness[selected.deliverable_id],
            format=selected.format,
            title=selected.title,
        )
        if selected is not None
        else TaskOutputPresentation(freshness=TaskDeliverableFreshness() if documents else None),
    )


async def load_task_outputs(session: AsyncSession, task: Task | TaskModel) -> TaskOutputState:
    """Load task metadata in the caller's existing read transaction."""
    runs = list(
        await session.scalars(
            select(StepRun)
            .where(StepRun.task_id == task.task_id)
            .options(
                load_only(
                    StepRun.step_run_id,
                    StepRun.task_id,
                    StepRun.step_name,
                    StepRun.attempt_number,
                    StepRun.attempt,
                    StepRun.started_at,
                    StepRun.status,
                    StepRun.deliverable_id,
                    StepRun.superseded_by_step_run_id,
                    raiseload=True,
                )
            )
        )
    )
    documents = list(
        await session.scalars(
            select(DeliverableRow)
            .where(DeliverableRow.step_run_id.in_([run.step_run_id for run in runs]))
            .options(
                load_only(
                    DeliverableRow.deliverable_id,
                    DeliverableRow.step_run_id,
                    DeliverableRow.revision,
                    DeliverableRow.version,
                    DeliverableRow.format,
                    DeliverableRow.title,
                    raiseload=True,
                )
            )
        )
    )
    return resolve_task_outputs(task, runs, documents)
