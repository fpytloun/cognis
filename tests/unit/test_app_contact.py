from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from cognis.channels.app_contact import AppContactService, observe_app_recipient
from cognis.channels.delivery import ChannelDeliveryService
from cognis.channels.managed import ManagedChannelService, managed_route_key
from cognis.core.events import EventBus
from cognis.models.session import EventAppendResult
from cognis.runtime_context import current_user_email
from cognis.store import queries
from cognis.store.database import create_session_factory
from cognis.store.models import (
    AppChannelDeliveryRow,
    AppContactBlockRow,
    Base,
    ChannelAccountRow,
    ChannelDeliveryOutboxRow,
    Conversation,
    InboxNotificationRow,
    ManagedChannelBinding,
)

OWNER = "sender@example.com"
RECIPIENT = "recipient@example.com"


@pytest.fixture
async def contact_env(tmp_path):
    postgres_url = os.environ.get("COGNIS_APP_CONTACT_TEST_POSTGRES_URL")
    schema = f"app_contact_{uuid.uuid4().hex}" if postgres_url else None
    engine = create_async_engine(
        postgres_url or f"sqlite+aiosqlite:///{tmp_path}/contacts.db",
        **({"connect_args": {"server_settings": {"search_path": schema}}} if schema else {}),
    )
    async with engine.begin() as connection:
        if schema:
            await connection.execute(CreateSchema(schema))
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db:
        for email in (OWNER, RECIPIENT, "other@example.com"):
            await queries.create_user(db, email, email, "hash")
        await queries.create_agent(
            db, agent_id="contact-agent", owner_email=OWNER, name="Contact agent", status="active"
        )
        source = await queries.create_conversation(db, OWNER, "contact-agent", "web")
        source_session = await queries.create_session(
            db, conversation_id=source.conversation_id, user_email=OWNER, agent_id="contact-agent"
        )
        main = await queries.create_conversation(db, RECIPIENT, "contact-agent", "web")
        main_session = await queries.create_session(
            db, conversation_id=main.conversation_id, user_email=RECIPIENT, agent_id="contact-agent"
        )
        main.active_session_id = main_session.session_id
        main_session.intaris_session_id = "intaris-recipient-main"
        await queries.create_channel_account(
            db,
            account_id="app-account",
            channel_type="web",
            display_name="App",
            agent_id="contact-agent",
            user_email=OWNER,
        )
        await db.commit()
    scheduler = SimpleNamespace(submit_turn=AsyncMock(return_value=None), cancel_turn=AsyncMock())
    bus = EventBus()
    managed = ManagedChannelService(session_factory=factory, turn_scheduler=scheduler)
    manager = SimpleNamespace(
        get_or_create_agent_direct_conversation=AsyncMock(return_value=main),
        ensure_root_session=AsyncMock(),
    )
    recorded = []

    async def record(
        session_id,
        events,
        source="cognis",
        idempotency_key=None,
        retry_missing_session=False,
        user_email=None,
        agent_id=None,
        agent_owner_email=None,
    ):
        assert idempotency_key.startswith(f"{session_id}:")
        assert events[0].type == "assistant_message"
        assert events[0].data["role"] == "assistant"
        recorded.append((current_user_email.get(), session_id, events))
        return EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1)

    providers = SimpleNamespace(
        guardrails=SimpleNamespace(record_events=AsyncMock(side_effect=record))
    )
    service = AppContactService(
        session_factory=factory,
        session_manager=manager,
        providers=providers,
        event_bus=bus,
        managed_channels=managed,
        turn_scheduler=scheduler,
    )
    env = SimpleNamespace(
        factory=factory,
        service=service,
        scheduler=scheduler,
        bus=bus,
        manager=manager,
        managed=managed,
        recorded=recorded,
        source=source,
        source_session=source_session,
        main=main,
        main_session=main_session,
        engine=engine,
    )
    yield env
    if schema:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
    await engine.dispose()


