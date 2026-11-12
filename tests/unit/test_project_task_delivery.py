"""Project result fan-out through real recipient policy and durable channel outbox."""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.channels.delivery import ChannelDeliveryService, ChannelDeliveryStatus
from cognis.core.agent_direct import agent_direct_context_ref
from cognis.core.agent_loop import PauseWaiter
from cognis.core.channel_delivery_targets import preferred_channel_target
from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import TaskResultFollowUp, render_follow_up_block
from cognis.core.project_task_delivery import (
    fan_out_project_result,
    project_result_direct_turn_allowed,
    project_result_follow_up_allowed,
)
from cognis.core.session import SessionManager
from cognis.core.task_outcomes import task_outcome_key
from cognis.core.turn_scheduler import TurnError, TurnScheduler
from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.channel import ChannelCapabilities
from cognis.models.session import EventAppendResult
from cognis.models.task import TaskDelivery, TaskModel
from cognis.models.workflow import CompletionDeliveryPolicy
from cognis.store.database import create_engine, create_session_factory
from cognis.store.direct_turns import DirectTurnPayloadV1, DirectTurnStore
from cognis.store.models import Base, ChannelDeliveryOutboxRow
from cognis.store.project_queries import remove_project_agent, upsert_project_agent
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_channel_account,
    create_channel_delivery_outbox,
    create_conversation,
    create_deliverable,
    create_project,
    create_project_grant,
    create_task,
    create_user,
    get_channel_account,
    get_conversation,
    get_task,
)


