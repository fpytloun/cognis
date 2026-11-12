"""Exercise real artifact CAS writes with each runtime execution fence."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.direct_turn_runtime import (
    BackgroundDirectTurnFence,
    DirectTurnExecutionFence,
    StaleDirectTurnOwner,
)
from cognis.core.hook_subsessions import HookExecutionFence
from cognis.core.task_execution import (
    StaleTaskExecutionOwner,
    TaskExecutionFence,
    TaskExecutionStore,
)
from cognis.core.tool_router import ToolRouter
from cognis.models.agent import AgentDefinition
from cognis.models.session import SessionModel
from cognis.models.tool import ToolCall, ToolResult
from cognis.store.coordination import DatabaseLeaseStore
from cognis.store.database import create_session_factory
from cognis.store.direct_turns import DirectTurnStatus, DirectTurnStore, conversation_lease_key
from cognis.store.models import ArtifactRecordRow, Base, CoordinationLeaseRow, DirectTurnRequestRow
from cognis.store.queries import create_agent, create_conversation, create_task, create_user
from cognis.tools.builtin.artifact_tools import ARTIFACT_WRITE_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry

OWNER = "fence-test@example.com"


@pytest.fixture(params=["sqlite", "postgresql"])
async def env(request, tmp_path: Path):
    postgres = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if request.param == "postgresql" and not postgres:
        pytest.skip("needs a disposable COGNIS_TEST_POSTGRES_URL")
    schema = f"artifact_fence_{uuid.uuid4().hex}"
    admin = None
    if request.param == "postgresql":
        admin = create_async_engine(postgres)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        database = create_async_engine(
            postgres, connect_args={"server_settings": {"search_path": schema}}
        )
    else:
        database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fence.db")
    try:
        async with database.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(database)
        async with factory() as db:
            await create_user(db, email=OWNER, name="Fence test", password_hash="test")
            await create_agent(
                db, agent_id="fence-agent", owner_email=OWNER, name="Fence agent", status="active"
            )
            for conversation_id, context_type in (("direct", "web"), ("managed", "agent_work")):
                await create_conversation(
                    db,
                    user_email=OWNER,
                    agent_id="fence-agent",
                    conversation_id=conversation_id,
                    context_type=context_type,
                )
            await db.commit()
        artifacts = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        yield SimpleNamespace(
            factory=factory,
            artifacts=artifacts,
            root=tmp_path / "artifacts",
            leases=DatabaseLeaseStore(factory),
            turns=DirectTurnStore(factory),
        )
    finally:
        await database.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def _fence(env, kind: str):
    conversation_id = "managed" if kind == "managed" else "direct"
    if kind == "workflow":
        async with env.factory() as db:
            await create_task(
                db,
                task_id="fence-task",
                created_by=OWNER,
                agent_id="fence-agent",
                title="Fence contract",
                status="ready",
            )
            await db.commit()
        store = TaskExecutionStore(
            env.factory,
            owner_id="controller:boot",
            max_active_global=1,
            max_active_per_agent=1,
        )
        claim = await store.claim_ready()
        assert claim is not None
        return TaskExecutionFence(store, claim, asyncio.Event()), conversation_id
    if kind == "hook":
        lease = await env.leases.acquire("artifact-fence-hook", "controller:boot", ttl_seconds=60)
        assert lease is not None
        return (
            HookExecutionFence(env.leases, lease, run_id="fence-hook", session_factory=env.factory),
            conversation_id,
        )
    admitted = await env.turns.admit(
        conversation_id=conversation_id,
        session_id=None,
        agent_id="fence-agent",
        user_id=OWNER,
        idempotency_scope="artifact-fence",
        idempotency_key=uuid.uuid4().hex,
        payload={"schema_version": 1, "content": "", "attachments": []},
    )
    lease = await env.leases.acquire(
        conversation_lease_key(conversation_id), "controller:boot", ttl_seconds=60
    )
    assert lease is not None
    request_id = admitted.request.request_id
    assert await env.turns.claim(
        request_id, lease=lease, controller_id="controller", incarnation_id="boot"
    )
    assert await env.turns.mark_running(request_id, lease=lease)
    parent = DirectTurnExecutionFence(env.turns, request_id, lease)
    return (BackgroundDirectTurnFence(parent) if kind == "background" else parent), conversation_id


async def _write(env, fence, conversation_id: str, supersedes: str | None = None):
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=ARTIFACT_WRITE_TOOL))
    arguments = {"filename": "checkpoint.json", "content": '{"revision": 1}'}
    if supersedes is not None:
        arguments.update(supersedes=supersedes, content='{"revision": 2}')
    return await ToolRouter(
        guardrails=None, session_factory=env.factory, artifact_store=env.artifacts
    ).execute(
        ToolCall(call_id=uuid.uuid4().hex, name="artifact_write", arguments=arguments),
        SessionModel(
            session_id="fence-session",
            conversation_id=conversation_id,
            user_email=OWNER,
            agent_id="fence-agent",
        ),
        AgentDefinition(agent_id="fence-agent", owner_email=OWNER, name="Fence agent"),
        registry,
        executor=None,
        transaction_fence=fence.assert_current,
    )


async def _assert_only_original(env, artifact_id: str) -> None:
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ArtifactRecordRow)) == 1
        row = await db.get(ArtifactRecordRow, artifact_id)
        assert row is not None and row.revision == 1
    _assert_stored_files(env, {artifact_id})


def _assert_stored_files(env, artifact_ids: set[str]) -> None:
    assert {(p.parent.name, p.name) for p in env.root.rglob("*") if p.is_file()} == {
        (artifact_id, filename)
        for artifact_id in artifact_ids
        for filename in ("checkpoint.json", ".metadata.json")
    }


def _payload(result: ToolResult) -> dict:
    """Decode JSON within the router's normal untrusted-result wrapper."""
    assert result.output.startswith('<tool_result trust="untrusted">\n')
    return json.loads(result.output.splitlines()[1])