async def enqueue(env, *, managed=False, delivery_id="cdel_contact"):
    async with env.factory() as db:
        binding = None
        if managed:
            target = await queries.create_conversation(db, OWNER, "contact-agent", "agent_work")
            target_session = await queries.create_session(
                db,
                conversation_id=target.conversation_id,
                user_email=OWNER,
                agent_id="contact-agent",
            )
            link = await queries.create_managed_conversation_link(
                db,
                user_email=OWNER,
                controller_agent_id="contact-agent",
                controller_conversation_id=env.source.conversation_id,
                controller_session_id=env.source_session.session_id,
                target_agent_id="contact-agent",
                target_conversation_id=target.conversation_id,
                target_session_id=target_session.session_id,
                title="Contact",
                kind="channel",
                completion_policy="explicit",
            )
            binding = ManagedChannelBinding(
                link_id=link.link_id,
                user_email=OWNER,
                account_id="app-account",
                channel_type="web",
                chat_id=RECIPIENT,
                sender_id=RECIPIENT,
                thread_key="",
                active_route_key=managed_route_key(OWNER, "app-account", RECIPIENT, None),
                state="waiting_external",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                objective="Arrange a meeting",
            )
            db.add(binding)
            await db.flush()
        outbox = ChannelDeliveryOutboxRow(
            delivery_id=delivery_id,
            user_email=OWNER,
            conversation_id=env.source.conversation_id,
            source_type="managed_channel_final" if managed else "channel_tool",
            channel_type="web",
            account_id="app-account",
            chat_id=RECIPIENT,
            fallback_text="Would Tuesday work?",
            status="pending",
            managed_binding_id=binding.binding_id if binding else None,
        )
        db.add(outbox)
        await db.commit()
        return outbox, binding


@pytest.mark.parametrize("shared", [False, True])
async def test_one_shot_uses_main_only_when_shared_and_is_idempotent(contact_env, shared):
    env = contact_env
    if shared:
        async with env.factory() as db:
            await queries.create_agent_grant(
                db,
                agent_id="contact-agent",
                grantee_user_email=RECIPIENT,
                executor_scope="grantee_executor",
                granted_by=OWNER,
            )
            await db.commit()
    outbox, _ = await enqueue(env)
    assert await env.service.deliver(outbox.delivery_id) == outbox.delivery_id
    assert await env.service.deliver(outbox.delivery_id) == outbox.delivery_id
    view = await env.service.view(outbox.delivery_id, RECIPIENT)
    assert view.messages[0].content == "Would Tuesday work?"
    assert not view.can_reply
    assert view.main_conversation_id == (env.main.conversation_id if shared else None)
    assert len(env.recorded) == (1 if shared else 0)
    if shared:
        user, session_id, events = env.recorded[0]
        assert user == RECIPIENT
        assert session_id == env.main_session.intaris_session_id
        assert events[0].data["untrusted"] is True
        assert events[0].data["turn_id"] is None
    env.scheduler.submit_turn.assert_not_called()
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(InboxNotificationRow)) == (
            0 if shared else 1
        )
        if not shared:
            notification = (await db.execute(select(InboxNotificationRow))).scalar_one()
            assert notification.user_email == RECIPIENT


@pytest.mark.parametrize("shared", [False, True])
async def test_managed_contact_is_sender_owned_and_reply_uses_existing_admission(
    contact_env, shared
):
    env = contact_env
    if shared:
        async with env.factory() as db:
            await queries.create_agent_grant(
                db,
                agent_id="contact-agent",
                grantee_user_email=RECIPIENT,
                executor_scope="grantee_executor",
                granted_by=OWNER,
            )
            await db.commit()
    outbox, binding = await enqueue(env, managed=True)
    await env.service.deliver(outbox.delivery_id)
    view = await env.service.view(binding.binding_id, RECIPIENT)
    assert view.can_reply
    assert view.main_conversation_id is None
    async with env.factory() as db:
        projection = await db.get(AppChannelDeliveryRow, outbox.delivery_id)
        recipient_chat = await db.get(Conversation, projection.destination_conversation_id)
        assert recipient_chat.user_email == RECIPIENT
        assert recipient_chat.context_type == "web"
        assert recipient_chat.context_data["app_contact_binding_id"] == binding.binding_id
        assert recipient_chat.active_session_id is None
        assert await db.scalar(select(func.count()).select_from(InboxNotificationRow)) == 0
    env.manager.get_or_create_agent_direct_conversation.assert_not_called()
    assert (
        await env.service.reply(binding.binding_id, RECIPIENT, "Tuesday works", "reply-1")
        == "accepted"
    )
    args, kwargs = env.scheduler.submit_turn.call_args
    assert args[1] == "Tuesday works"
    assert args[0] != env.source.conversation_id
    assert kwargs["user_email"] == OWNER
    assert kwargs["user_message_metadata"]["untrusted"] is True
    assert kwargs["intention_eligible"] is False
    with pytest.raises(ValueError, match="idempotency"):
        await env.service.reply(binding.binding_id, RECIPIENT, "Changed content", "reply-1")
    await env.service.reply(binding.binding_id, RECIPIENT, "Tuesday works", "reply-1")
    assert env.scheduler.submit_turn.await_count == 1
    view = await env.service.view(binding.binding_id, RECIPIENT)
    assert [message.content for message in view.messages] == [
        "Would Tuesday work?",
        "Tuesday works",
    ]
    assert not {"session_id", "controller_conversation_id", "tools"} & view.model_dump().keys()