@pytest.fixture
async def setup(tmp_path):
    postgres_url = os.environ.get("COGNIS_PROJECT_RESULT_TEST_POSTGRES_URL")
    engine = create_engine(
        postgres_url or f"sqlite+aiosqlite:///{tmp_path / 'project-delivery.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        for email in ("owner@test.com", "member@test.com", "viewer@test.com", "outsider@test.com"):
            await create_user(session, email=email, name=email, password_hash="hash", role="user")
        agent = await create_agent(
            session,
            agent_id="project-agent",
            owner_email="owner@test.com",
            name="Project agent",
            status="active",
        )
        project = await create_project(session, owner_email="owner@test.com", name="Project")
        await upsert_project_agent(
            session,
            project_id=project.project_id,
            agent_id=agent.agent_id,
            added_by="owner@test.com",
        )
        grants = {}
        conversations = {}
        for email, role in (("member@test.com", "member"), ("viewer@test.com", "viewer")):
            grants[email] = await create_project_grant(
                session,
                project_id=project.project_id,
                granted_by="owner@test.com",
                grantee_user_email=email,
                permission=role,
            )
        for email in ("owner@test.com", "member@test.com", "viewer@test.com", "outsider@test.com"):
            account = await create_channel_account(
                session,
                channel_type="matrix",
                display_name=email,
                agent_id=agent.agent_id,
                user_email=email,
                preferred_for_task_delivery=True,
            )
            conversation = await create_conversation(
                session,
                user_email=email,
                agent_id=agent.agent_id,
                context_type="matrix",
                context_data={
                    "channel_type": "matrix",
                    "account_id": account.account_id,
                    "chat_id": email,
                },
            )
            account.default_conversation_id = conversation.conversation_id
            conversations[email] = conversation.conversation_id
        row = await create_task(
            session,
            created_by="owner@test.com",
            agent_id=agent.agent_id,
            title="Shared result",
            status="completed",
            project_id=project.project_id,
            completion_mode_family="default",
        )
        row.result_summary = "Final result"
        await session.commit()
    task = TaskModel(
        task_id=row.task_id,
        title=row.title,
        created_by=row.created_by,
        agent_id=row.agent_id,
        project_id=row.project_id,
        status="completed",
        result_summary=row.result_summary,
        completion_delivery=CompletionDeliveryPolicy(completion_mode_family="default"),
    )
    try:
        yield factory, task, grants, conversations
    finally:
        if postgres_url:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _fan_out_direct(task, factory, delivery):
    async def deliver(user_email):
        async with factory() as session:
            target = await preferred_channel_target(
                session, user_email=user_email, agent_id=task.agent_id
            )
        if target is not None:
            await delivery.deliver_task_to_conversation(
                target,
                task_id=task.task_id,
                content=task.result_summary,
                project_result=True,
                outcome_key=task_outcome_key(task),
            )

    await fan_out_project_result(task, factory, deliver)


@pytest.fixture
async def runtime(setup):
    factory, task, grants, conversations = setup
    bus = EventBus()
    events = []

    async def capture(event):
        events.append(event)

    bus.subscribe_all(capture)
    guardrails = SimpleNamespace(
        create_session=AsyncMock(),
        record_events=AsyncMock(
            return_value=EventAppendResult(ok=True, count=2, first_seq=1, last_seq=2)
        ),
    )
    providers = SimpleNamespace(guardrails=guardrails)
    session_manager = SessionManager(factory, providers, SimpleNamespace(), bus)
    workflow = WorkflowEngine(
        session_factory=factory,
        providers=providers,
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=session_manager,
        event_bus=bus,
        pause_waiter=PauseWaiter(),
        channel_delivery=SimpleNamespace(
            deliver_task_to_conversation=AsyncMock(return_value=ChannelDeliveryStatus.SENT)
        ),
    )
    return SimpleNamespace(
        factory=factory,
        task=task,
        grants=grants,
        conversations=conversations,
        workflow=workflow,
        guardrails=guardrails,
        events=events,
    )


@pytest.mark.asyncio
async def test_success_fanout_is_recipient_scoped_and_idempotent(setup):
    factory, task, _, conversations = setup
    adapter = SimpleNamespace(
        capabilities=ChannelCapabilities(supports_idempotent_send=True),
        send_message=AsyncMock(return_value="receipt"),
    )
    manager = SimpleNamespace(find_adapter_for_channel=lambda *args: (adapter, object()))
    bus = EventBus()
    events = []

    async def capture(event):
        events.append(event)

    bus.subscribe_all(capture)
    service = ChannelDeliveryService(
        session_factory=factory, event_bus=bus, channel_manager_ref=lambda: manager
    )
    await _fan_out_direct(task, factory, service)
    await service.recover_pending_deliveries()
    await _fan_out_direct(task, factory, service)
    await service.recover_pending_deliveries()
    assert adapter.send_message.await_count == 1
    assert {call.args[0].chat_id for call in adapter.send_message.await_args_list} == {
        "member@test.com",
    }
    assert not events  # No lifecycle events or follow-up agent turns for recipients.
    async with factory() as session:
        rows = list(await session.scalars(select(ChannelDeliveryOutboxRow)))
    assert {row.conversation_id for row in rows} == {
        conversations["member@test.com"],
    }
    assert all(row.status == "sent" and row.source_type == "project_task_result" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "failed"),
        ("status", "cancelled"),
        ("status", "paused"),
        ("status", "running"),
        ("visibility", "private"),
        ("delivery_mode", "same_conversation"),
        ("delivery_mode", "specific_conversation"),
        ("delivery_mode", "latest_active_for_agent"),
        ("delivery_mode", "silent"),
        ("applied_completion_mode", "silent"),
        ("project_id", None),
    ],
)
async def test_non_final_private_and_other_modes_do_not_fanout(setup, field, value):
    factory, task, _, _ = setup
    async with factory() as session:
        row = await get_task(session, task.task_id)
        setattr(row, field, value)
        await session.commit()
    delivery = SimpleNamespace(deliver_task_to_conversation=AsyncMock())
    await _fan_out_direct(task, factory, delivery)
    delivery.deliver_task_to_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_membership_and_unavailable_agent_are_excluded(setup):
    factory, task, grants, _ = setup
    async with factory() as session:
        grant = await session.merge(grants["member@test.com"])
        grant.revoked_at = datetime.now(UTC)
        await remove_project_agent(session, project_id=task.project_id, agent_id=task.agent_id)
        await session.commit()
    delivery = SimpleNamespace(deliver_task_to_conversation=AsyncMock())
    await _fan_out_direct(task, factory, delivery)
    delivery.deliver_task_to_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_delivery_rechecks_membership_before_retry(setup):
    factory, task, grants, _ = setup
    adapter = SimpleNamespace(
        capabilities=ChannelCapabilities(supports_idempotent_send=True),
        send_message=AsyncMock(side_effect=RuntimeError("temporarily unavailable")),
    )
    manager = SimpleNamespace(find_adapter_for_channel=lambda *args: (adapter, object()))
    service = ChannelDeliveryService(
        session_factory=factory, event_bus=EventBus(), channel_manager_ref=lambda: manager
    )
    await _fan_out_direct(task, factory, service)
    await service.recover_pending_deliveries()
    async with factory() as session:
        for detached in grants.values():
            grant = await session.merge(detached)
            grant.revoked_at = datetime.now(UTC)
        rows = list(await session.scalars(select(ChannelDeliveryOutboxRow)))
        await session.commit()
    adapter.send_message.reset_mock()
    for row in rows:
        await service._deliver_outbox(
            delivery_id=row.delivery_id,
            final_content=None,
            fallback_text=None,
            attachments=None,
            deliverable_id=None,
            ignore_next_attempt=True,
        )
    adapter.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_fans_out_scheduled_success_and_keeps_owner_delivery(setup, monkeypatch):
    factory, task, _, conversations = setup
    task.source_type = "scheduler"
    task.delivery = TaskDelivery(mode="preferred_channel")
    engine = object.__new__(WorkflowEngine)
    engine._session_factory = factory
    engine._channel_delivery = SimpleNamespace(deliver_task_to_conversation=AsyncMock())
    engine._resolve_task_delivery_conversation = AsyncMock(
        return_value=conversations["owner@test.com"]
    )
    engine._deliver_task_result_default = AsyncMock()
    engine._deliver_project_task_result = AsyncMock()
    monkeypatch.setattr("cognis.core.workflow_engine.assert_task_execution_fence", AsyncMock())
    await engine._deliver_task_result(task)
    engine._deliver_task_result_default.assert_awaited_once_with(
        task, conversations["owner@test.com"]
    )
    engine._channel_delivery.deliver_task_to_conversation.assert_not_awaited()
    engine._deliver_project_task_result.assert_awaited_once_with(task, "member@test.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["archived", "preference", "route", "agent", "private", "failed"]
)
async def test_pending_delivery_suppresses_stale_destinations(setup, change):
    factory, task, _, conversations = setup
    adapter = SimpleNamespace(
        capabilities=ChannelCapabilities(supports_idempotent_send=True),
        send_message=AsyncMock(return_value="receipt"),
    )
    manager = SimpleNamespace(find_adapter_for_channel=lambda *args: (adapter, object()))
    service = ChannelDeliveryService(
        session_factory=factory, event_bus=EventBus(), channel_manager_ref=lambda: manager
    )
    await _fan_out_direct(task, factory, service)
    async with factory() as session:
        for email in ("member@test.com", "viewer@test.com"):
            conversation = await get_conversation(session, conversations[email])
            if change == "archived":
                conversation.status = "archived"
            elif change == "preference":
                account = await get_channel_account(
                    session, conversation.context_data["account_id"]
                )
                account.preferred_for_task_delivery = False
            elif change == "route":
                conversation.context_data = {**conversation.context_data, "chat_id": "new-room"}
        if change == "agent":
            await remove_project_agent(session, task.project_id, task.agent_id)
        elif change == "private":
            row = await get_task(session, task.task_id)
            row.visibility = "private"
        elif change == "failed":
            row = await get_task(session, task.task_id)
            row.status = "failed"
        await session.commit()
    await service.recover_pending_deliveries()
    adapter.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_preference_change_does_not_duplicate_recipient_result(setup):
    factory, task, _, conversations = setup
    adapter = SimpleNamespace(
        capabilities=ChannelCapabilities(supports_idempotent_send=True),
        send_message=AsyncMock(return_value="receipt"),
    )
    manager = SimpleNamespace(find_adapter_for_channel=lambda *args: (adapter, object()))
    service = ChannelDeliveryService(
        session_factory=factory, event_bus=EventBus(), channel_manager_ref=lambda: manager
    )
    await _fan_out_direct(task, factory, service)
    await service.recover_pending_deliveries()
    async with factory() as session:
        old = await get_conversation(session, conversations["member@test.com"])
        account = await get_channel_account(session, old.context_data["account_id"])
        new = await create_conversation(
            session,
            user_email="member@test.com",
            agent_id=task.agent_id,
            context_type="matrix",
            context_data={**old.context_data, "chat_id": "new-room"},
        )
        account.default_conversation_id = new.conversation_id
        await session.commit()
    await _fan_out_direct(task, factory, service)
    await service.recover_pending_deliveries()
    assert adapter.send_message.await_count == 1
    async with factory() as session:
        rows = list(await session.scalars(select(ChannelDeliveryOutboxRow)))
        assert len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "default", "direct"])
