"""Atomic runtime-command receipts and crash recovery."""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.api.chat_v2 import routes
from cognis.api.chat_v2.schemas import CommandV2Request
from cognis.core.commands import CommandDispatcher
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, ChatClientTransactionRow, Conversation, Session, User


@pytest.fixture
async def runtime_app(tmp_path, monkeypatch):
    postgres_url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
    admin = None
    if postgres_url:
        schema = f"runtime_{uuid.uuid4().hex}"
        admin = create_async_engine(postgres_url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            postgres_url, connect_args={"server_settings": {"search_path": schema}}
        )
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/commands.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add(User(email="runtime@test.local"))
        await db.commit()
        db.add(
            Agent(
                agent_id="runtime-agent",
                owner_email="runtime@test.local",
                name="Runtime",
                agent_profiles={"review": {"profile_id": "review", "model": "review-model"}},
            )
        )
        await db.commit()
        db.add(
            Conversation(
                conversation_id="runtime-conversation",
                user_email="runtime@test.local",
                agent_id="runtime-agent",
                context_type="web",
                context_data={},
                memory_labels={},
            )
        )
        await db.commit()
        db.add(
            Session(
                session_id="runtime-session",
                activity_scope_id="runtime-session",
                conversation_id="runtime-conversation",
                user_email="runtime@test.local",
                agent_id="runtime-agent",
                model_override="old-model",
                reasoning_effort_override="high",
                fast_mode_override=True,
            )
        )
        await db.commit()
        conversation = await db.get(Conversation, "runtime-conversation")
        conversation.active_session_id = "runtime-session"
        await db.commit()
    cache = Mock()
    providers = SimpleNamespace(
        llm=SimpleNamespace(
            list_model_ids=AsyncMock(return_value=["first-model", "second-model"]),
        )
    )
    lock = asyncio.Lock()
    dispatcher = CommandDispatcher(
        session_factory=factory,
        session_manager=None,
        session_cache=cache,
        compaction_strategy=None,
        providers=providers,
        pause_waiter=None,
        notification_service=None,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=factory,
                session_cache=cache,
                command_dispatcher=dispatcher,
                providers=providers,
                turn_scheduler=SimpleNamespace(turn_admission_lock=lambda _: lock),
            )
        )
    )

    async def authorize(*_):
        async with factory() as db:
            conversation = await db.get(Conversation, "runtime-conversation")
        return SimpleNamespace(email="runtime@test.local"), conversation

    monkeypatch.setattr(routes, "_require_mutable_conversation", authorize)
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "persist_command_system_notice", notice)

    async def send(content="/model first-model", txn="runtime-txn"):
        return await routes.chat_v2_execute_command(
            request, "runtime-conversation", txn, CommandV2Request(content=content)
        )

    yield SimpleNamespace(
        send=send,
        factory=factory,
        cache=cache,
        notice=notice,
        scheduler=request.app.state.turn_scheduler,
    )
    await engine.dispose()
    if admin is not None:
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.parametrize("content", ["/model first-model", "/profile review"])
async def test_receipt_failure_rolls_back_mutation_and_retry_applies_once(
    runtime_app, monkeypatch, content
):
    original = routes.complete_chat_client_transaction
    monkeypatch.setattr(
        routes,
        "complete_chat_client_transaction",
        AsyncMock(side_effect=RuntimeError("receipt failure")),
    )
    with pytest.raises(RuntimeError, match="receipt failure"):
        await runtime_app.send(content)
    async with runtime_app.factory() as db:
        session = await db.get(Session, "runtime-session")
        receipt = (await db.execute(select(ChatClientTransactionRow))).scalar_one()
        conversation = await db.get(Conversation, "runtime-conversation")
        assert session.model_override == "old-model"
        assert session.runtime_override_revision == 0
        assert conversation.agent_profile_id is None
        assert receipt.status == "pending"
    runtime_app.cache.set_model_override.assert_not_called()
    monkeypatch.setattr(routes, "complete_chat_client_transaction", original)
    response = await runtime_app.send(content)
    assert response.data["runtime_selection"]["revision"] == 1
    if content.startswith("/profile"):
        async with runtime_app.factory() as db:
            session = await db.get(Session, "runtime-session")
            conversation = await db.get(Conversation, "runtime-conversation")
            assert conversation.agent_profile_id == session.agent_profile_id == "review"
            assert session.model_override is None
            assert session.reasoning_effort_override is None
            assert session.fast_mode_override is None


async def test_duplicate_does_not_reapply_over_newer_selection(runtime_app):
    first = await runtime_app.send()
    await runtime_app.send("/model second-model", "second-txn")
    duplicate = await runtime_app.send()
    assert duplicate.status == "duplicate"
    assert duplicate.data == first.data
    async with runtime_app.factory() as db:
        session = await db.get(Session, "runtime-session")
        assert session.model_override == "second-model"
        assert session.runtime_override_revision == 2


async def test_concurrent_duplicate_executes_once_and_conflict_is_rejected(runtime_app):
    responses = await asyncio.gather(runtime_app.send(), runtime_app.send())
    assert sorted(response.status for response in responses) == ["completed", "duplicate"]
    with pytest.raises(HTTPException) as error:
        await runtime_app.send("/model second-model")
    assert error.value.status_code == 409
    async with runtime_app.factory() as db:
        session = await db.get(Session, "runtime-session")
        assert session.runtime_override_revision == 1


async def test_failed_notice_is_retried_without_mutation(runtime_app):
    runtime_app.notice.return_value = False
    response = await runtime_app.send()
    assert response.data["notice_persisted"] is False
    notice_id = response.data["notice_id"]
    runtime_app.notice.return_value = True
    response = await runtime_app.send()
    assert response.status == "duplicate"
    assert response.data["notice_id"] == notice_id
    assert "notice_persisted" not in response.data
    assert runtime_app.notice.await_count == 2
    assert runtime_app.notice.call_args.kwargs["result"].data["notice_id"] == notice_id


@pytest.mark.skipif(not os.environ.get("COGNIS_TEST_POSTGRES_URL"), reason="PostgreSQL required")
async def test_duplicate_controllers_serialize_on_receipt_row(runtime_app):
    runtime_app.scheduler.turn_admission_lock = lambda _: asyncio.Lock()
    responses = await asyncio.gather(runtime_app.send(), runtime_app.send())
    assert sorted(response.status for response in responses) == ["completed", "duplicate"]
    async with runtime_app.factory() as db:
        session = await db.get(Session, "runtime-session")
        assert session.runtime_override_revision == 1


async def test_rebase_releases_database_before_intaris_and_copies_latest_runtime(runtime_app):
    from cognis.core.session import SessionManager, _to_session_model

    async with runtime_app.factory() as db:
        source = _to_session_model(await db.get(Session, "runtime-session"))

    async def create_external_session(**_):
        await asyncio.wait_for(runtime_app.send(), timeout=10)

    manager = SessionManager(
        runtime_app.factory,
        SimpleNamespace(guardrails=SimpleNamespace(create_session=create_external_session)),
        runtime_app.cache,
    )
    rebased = await manager._create_history_rebase_session(
        current_session=source, intention="Undo last turn"
    )
    assert rebased.model_override == "first-model"
    assert rebased.runtime_override_revision == 1
