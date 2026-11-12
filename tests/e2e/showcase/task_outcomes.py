"""Illustrative terminal-task history for real-API chat presentation captures."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.config import load_config
from cognis.core.deliverable_presentation import assistant_deliverable_event
from cognis.core.task_outcomes import task_outcome_key, task_outcome_reference
from cognis.core.task_output_presentation import load_task_outputs
from cognis.models.session import SessionEvent
from cognis.models.task import TaskModel
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.store.queries import (
    create_conversation,
    create_deliverable,
    create_session,
    create_step_run,
    create_task,
    update_conversation_active_session,
)


async def seed_task_outcomes(
    factory: async_sessionmaker[AsyncSession],
    provider: IntarisProvider,
    owner: str,
    project_id: str,
) -> dict[str, Any]:
    """Seed synthetic history, not workflow execution or model replies."""
    config = load_config()
    store = ArtifactStore(
        ArtifactStoreConfig(
            backend=config.artifact_backend,
            path=str(config.artifact_path),
            s3_endpoint=config.artifact_s3_endpoint,
            s3_bucket=config.artifact_s3_bucket,
            s3_region=config.artifact_s3_region,
            s3_access_key=config.artifact_s3_access_key,
            s3_secret_key=config.artifact_s3_secret_key,
        )
    )
    async with factory.begin() as db:
        conversation = await create_conversation(
            db,
            owner,
            "atlas",
            "web",
            title="Morning operations",
            project_id=project_id,
            title_source="manual",
        )
        stream = await create_session(
            db,
            conversation.conversation_id,
            owner,
            "atlas",
            status="idle",
        )
        stream.intaris_session_id = stream.session_id
        await update_conversation_active_session(
            db, conversation.conversation_id, stream.session_id
        )
        events = []
        tasks = {}
        for status, title in (
            ("completed", "Daily launch brief"),
            ("failed", "Refresh supplier availability"),
        ):
            task = await create_task(
                db,
                title=title,
                description="Prepare the morning operations brief.",
                created_by=owner,
                agent_id="atlas",
                project_id=project_id,
                status=status,
                source_session_id=stream.session_id,
            )
            task.attempt_number = 2
            task.completed_at = datetime.now(UTC)
            task.result_summary = (
                "Ready for early access."
                if status == "completed"
                else "The supplier feed did not respond. No inventory changes were made."
            )
            tasks[status] = task.task_id
            for name, step_status in (
                ("Collect sources", "completed"),
                ("Verify availability", status),
            ):
                run = await create_step_run(
                    db,
                    task_id=task.task_id,
                    step_name=name,
                    step_type="run",
                    agent_id="atlas",
                    status=step_status,
                    attempt_number=2,
                )
            document = await create_deliverable(
                db,
                step_run_id=run.step_run_id,
                title=title,
                format="markdown",
                content=(
                    "## Ready for today\n\nThe Autumn collection is ready for early access. "
                    "Stock and pricing are verified.\n\n"
                    "**Next:** Iris sends the invitation at 10:00; Vela watches checkout health."
                    if status == "completed"
                    else "## Verified so far\n\nPricing is unchanged. Supplier availability could not be verified."
                ),
                artifact_store=store,
            )
            run.deliverable_id = document.deliverable_id
            task.result_data = {"final_deliverable_id": document.deliverable_id}
            model = TaskModel(
                task_id=task.task_id,
                title=title,
                description=task.description,
                created_by=owner,
                agent_id="atlas",
                status=status,
                attempt_number=2,
                completed_at=task.completed_at,
                result_data=task.result_data,
            )
            if status == "completed":
                event = assistant_deliverable_event(document)
                event.data["task_outcome"] = task_outcome_reference(model)
            else:
                await db.flush()
                output = (await load_task_outputs(db, model)).output
                event = SessionEvent(
                    type="lifecycle",
                    data={
                        "event": "task_failed",
                        "task_id": task.task_id,
                        "title": title,
                        "status": status,
                        "task_outcome_card": True,
                        "outcome_key": task_outcome_key(model),
                        "attempt_number": 2,
                        "workflow_step": "Verify availability",
                        "failure_reason": task.result_summary,
                        "partial_result": output.model_dump(mode="json"),
                        "deliverable_ids": [document.deliverable_id],
                    },
                )
            events.append(event)
        handled = await create_task(
            db,
            title="Check courier handoff",
            description="Confirm the courier pickup time.",
            created_by=owner,
            agent_id="atlas",
            project_id=project_id,
            status="failed",
            source_session_id=stream.session_id,
        )
        handled.completed_at = datetime.now(UTC)
        tasks["handled"] = handled.task_id
        handled_model = TaskModel(
            task_id=handled.task_id,
            title=handled.title,
            description=handled.description,
            created_by=owner,
            agent_id="atlas",
            status="failed",
            completed_at=handled.completed_at,
        )
        events.append(
            SessionEvent(
                type="lifecycle",
                data={"event": "task_failed", **task_outcome_reference(handled_model)},
            )
        )
        events.append(
            SessionEvent(
                type="system_message",
                data={
                    "event": "turn_initiated",
                    "kind": "turn_initiated",
                    "notice_id": f"turn-init:{handled.task_id}",
                    "scope": "turn",
                    "content": "Handling task: Check courier handoff (failed)",
                    "task_outcome": task_outcome_reference(handled_model),
                },
            )
        )
        events.append(
            SessionEvent(
                type="assistant_message",
                data={
                    "role": "assistant",
                    "content": "Pickup is confirmed for 14:00. No customer notification needs changing.",
                },
            )
        )
    await provider.create_session(
        stream.session_id, "Illustrative task outcome history", "atlas", user_id=owner
    )
    result = await provider.record_events(
        stream.session_id,
        events,
        source="showcase_seed",
        idempotency_key=f"{stream.session_id}:task-outcomes",
        user_email=owner,
        agent_id="atlas",
    )
    if not result.ok or result.count != len(events):
        raise RuntimeError("Task outcome showcase history requires reconciliation")
    return {"conversation_id": conversation.conversation_id, "tasks": tasks}