async def test_engine_routes_each_recipient_using_task_policy(runtime, mode):
    task = runtime.task
    task.completion_delivery = CompletionDeliveryPolicy(
        completion_mode_family=mode,
        on_finish_instructions="Report useful changes." if mode != "direct" else None,
    )
    async with runtime.factory() as db:
        row = await get_task(db, task.task_id)
        row.completion_mode_family = mode
        await db.commit()
    await runtime.workflow._deliver_task_result(task)
    follow_ups = [e for e in runtime.events if e.type == EventType.FOLLOW_UP_TURN_REQUESTED]
    terminals = [e for e in runtime.events if e.type == EventType.TASK_COMPLETED]
    assert len(terminals) == 1  # Recipient delivery never re-emits task completion.
    if mode == "default":
        assert len(follow_ups) == 2
        member = next(e for e in follow_ups if e.data["follow_up"]["project_result"])
        assert member.data["follow_up"]["on_finish_instructions"] is None
        assert member.data["follow_up"]["mode"] == "notify"
        assert member.data["follow_up"]["required_action"] == "present_update"
        owner = next(e for e in follow_ups if not e.data["follow_up"]["project_result"])
        assert (
            owner.data["follow_up"]["on_finish_instructions"]
            == task.completion_delivery.on_finish_instructions
        )
        async with runtime.factory() as db:
            target = await get_conversation(db, member.data["conversation_id"])
            assert target.user_email == "member@test.com"
            assert target.project_id == task.project_id
            assert target.visibility == "private"
            assert (
                target.context_data["source_conversation_id"]
                == runtime.conversations["member@test.com"]
            )
            assert await project_result_follow_up_allowed(
                db,
                TaskResultFollowUp.model_validate(member.data["follow_up"]),
                target.conversation_id,
            )
            outbox = list(await db.scalars(select(ChannelDeliveryOutboxRow)))
            assert {r.source_type for r in outbox} == {
                "task_result_follow_up",
                "project_task_result_follow_up",
            }
            assert {r.user_email for r in outbox} == {"owner@test.com", "member@test.com"}
    else:
        assert not follow_ups
        calls = runtime.workflow._channel_delivery.deliver_task_to_conversation.await_args_list
        assert len(calls) == 2
        recipient = next(call for call in calls if call.kwargs.get("project_result"))
        assert recipient.args == (runtime.conversations["member@test.com"],)
        assert recipient.kwargs["outcome_key"] == task_outcome_key(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "default", "direct"])
async def test_app_fallback_creates_recipient_session_and_respects_policy(runtime, mode):
    task = runtime.task
    task.completion_delivery = CompletionDeliveryPolicy(completion_mode_family=mode)
    async with runtime.factory() as db:
        row = await get_task(db, task.task_id)
        row.completion_mode_family = mode
        external = await get_conversation(db, runtime.conversations["member@test.com"])
        account = await get_channel_account(db, external.context_data["account_id"])
        account.preferred_for_task_delivery = False
        await db.commit()
    await runtime.workflow._deliver_task_result(task)
    async with runtime.factory() as db:
        from cognis.store.models import Conversation

        recipient_chats = list(
            await db.scalars(
                select(Conversation).where(
                    Conversation.user_email == "member@test.com",
                    Conversation.context_type == "web",
                )
            )
        )
        main = next(
            c
            for c in recipient_chats
            if c.context_ref == agent_direct_context_ref("member@test.com", task.agent_id)
        )
        assert main.project_id is None  # The recipient's main chat is not reassigned.
        target = (
            main
            if mode != "default"
            else next(c for c in recipient_chats if c.project_id == task.project_id)
        )
        assert target.active_session_id is not None
    calls = runtime.guardrails.record_events.await_args_list
    assert len(calls) == 1
    if mode != "default":
        assert any(event.type == "assistant_message" for event in calls[0].kwargs["events"])
        assert not any(
            e.type == EventType.FOLLOW_UP_TURN_REQUESTED and e.data["follow_up"]["project_result"]
            for e in runtime.events
        )
    else:
        follow_up = next(
            e
            for e in runtime.events
            if e.type == EventType.FOLLOW_UP_TURN_REQUESTED
            and e.data["follow_up"]["project_result"]
        )
        assert follow_up.data["conversation_id"] == target.conversation_id
        assert follow_up.data["origin_session_id"] == target.active_session_id
    assert not any("viewer@test.com" in str(call) for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "paused"])
