"""Accepted-message reconciliation uses durable metadata, never input content."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.api.chat_v2 import routes
from cognis.api.common import AuthenticatedUser
from cognis.models.session import EventReadResult
from cognis.store import queries
from cognis.store.chat_admissions import read_message_admission
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, ChatClientTransactionRow, DirectTurnRequestRow


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'admissions.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        for email in ("owner@test.local", "other@test.local"):
            await queries.create_user(session, email=email, name="User", password_hash="test-hash")
        await queries.create_agent(
            session, agent_id="agent", owner_email="owner@test.local", name="Agent", status="active"
        )
        for conversation in ("conversation-a", "conversation-b"):
            await queries.create_conversation(
                session,
                conversation_id=conversation,
                agent_id="agent",
                user_email="owner@test.local",
                context_type="web",
            )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def seed(
    factory: async_sessionmaker[AsyncSession],
    txn: str,
    order: int,
    *,
    status: str = "failed",
    started: bool = False,
    conversation: str = "conversation-a",
    principal: str = "owner@test.local",
    operation: str = "send_message",
    receipt_status: str = "accepted",
) -> None:
    async with factory() as session:
        session.add(
            DirectTurnRequestRow(
                request_id=f"dtr_{conversation}_{principal}_{txn}",
                conversation_id=conversation,
                agent_id="agent",
                user_id=principal,
                turn_id=f"turn_{txn}",
                idempotency_scope=f"chat-v2:{conversation}:{principal}",
                idempotency_key=txn,
                admission_hash="test-admission-hash",
                payload_hash="test-payload-hash",
                status=status,
                admission_order=order,
                started_at=datetime.now(UTC) if started else None,
                payload_version=1,
                payload={"content": "must-not-be-read-or-returned"},
            )
        )
        session.add(
            ChatClientTransactionRow(
                conversation_id=conversation,
                principal_id=principal,
                client_txn_id=txn,
                operation=operation,
                payload_hash="test-hash",
                status=receipt_status,
                result={"client_message_id": f"message-{txn}", "status": receipt_status},
            )
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_status", ["accepted", "queued"])
@pytest.mark.parametrize("successor_receipt_status", ["accepted", "queued"])
async def test_failed_before_execution_has_exact_later_receipt_not_a_timestamp_guess(
    factory, receipt_status, successor_receipt_status
):
    await seed(factory, "failed", 22124, receipt_status=receipt_status)
    await seed(factory, "not-started", 22125, status="queued")
    await seed(
        factory,
        "later",
        22129,
        status="completed",
        started=True,
        receipt_status=successor_receipt_status,
    )
    await seed(factory, "latest", 22130, status="running", started=True)
    outcome = await read_message_admission(
        factory,
        conversation_id="conversation-a",
        principal_id="owner@test.local",
        client_txn_id="failed",
    )
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.started is False
    assert outcome.admission_order == 22124
    assert outcome.before_client_message_id == "message-later"
    assert "must-not-be-read-or-returned" not in repr(outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_status", ["pending", "failed", "completed"])
async def test_nonaccepted_receipts_are_unknown(factory, receipt_status):
    await seed(factory, "unaccepted", 1, receipt_status=receipt_status)
    assert (
        await read_message_admission(
            factory,
            conversation_id="conversation-a",
            principal_id="owner@test.local",
            client_txn_id="unaccepted",
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "started,status", [(True, "failed"), (False, "running"), (False, "completed")]
)
async def test_only_terminal_unstarted_failures_get_an_admission_boundary(factory, started, status):
    await seed(factory, "first", 1, started=started, status=status)
    await seed(factory, "later", 2, started=True)
    outcome = await read_message_admission(
        factory,
        conversation_id="conversation-a",
        principal_id="owner@test.local",
        client_txn_id="first",
    )
    assert outcome is not None
    assert outcome.before_client_message_id is None


@pytest.mark.asyncio
async def test_lookup_and_successor_are_bound_to_conversation_principal_and_operation(factory):
    await seed(factory, "same", 1)
    await seed(factory, "other-conversation", 2, conversation="conversation-b", started=True)
    await seed(factory, "other-principal", 3, principal="other@test.local", started=True)
    await seed(factory, "other-operation", 4, operation="cancel_turn", started=True)
    outcome = await read_message_admission(
        factory,
        conversation_id="conversation-a",
        principal_id="owner@test.local",
        client_txn_id="same",
    )
    assert outcome is not None
    assert outcome.before_client_message_id is None
    for conversation, principal in [
        ("conversation-b", "owner@test.local"),
        ("conversation-a", "other@test.local"),
    ]:
        assert (
            await read_message_admission(
                factory,
                conversation_id=conversation,
                principal_id=principal,
                client_txn_id="same",
            )
            is None
        )


@pytest.mark.asyncio
async def test_admission_route_authorizes_before_lookup(monkeypatch):
    request = SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="owner@test.local", role="user")),
        app=SimpleNamespace(state=SimpleNamespace(session_factory=None)),
    )
    authorize = AsyncMock(side_effect=HTTPException(403, "Forbidden"))
    lookup = AsyncMock()
    monkeypatch.setattr(routes, "_conversation_read_authority", authorize)
    monkeypatch.setattr(routes, "read_message_admission", lookup)
    with pytest.raises(HTTPException) as error:
        await routes.message_admission("conversation-a", "transaction", request)
    assert error.value.status_code == 403
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_admission_route_has_no_event_store_dependency_and_returns_no_input(
    factory,
):
    await seed(factory, "failed", 1)
    request = SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="owner@test.local", role="user")),
        app=SimpleNamespace(state=SimpleNamespace(session_factory=factory)),
    )
    result = await routes.message_admission("conversation-a", "failed", request)
    assert result.client_message_id == "message-failed"
    assert result.status == "failed"
    assert result.before_client_message_id is None
    assert "must-not-be-read-or-returned" not in result.model_dump_json()
    with pytest.raises(HTTPException) as error:
        await routes.message_admission("conversation-a", "absent", request)
    assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_admission_route_does_not_expose_another_users_private_receipt(factory, role):
    await seed(factory, "failed", 1)
    request = SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="other@test.local", role=role)),
        app=SimpleNamespace(state=SimpleNamespace(session_factory=factory)),
    )
    with pytest.raises(HTTPException) as error:
        await routes.message_admission("conversation-a", "failed", request)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_legacy_client_id_lookup_returns_exact_transaction_and_visible_successor(factory):
    await seed(factory, "old-input", 1)
    await seed(factory, "off-window", 2, started=True, status="completed")
    await seed(factory, "visible", 3, started=True, status="completed")
    outcome = await read_message_admission(
        factory,
        conversation_id="conversation-a",
        principal_id="owner@test.local",
        client_message_id="message-old-input",
        canonical_client_message_ids=("message-visible",),
    )
    assert outcome is not None
    assert outcome.client_txn_id == "old-input"
    assert outcome.client_message_id == "message-old-input"
    assert outcome.before_client_message_id == "message-visible"
    for conversation, principal in [
        ("conversation-b", "owner@test.local"),
        ("conversation-a", "other@test.local"),
    ]:
        assert (
            await read_message_admission(
                factory,
                conversation_id=conversation,
                principal_id=principal,
                client_message_id="message-old-input",
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_client_id_route_is_read_only_and_owner_scoped(factory, role):
    await seed(factory, "old-input", 1, started=True, status="completed")
    request = SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="owner@test.local", role=role)),
        app=SimpleNamespace(state=SimpleNamespace(session_factory=factory)),
    )
    result = await routes.message_admission_by_client(
        "conversation-a", "message-old-input", request
    )
    assert result.client_txn_id == "old-input"
    assert result.status == "completed"
    assert "must-not-be-read-or-returned" not in result.model_dump_json()
    request.state.user = AuthenticatedUser(email="other@test.local", role=role)
    with pytest.raises(HTTPException) as error:
        await routes.message_admission_by_client("conversation-a", "message-old-input", request)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_ambiguous_client_identity_does_not_retire_a_newer_request(factory):
    await seed(factory, "finished", 1, started=True, status="completed")
    await seed(factory, "newer", 2, status="queued")
    async with factory() as session:
        await session.execute(
            update(ChatClientTransactionRow)
            .where(ChatClientTransactionRow.client_txn_id == "newer")
            .values(result={"client_message_id": "message-finished", "status": "accepted"})
        )
        await session.commit()
    assert (
        await read_message_admission(
            factory,
            conversation_id="conversation-a",
            principal_id="owner@test.local",
            client_message_id="message-finished",
        )
        is None
    )
    exact = await read_message_admission(
        factory,
        conversation_id="conversation-a",
        principal_id="owner@test.local",
        client_txn_id="newer",
    )
    assert exact is not None
    assert exact.status == "queued"


@pytest.mark.asyncio
async def test_registered_receipt_routes_validate_the_visible_anchor_query(factory):
    await seed(factory, "old-input", 1)
    await seed(factory, "off-window", 2, started=True, status="completed")
    await seed(factory, "visible", 3, started=True, status="completed")
    app = FastAPI()
    app.include_router(routes.router)
    app.state.session_factory = factory

    @app.middleware("http")
    async def fixture_identity(request, call_next):
        request.state.user = AuthenticatedUser(email="owner@test.local", role="user")
        return await call_next(request)

    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://admissions.test"
    ) as client:
        for path in [
            "/api/v1/chat/v2/conversations/conversation-a/messages/old-input/admission",
            "/api/v1/chat/v2/conversations/conversation-a/message-admissions/message-old-input",
        ]:
            response = await client.get(
                path, params={"canonical_client_message_id": "message-visible"}
            )
            assert response.status_code == 200
            assert response.json()["client_txn_id"] == "old-input"
            assert response.json()["before_client_message_id"] == "message-visible"
            oversized = await client.get(
                path, params=[("canonical_client_message_id", str(index)) for index in range(101)]
            )
            assert oversized.status_code == 422


@pytest.fixture
async def persisted_input_app(factory):
    await seed(factory, "persisted", 1, started=True, status="cancelled")
    async with factory() as db:
        old = await queries.create_session(
            db,
            "conversation-a",
            "owner@test.local",
            "agent",
            session_id="source-session",
            intaris_session_id="intaris-source",
        )
        active = await queries.create_session(
            db,
            "conversation-a",
            "owner@test.local",
            "agent",
            session_id="active-session",
            previous_session_id=old.session_id,
        )
        conversation = await queries.get_conversation(db, "conversation-a")
        conversation.active_session_id = active.session_id
        request = await db.get(DirectTurnRequestRow, 1)
        request.session_id = old.session_id
        await db.commit()
    app = FastAPI()
    app.include_router(routes.router)
    app.state.session_factory = factory
    app.state.registry = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(owner_email="owner@test.local"))
    )
    app.state.providers = SimpleNamespace(
        guardrails=SimpleNamespace(
            read_events=AsyncMock(
                return_value=EventReadResult(
                    events=[],
                    last_seq=0,
                    has_more=False,
                )
            )
        )
    )

    @app.middleware("http")
    async def fixture_identity(request, call_next):
        request.state.user = AuthenticatedUser(email="owner@test.local", role="user")
        return await call_next(request)

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "failed"])
@pytest.mark.parametrize("by_client", [False, True])
async def test_receipt_confirms_persisted_input_outside_the_window(
    factory,
    persisted_input_app,
    status,
    by_client,
):
    app = persisted_input_app
    async with factory() as db:
        request = await db.get(DirectTurnRequestRow, 1)
        request.status = status
        await db.commit()
    event = {
        "seq": 282,
        "type": "user_message",
        "data": {"client_message_id": "message-persisted", "turn_id": "turn_persisted"},
    }
    app.state.providers.guardrails.read_events.return_value = EventReadResult(
        events=[event],
        last_seq=90000,
        has_more=False,
    )
    path = "message-admissions/message-persisted" if by_client else "messages/persisted/admission"
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://admissions.test",
    ) as client:
        response = await client.get(f"/api/v1/chat/v2/conversations/conversation-a/{path}")
    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["input_persisted"] is True
    assert "session_id" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "wrong-client",
        "wrong-turn",
        "assistant",
        "synthetic",
        "wrong-stream",
        "empty",
        "stalled",
        "unavailable",
        "budget",
        "revoked-source",
    ],
)
async def test_receipt_preserves_execution_metadata_without_canonical_confirmation(
    persisted_input_app,
    case,
):
    app = persisted_input_app
    if case == "revoked-source":
        app.state.registry.get.return_value = None
    data = {"client_message_id": "message-persisted", "turn_id": "turn_persisted"}
    event = {"seq": 282, "type": "user_message", "data": data}
    if case == "wrong-client":
        data["client_message_id"] = "another-input"
    elif case == "wrong-turn":
        data["turn_id"] = "another-turn"
    elif case == "assistant":
        event["type"] = "assistant_message"
    elif case == "synthetic":
        data["synthetic"] = True
    elif case == "wrong-stream":
        event["session_id"] = "unrelated-stream"
    if case == "unavailable":
        app.state.providers.guardrails.read_events.side_effect = TimeoutError()
    elif case == "budget":

        async def paged(_session_id, *, after_seq, **_kwargs):
            seq = after_seq + 1
            return EventReadResult(
                events=[
                    {
                        "seq": seq,
                        "type": "user_message",
                        "data": {
                            "client_message_id": "message-persisted" if seq == 9 else "other",
                            "turn_id": "turn_persisted",
                        },
                    }
                ],
                last_seq=90000,
                has_more=True,
            )

        app.state.providers.guardrails.read_events.side_effect = paged
    else:
        app.state.providers.guardrails.read_events.return_value = EventReadResult(
            events=[] if case in {"empty", "stalled"} else [event],
            last_seq=90000,
            has_more=case == "stalled",
        )
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://admissions.test",
    ) as client:
        response = await client.get(
            "/api/v1/chat/v2/conversations/conversation-a/messages/persisted/admission"
        )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["started"] is True
    assert response.json()["input_persisted"] is False


@pytest.mark.asyncio
async def test_confirmation_never_reads_a_child_stream(factory, persisted_input_app):
    app = persisted_input_app
    async with factory() as db:
        child = await queries.create_session(
            db,
            "conversation-a",
            "owner@test.local",
            "agent",
            session_id="child-session",
            intaris_session_id="intaris-child",
            parent_session_id="source-session",
        )
        request = await db.get(DirectTurnRequestRow, 1)
        request.session_id = child.session_id
        conversation = await queries.get_conversation(db, "conversation-a")
        conversation.active_session_id = "active-session"
        await db.commit()

    async def read(session_id, **_kwargs):
        return EventReadResult(
            events=[
                {
                    "seq": 282,
                    "type": "user_message",
                    "data": {"client_message_id": "message-persisted", "turn_id": "turn_persisted"},
                }
            ]
            if session_id == "intaris-child"
            else [],
            last_seq=282,
            has_more=False,
        )

    app.state.providers.guardrails.read_events.side_effect = read
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://admissions.test",
    ) as client:
        response = await client.get(
            "/api/v1/chat/v2/conversations/conversation-a/messages/persisted/admission"
        )
    assert response.status_code == 200
    assert response.json()["input_persisted"] is False


@pytest.mark.asyncio
async def test_confirmation_advances_by_filtered_page_events_not_stream_head(persisted_input_app):
    app = persisted_input_app

    async def paged(_session_id, *, after_seq, **_kwargs):
        return EventReadResult(
            events=[
                {
                    "seq": 200 if after_seq == 0 else 324,
                    "type": "user_message",
                    "data": {
                        "client_message_id": "other" if after_seq == 0 else "message-persisted",
                        "turn_id": "turn_persisted",
                    },
                }
            ],
            last_seq=90000,
            has_more=after_seq == 0,
        )

    app.state.providers.guardrails.read_events.side_effect = paged
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://admissions.test",
    ) as client:
        response = await client.get(
            "/api/v1/chat/v2/conversations/conversation-a/messages/persisted/admission"
        )
    assert response.status_code == 200
    assert response.json()["input_persisted"] is True


@pytest.mark.asyncio
async def test_confirmation_does_not_swallow_request_cancellation(persisted_input_app):
    app = persisted_input_app
    read_started = asyncio.Event()

    async def read_events(*_args, **_kwargs):
        read_started.set()
        await asyncio.Event().wait()

    app.state.providers.guardrails.read_events.side_effect = read_events
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://admissions.test",
    ) as client:
        request = asyncio.create_task(
            client.get("/api/v1/chat/v2/conversations/conversation-a/messages/persisted/admission")
        )
        await asyncio.wait_for(read_started.wait(), timeout=5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
