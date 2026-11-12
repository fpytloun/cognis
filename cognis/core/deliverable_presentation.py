"""Canonical session-event presentation of readable deliverables."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cognis.models.deliverable import Deliverable
from cognis.models.session import SessionEvent
from cognis.store.models import DeliverableRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from cognis.models.task import TaskModel


async def task_result_deliverable_event(
    session: AsyncSession, task: TaskModel, *, recipient_email: str, include_partial: bool = False
) -> SessionEvent | None:
    """Select a readable canonical result, including partial output when requested."""
    from cognis.core.content_refs import authorize_deliverable_access
    from cognis.core.project_access import can_project_read_deliverable
    from cognis.store.queries import get_deliverable

    deliverable_id = (task.result_data or {}).get("final_deliverable_id")
    output = None
    if task.status != "completed" and include_partial:
        from cognis.core.task_output_presentation import load_task_outputs

        output = (await load_task_outputs(session, task)).output
        deliverable_id = output.deliverable_id or deliverable_id
    if (task.status != "completed" and not include_partial) or not isinstance(deliverable_id, str):
        return None
    deliverable = await get_deliverable(session, deliverable_id)
    if deliverable is None:
        return None
    if not await authorize_deliverable_access(
        session, deliverable, recipient_email
    ) and not await can_project_read_deliverable(session, deliverable, recipient_email):
        return None
    event = assistant_deliverable_event(deliverable)
    if output is not None:
        if output.deliverable_id is None:
            from cognis.core.task_output_presentation import resolve_task_outputs

            output = resolve_task_outputs(task, [], [deliverable]).output
        event.data["task_output"] = output.model_dump(mode="json")
    return event


def assistant_deliverable_event(
    deliverable: Deliverable | DeliverableRow, *, turn_id: str | None = None
) -> SessionEvent:
    """Reference a canonical deliverable without copying its content."""
    return SessionEvent(
        type="lifecycle",
        data={
            "event": "assistant_deliverable",
            "turn_id": turn_id,
            "deliverable_id": deliverable.deliverable_id,
            "lineage_id": deliverable.lineage_id or deliverable.deliverable_id,
            "revision": deliverable.revision,
            "format": deliverable.format,
            "title": deliverable.title,
            "render_metadata": deliverable.render_metadata,
            "export_metadata": deliverable.export_metadata,
        },
    )