@pytest.mark.parametrize("mode", ["auto", "default", "direct"])
async def test_engine_non_success_outcomes_remain_owner_only(runtime, status, mode):
    task = runtime.task
    task.status = status
    task.completion_delivery = CompletionDeliveryPolicy(completion_mode_family=mode)
    async with runtime.factory() as db:
        row = await get_task(db, task.task_id)
        row.status = status
        row.completion_mode_family = mode
        await db.commit()
    await runtime.workflow._deliver_task_result(task)
    follow_ups = [e for e in runtime.events if e.type == EventType.FOLLOW_UP_TURN_REQUESTED]
    if mode == "direct":
        assert not follow_ups
        calls = runtime.workflow._channel_delivery.deliver_task_to_conversation.await_args_list
        assert len(calls) == 1
        assert calls[0].args == (runtime.conversations["owner@test.com"],)
        assert not calls[0].kwargs.get("project_result")
    else:
        assert len(follow_ups) == 1
        assert follow_ups[0].data["conversation_id"] == runtime.conversations["owner@test.com"]
        assert not follow_ups[0].data["follow_up"]["project_result"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["viewer", "revoked", "agent", "private", "failed", "silent", "attempt", "archived"]
)
async def test_recipient_follow_up_rechecks_access_at_turn_admission(runtime, change):
    await runtime.workflow._deliver_task_result(runtime.task)
    event = next(
        e
        for e in runtime.events
        if e.type == EventType.FOLLOW_UP_TURN_REQUESTED and e.data["follow_up"]["project_result"]
    )
    follow_up = TaskResultFollowUp.model_validate(event.data["follow_up"])
    async with runtime.factory() as db:
        row = await get_task(db, runtime.task.task_id)
        grant = await db.merge(runtime.grants["member@test.com"])
        if change == "viewer":
            grant.permission = "viewer"
        elif change == "revoked":
            grant.revoked_at = datetime.now(UTC)
        elif change == "agent":
            await remove_project_agent(
                db, project_id=runtime.task.project_id, agent_id=runtime.task.agent_id
            )
        elif change == "private":
            row.visibility = "private"
        elif change == "failed":
            row.status = "failed"
        elif change == "silent":
            row.applied_completion_mode = "silent"
        elif change == "attempt":
            row.attempt_number += 1
        elif change == "archived":
            target = await get_conversation(db, event.data["conversation_id"])
            target.status = "archived"
        await db.commit()
        assert not await project_result_follow_up_allowed(
            db, follow_up, event.data["conversation_id"]
        )
    scheduler = object.__new__(TurnScheduler)
    scheduler._accepting_turns = True
    scheduler._direct_turn_store = None
    scheduler._session_factory = runtime.factory
    result = await scheduler.submit_turn(
        conversation_id=event.data["conversation_id"],
        user_email="member@test.com",
        content="Handle the shared task result.",
        follow_up=follow_up,
        system_initiated=True,
    )
    assert isinstance(result, TurnError)
    assert result.code == "access_denied"
    assert not result.recoverable