async def test_recipient_scope_blocks_other_users_and_sender_readback(contact_env):
    env = contact_env
    outbox, binding = await enqueue(env, managed=True)
    await env.service.deliver(outbox.delivery_id)
    for email in (OWNER, "other@example.com"):
        with pytest.raises(LookupError):
            await env.service.view(binding.binding_id, email)
        with pytest.raises(LookupError):
            await env.service.reply(binding.binding_id, email, "Not authorized", "reply-2")
        with pytest.raises(LookupError):
            await env.service.end(binding.binding_id, email, block=True)
    env.scheduler.submit_turn.assert_not_called()


async def test_end_fences_replies_and_block_prevents_new_delivery(contact_env):
    env = contact_env
    outbox, binding = await enqueue(env, managed=True)
    await env.service.deliver(outbox.delivery_id)
    await env.service.end(binding.binding_id, RECIPIENT, block=True)
    view = await env.service.view(binding.binding_id, RECIPIENT)
    assert not view.can_reply
    assert view.blocked
    with pytest.raises(ValueError):
        await env.service.reply(binding.binding_id, RECIPIENT, "Too late", "reply-3")
    next_outbox, _ = await enqueue(env, delivery_id="cdel_after_block")
    with pytest.raises(ValueError, match="blocked"):
        await env.service.deliver(next_outbox.delivery_id)
    async with env.factory() as db:
        assert await db.get(AppContactBlockRow, (RECIPIENT, OWNER))
        ended = await db.get(ManagedChannelBinding, binding.binding_id)
        assert ended.active_route_key is None
    env.scheduler.cancel_turn.assert_awaited_once()


async def test_destination_session_stays_pinned_after_recording_failure(contact_env):
    env = contact_env
    async with env.factory() as db:
        await queries.create_agent_grant(
            db,
            agent_id="contact-agent",
            grantee_user_email=RECIPIENT,
            executor_scope="grantee_executor",
            granted_by=OWNER,
        )
        await db.commit()
    outbox, _ = await enqueue(env)
    recorder = env.service._providers.guardrails.record_events
    recorder.side_effect = RuntimeError("Event store unavailable")
    with pytest.raises(RuntimeError):
        await env.service.deliver(outbox.delivery_id)
    async with env.factory() as db:
        rotated = await queries.create_session(
            db,
            user_email=RECIPIENT,
            agent_id="contact-agent",
            conversation_id=env.main.conversation_id,
        )
        rotated.intaris_session_id = "intaris-recipient-main-rotated"
        main = await db.get(Conversation, env.main.conversation_id)
        main.active_session_id = rotated.session_id
        agent = await queries.get_agent(db, "contact-agent")
        agent.display_name = "Renamed after an uncertain append"
        await db.commit()
    env.manager.get_or_create_agent_direct_conversation.reset_mock()
    recorder.side_effect = None
    recorder.return_value = EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1)
    await env.service.deliver(outbox.delivery_id)
    env.manager.get_or_create_agent_direct_conversation.assert_not_called()
    assert recorder.call_args.kwargs["session_id"] == env.main_session.intaris_session_id
    before, after = recorder.call_args_list
    assert before.kwargs["idempotency_key"] == after.kwargs["idempotency_key"]
    assert before.kwargs["events"][0].model_dump() == after.kwargs["events"][0].model_dump()


