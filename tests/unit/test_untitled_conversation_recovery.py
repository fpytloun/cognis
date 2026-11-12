"""DB, provider-wire, recovery and race coverage for Cognis-owned titles."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import event, update

from cognis.bootstrap import ensure_jwt_keypair
from cognis.config import load_config
from cognis.core.events import EventType
from cognis.core.invariants import UntitledConversationReconciler
from cognis.core.maintenance_lease import run_periodic_maintenance
from cognis.core.title_policy import sync_intaris_title
from cognis.models.session import ConversationContext, ConversationModel, EventReadResult
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.backends.guardrails.null import NoGuardrailsProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, Session, User
from cognis.store.queries import initialize_conversation_title, update_conversation


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def database(request, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/titles.db"
    if request.param == "postgresql":
        url = os.environ.get("COGNIS_TITLE_TEST_DATABASE_URL")
        if not url:
            pytest.skip("COGNIS_TITLE_TEST_DATABASE_URL is not configured")
    engine = create_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all([User(email="reader@example.com"), User(email="owner@example.com")])
        await db.flush()
        db.add(Agent(agent_id="shared-agent", owner_email="owner@example.com", name="Shared"))
        await db.commit()
    try:
        yield engine, factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def seed(factory, cid="conversation", **values):
    async with factory() as db:
        db.add(
            Conversation(
                conversation_id=cid,
                user_email="reader@example.com",
                agent_id="shared-agent",
                context_type="web",
                **values,
            )
        )
        await db.flush()
        db.add(
            Session(
                session_id=f"{cid}-session",
                intaris_session_id=f"{cid}-intaris",
                conversation_id=cid,
                user_email="reader@example.com",
                agent_id="shared-agent",
                status="completed",
                started_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        await db.commit()


async def visible(factory, cid="conversation"):
    async with factory() as db:
        row = await db.get(Conversation, cid)
        return row.title, row.title_source


def user_event(text="Oprav přihlašování", **data):
    return {
        "type": "user_message",
        "data": {
            "role": "user",
            "source": "user_input",
            "prompt_visibility": "user_visible",
            "prompt_provenance": {"kind": "user_authored"},
            "content": text,
            **data,
        },
    }


def page(*events):
    return EventReadResult(events=list(events), last_seq=len(events), has_more=False)


def completion(text="Oprava přihlašování"):
    return {"choices": [{"message": {"content": text}}]}


def worker(factory, guardrails=None, llm=None, *, batch_size=4):
    guardrails = guardrails or SimpleNamespace(
        read_events=AsyncMock(return_value=page(user_event())),
        get_session=AsyncMock(side_effect=AssertionError("No Intaris title adoption")),
    )
    llm = llm or SimpleNamespace(generate=AsyncMock(return_value=completion()))
    bus = SimpleNamespace(publish=AsyncMock())
    return UntitledConversationReconciler(factory, guardrails, bus, llm, batch_size=batch_size), bus


async def expire_retry(factory):
    async with factory() as db:
        await db.execute(
            update(Conversation).values(
                title_generation_retry_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_failure_cooldown_then_restart_recovery_without_another_turn(database):
    _, factory = database
    await seed(factory)
    llm = SimpleNamespace(generate=AsyncMock(side_effect=[RuntimeError("outage"), completion()]))
    reconciler, bus = worker(factory, llm=llm)
    assert await reconciler.reconcile() == 0
    assert await reconciler.reconcile() == 0
    restarted, bus = worker(factory, llm=llm)
    assert await restarted.reconcile() == 0
    assert llm.generate.await_count == 1
    await expire_retry(factory)
    assert await restarted.reconcile() == 1
    assert await visible(factory) == ("Oprava přihlašování", "cognis")
    assert await restarted.reconcile() == 0
    assert llm.generate.await_count == 2
    published = bus.publish.call_args.args[0]
    assert published.type == EventType.CONVERSATION_UPDATED
    assert published.data == {"conversation_id": "conversation", "title": "Oprava přihlašování"}
    kwargs = llm.generate.call_args.kwargs
    assert kwargs["task_type"] == "auxiliary_generation"
    assert "model" not in kwargs and "provider_id" not in kwargs
    assert kwargs["acting_user_email"] == "reader@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title,source,data,status",
    [
        ("Pinned", "manual", {}, "active"),
        ("", "manual", {}, "active"),
        ("Channel", "channel_seed", {}, "active"),
        ("Existing", "intaris", {}, "active"),
        ("Existing", "cognis", {}, "active"),
        ("Existing", "unset", {}, "active"),
        ("Task step", "manual", {}, "active"),
        ("Managed", "user", {}, "active"),
        ("Contact", "app_contact", {}, "active"),
        (None, "unset", {"kind": "agent_direct"}, "active"),
        (None, "unset", {}, "deleted"),
    ],
)
async def test_supplied_or_generated_titles_are_never_refreshed(
    database, title, source, data, status
):
    _, factory = database
    await seed(factory, title=title, title_source=source, context_data=data, status=status)
    llm = SimpleNamespace(generate=AsyncMock())
    reconciler, bus = worker(factory, llm=llm)
    assert await reconciler.reconcile() == 0
    llm.generate.assert_not_awaited()
    bus.publish.assert_not_awaited()
    assert await visible(factory) == (title, source)


@pytest.mark.asyncio
async def test_manual_rename_during_inference_wins_without_db_connection_held(database):
    engine, factory = database
    await seed(factory)
    checked_out = 0

    def checkout(*_):
        nonlocal checked_out
        checked_out += 1

    def checkin(*_):
        nonlocal checked_out
        checked_out -= 1

    event.listen(engine.sync_engine, "checkout", checkout)
    event.listen(engine.sync_engine, "checkin", checkin)

    async def generate(*_, **__):
        assert checked_out == 0
        async with factory() as db:
            await update_conversation(
                db, "conversation", title="Manual winner", title_source="manual"
            )
            await db.commit()
        return completion("Stale generated title")

    try:
        reconciler, bus = worker(
            factory, llm=SimpleNamespace(generate=AsyncMock(side_effect=generate))
        )
        assert await reconciler.reconcile() == 0
        assert await visible(factory) == ("Manual winner", "manual")
        bus.publish.assert_not_awaited()
    finally:
        event.remove(engine.sync_engine, "checkout", checkout)
        event.remove(engine.sync_engine, "checkin", checkin)


@pytest.mark.asyncio
async def test_concurrent_workers_claim_only_one_model_request(database):
    _, factory = database
    await seed(factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*_, **__):
        entered.set()
        await release.wait()
        return completion()

    first, first_bus = worker(
        factory, llm=SimpleNamespace(generate=AsyncMock(side_effect=generate))
    )
    task = asyncio.create_task(first.reconcile())
    await asyncio.wait_for(entered.wait(), 2)
    other_llm = SimpleNamespace(generate=AsyncMock())
    second, second_bus = worker(factory, llm=other_llm)
    try:
        assert await second.reconcile() == 0
        other_llm.generate.assert_not_awaited()
    finally:
        release.set()
        assert await asyncio.wait_for(task, 2) == 1
    assert first_bus.publish.await_count + second_bus.publish.await_count == 1


@pytest.mark.asyncio
async def test_rotation_empty_successors_do_not_strand_persisted_user_evidence(database):
    _, factory = database
    await seed(factory)
    async with factory() as db:
        for n in range(3):
            db.add(
                Session(
                    session_id=f"rotation-{n}",
                    intaris_session_id=f"rotation-{n}-intaris",
                    conversation_id="conversation",
                    user_email="reader@example.com",
                    agent_id="shared-agent",
                    status="completed",
                    started_at=datetime.now(UTC) + timedelta(seconds=n),
                )
            )
        await db.commit()
    seen = []

    async def read(sid, **_):
        seen.append(sid)
        return page(user_event()) if sid == "conversation-intaris" else page()

    reconciler, bus = worker(
        factory, SimpleNamespace(read_events=AsyncMock(side_effect=read)), batch_size=1
    )
    for _ in range(3):
        assert await reconciler.reconcile() == 0
    assert await reconciler.reconcile() == 1
    assert seen == [
        "rotation-2-intaris",
        "rotation-1-intaris",
        "rotation-0-intaris",
        "conversation-intaris",
    ]
    assert await visible(factory) == ("Oprava přihlašování", "cognis")
    bus.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_intaris_suggestion_racing_generation_never_initializes_or_replaces_title(database):
    _, factory = database
    await seed(factory)
    stale = ConversationModel(
        conversation_id="conversation",
        user_email="reader@example.com",
        agent_id="shared-agent",
        context=ConversationContext(type="web"),
    )

    async def generate(*_, **__):
        async with factory() as db:
            await sync_intaris_title(db, stale, "Wrong-route suggestion")
            await db.commit()
        assert await visible(factory) == (None, "unset")
        return completion()

    reconciler, bus = worker(factory, llm=SimpleNamespace(generate=AsyncMock(side_effect=generate)))
    assert await reconciler.reconcile() == 1
    assert await visible(factory) == ("Oprava přihlašování", "cognis")
    bus.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_maintenance_cancellation_preserves_title_and_persisted_cooldown(database):
    _, factory = database
    await seed(factory)
    entered = asyncio.Event()

    async def generate(*_, **__):
        entered.set()
        await asyncio.Event().wait()

    reconciler, bus = worker(factory, llm=SimpleNamespace(generate=AsyncMock(side_effect=generate)))
    task = asyncio.create_task(
        run_periodic_maintenance(
            reconciler.reconcile,
            stop=asyncio.Event(),
            interval_seconds=30,
            resource_key="test-untitled-titles",
            lease_store=None,
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await visible(factory) == (None, "unset")
    bus.publish.assert_not_awaited()
    restarted, _ = worker(factory)
    assert await restarted.reconcile() == 0
    await expire_retry(factory)
    assert await restarted.reconcile() == 1


@pytest.mark.asyncio
async def test_role_visibility_and_provenance_filtering_and_input_output_bounds(database):
    _, factory = database
    await seed(factory)
    guardrails = SimpleNamespace(
        read_events=AsyncMock(
            return_value=page(
                user_event("internal secret", prompt_visibility="model_only"),
                user_event("delegation secret", source="delegation_input"),
                user_event(
                    "workflow secret", prompt_provenance={"kind": "internal_workflow_prompt"}
                ),
                user_event("assistant secret", role="assistant"),
                user_event("hidden prefix", user_visible_content="Veřejný požadavek " * 1000),
            )
        )
    )
    llm = SimpleNamespace(generate=AsyncMock(return_value=completion("Ž" * 300)))
    reconciler, _ = worker(factory, guardrails, llm)
    assert await reconciler.reconcile() == 1
    prompt = llm.generate.call_args.args[0]
    assert len(prompt[1]["content"]) <= 6000
    assert "secret" not in prompt[1]["content"] and "hidden prefix" not in prompt[1]["content"]
    assert "user's language" in prompt[0]["content"]
    assert await visible(factory) == ("Ž" * 200, "cognis")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        completion(" "),
        completion(None),
        {"choices": [{"message": {"content": "Title", "tool_calls": [{"id": "call-1"}]}}]},
    ],
)
async def test_invalid_completions_never_change_or_publish_title(database, response):
    _, factory = database
    await seed(factory)
    reconciler, bus = worker(
        factory, llm=SimpleNamespace(generate=AsyncMock(return_value=response))
    )
    assert await reconciler.reconcile() == 0
    assert await visible(factory) == (None, "unset")
    bus.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_unset_title_atomic_update_is_idempotent(database):
    _, factory = database
    await seed(factory, title=" ")
    async with factory() as db:
        assert await initialize_conversation_title(db, "conversation", title="Recovered")
        assert not await initialize_conversation_title(db, "conversation", title="Second")
        await db.commit()
    assert await visible(factory) == ("Recovered", "cognis")


@pytest.mark.asyncio
async def test_retry_bootstrap_and_alembic_roundtrip_preserve_existing_titles(
    database, monkeypatch
):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    from cognis.bootstrap import _ensure_conversation_title_retry_column

    engine, factory = database
    await seed(factory, title="Existing Intaris title", title_source="intaris")
    migration = import_module("cognis.store.migrations.versions.180_conversation_title_retry")
    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE conversations DROP COLUMN title_generation_retry_at")
        )
        await connection.run_sync(_ensure_conversation_title_retry_column)
        await connection.run_sync(_ensure_conversation_title_retry_column)

        def roundtrip(sync):
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            assert "title_generation_retry_at" in {
                col["name"] for col in inspect(sync).get_columns("conversations")
            }
            migration.downgrade()
            migration.upgrade()

        await connection.run_sync(roundtrip)
    assert await visible(factory) == ("Existing Intaris title", "intaris")
    async with factory() as db:
        row = await db.get(Conversation, "conversation")
        assert row.title_generation_retry_at is None


@pytest.mark.asyncio
async def test_no_guardrails_uses_recorded_events_with_real_scoped_service_jwt(database, tmp_path):
    _, factory = database
    await seed(factory)
    config = replace(
        load_config(),
        data_dir=tmp_path,
        jwt_private_key_path=tmp_path / "private.pem",
        jwt_public_key_path=tmp_path / "public.pem",
        require_external_crypto=False,
    )
    ensure_jwt_keypair(config)
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    recorded = []

    def respond(request):
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            config.jwt_public_key_path.read_text(),
            algorithms=["ES256"],
            audience="intaris",
            issuer="cognis",
        )
        assert claims["sub"] == "reader@example.com"
        assert claims["agent_id"] == "shared-agent"
        assert claims["aow"] == "owner@example.com"
        assert claims["typ"] == "service"
        assert request.headers["X-Agent-Owner"] == "owner@example.com"
        assert request.url.path == "/api/v1/session/conversation-intaris/events"
        if request.method == "POST":
            import json

            recorded.extend(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "count": 1, "first_seq": 1, "last_seq": 1})
        return httpx.Response(200, json=page(*recorded).model_dump())

    provider = IntarisProvider("http://intaris.test", auth)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    wrapped = NoGuardrailsProvider(provider)
    try:
        from cognis.models.session import SessionEvent

        await wrapped.record_events(
            "conversation-intaris",
            [SessionEvent(**user_event())],
            user_email="reader@example.com",
            agent_id="shared-agent",
            agent_owner_email="owner@example.com",
        )
        reconciler, _ = worker(factory, wrapped)
        assert await reconciler.reconcile() == 1
        assert await visible(factory) == ("Oprava přihlašování", "cognis")
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
async def test_paging_retries_empty_rows_without_starving_other_conversations(database):
    _, factory = database
    for cid in ["a", "b", "c"]:
        await seed(factory, cid)
    seen = []

    async def read(sid, **_):
        seen.append(sid)
        return page() if sid.startswith("a-") else page(user_event())

    reconciler, _ = worker(
        factory, SimpleNamespace(read_events=AsyncMock(side_effect=read)), batch_size=1
    )
    for _ in range(5):
        await reconciler.reconcile()
    assert seen == ["a-intaris", "b-intaris", "c-intaris", "a-intaris"]


@pytest.mark.asyncio
async def test_missing_wrong_tenant_and_wrong_agent_sessions_are_not_candidates(database):
    _, factory = database
    async with factory() as db:
        db.add(Agent(agent_id="other-agent", owner_email="owner@example.com", name="Other"))
        for cid in ["empty", "wrong-tenant", "wrong-agent"]:
            db.add(
                Conversation(
                    conversation_id=cid,
                    user_email="reader@example.com",
                    agent_id="shared-agent",
                    context_type="web",
                )
            )
        await db.flush()
        db.add_all(
            [
                Session(
                    session_id="other-owner-session",
                    conversation_id="wrong-tenant",
                    user_email="owner@example.com",
                    agent_id="shared-agent",
                ),
                Session(
                    session_id="other-agent-session",
                    conversation_id="wrong-agent",
                    user_email="reader@example.com",
                    agent_id="other-agent",
                ),
            ]
        )
        await db.commit()
    guardrails = SimpleNamespace(read_events=AsyncMock())
    reconciler, _ = worker(factory, guardrails)
    assert await reconciler.reconcile() == 0
    guardrails.read_events.assert_not_awaited()