@pytest.mark.asyncio
async def test_recipient_delivery_failure_does_not_block_owner(setup):
    factory, task, _, conversations = setup
    workflow = object.__new__(WorkflowEngine)
    workflow._session_factory = factory
    workflow._channel_delivery = None
    workflow._deliver_project_task_result = AsyncMock(
        side_effect=RuntimeError("Unavailable recipient")
    )
    workflow._resolve_task_delivery_conversation = AsyncMock(
        return_value=conversations["owner@test.com"]
    )
    workflow._deliver_task_result_default = AsyncMock()
    await workflow._deliver_task_result(task)
    workflow._deliver_task_result_default.assert_awaited_once_with(
        task, conversations["owner@test.com"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("mode", ["auto", "direct"])
async def test_direct_app_result_references_only_recipient_readable_deliverable(
    runtime, tmp_path, shared, mode
):
    task = runtime.task
    task.completion_delivery = CompletionDeliveryPolicy(completion_mode_family=mode)
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    async with runtime.factory() as db:
        external = await get_conversation(db, runtime.conversations["member@test.com"])
        account = await get_channel_account(db, external.context_data["account_id"])
        account.preferred_for_task_delivery = False
        source = await create_conversation(
            db,
            user_email="owner@test.com",
            agent_id=task.agent_id,
            context_type="web",
            project_id=task.project_id if shared else None,
            visibility="project" if shared else "private",
        )
        deliverable = await create_deliverable(
            db,
            conversation_id=source.conversation_id,
            title="Canonical result",
            content="Reviewed result",
            artifact_store=store,
            published_owner_email="owner@test.com",
        )
        await db.commit()
    task.result_data = {"final_deliverable_id": deliverable.deliverable_id}
    await runtime.workflow._deliver_task_result(task)
    persisted = runtime.guardrails.record_events.await_args.kwargs["events"]
    presented = [
        event.data["deliverable_id"]
        for event in persisted
        if event.type == "lifecycle" and event.data.get("event") == "assistant_deliverable"
    ]
    assert presented == ([deliverable.deliverable_id] if shared else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "direct"])
async def test_engine_recipient_outbox_suppresses_revoked_execution_rights(runtime, mode):
    task = runtime.task
    task.completion_delivery = CompletionDeliveryPolicy(completion_mode_family=mode)
    adapter = SimpleNamespace(
        capabilities=ChannelCapabilities(supports_idempotent_send=True),
        send_message=AsyncMock(side_effect=RuntimeError("temporarily unavailable")),
    )
    manager = SimpleNamespace(find_adapter_for_channel=lambda *args: (adapter, object()))
    service = ChannelDeliveryService(
        session_factory=runtime.factory, event_bus=EventBus(), channel_manager_ref=lambda: manager
    )
    runtime.workflow._channel_delivery = service
    await runtime.workflow._deliver_task_result(task)
    async with runtime.factory() as db:
        grant = await db.merge(runtime.grants["member@test.com"])
        grant.permission = "viewer"
        rows = list(
            await db.scalars(
                select(ChannelDeliveryOutboxRow).where(
                    ChannelDeliveryOutboxRow.user_email == "member@test.com"
                )
            )
        )
        assert len(rows) == 1
        await db.commit()
    adapter.send_message.reset_mock()
    await service._deliver_outbox(
        delivery_id=rows[0].delivery_id,
        final_content=None,
        fallback_text="Shared result",
        attachments=None,
        deliverable_id=None,
        ignore_next_attempt=True,
    )
    adapter.send_message.assert_not_awaited()
    async with runtime.factory() as db:
        current = await db.get(ChannelDeliveryOutboxRow, rows[0].delivery_id)
        assert current.status == "suppressed"


@pytest.mark.asyncio
async def test_preferred_target_never_adopts_private_execution_chat(runtime):
    async with runtime.factory() as db:
        source = await get_conversation(db, runtime.conversations["member@test.com"])
        account = await get_channel_account(db, source.context_data["account_id"])
        account.default_conversation_id = None
        await db.commit()
    await runtime.workflow._deliver_task_result(runtime.task)
    await runtime.workflow._deliver_task_result(runtime.task)
    async with runtime.factory() as db:
        target = await preferred_channel_target(
            db, user_email="member@test.com", agent_id=runtime.task.agent_id
        )
        assert target == runtime.conversations["member@test.com"]
    requests = [
        event
        for event in runtime.events
        if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
        and event.data["follow_up"]["project_result"]
    ]
    assert len({event.data["conversation_id"] for event in requests}) == 1
    async with runtime.factory() as db:
        target = await get_conversation(db, requests[0].data["conversation_id"])
        assert target.context_type == "chat"
        assert target.project_id == runtime.task.project_id


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_default_recipient_generated_reply_retains_access_fence(runtime, revoked):
    await runtime.workflow._deliver_task_result(runtime.task)
    request = next(
        event
        for event in runtime.events
        if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
        and event.data["follow_up"]["project_result"]
    )
    adapter = SimpleNamespace(
        capabilities=ChannelCapabilities(supports_idempotent_send=True),
        send_message=AsyncMock(return_value="receipt"),
    )
    manager = SimpleNamespace(find_adapter_for_channel=lambda *args: (adapter, object()))
    service = ChannelDeliveryService(
        session_factory=runtime.factory, event_bus=EventBus(), channel_manager_ref=lambda: manager
    )
    reply_id = await service._ensure_follow_up_result_delivery(
        event=Event(
            type=EventType.TURN_COMPLETED,
            data={
                "conversation_id": request.data["conversation_id"],
                "turn_id": TurnScheduler.new_turn_id(),
            },
        ),
        grace_delivery_id=request.data["delivery_id"],
        final_content="Here are the useful changes.",
        attachments=[],
        deliverable_id=None,
    )
    assert reply_id is not None
    async with runtime.factory() as db:
        row = await db.get(ChannelDeliveryOutboxRow, reply_id)
        assert row.source_type == "project_task_result_reply"
        if revoked:
            grant = await db.merge(runtime.grants["member@test.com"])
            grant.permission = "viewer"
            await db.commit()
    await service._deliver_outbox(
        delivery_id=reply_id,
        final_content=None,
        fallback_text=None,
        attachments=None,
        deliverable_id=None,
        ignore_next_attempt=True,
    )
    assert adapter.send_message.await_count == (0 if revoked else 1)
    if not revoked:
        assert adapter.send_message.await_args.args[0].chat_id == "member@test.com"
        assert adapter.send_message.await_args.args[0].content == "Here are the useful changes."


@pytest.mark.asyncio
async def test_adopted_durable_terminal_keeps_recipient_permission_fence(runtime):
    await runtime.workflow._deliver_task_result(runtime.task)
    event = next(
        event
        for event in runtime.events
        if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
        and event.data["follow_up"]["project_result"]
    )
    store = DirectTurnStore(runtime.factory)
    admitted = await store.admit(
        conversation_id=event.data["conversation_id"],
        session_id=None,
        agent_id=runtime.task.agent_id,
        user_id="member@test.com",
        idempotency_scope="follow_up",
        idempotency_key=event.data["follow_up"]["follow_up_id"],
        payload=DirectTurnPayloadV1(
            content=render_follow_up_block(
                TaskResultFollowUp.model_validate(event.data["follow_up"])
            ),
            metadata={"system_initiated": True, "follow_up": event.data["follow_up"]},
        ).model_dump(mode="json"),
    )
    async with runtime.factory() as db:
        grace = await db.get(ChannelDeliveryOutboxRow, event.data["delivery_id"])
        terminal = await create_channel_delivery_outbox(
            db,
            delivery_id=f"direct-turn:{admitted.request.request_id}:final",
            user_email=grace.user_email,
            conversation_id=grace.conversation_id,
            session_id=grace.session_id,
            source_type="direct_turn_final",
            source_id=admitted.request.turn_id,
            channel_type=grace.channel_type,
            account_id=grace.account_id,
            chat_id=grace.chat_id,
            thread_id=grace.thread_id,
            fallback_text="Handled project result.",
        )
        terminal.direct_turn_request_id = admitted.request.request_id
        await db.commit()
        assert await project_result_direct_turn_allowed(db, terminal)
    service = ChannelDeliveryService(
        session_factory=runtime.factory, event_bus=EventBus(), channel_manager_ref=lambda: None
    )
    adopted_id = await service._ensure_follow_up_result_delivery(
        event=Event(
            type=EventType.TURN_COMPLETED,
            data={
                "conversation_id": event.data["conversation_id"],
                "turn_id": admitted.request.turn_id,
            },
        ),
        grace_delivery_id=event.data["delivery_id"],
        final_content="Handled project result.",
        attachments=[],
        deliverable_id=None,
    )
    assert adopted_id == terminal.delivery_id
    async with runtime.factory() as db:
        grant = await db.merge(runtime.grants["member@test.com"])
        grant.permission = "viewer"
        await db.commit()
        assert not await project_result_direct_turn_allowed(db, terminal)
    await service._deliver_outbox(
        delivery_id=terminal.delivery_id,
        final_content=None,
        fallback_text=None,
        attachments=None,
        deliverable_id=None,
        ignore_next_attempt=True,
    )
    async with runtime.factory() as db:
        terminal = await db.get(ChannelDeliveryOutboxRow, terminal.delivery_id)
        assert terminal.status == "suppressed"


async def _create_canonical_report(runtime, tmp_path, *, workflow_owned=True, source_task_id=None):
    from cognis.store.queries import create_step_run

    task = runtime.task
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "rich-artifacts")))
    image = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
    image_bytes = image.getvalue()
    artifact_id = f"art_{uuid.uuid4().hex}"
    object_id = store.generate_id()
    await store.async_save(
        "artifacts", object_id, "project.png", image_bytes, "image/png", "owner@test.com"
    )
    async with runtime.factory() as db:
        source = await create_conversation(
            db,
            user_email="owner@test.com",
            agent_id=task.agent_id,
            context_type="web",
            project_id=task.project_id,
            visibility="project",
        )
        await create_artifact_record(
            db,
            artifact_id=artifact_id,
            namespace="artifacts",
            object_id=object_id,
            filename="project.png",
            owner_email="owner@test.com",
            purpose="test",
            kind="image",
            mime_type="image/png",
            size_bytes=len(image_bytes),
            status="attached",
            conversation_id=source.conversation_id,
        )
        payload = {
            "blocks": [
                {
                    "id": "project-image",
                    "type": "figure",
                    "media": {"ref": artifact_id, "alt": "Project result"},
                }
            ]
        }
        step_run = (
            await create_step_run(
                db,
                task_id=source_task_id or task.task_id,
                step_name="report",
                step_type="run",
                agent_id=task.agent_id,
                conversation_id=source.conversation_id,
                status="completed",
            )
            if workflow_owned
            else None
        )
        deliverable = await create_deliverable(
            db,
            step_run_id=step_run.step_run_id if step_run is not None else None,
            conversation_id=None if step_run is not None else source.conversation_id,
            format="rich",
            title="Project result",
            content="Verified shared result",
            rich=payload,
            artifact_store=store,
            media_owner_email="owner@test.com",
            media_accessor_conversation_id=source.conversation_id,
            media_accessor_agent_id=task.agent_id,
            published_owner_email="owner@test.com",
        )
        task_row = await get_task(db, task.task_id)
        task_row.result_json = {"final_deliverable_id": deliverable.deliverable_id}
        task_row.completion_mode_family = task.completion_delivery.completion_mode_family
        await db.commit()
    task.result_data = {"final_deliverable_id": deliverable.deliverable_id}
    return store, deliverable, image_bytes