async def test_explicit_app_recipient_reuses_the_existing_recipient_intent(contact_env):
    from cognis.channels.recipients import RecipientResolutionService
    from cognis.channels.target_refs import ChannelTargetRef, ChannelTargetRefCodec
    from cognis.models.channel import ChannelRecipient, ChannelRecipientError

    env = contact_env
    codec = ChannelTargetRefCodec("contact-test-signing-secret")
    resolver = RecipientResolutionService(env.factory, codec=codec)
    account_ref = codec.encode(
        ChannelTargetRef(
            kind="account",
            user_email=OWNER,
            account_id="app-account",
            channel_type="web",
        )
    )
    recipient = ChannelRecipient(
        channel_type="web",
        address_kind="cognis_user_email",
        address=RECIPIENT.upper(),
        account_ref=account_ref,
    )
    arguments = dict(
        user_email=OWNER,
        recipient=recipient,
        content="Native one-shot",
        artifact_metadata=[],
        idempotency_key="native-recipient-key",
        conversation_id=env.source.conversation_id,
    )
    first = await resolver.send(**arguments)
    assert first.status == "queued", first
    second = await resolver.send(**arguments)
    assert second.delivery_id == first.delivery_id
    await env.service.deliver(first.delivery_id)
    view = await env.service.view(first.delivery_id, RECIPIENT)
    assert view.messages[0].content == "Native one-shot"
    conflict = await resolver.send(**{**arguments, "content": "Changed content"})
    assert conflict.status == "conflict"
    assert conflict.error is not None
    assert ChannelRecipientError.model_validate(conflict.error).code == "idempotency_conflict"
    await env.service.end(first.delivery_id, RECIPIENT, block=True)
    blocked = await resolver.send(
        **{**arguments, "idempotency_key": "blocked-native-recipient-key"}
    )
    assert blocked.status == "failed"
    assert blocked.error is not None
    assert ChannelRecipientError.model_validate(blocked.error).code == "recipient_unavailable"


async def test_exact_user_lookup_reuses_observed_target_refs(contact_env):
    env = contact_env
    async with env.factory() as db:
        accounts = list((await db.execute(select(ChannelAccountRow))).scalars())
        await observe_app_recipient(db, accounts=accounts, user_email=OWNER, query="recipient")
        assert (
            await queries.list_channel_observed_targets(
                db, user_email=OWNER, account_ids=["app-account"]
            )
            == []
        )
        await observe_app_recipient(
            db, accounts=accounts, user_email=OWNER, query=RECIPIENT.upper()
        )
        targets = await queries.list_channel_observed_targets(
            db, user_email=OWNER, account_ids=["app-account"]
        )
        assert len(targets) == 1
        assert targets[0].chat_id == targets[0].sender_id == RECIPIENT


async def test_app_send_reuses_delivery_callbacks_without_an_external_adapter(contact_env):
    env = contact_env
    outbox, _ = await enqueue(env)
    delivery = ChannelDeliveryService(
        session_factory=env.factory, event_bus=env.bus, channel_manager_ref=lambda: None
    )
    delivery.set_app_contact_service(env.service)
    started, sent = AsyncMock(return_value=True), AsyncMock(return_value=True)
    result = await delivery._send_to_route(
        channel_type="web",
        account_id="app-account",
        chat_id=RECIPIENT,
        thread_id=None,
        content=outbox.fallback_text,
        delivery_idempotency_key=outbox.delivery_id,
        on_chunk_start=started,
        on_chunk_sent=sent,
    )
    assert result == "sent"
    assert started.call_args.args[3] is True
    assert sent.call_args.args[3]["external_message_id"] == outbox.delivery_id


async def test_app_schema_bootstrap_is_idempotent(contact_env):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from cognis.store.app_contact_schema import ensure_app_contact_schema
    from cognis.store.models import AppContactBlockRow

    migration = importlib.import_module("cognis.store.migrations.versions.177_app_agent_contact")

    def upgrade_downgrade_upgrade(connection):
        AppContactBlockRow.__table__.drop(connection)
        AppChannelDeliveryRow.__table__.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()

    async with contact_env.engine.begin() as connection:
        await connection.run_sync(upgrade_downgrade_upgrade)
        await connection.run_sync(ensure_app_contact_schema)
        await connection.run_sync(ensure_app_contact_schema)
    async with contact_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(AppChannelDeliveryRow)) == 0


