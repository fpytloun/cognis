from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from prometheus_client import generate_latest

from cognis.api.chat_v2.snapshot_activity import (
    conversation_needs_snapshot_warm,
    iter_active_snapshot_conversation_ids,
    resolve_event_session_conversation_id,
)
from cognis.api.chat_v2.snapshot_warmer import (
    ChatSnapshotActiveReconciler,
    ChatSnapshotWarmer,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, DirectTurnRequestRow, Session, User
from cognis.store.queries import (
    create_conversation,
    create_managed_conversation_link,
    create_step_run,
    create_task,
)


@pytest.mark.anyio
async def test_warmer_coalesces_without_dropping_more_than_128_conversations() -> None:
    warmed: list[str] = []

    async def warm(conversation_id: str):
        warmed.append(conversation_id)
        return "succeeded", None

    warmer = ChatSnapshotWarmer(warm, worker_count=4)
    await warmer.start()
    for index in range(200):
        assert warmer.enqueue(f"conversation-{index}")
    for _ in range(500):
        if len(warmed) == 200:
            break
        await asyncio.sleep(0.01)
    await warmer.stop()

    assert set(warmed) == {f"conversation-{index}" for index in range(200)}


@pytest.mark.anyio
async def test_warmer_preserves_request_arriving_during_active_refresh() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def warm(_conversation_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return "succeeded", None

    warmer = ChatSnapshotWarmer(warm, worker_count=2)
    await warmer.start()
    warmer.enqueue("conversation-a")
    await entered.wait()
    warmer.enqueue("conversation-a")
    release.set()
    for _ in range(100):
        if calls == 2:
            break
        await asyncio.sleep(0.01)
    await warmer.stop()

    assert calls == 2


@pytest.mark.anyio
async def test_warmer_and_periodic_reconciler_are_bounded() -> None:
    async def warm(_conversation_id: str):
        await asyncio.sleep(0)
        return "succeeded", None

    async def discover():
        for conversation_id in ("a", "b", "c"):
            yield conversation_id

    warmer = ChatSnapshotWarmer(warm, worker_count=1, max_pending=2)
    await warmer.start()
    reconciler = ChatSnapshotActiveReconciler(
        discover,
        warmer.enqueue,
        interval_seconds=1800,
    )
    await reconciler.reconcile_once()
    assert warmer.pending_count <= 2
    await warmer.stop()
    exposition = generate_latest().decode()
    assert "cognis_chat_snapshot_warmer_pending 0.0" in exposition
    assert "cognis_chat_snapshot_warmer_active 0.0" in exposition


@pytest.mark.anyio
async def test_warmer_records_factored_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasons: list[str] = []

    async def warm(_conversation_id: str):
        return "retry", "redis_unavailable"

    monkeypatch.setattr(
        "cognis.api.chat_v2.snapshot_warmer.SNAPSHOT_CACHE_METRICS.warm_failure",
        reasons.append,
    )
    warmer = ChatSnapshotWarmer(warm, worker_count=1, retry_seconds=0.01)
    await warmer.start()
    warmer.enqueue("conversation-a")
    for _ in range(100):
        if reasons:
            break
        await asyncio.sleep(0.01)
    await warmer.stop(drain_timeout_seconds=0.01)

    assert reasons
    assert set(reasons) == {"redis_unavailable"}


@pytest.mark.anyio
async def test_activity_discovery_covers_direct_channel_task_workflow_and_managed(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/snapshot-activity.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    contexts = {
        "direct": "direct",
        "agent-direct": "agent_direct",
        "channel": "slack",
        "task": "task",
        "workflow": "workflow",
        "managed": "agent_work",
    }
    async with factory() as session:
        session.add(User(email="user@example.com", name="User", role="user"))
        await session.flush()
        session.add(Agent(agent_id="agent-a", owner_email="user@example.com", name="Agent"))
        await session.flush()
        conversations = {
            name: await create_conversation(
                session,
                "user@example.com",
                "agent-a",
                context_type,
                conversation_id=f"conversation-{name}",
            )
            for name, context_type in contexts.items()
        }
        session.add(
            Session(
                session_id="canonical-session",
                conversation_id=conversations["direct"].conversation_id,
                user_email="user@example.com",
                agent_id="agent-a",
                intaris_session_id=None,
            )
        )
        for name in ("direct", "agent-direct", "channel"):
            session.add(
                DirectTurnRequestRow(
                    request_id=f"request-{name}",
                    turn_id=f"turn-{name}",
                    conversation_id=conversations[name].conversation_id,
                    agent_id="agent-a",
                    user_id="user@example.com",
                    idempotency_scope=f"scope-{name}",
                    idempotency_key=f"key-{name}",
                    admission_hash="admission",
                    payload_hash="payload",
                    payload={},
                    status="running",
                )
            )
        for name in ("task", "workflow"):
            task = await create_task(
                session,
                created_by="user@example.com",
                agent_id="agent-a",
                title=name,
                task_id=f"task-{name}",
                status="running",
                workflow_id="workflow-a" if name == "workflow" else None,
            )
            await create_step_run(
                session,
                task_id=task.task_id,
                step_name="execute",
                step_type="agent",
                agent_id="agent-a",
                conversation_id=conversations[name].conversation_id,
                status="running",
            )
        await create_managed_conversation_link(
            session,
            user_email="user@example.com",
            controller_agent_id="agent-a",
            controller_conversation_id=conversations["direct"].conversation_id,
            controller_session_id="controller-session",
            target_agent_id="agent-a",
            target_conversation_id=conversations["managed"].conversation_id,
            target_session_id="target-session",
            title="Managed",
            turn_state="running",
            active_turn_id="managed-turn",
        )
        await session.commit()

    cache = SimpleNamespace(has_warm_scope=lambda _scope: False)
    for conversation in conversations.values():
        assert await conversation_needs_snapshot_warm(factory, cache, conversation.conversation_id)
    discovered = {
        item async for item in iter_active_snapshot_conversation_ids(factory, page_size=2)
    }

    assert discovered == {conversation.conversation_id for conversation in conversations.values()}
    async with factory() as session:
        assert (
            await resolve_event_session_conversation_id(session, "canonical-session")
            == conversations["direct"].conversation_id
        )
    await engine.dispose()