async def _forward_canonical_report(
    runtime, store, deliverable, actor, monkeypatch, *, rotated=False, run_step=True
):
    from cognis.core.agent_loop import CONTROLLER_TOOL_SURFACE_DIRECT_CHAT
    from cognis.models.agent import AgentDefinition
    from cognis.models.workflow import StepDefinition
    from cognis.store.queries import create_session
    from tests.unit.test_agent_loop import (
        CHAT_POLICY,
        OrchestrationMode,
        _EmptyThenTextDirectLLM,
        _step_complete_test_context,
        _step_complete_test_loop,
    )

    class ForwardLLM(_EmptyThenTextDirectLLM):
        async def stream_generate(self, messages, **kwargs):
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "forward-canonical-report",
                                        "type": "function",
                                        "function": {
                                            "name": "attach_artifact",
                                            "arguments": json.dumps(
                                                {"content_ref": deliverable.deliverable_id}
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}

    async with runtime.factory() as db:
        target = await get_conversation(db, runtime.conversations[actor])
        session = await create_session(
            db,
            target.conversation_id,
            actor,
            runtime.task.agent_id,
            intaris_session_id=f"intaris-{uuid.uuid4().hex}",
        )
        if rotated:
            session.status = "completed"
            session.completion_reason = "compacted"
            session = await create_session(
                db,
                target.conversation_id,
                actor,
                runtime.task.agent_id,
                previous_session_id=session.session_id,
                intaris_session_id=f"intaris-{uuid.uuid4().hex}",
            )
        target.active_session_id = session.session_id
        await db.commit()

    loop = _step_complete_test_loop(ForwardLLM())
    monkeypatch.setattr(loop.session_manager, "session_factory", runtime.factory)
    loop.artifact_store = store
    ctx = _step_complete_test_context(StepDefinition(name="direct", type="run", prompt=""))
    ctx.session.session_id = session.session_id
    ctx.session.intaris_session_id = session.intaris_session_id
    ctx.session.user_email = actor
    ctx.session.agent_id = runtime.task.agent_id
    ctx.session.conversation_id = target.conversation_id
    ctx.conversation = SimpleNamespace(
        conversation_id=target.conversation_id,
        user_email=actor,
        title=None,
        title_source="unset",
    )
    ctx.agent = AgentDefinition(
        agent_id=runtime.task.agent_id,
        owner_email=runtime.task.created_by,
        name="Project agent",
    )
    ctx.policy = CHAT_POLICY
    ctx.controller_tool_surface = CONTROLLER_TOOL_SURFACE_DIRECT_CHAT
    ctx.system_initiated = True
    ctx.orchestration_mode = OrchestrationMode.FULL
    output = await loop.run_step(ctx) if run_step else None
    return output, ctx, loop


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
@pytest.mark.parametrize("mode", ["auto", "default", "direct"])
@pytest.mark.parametrize("separate_source", [False, True])
async def test_rich_project_result_delivers_canonical_media_with_recipient_access(
    runtime, tmp_path, revoked, mode, separate_source, monkeypatch
):
    task = runtime.task
    task.completion_delivery = CompletionDeliveryPolicy(completion_mode_family=mode)
    source_task_id = None
    if separate_source:
        async with runtime.factory() as db:
            source = await create_task(
                db,
                created_by=task.created_by,
                agent_id=task.agent_id,
                title="Manually forwarded source",
                status="running",
                project_id=task.project_id,
                delivery_mode="same_conversation",
                allow_silent_completion=True,
            )
            source_task_id = source.task_id
            await db.commit()
    store, deliverable, image_bytes = await _create_canonical_report(
        runtime, tmp_path, source_task_id=source_task_id
    )
    adapter = SimpleNamespace(
        capabilities=ChannelCapabilities(
            supports_idempotent_send=True,
            supports_media=True,
            supports_inline_media=True,
            supports_markdown=True,
        ),
        send_message=AsyncMock(return_value="receipt"),
    )
    manager = SimpleNamespace(
        _artifact_store=store, find_adapter_for_channel=lambda *args: (adapter, object())
    )
    service = ChannelDeliveryService(
        session_factory=runtime.factory, event_bus=EventBus(), channel_manager_ref=lambda: manager
    )
    owner_projection = await service._deliverable_channel_projection(
        deliverable_id=deliverable.deliverable_id,
        fallback_text="Original canonical report.",
        capabilities=adapter.capabilities,
        artifact_store=store,
        owner_email=task.created_by,
        conversation_id=runtime.conversations[task.created_by],
    )
    assert owner_projection.media_complete
    assert owner_projection.media
    runtime.workflow._channel_delivery = service
    await runtime.workflow._deliver_task_result(task)
    if mode == "default":
        request = next(
            event
            for event in runtime.events
            if event.type == EventType.FOLLOW_UP_TURN_REQUESTED
            and event.data["follow_up"]["project_result"]
        )
        # Exercise real reference authorization and finalization before the
        # durable follow-up outbox consumes the original canonical identity.
        output, ctx, _loop = await _forward_canonical_report(
            runtime,
            store,
            deliverable,
            "member@test.com",
            monkeypatch,
            rotated=separate_source,
        )
        assert output.error is None
        assert output.deliverable_id == deliverable.deliverable_id
        assert ctx.current_deliverable is None
        assert ctx.turn_result_deliverable.step_run_id == deliverable.step_run_id
        reply_id = await service._ensure_follow_up_result_delivery(
            event=Event(
                type=EventType.TURN_COMPLETED,
                data={
                    "conversation_id": request.data["conversation_id"],
                    "turn_id": TurnScheduler.new_turn_id(),
                },
            ),
            grace_delivery_id=request.data["delivery_id"],
            final_content=output.content,
            attachments=[],
            deliverable_id=output.deliverable_id,
        )
        assert reply_id is not None
    if revoked:
        async with runtime.factory() as db:
            grant = await db.merge(runtime.grants["member@test.com"])
            grant.permission = "viewer"
            await db.commit()
    await service.recover_pending_deliveries()
    recipient_messages = [
        call.args[0]
        for call in adapter.send_message.await_args_list
        if call.args[0].chat_id == "member@test.com"
    ]
    if revoked:
        assert not recipient_messages
    else:
        assert recipient_messages
        assert any(message.media for message in recipient_messages)
        assert any("Project result" in message.content for message in recipient_messages)
    async with runtime.factory() as db:
        recipient = await db.scalar(
            select(ChannelDeliveryOutboxRow).where(
                ChannelDeliveryOutboxRow.user_email == "member@test.com",
                ChannelDeliveryOutboxRow.source_type
                == ("project_task_result_reply" if mode == "default" else "project_task_result"),
            )
        )
        assert recipient.status == ("suppressed" if revoked else "sent")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_owned", "actor"),
    [(True, "member@test.com"), (False, "owner@test.com"), (False, "member@test.com")],
)
@pytest.mark.parametrize("source_status", ["completed", "running"])
async def test_explicit_forwarding_uses_document_read_scope_after_rotation(
    runtime, tmp_path, monkeypatch, workflow_owned, actor, source_status
):
    store, deliverable, image_bytes = await _create_canonical_report(
        runtime, tmp_path, workflow_owned=workflow_owned
    )
    async with runtime.factory() as db:
        source = await get_task(db, runtime.task.task_id)
        source.delivery_mode = "same_conversation"
        source.status = source_status
        source.allow_silent_completion = True
        await db.commit()
    output, ctx, _loop = await _forward_canonical_report(
        runtime, store, deliverable, actor, monkeypatch, rotated=True
    )
    assert output.error is None
    assert output.deliverable_id == deliverable.deliverable_id
    assert output.deliverable_version == deliverable.version
    assert output.deliverable_format == "rich"
    assert ctx.current_deliverable is None
    assert ctx.turn_result_deliverable.step_run_id == deliverable.step_run_id
    service = ChannelDeliveryService(
        session_factory=runtime.factory, event_bus=EventBus(), channel_manager_ref=lambda: None
    )
    projection = await service._deliverable_channel_projection(
        deliverable_id=output.deliverable_id,
        fallback_text=output.content,
        capabilities=ChannelCapabilities(supports_inline_media=True),
        artifact_store=store,
        owner_email=actor,
        conversation_id=ctx.conversation.conversation_id,
    )
    assert projection.media_complete
    assert len(projection.media) == 1
    assert projection.media[0].content_b64 == base64.b64encode(image_bytes).decode("ascii")
    async with runtime.factory() as db:
        from cognis.store.queries import get_deliverable

        original = await get_deliverable(db, deliverable.deliverable_id)
        assert original.owner_email == "owner@test.com"
        assert original.step_run_id == deliverable.step_run_id
        assert original.project_id is None
        assert original.revision == deliverable.revision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restriction",
    [
        "private_task",
        "private_conversation",
        "revoked_membership",
        "unassigned_agent",
        "explicit_other_project",
        "outside_project_context",
    ],
)
async def test_forwarding_rechecks_real_document_authorization(
    runtime, tmp_path, monkeypatch, restriction
):
    store, deliverable, _image = await _create_canonical_report(
        runtime, tmp_path, workflow_owned=restriction != "private_conversation"
    )
    actor = "member@test.com"
    async with runtime.factory() as db:
        if restriction == "private_task":
            source = await get_task(db, runtime.task.task_id)
            source.visibility = "private"
        elif restriction == "private_conversation":
            source = await get_conversation(db, deliverable.conversation_id)
            source.visibility = "private"
        elif restriction == "revoked_membership":
            grant = await db.merge(runtime.grants[actor])
            grant.revoked_at = datetime.now(UTC)
        elif restriction == "unassigned_agent":
            await remove_project_agent(
                db, project_id=runtime.task.project_id, agent_id=runtime.task.agent_id
            )
        else:
            other = await create_project(db, owner_email="owner@test.com", name="Other")
            await create_project_grant(
                db,
                project_id=other.project_id,
                granted_by="owner@test.com",
                grantee_user_email=actor,
                permission="member",
            )
            if restriction == "explicit_other_project":
                from cognis.store.queries import get_deliverable

                original = await get_deliverable(db, deliverable.deliverable_id)
                original.project_id = other.project_id
            else:
                await upsert_project_agent(
                    db,
                    project_id=other.project_id,
                    agent_id=runtime.task.agent_id,
                    added_by="owner@test.com",
                )
                target = await create_conversation(
                    db,
                    user_email=actor,
                    agent_id=runtime.task.agent_id,
                    context_type="web",
                    project_id=other.project_id,
                    visibility="project",
                )
                runtime.conversations[actor] = target.conversation_id
        await db.commit()
    _output, ctx, loop = await _forward_canonical_report(
        runtime, store, deliverable, actor, monkeypatch, rotated=True, run_step=False
    )
    with pytest.raises(ValueError, match="not found or is unavailable"):
        await loop._resolve_attachable_content_ref(ctx, deliverable.deliverable_id)
    assert ctx.turn_result_deliverable is None
    service = ChannelDeliveryService(
        session_factory=runtime.factory, event_bus=EventBus(), channel_manager_ref=lambda: None
    )
    projection = await service._deliverable_channel_projection(
        deliverable_id=deliverable.deliverable_id,
        fallback_text="Forwarded report.",
        capabilities=ChannelCapabilities(supports_inline_media=True),
        artifact_store=store,
        owner_email=actor,
        conversation_id=ctx.conversation.conversation_id,
    )
    assert not projection.media_complete
    assert not projection.media