async def test_normal_chat_discovery_does_not_grant_agent_or_session_access(contact_env):
    import httpx
    from fastapi import FastAPI, Request

    from cognis.api.authentication import AuthenticatedUser
    from cognis.api.chat_v2.routes import router as chat_router
    from cognis.api.routes.agents import router as agents_router
    from cognis.api.routes.app_contacts import router as contacts_router
    from cognis.api.routes.conversations import router as conversations_router
    from cognis.core.session import SessionManager

    env = contact_env
    outbox, binding = await enqueue(env, managed=True)
    await env.service.deliver(outbox.delivery_id)
    async with env.factory() as db:
        projection = await db.get(AppChannelDeliveryRow, outbox.delivery_id)
        conversation_id = projection.destination_conversation_id
        target = await queries.get_managed_conversation_link(db, binding.link_id)
        target_id = target.target_conversation_id
    env.scheduler.running_turn_state = lambda _: None
    app = FastAPI()
    app.state.session_factory = env.factory
    app.state.app_contact_service = env.service
    app.state.turn_scheduler = env.scheduler
    app.state.event_bus = env.bus
    app.state.agent_registry = SimpleNamespace(list_all=AsyncMock(return_value=[]))

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        request.state.user = AuthenticatedUser(
            email=request.headers.get("X-User", RECIPIENT),
            role=request.headers.get("X-Role", "user"),
        )
        return await call_next(request)

    for router in (agents_router, contacts_router, conversations_router, chat_router):
        app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/agents")).json()["items"] == []
        agents = await client.get("/api/v1/agents?include_contacts=true")
        assert agents.status_code == 200, agents.text
        identity = agents.json()["items"][0]
        assert identity["is_contact_only"]
        assert identity["contact_conversation_id"] == conversation_id
        assert identity["system_prompt"] is None
        assert identity["tools"] is None
        assert identity["permissions"] is None
        assert (await client.get("/api/v1/agents/contact-agent")).status_code == 403
        detail = await client.get(f"/api/v1/conversations/{conversation_id}?include_state=false")
        assert detail.status_code == 200, detail.text
        assert detail.json()["active_session_id"] is None
        assert (
            detail.json()["context"]["platform_data"]["app_contact_binding_id"]
            == binding.binding_id
        )
        assert (await client.get(f"/api/v1/conversations/{target_id}")).status_code in {403, 404}
        forbidden = await client.put(
            f"/api/v1/chat/v2/conversations/{conversation_id}/messages/contact-txn",
            json={"content": "Start a normal agent turn", "client_message_id": "contact-message"},
        )
        assert forbidden.status_code == 409
        reply = await client.post(
            f"/api/v1/app-contacts/{binding.binding_id}/replies",
            json={"content": "Hello", "client_message_id": "api-reply-1"},
        )
        assert reply.status_code == 200, reply.text
        other = await client.get(
            f"/api/v1/app-contacts/{binding.binding_id}", headers={"X-User": "other@example.com"}
        )
        assert other.status_code == 404
        viewer = await client.post(
            f"/api/v1/app-contacts/{binding.binding_id}/replies",
            headers={"X-Role": "viewer"},
            json={"content": "Hello", "client_message_id": "viewer-reply"},
        )
        assert viewer.status_code == 403
    with pytest.raises(ValueError, match="execution sessions"):
        await SessionManager.ensure_root_session(
            SimpleNamespace(session_factory=env.factory),
            conversation_id=conversation_id,
            user_email=RECIPIENT,
            agent_id="contact-agent",
            intention="Not a runtime",
        )


async def test_stale_contact_reply_never_attaches_to_a_new_exchange(contact_env):
    from cognis.models.channel import InboundMessage

    env = contact_env
    first, old = await enqueue(env, managed=True)
    await env.service.deliver(first.delivery_id)
    await env.service.end(old.binding_id, RECIPIENT, block=False)
    second, current = await enqueue(env, managed=True, delivery_id="cdel_second_exchange")
    await env.service.deliver(second.delivery_id)
    admitted = await env.managed.admit_inbound(
        InboundMessage(
            channel_type="web",
            account_id="app-account",
            chat_id=RECIPIENT,
            sender_id=RECIPIENT,
            sender_name=RECIPIENT,
            message_id="stale-route-reply",
            content="Reply to old exchange",
            timestamp=datetime.now(UTC),
            platform_data={"app_binding_id": old.binding_id},
        ),
        user_email=OWNER,
    )
    assert admitted is None
    async with env.factory() as db:
        active = await db.get(ManagedChannelBinding, current.binding_id)
        assert active.state == "waiting_external"


