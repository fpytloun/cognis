from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.task_execution import TaskExecutionFence, TaskExecutionStore
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, User
from cognis.store.queries import (
    create_conversation,
    create_deliverable,
    create_step_run,
    create_task,
)


@pytest.fixture
def usage_events():
    """Task-local accepted observation collection; never used in production."""
    return []


@pytest.fixture
def usage_capture(usage_events):
    """Inject a synchronous recording observer into provider tests."""

    class Sink:
        def try_emit(self, observation):
            usage_events.append(observation)
            return True

    return UsageCapture(
        sink=Sink(),
        controller_owner_id="controller-unit",
        coverage_epoch_id="coverage-unit",
    )


@pytest.fixture
async def claimed_task_execution(
    task_continuation_db: Any,
) -> AsyncIterator[tuple[Any, TaskExecutionFence]]:
    """Provide canonical metadata and a real lease for task dispatch."""
    factory = task_continuation_db
    async with factory() as session:
        session.add(User(email="user@example.com", name="User", role="user"))
        await session.flush()
        session.add(Agent(agent_id="agent-1", owner_email="user@example.com", name="Agent"))
        await session.flush()
        await create_task(
            session,
            task_id="task-scheduled",
            created_by="user@example.com",
            agent_id="agent-1",
            title="Scheduled task",
            status="ready",
        )
        await session.commit()
    store = TaskExecutionStore(
        factory,
        owner_id="controller-dispatch",
        max_active_global=1,
        max_active_per_agent=1,
    )
    claim = await store.claim_ready()
    assert claim is not None
    fence = TaskExecutionFence(store, claim, asyncio.Event())
    try:
        yield factory, fence
    finally:
        await fence.close()


@pytest.fixture
async def task_continuation_db(tmp_path: object):
    """Create a small task DB with one user-owned deliverable and one foreign task."""

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-continuation.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "artifacts"),
            base_url="http://testserver",
            signing_secret="test-secret",
        )
    )
    factory.artifact_store = artifact_store  # type: ignore[attr-defined]

    async with factory() as session:
        session.add_all(
            [
                User(email="owner@example.com", name="Owner", role="user"),
                User(email="other@example.com", name="Other", role="user"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Agent(agent_id="agent-owner", owner_email="owner@example.com", name="Owner Agent"),
                Agent(agent_id="agent-other", owner_email="other@example.com", name="Other Agent"),
            ]
        )
        await session.flush()
        task = await create_task(
            session,
            task_id="task-owner",
            created_by="owner@example.com",
            agent_id="agent-owner",
            title="Owner task",
            status="completed",
        )
        await create_conversation(
            session,
            "owner@example.com",
            "agent-owner",
            "agent_direct",
            conversation_id="conv-owner",
        )
        step_run = await create_step_run(
            session,
            step_run_id="sr-owner",
            task_id=task.task_id,
            step_name="execute",
            step_type="direct",
            agent_id="agent-owner",
            conversation_id="conv-owner",
            status="approved",
            runtime_info={"source": "test"},
        )
        deliverable = await create_deliverable(
            session,
            deliverable_id="dlv_owner",
            step_run_id=step_run.step_run_id,
            content="# Full report\n\nComplete deliverable body.",
            format="markdown",
            title="Full report",
            outputs={"kind": "report"},
            artifact_store=artifact_store,
        )
        step_run.deliverable_id = deliverable.deliverable_id

        sibling_task = await create_task(
            session,
            task_id="task-sibling",
            created_by="owner@example.com",
            agent_id="agent-owner",
            title="Owner sibling task",
            status="completed",
        )
        sibling_step_run = await create_step_run(
            session,
            step_run_id="sr-sibling",
            task_id=sibling_task.task_id,
            step_name="execute",
            step_type="direct",
            agent_id="agent-owner",
            status="approved",
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_sibling",
            step_run_id=sibling_step_run.step_run_id,
            content="Sibling task content",
            format="plain",
            title="Sibling notes",
            artifact_store=artifact_store,
        )

        rich_task = await create_task(
            session,
            task_id="task-rich",
            created_by="owner@example.com",
            agent_id="agent-owner",
            title="Owner rich task",
            status="completed",
        )
        rich_step_run = await create_step_run(
            session,
            step_run_id="sr-rich",
            task_id=rich_task.task_id,
            step_name="execute",
            step_type="direct",
            agent_id="agent-owner",
            status="approved",
        )
        rich_deliverable = await create_deliverable(
            session,
            deliverable_id="dlv_rich",
            step_run_id=rich_step_run.step_run_id,
            content="Rich fallback",
            format="rich",
            title="Rich report",
            rich={"blocks": [{"type": "card", "title": "Finding", "content": "Body"}]},
            outputs={"kind": "rich_report"},
            artifact_store=artifact_store,
        )
        rich_step_run.deliverable_id = rich_deliverable.deliverable_id

        foreign_task = await create_task(
            session,
            task_id="task-other",
            created_by="other@example.com",
            agent_id="agent-other",
            title="Other task",
            status="completed",
        )
        foreign_step_run = await create_step_run(
            session,
            step_run_id="sr-other",
            task_id=foreign_task.task_id,
            step_name="execute",
            step_type="direct",
            agent_id="agent-other",
            status="approved",
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_other",
            step_run_id=foreign_step_run.step_run_id,
            content="Other user content",
            artifact_store=artifact_store,
        )
        await session.commit()

    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def standalone_ui_build(tmp_path, monkeypatch):
    """Provide a minimal standalone UI build so tests do not depend on ui/standalone-build."""
    build_dir = tmp_path / "standalone-build"
    (build_dir / ".vite").mkdir(parents=True)
    (build_dir / "assets").mkdir()
    # Vite emits content-hashed names; the asset resolver only serves those.
    (build_dir / "assets" / "standalone-Test0001.js").write_text("export {};", encoding="utf-8")
    (build_dir / "assets" / "standalone-Test0001.css").write_text(":root{}", encoding="utf-8")
    (build_dir / ".vite" / "manifest.json").write_text(
        json.dumps(
            {
                "src/standalone.ts": {
                    "file": "assets/standalone-Test0001.js",
                    "isEntry": True,
                    "css": ["assets/standalone-Test0001.css"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("cognis.ui_assets.resolve_standalone_build_dir", lambda: build_dir)
    return build_dir