@pytest.mark.asyncio
async def test_inherited_reference_read_does_not_grant_revision_or_unrelated_task_scope(
    runtime, tmp_path, monkeypatch
):
    from cognis.core.content_refs import authorize_deliverable_access, authorize_supersedes_target
    from cognis.models.deliverable import DeliverableRevisionError
    from cognis.store.queries import get_deliverable

    store, deliverable, _image = await _create_canonical_report(runtime, tmp_path)
    _output, ctx, loop = await _forward_canonical_report(
        runtime, store, deliverable, "member@test.com", monkeypatch, run_step=False
    )
    _attachment, resolved = await loop._resolve_attachable_content_ref(
        ctx, deliverable.deliverable_id
    )
    assert resolved.deliverable_id == deliverable.deliverable_id
    async with runtime.factory() as db:
        other = await create_task(
            db,
            created_by="owner@test.com",
            agent_id=runtime.task.agent_id,
            project_id=runtime.task.project_id,
            title="Unrelated scope",
        )
        original = await get_deliverable(db, deliverable.deliverable_id)
        assert (
            await authorize_deliverable_access(
                db,
                original,
                "member@test.com",
                scope_task_id=other.task_id,
                accessor_conversation_id=ctx.conversation.conversation_id,
                accessor_agent_id=ctx.session.agent_id,
                accessor_session_id=ctx.session.session_id,
            )
            is None
        )
        with pytest.raises(DeliverableRevisionError, match="not found or is not accessible"):
            await authorize_supersedes_target(
                db,
                deliverable.deliverable_id,
                user_email="member@test.com",
                accessor_conversation_id=ctx.conversation.conversation_id,
                accessor_agent_id=ctx.session.agent_id,
                accessor_session_id=ctx.session.session_id,
            )