@pytest.fixture
def recipient_app(contact_env):
    from fastapi import FastAPI, Request

    from cognis.api.authentication import AuthenticatedUser
    from cognis.api.routes.app_contacts import router

    app = FastAPI()
    app.state.session_factory = contact_env.factory
    app.state.artifact_store = SimpleNamespace(
        async_get_public_url=AsyncMock(return_value="https://assets.cognis.test/contact-attachment")
    )
    app.state.turn_scheduler = SimpleNamespace(running_turn_state=lambda _: None)

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.user = AuthenticatedUser(
            email=request.headers.get("X-User", RECIPIENT), role="user"
        )
        return await call_next(request)

    app.include_router(router)
    return app


async def test_sidebar_delta_preserves_an_unchanged_shared_main_chat(contact_env, recipient_app):
    from starlette.requests import Request

    from cognis.api.routes.conversations import _agent_direct_chat_projection

    env = contact_env
    async with env.factory() as db:
        await queries.create_agent_grant(
            db,
            agent_id="contact-agent",
            grantee_user_email=RECIPIENT,
            executor_scope="grantee_executor",
            granted_by=OWNER,
        )
        main = await queries.get_conversation(db, env.main.conversation_id)
        main.context_ref = queries.agent_direct_context_ref(RECIPIENT, "contact-agent")
        await db.commit()
    outbox, _ = await enqueue(env, managed=True)
    await env.service.deliver(outbox.delivery_id)
    request = Request({"type": "http", "app": recipient_app})
    snapshot = await _agent_direct_chat_projection(request, user_email=RECIPIENT)
    assert [entry.conversation.conversation_id for entry in snapshot] == [env.main.conversation_id]
    delta = await _agent_direct_chat_projection(
        request,
        user_email=RECIPIENT,
        changed_since=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert delta == []


async def test_contact_attachment_download_is_bound_to_published_recipient_and_lifetime(
    contact_env, recipient_app
):
    import httpx

    from cognis.core.artifact_inputs import authorize_outbound_artifact_refs_in_session

    env = contact_env
    outbox, _ = await enqueue(env)
    artifact_id = f"art_{uuid.uuid4().hex}"
    async with env.factory() as db:
        row = await db.get(ChannelDeliveryOutboxRow, outbox.delivery_id)
        await queries.create_artifact_record(
            db,
            artifact_id=artifact_id,
            namespace="contact-test",
            object_id=uuid.uuid4().hex,
            filename="note.pdf",
            owner_email=OWNER,
            purpose="attachment",
            kind="pdf",
            mime_type="application/pdf",
            size_bytes=4,
            status="attached",
            conversation_id=env.source.conversation_id,
        )
        row.attachments_json = await authorize_outbound_artifact_refs_in_session(
            db,
            [
                {
                    "artifact_id": artifact_id,
                    "filename": "note.pdf",
                    "kind": "pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 4,
                }
            ],
            user_email=OWNER,
            conversation_id=env.source.conversation_id,
        )
        await db.commit()
    await env.service.deliver(outbox.delivery_id)
    url = f"/api/v1/app-contacts/messages/{outbox.delivery_id}/attachments/{artifact_id}"
    store = recipient_app.state.artifact_store.async_get_public_url
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recipient_app), base_url="http://test"
    ) as client:
        assert (await client.get(url)).status_code == 200
        assert (await client.get(url, headers={"X-User": "other@example.com"})).status_code == 404
        assert (
            await client.get(
                f"/api/v1/app-contacts/messages/{outbox.delivery_id}/attachments/art_{uuid.uuid4().hex}"
            )
        ).status_code == 404
        async with env.factory() as db:
            publication = await db.get(AppChannelDeliveryRow, outbox.delivery_id)
            published_at = publication.published_at
            publication.published_at = None
            await db.commit()
        assert (await client.get(url)).status_code == 404
        async with env.factory() as db:
            publication = await db.get(AppChannelDeliveryRow, outbox.delivery_id)
            publication.published_at = published_at
            artifact = await queries.get_artifact_record(db, artifact_id)
            artifact.deleted_at = datetime.now(UTC)
            await db.commit()
        assert (await client.get(url)).status_code == 404
        async with env.factory() as db:
            artifact = await queries.get_artifact_record(db, artifact_id)
            artifact.deleted_at = None
            artifact.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        assert (await client.get(url)).status_code == 404
        async with env.factory() as db:
            artifact = await queries.get_artifact_record(db, artifact_id)
            await db.delete(artifact)
            await db.commit()
        assert (await client.get(url)).status_code == 404
    assert store.await_count == 1