@pytest.mark.parametrize("kind", ["direct", "managed", "background", "workflow", "hook"])
async def test_artifact_mutation_accepts_current_runtime_fence(env, kind: str) -> None:
    fence, conversation_id = await _fence(env, kind)
    await fence.assert_current()
    first = await _write(env, fence, conversation_id)
    assert not first.is_error, first.output
    original = _payload(first)
    second = await _write(env, fence, conversation_id, original["artifact_id"])
    assert not second.is_error, second.output
    revised = _payload(second)
    assert revised["lineage_id"] == original["artifact_id"]
    assert revised["revision"] == 2
    stale_cas = await _write(env, fence, conversation_id, original["artifact_id"])
    assert stale_cas.is_error
    assert _payload(stale_cas)["reason"] == "supersedes_not_latest"
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ArtifactRecordRow)) == 2
    _assert_stored_files(env, {original["artifact_id"], revised["artifact_id"]})


@pytest.mark.parametrize("kind", ["direct", "managed", "background", "workflow", "hook"])
async def test_artifact_mutation_rejects_expired_runtime_fence(env, kind: str) -> None:
    fence, conversation_id = await _fence(env, kind)
    first = await _write(env, fence, conversation_id)
    assert not first.is_error, first.output
    original = _payload(first)
    async with env.factory() as db:
        await db.execute(
            update(CoordinationLeaseRow).values(
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    error = (
        asyncio.CancelledError
        if kind == "background"
        else StaleTaskExecutionOwner
        if kind in {"workflow", "hook"}
        else StaleDirectTurnOwner
    )
    with pytest.raises(error):
        await _write(env, fence, conversation_id, original["artifact_id"])
    await _assert_only_original(env, original["artifact_id"])


@pytest.mark.parametrize("kind", ["direct", "managed"])
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_artifact_mutation_rejects_terminal_direct_turn(env, kind: str, status: str) -> None:
    fence, conversation_id = await _fence(env, kind)
    first = await _write(env, fence, conversation_id)
    assert not first.is_error, first.output
    original = _payload(first)
    assert await env.turns.mark_terminal(
        fence.request_id, lease=fence.lease, status=DirectTurnStatus(status)
    )
    with pytest.raises(StaleDirectTurnOwner):
        await _write(env, fence, conversation_id, original["artifact_id"])
    await _assert_only_original(env, original["artifact_id"])


@pytest.mark.parametrize("kind", ["direct", "background"])
async def test_direct_fence_uses_caller_transaction_without_owning_it(env, kind: str) -> None:
    fence, _ = await _fence(env, kind)
    parent = fence.parent if kind == "background" else fence
    factory = env.turns._session_factory
    env.turns._session_factory = Mock(side_effect=AssertionError("must use caller session"))
    try:
        async with factory() as db:
            await fence.assert_current(db)
            await db.execute(
                update(DirectTurnRequestRow)
                .where(DirectTurnRequestRow.request_id == parent.request_id)
                .values(fencing_token=parent.lease.fencing_token + 1)
            )
            with pytest.raises(
                asyncio.CancelledError if kind == "background" else StaleDirectTurnOwner
            ):
                await fence.assert_current(db)
            assert db.in_transaction()
            await db.rollback()
            await fence.assert_current(db)
    finally:
        env.turns._session_factory = factory


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_background_artifact_write_preserves_parent_completion_policy(
    env, outcome: str
) -> None:
    fence, conversation_id = await _fence(env, "background")
    first = await _write(env, fence, conversation_id)
    assert not first.is_error, first.output
    original = _payload(first)
    assert await env.turns.mark_terminal(
        fence.parent.request_id, lease=fence.parent.lease, status=DirectTurnStatus(outcome)
    )
    assert await env.leases.release(fence.parent.lease)
    if outcome == "completed":
        revised = await _write(env, fence, conversation_id, original["artifact_id"])
        assert not revised.is_error, revised.output
        assert _payload(revised)["revision"] == 2
    else:
        with pytest.raises(asyncio.CancelledError):
            await _write(env, fence, conversation_id, original["artifact_id"])
        await _assert_only_original(env, original["artifact_id"])


async def test_background_fence_refreshes_cached_parent_cancellation(env) -> None:
    fence, _ = await _fence(env, "background")
    assert await env.turns.mark_terminal(
        fence.parent.request_id, lease=fence.parent.lease, status=DirectTurnStatus.COMPLETED
    )
    assert await env.leases.release(fence.parent.lease)
    async with env.factory() as db:
        cached = await db.scalar(
            select(DirectTurnRequestRow).where(
                DirectTurnRequestRow.request_id == fence.parent.request_id
            )
        )
        assert cached is not None and cached.cancel_requested_at is None
        await db.execute(
            update(DirectTurnRequestRow)
            .where(DirectTurnRequestRow.request_id == fence.parent.request_id)
            .values(cancel_requested_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        assert cached.cancel_requested_at is None
        with pytest.raises(asyncio.CancelledError):
            await fence.assert_current(db)
        await db.rollback()
        await fence.assert_current(db)