@pytest.mark.asyncio
async def test_media_projection_rechecks_document_sharing_after_payload_hydration(
    runtime, tmp_path, monkeypatch
):
    from cognis.store import deliverable_storage
    from cognis.store.queries import get_deliverable

    store, deliverable, _image = await _create_canonical_report(runtime, tmp_path)
    async with runtime.factory() as db:
        task = await get_task(db, runtime.task.task_id)
        task.visibility = "private"
        document = await get_deliverable(db, deliverable.deliverable_id)
        document.project_id = runtime.task.project_id
        await db.commit()
    _output, ctx, loop = await _forward_canonical_report(
        runtime, store, deliverable, "member@test.com", monkeypatch, run_step=False
    )
    _attachment, authorized = await loop._resolve_attachable_content_ref(
        ctx, deliverable.deliverable_id
    )
    assert authorized is not None
    hydrate = deliverable_storage.hydrate_deliverable_payload

    async def revoke_during_hydration(row, artifact_store):
        await hydrate(row, artifact_store)
        async with runtime.factory() as db:
            document = await get_deliverable(db, row.deliverable_id)
            document.project_id = None
            await db.commit()

    monkeypatch.setattr(deliverable_storage, "hydrate_deliverable_payload", revoke_during_hydration)
    service = ChannelDeliveryService(
        session_factory=runtime.factory, event_bus=EventBus(), channel_manager_ref=lambda: None
    )
    projection = await service._deliverable_channel_projection(
        deliverable_id=deliverable.deliverable_id,
        fallback_text="Forwarded report.",
        capabilities=ChannelCapabilities(supports_inline_media=True),
        artifact_store=store,
        owner_email="member@test.com",
        conversation_id=ctx.conversation.conversation_id,
    )
    assert not projection.media_complete
    assert not projection.media
