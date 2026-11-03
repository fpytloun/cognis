"""Hook CRUD, matching, isolation, recovery, and presentation contracts."""

from __future__ import annotations

import asyncio
import importlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy import event as sql_event

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.core.context import events_to_messages
from cognis.core.events import Event, EventBus, EventType
from cognis.core.hook_runtime import RECOVERY_SECONDS, HookRuntime
from cognis.core.hooks import (
    BUNDLED_HOOKS,
    HookError,
    manage_hooks,
    notify_hook_change,
    validate_hook,
)
from cognis.models.agent import AgentDefinition
from cognis.models.hooks import HookDefinition
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    EventAppendResult,
    EventReadResult,
    SessionEvent,
    SessionModel,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    AgentHookAssignmentRow,
    AgentHookRunRow,
    Base,
    Conversation,
    DirectTurnRequestRow,
    Session,
    Task,
    User,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL


@pytest_asyncio.fixture(
    params=["sqlite", *(["postgresql"] if os.getenv("COGNIS_HOOKS_TEST_POSTGRES_URL") else [])]
)
async def factory(request, tmp_path):
    url = (
        os.environ["COGNIS_HOOKS_TEST_POSTGRES_URL"]
        if request.param == "postgresql"
        else f"sqlite+aiosqlite:///{tmp_path / 'hooks.db'}"
    )
    engine = create_engine(url)
    owner_loop = asyncio.get_running_loop()

    @sql_event.listens_for(engine.sync_engine, "checkout")
    def check_loop(_connection, _record, _proxy):
        assert asyncio.get_running_loop() is owner_loop

    async with engine.begin() as connection:
        if request.param == "postgresql":
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all([User(email="owner@hooks.test"), User(email="other@hooks.test")])
        await db.flush()
        db.add(Agent(agent_id="agent-hooks", owner_email="owner@hooks.test", name="Hooks"))
        await db.flush()
        db.add(
            Conversation(
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_email="owner@hooks.test",
                context_type="web",
                active_session_id="session-hooks",
                last_message_at=datetime.now(UTC) - timedelta(minutes=20),
            )
        )
        db.add(
            Session(
                session_id="session-hooks",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_email="owner@hooks.test",
            )
        )
        await db.flush()
        db.add(
            DirectTurnRequestRow(
                turn_id="fixture-turn",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_id="owner@hooks.test",
                idempotency_scope="user",
                idempotency_key="fixture-turn",
                admission_hash="hash",
                payload_hash="hash",
                payload={"metadata": {"system_initiated": False}},
                status="completed",
                created_at=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
        await db.commit()
    yield factory
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def runtime(factory, *, legacy_detached=True):
    bus = EventBus()
    streams = {"session-hooks": [{"seq": 1, "type": "user_message", "data": {"content": "Task"}}]}
    idempotent = {}

    async def record_events(session_id, events, **kwargs):
        from cognis.runtime_context import current_user_email

        assert current_user_email.get() == "owner@hooks.test"
        if kwargs.get("idempotency_key") is not None:
            assert kwargs["idempotency_key"].startswith(session_id + ":"), (
                "idempotency_key must start with the current session_id"
            )
        key = (session_id, kwargs.get("idempotency_key", str(len(streams.get(session_id, [])))))
        if key in idempotent:
            return idempotent[key]
        stream = streams.setdefault(session_id, [])
        first = len(stream) + 1
        stream.extend(
            {"seq": first + index, "type": event.type, "data": event.data}
            for index, event in enumerate(events)
        )
        result = EventAppendResult(
            ok=True, count=len(events), first_seq=first, last_seq=len(stream)
        )
        idempotent[key] = result
        return result

    async def read_events(session_id, after_seq=0, **kwargs):
        from cognis.runtime_context import current_user_email

        assert current_user_email.get() == "owner@hooks.test"
        stream = streams.get(session_id, [])
        selected = [event for event in stream if event["seq"] > after_seq]
        if kwargs.get("seqs") is not None:
            selected = [event for event in selected if event["seq"] in kwargs["seqs"]]
        if kwargs.get("types") is not None:
            selected = [event for event in selected if event["type"] in kwargs["types"]]
        if kwargs.get("last_n"):
            selected = selected[-kwargs["last_n"] :]
        return EventReadResult(
            events=selected,
            last_seq=len(stream),
            has_more=False,
        )

    guardrails = SimpleNamespace(
        record_events=AsyncMock(side_effect=record_events),
        read_events=AsyncMock(side_effect=read_events),
        streams=streams,
    )
    child = ConversationModel(
        conversation_id="child-hooks",
        agent_id="agent-hooks",
        user_email="owner@hooks.test",
        context=ConversationContext(type="web", platform_data={"hook_run_id": "child-run"}),
    )

    async def create_child(*args, **kwargs):
        async with factory() as db:
            if await db.get(Conversation, child.conversation_id) is None:
                db.add(
                    Conversation(
                        conversation_id=child.conversation_id,
                        user_email=child.user_email,
                        agent_id=child.agent_id,
                        context_type=child.context.type,
                    )
                )
                await db.commit()
        return child, session()

    async def fork_child(*args, **kwargs):
        conversation, root_session = await create_child(*args, **kwargs)
        return conversation, root_session, {}

    service = HookRuntime(
        session_factory=factory,
        providers=SimpleNamespace(guardrails=guardrails),
        session_manager=SimpleNamespace(
            fork_into_new_conversation=AsyncMock(side_effect=fork_child),
            create_conversation_with_root_session=AsyncMock(side_effect=create_child),
        ),
        session_cache=SimpleNamespace(append_recorded_events=AsyncMock()),
        event_bus=bus,
        lease_store=None,
        workflow_registry=SimpleNamespace(create=AsyncMock(), get=AsyncMock(return_value=None)),
        agent_registry=SimpleNamespace(
            get=AsyncMock(
                return_value=AgentDefinition(
                    agent_id="agent-hooks",
                    owner_email="owner@hooks.test",
                    name="Hooks",
                )
            )
        ),
    )

    async def submit_turn(*args, **kwargs):
        participant = kwargs.get("admission_transaction_participant")
        if participant:
            async with factory() as db:
                await participant(db, SimpleNamespace(turn_id="child-turn"), True)
                await db.commit()
        return None

    service.scheduler = SimpleNamespace(
        submit_turn=AsyncMock(side_effect=submit_turn),
        durable_running_turn_state=AsyncMock(return_value=None),
        get_queued_messages=AsyncMock(return_value=[]),
        cluster_signals=SimpleNamespace(publish_chat_change=AsyncMock()),
        cancel_turn=AsyncMock(),
        cancel_request=AsyncMock(),
    )
    service.task_queue = SimpleNamespace(
        submit=AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(task_id=kwargs["task_id"])),
        cancel_task=AsyncMock(),
    )
    if not legacy_detached:
        from cognis.store.coordination import DatabaseLeaseStore

        service.lease_store = DatabaseLeaseStore(factory)
    if legacy_detached:
        # Existing direct-turn lifecycle tests cover already-admitted legacy
        # executions during rollout. New admissions are tested independently in
        # test_hook_subsessions with legacy_detached=False.
        dispatch = service._dispatch

        async def legacy_dispatch(run):
            if (
                run.configuration.get("mode") in {"fork", "fresh"}
                and run.configuration.get("action") == "prompt"
                and run.child_conversation_id is None
            ):
                async with factory() as db:
                    saved = await db.get(AgentHookRunRow, run.run_id)
                    saved.child_conversation_id = "hook_conv_" + run.run_id.removeprefix("hrun_")
                    run.child_conversation_id = saved.child_conversation_id
                    await db.commit()
            await dispatch(run)

        service._dispatch = legacy_dispatch
    return service


def conversation():
    return ConversationModel(
        conversation_id="conv-hooks",
        agent_id="agent-hooks",
        user_email="owner@hooks.test",
        context=ConversationContext(type="web"),
    )


def session():
    return SessionModel(
        session_id="session-hooks",
        conversation_id="conv-hooks",
        agent_id="agent-hooks",
        user_email="owner@hooks.test",
    )


async def assign(factory, definition=None, hook_id=None, *, prospective=False):
    async with factory() as db:
        if hook_id is None:
            item = await manage_hooks(
                db,
                owner="owner@hooks.test",
                action="hooks_create",
                definition=definition
                or {
                    "name": "Reminder",
                    "trigger": "user_message",
                    "prompt": "Remember {{ parameters.subject }}",
                    "parameters": {"subject": "validation"},
                },
            )
            hook_id = item["hook_id"]
        await manage_hooks(
            db,
            owner="owner@hooks.test",
            action="hook_assignments_put",
            agent_id="agent-hooks",
            assignment={"hook_id": hook_id, "enabled": True, "parameters": {}},
        )
        if not prospective:
            # Existing execution tests model a hook enabled before their source turn.
            row = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
            row.activated_at = datetime(2026, 1, 1, tzinfo=UTC)
        await db.commit()
    return hook_id


@pytest.mark.asyncio
async def test_crud_ownership_revisions_and_assignment_delete_guard(factory):
    hook_id = await assign(factory)
    async with factory() as db:
        with pytest.raises(HookError, match="not found"):
            await manage_hooks(db, owner="other@hooks.test", action="hooks_get", hook_id=hook_id)
        with pytest.raises(HookError, match="revision"):
            await manage_hooks(
                db,
                owner="owner@hooks.test",
                action="hooks_update",
                hook_id=hook_id,
                expected_revision=9,
                definition={},
            )
        with pytest.raises(HookError, match="assignments"):
            await manage_hooks(
                db,
                owner="owner@hooks.test",
                action="hooks_delete",
                hook_id=hook_id,
                expected_revision=1,
            )
        await manage_hooks(
            db,
            owner="owner@hooks.test",
            action="hook_assignments_remove",
            hook_id=hook_id,
            agent_id="agent-hooks",
        )
        await manage_hooks(
            db,
            owner="owner@hooks.test",
            action="hooks_delete",
            hook_id=hook_id,
            expected_revision=1,
        )
        await db.commit()


@pytest.mark.asyncio
async def test_no_shared_or_foreign_agent_assignment(factory):
    async with factory() as db:
        with pytest.raises(HookError, match="Owned agent"):
            await manage_hooks(
                db,
                owner="other@hooks.test",
                action="hook_assignments_put",
                agent_id="agent-hooks",
                assignment={"hook_id": "system:working-reminder"},
            )


@pytest.mark.parametrize(
    "definition",
    [
        {"name": "Bad", "trigger": "conversation_idle", "prompt": "Hi"},
        {"name": "Bad", "trigger": "user_message", "action": "tool_call"},
        {"name": "Bad", "trigger": "user_message", "prompt": ""},
    ],
)
def test_invalid_action_combinations(definition):
    with pytest.raises(ValidationError):
        HookDefinition.model_validate(definition)


def test_bundled_hooks_have_native_actions_and_valid_templates():
    for definition in BUNDLED_HOOKS.values():
        validate_hook(definition)
        assert "obsidian" not in definition.prompt.lower()
        assert "todoist" not in definition.prompt.lower()


@pytest.mark.asyncio
async def test_matching_renders_parameters_and_deduplicates(factory):
    await assign(factory)
    service = runtime(factory)
    kwargs = dict(
        conversation=conversation(),
        session=session(),
        triggers=["user_message"],
        occurrence="turn:first",
        context={"parameters": {}},
    )
    first = await service.match(**kwargs)
    second = await service.match(**kwargs)
    assert first == second
    assert "\nRemember validation\n</agent_hook>" in first[0]["content"]
    assert "not a new user request" in first[0]["content"]
    service.scheduler.submit_turn.assert_not_awaited()
    async with factory() as db:
        rows = (await db.scalars(select(AgentHookRunRow))).all()
    assert len(rows) == 1
    assert rows[0].status == "completed"


@pytest.mark.asyncio
async def test_cycle_condition_and_first_message_are_distinct(factory):
    await assign(factory, hook_id="system:working-reminder")
    await assign(factory, {"name": "First", "trigger": "first_user_message", "prompt": "Welcome"})
    service = runtime(factory)
    ctx = SimpleNamespace(
        conversation=conversation(),
        session=session(),
        task_id=None,
        system_initiated=False,
        agent=SimpleNamespace(agent_id="agent-hooks", owner_email="owner@hooks.test"),
        turn_id="turn-first",
    )
    first = await service.before_cycle(ctx, 1)
    assert len(first) == 1
    assert "\nWelcome\n</agent_hook>" in first[0]["content"]
    assert await service.before_cycle(ctx, 2) == []
    assert len(await service.before_cycle(ctx, 10)) == 1
    ctx.turn_id = "turn-second"
    assert await service.before_cycle(ctx, 1) == []
    ctx.system_initiated = True
    assert await service.before_cycle(ctx, 10) == []


@pytest.mark.asyncio
async def test_idle_fork_runs_once_and_returning_user_does_not_cancel(factory):
    await assign(factory, hook_id="system:memory-consolidation")
    service = runtime(factory)
    await service.scan()
    service.session_manager.fork_into_new_conversation.assert_awaited_once()
    service.scheduler.submit_turn.assert_awaited_once()
    kwargs = service.scheduler.submit_turn.await_args.kwargs
    assert kwargs["system_initiated"] is True
    async with factory() as db:
        source = await db.get(Conversation, "conv-hooks")
        source.last_message_at = datetime.now(UTC)
        await db.commit()
    await service.scan()
    service.scheduler.cancel_turn.assert_not_awaited()
    service.scheduler.submit_turn.assert_awaited_once()
    await service._settle_child("child-hooks", "completed")
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        assert run.status == "running"
        db.add(
            DirectTurnRequestRow(
                request_id="request-idle-fork",
                turn_id="child-turn",
                conversation_id="child-hooks",
                agent_id=run.agent_id,
                user_id=run.owner_email,
                idempotency_scope="hook",
                idempotency_key=run.run_id,
                admission_hash="hash",
                payload_hash="hash",
                payload={},
                status="completed",
                terminal_at=datetime.now(UTC),
            )
        )
        await db.commit()
    await service.scan()
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        assert run.status == "completed"
    events = service.providers.guardrails.record_events.await_args.args[1]
    assert events[0].data["status"] == "completed"
    assert "prompt" not in events[0].data


@pytest.mark.asyncio
async def test_idle_busy_and_thresholds(factory):
    await assign(factory, hook_id="system:memory-consolidation")
    await assign(
        factory,
        {
            "name": "Later",
            "trigger": "conversation_idle",
            "mode": "fresh",
            "idle_seconds": 3600,
            "prompt": "Later",
        },
    )
    service = runtime(factory)
    service.scheduler.get_queued_messages.return_value = [{"turn_id": "queued"}]
    await service.scan()
    service.scheduler.submit_turn.assert_not_awaited()
    service.scheduler.get_queued_messages.return_value = []
    await service.scan()
    service.scheduler.submit_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_hook_uses_deterministic_workflow_and_normal_queue(factory):
    await assign(
        factory,
        {
            "name": "Tool",
            "trigger": "turn_completed",
            "mode": "fresh",
            "action": "tool_call",
            "tool_call": {
                "tool": "builtin:memory_search",
                "args": {"query": "decisions"},
            },
        },
    )
    service = runtime(factory)
    await service.match(
        conversation=conversation(),
        session=session(),
        triggers=["turn_completed"],
        occurrence="completed:tool",
        context={},
    )
    await service.scan()
    workflow = service.workflow_registry.create.await_args.args[0]
    assert workflow.steps[0].type == "tool_call"
    assert workflow.steps[0].tool_call.tool == "builtin:memory_search"
    service.task_queue.submit.assert_awaited_once()
    service.scheduler.submit_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_child_reservation_before_admission_recovers_with_stable_idempotency(factory):
    await assign(factory, hook_id="system:memory-consolidation")
    service = runtime(factory)
    await service._match_idle()
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        run.status = "dispatching"
        run.child_conversation_id = "hook_conv_" + run.run_id.removeprefix("hrun_")
        await db.commit()
    await service.scan()
    service.scheduler.submit_turn.assert_awaited_once()
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))).status == "running"


@pytest.mark.parametrize(
    "statuses",
    [
        ["dispatching", "running", "completed"],
        ["dispatching", "completed", "running"],
        ["running", "dispatching", "completed"],
    ],
)
def test_lifecycle_updates_one_box_and_is_not_model_history(statuses):
    raw = [
        RawSessionEvent(
            store_id="intaris",
            session_id="session-hooks",
            seq=index + 1,
            type="lifecycle",
            data={
                "event": "agent_hook",
                "hook_run_id": "run-hooks",
                "hook_id": "hook-hooks",
                "name": "Journal",
                "status": status,
                "child_conversation_id": "child-hooks",
            },
        )
        for index, status in enumerate(statuses)
    ]
    items = project_timeline(normalize_session_events(raw).events).timeline.items
    assert len(items) == 1
    assert items[0].id == "hook:run-hooks"
    assert items[0].hook.status == "completed"
    assert (
        events_to_messages([SessionEvent(type="lifecycle", data=item.data) for item in raw]) == []
    )
    assert (
        events_to_messages(
            [
                SessionEvent(
                    type="lifecycle",
                    data={
                        "event": "hook_snapshot",
                        "hook_run_id": "run-hooks",
                        "input": {
                            "prompt": "Private detached instructions and quoted work evidence"
                        },
                    },
                )
            ]
        )
        == []
    )


def test_management_tool_exposes_hook_crud():
    from cognis.tools.introspection import audit_tool_descriptors

    assert audit_tool_descriptors([MANAGE_AGENTS_TOOL]) == []
    actions = {operation.operation for operation in MANAGE_AGENTS_TOOL.native_operations}
    assert {
        "hooks_list",
        "hooks_get",
        "hooks_create",
        "hooks_update",
        "hooks_delete",
        "hook_assignments_get",
        "hook_assignments_put",
        "hook_assignments_remove",
        "hook_runs_list",
    } <= actions


@pytest.mark.asyncio
async def test_bootstrap_and_migration_agree(factory):
    migration = importlib.import_module("cognis.store.migrations.versions.162_agent_hooks")
    engine = factory.kw["bind"]
    async with engine.begin() as connection:

        def check(sync):
            context = MigrationContext.configure(sync)
            with Operations.context(context):
                migration.downgrade()
                migration.upgrade()
            Base.metadata.create_all(sync)
            Base.metadata.create_all(sync)
            from cognis.store.hook_schema import ensure_hook_activation_boundary

            ensure_hook_activation_boundary(sync)
            tables = inspect(sync).get_table_names()
            assert {
                "agent_hooks",
                "agent_hook_assignments",
                "agent_hook_runs",
                "agent_hook_turns",
            } <= set(tables)
            columns = {column["name"] for column in inspect(sync).get_columns("agent_hook_runs")}
            assert "lifecycle_pending" in columns

        await connection.run_sync(check)


@pytest.mark.asyncio
async def test_terminal_lifecycle_delivery_retries_without_reexecuting(factory):
    await assign(factory, hook_id="system:memory-consolidation")
    service = runtime(factory)
    await service.scan()
    original = service.providers.guardrails.record_events.side_effect

    async def unavailable_terminal_card(session_id, events, **kwargs):
        if (
            events[0].data.get("event") == "agent_hook"
            and events[0].data.get("status") == "completed"
        ):
            raise RuntimeError("Temporarily unavailable")
        return await original(session_id, events, **kwargs)

    service.providers.guardrails.record_events.side_effect = unavailable_terminal_card
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        db.add(
            DirectTurnRequestRow(
                request_id="request-terminal-delivery",
                turn_id="child-turn",
                conversation_id="child-hooks",
                agent_id=run.agent_id,
                user_id=run.owner_email,
                idempotency_scope="hook",
                idempotency_key=run.run_id,
                admission_hash="hash",
                payload_hash="hash",
                payload={},
                status="completed",
                terminal_at=datetime.now(UTC),
            )
        )
        await db.commit()
    await service._settle_child("child-hooks", "completed")
    await service.scan()
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        assert run.status == "completed" and run.lifecycle_pending
    service.providers.guardrails.record_events.side_effect = original
    await service.scan()
    service.scheduler.submit_turn.assert_awaited_once()
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        assert run.status == "completed" and not run.lifecycle_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError, RuntimeError])
async def test_crash_after_task_admission_recovers_without_repeating_task(factory, error):
    await assign(
        factory,
        {
            "name": "Tool",
            "trigger": "turn_completed",
            "mode": "fresh",
            "action": "tool_call",
            "tool_call": {
                "tool": "builtin:memory_search",
                "args": {"query": "decisions"},
            },
        },
    )
    service = runtime(factory)
    await service.match(
        conversation=conversation(),
        session=session(),
        triggers=["turn_completed"],
        occurrence="completed:crash",
        context={},
    )

    async def submit_and_crash(**kwargs):
        async with factory() as db:
            db.add(
                Task(
                    task_id=kwargs["task_id"],
                    agent_id="agent-hooks",
                    created_by="owner@hooks.test",
                    title="Hook",
                    status="completed",
                )
            )
            await db.commit()
        raise error

    service.task_queue.submit.side_effect = submit_and_crash
    if error is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await service.scan()
    else:
        await service.scan()
    await service.scan()
    service.task_queue.submit.assert_awaited_once()
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        assert run.status == "completed" and run.task_id


@pytest.mark.asyncio
async def test_fork_boundary_is_frozen_before_later_source_messages(factory):
    await assign(factory, hook_id="system:memory-consolidation")
    service = runtime(factory)
    await service._match_idle()
    stream = service.providers.guardrails.streams["session-hooks"]
    marker = next(event for event in stream if event["data"].get("event") == "hook_snapshot")
    stream.append({"seq": len(stream) + 1, "type": "user_message", "data": {"content": "Later"}})
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
    await service._dispatch(run)
    assert (
        service.session_manager.fork_into_new_conversation.await_args.kwargs["max_source_seq"]
        == marker["seq"] - 1
    )


@pytest.mark.asyncio
async def test_hook_definition_indirect_self_mutation_requires_approval(factory):
    from cognis.core.agent_management import AgentManagementError, handle_agent_management_action

    hook_id = await assign(factory)
    with pytest.raises(AgentManagementError, match="explicit user approval"):
        await handle_agent_management_action(
            deps=SimpleNamespace(session_factory=factory),
            actor_email="owner@hooks.test",
            current_agent_id="agent-hooks",
            arguments={
                "action": "hooks_update",
                "hook_id": hook_id,
                "expected_revision": 1,
                "definition": {"name": "Changed", "trigger": "user_message", "prompt": "Changed"},
            },
        )


@pytest.mark.asyncio
async def test_invalid_runtime_condition_does_not_break_foreground(factory):
    await assign(
        factory,
        {
            "name": "Strict",
            "trigger": "user_message",
            "prompt": "Reminder",
            "condition": "{{ parameters.missing }}",
        },
    )
    service = runtime(factory)
    assert (
        await service.match(
            conversation=conversation(),
            session=session(),
            triggers=["user_message"],
            occurrence="bad-condition",
            context={},
        )
        == []
    )
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))).status == "invalid_condition"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [True, False])
async def test_follow_up_uses_exact_turn_evidence_and_is_not_user_request(factory, wrapped):
    await assign(
        factory,
        {
            "name": "Document changes",
            "trigger": "turn_completed",
            "mode": "follow_up",
            "condition": ("{{ " if wrapped else "")
            + "'implemented' in (last_assistant_message | lower) and work.file_count > 0"
            + (" }}" if wrapped else ""),
            "prompt": "Update docs for {{ work.files_changed | join(', ') }}. {{ last_user_message }}",
        },
    )
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"turn_id": "turn-done", "content": "Private task"},
        },
        {
            "seq": 2,
            "type": "tool_result",
            "data": {
                "turn_id": "turn-done",
                "call_id": "edit-doc",
                "is_error": False,
                "file_diffs": [{"path": "/src/feature.py"}],
            },
        },
        {
            "seq": 3,
            "type": "assistant_message",
            "data": {
                "turn_id": "turn-done",
                "content": "Implemented the feature",
            },
        },
    ]
    await service._on_completed(
        Event(
            type=EventType.TURN_COMPLETED,
            data={
                "conversation_id": "conv-hooks",
                "session_id": "session-hooks",
                "turn_id": "turn-done",
            },
        )
    )
    await service.scan()
    call = service.scheduler.submit_turn.await_args
    if call is None:
        async with factory() as db:
            rows = (await db.scalars(select(AgentHookRunRow))).all()
            raise AssertionError([(row.status, row.configuration) for row in rows])
    assert call.args[0] == "conv-hooks"
    assert "Update docs for /src/feature.py. Private task" in call.args[1]
    assert "not a new user request" in call.args[1]
    assert call.kwargs["allow_queue"] and call.kwargs["system_initiated"]
    assert call.kwargs["follow_up"].title == "Hook: Document changes"
    from cognis.core.followups import is_hook_follow_up
    from cognis.core.turn_scheduler import TurnScheduler, _QueuedMessage

    assert is_hook_follow_up(call.kwargs["follow_up"])
    scheduler = object.__new__(TurnScheduler)
    assert not scheduler._queued_message_is_absorbable(
        _QueuedMessage(
            content=call.args[1],
            user_email="owner@hooks.test",
            system_initiated=True,
            follow_up=call.kwargs["follow_up"],
        )
    )
    service.session_manager.create_conversation_with_root_session.assert_not_awaited()
    async with factory() as db:
        row = await db.scalar(select(AgentHookRunRow))
        assert "Private task" not in str(row.configuration)
        assert "/src/feature.py" not in str(row.configuration)
        assert row.child_conversation_id == "conv-hooks"
    # Completing a different foreground turn must not settle this hook.
    await service._settle_child("conv-hooks", "completed", "turn-user")
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))).status == "running"


@pytest.mark.asyncio
async def test_deliverable_match_starts_a_pinned_fork(factory):
    await assign(
        factory,
        {
            "name": "Ship deliverable",
            "trigger": "turn_completed",
            "mode": "fork",
            "condition": "{{ work.deliverable_count > 0 }}",
            "prompt": "Ship {{ work.deliverables | tojson }} using the configured vault skill.",
        },
    )
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = [
        {"seq": 1, "type": "user_message", "data": {"turn_id": "turn-report", "content": "Report"}},
        {
            "seq": 2,
            "type": "lifecycle",
            "data": {
                "turn_id": "turn-report",
                "event": "assistant_deliverable",
                "deliverable_id": "dlv_report",
                "title": "Verified report",
            },
        },
    ]
    await service._on_completed(
        Event(
            type=EventType.TURN_COMPLETED,
            data={
                "conversation_id": "conv-hooks",
                "session_id": "session-hooks",
                "turn_id": "turn-report",
            },
        )
    )
    await service.scan()
    service.session_manager.fork_into_new_conversation.assert_awaited_once()
    assert "dlv_report" in service.scheduler.submit_turn.await_args.args[1]
    assert "not a new user request" in service.scheduler.submit_turn.await_args.args[1]


@pytest.mark.asyncio
async def test_follow_up_timeout_targets_request_not_parent_turn(factory):
    service = runtime(factory)
    async with factory() as db:
        row = AgentHookRunRow(
            run_id="hrun_follow_timeout",
            hook_id="hook-docs",
            hook_revision=1,
            owner_email="owner@hooks.test",
            agent_id="agent-hooks",
            conversation_id="conv-hooks",
            source_session_id="session-hooks",
            child_conversation_id="conv-hooks",
            occurrence="completed:source",
            status="running",
            configuration={"mode": "follow_up", "timeout_seconds": 10, "name": "Docs"},
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db.add(row)
        db.add(
            DirectTurnRequestRow(
                request_id="request-hook-timeout",
                turn_id="turn-hook-timeout",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_id="owner@hooks.test",
                idempotency_scope="hook",
                idempotency_key=row.run_id,
                admission_hash="hash",
                payload_hash="hash",
                payload={},
                status="running",
            )
        )
        db.add(
            DirectTurnRequestRow(
                request_id="request-user-newer",
                turn_id="turn-user-newer",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_id="owner@hooks.test",
                idempotency_scope="user",
                idempotency_key="newer-user",
                admission_hash="hash",
                payload_hash="hash",
                payload={},
                status="running",
            )
        )
        await db.commit()
    await service._reconcile(row)
    service.scheduler.cancel_request.assert_awaited_once_with("conv-hooks", "request-hook-timeout")
    service.scheduler.cancel_turn.assert_not_awaited()
    from cognis.core.turn_scheduler import TurnScheduler
    from cognis.store.direct_turns import DirectTurnStore

    scheduler = object.__new__(TurnScheduler)
    scheduler._direct_turn_store = DirectTurnStore(factory)
    scheduler._remove_durable_queue_cache_entry = MagicMock()
    scheduler._publish_durable_turn_change = AsyncMock()
    scheduler._notify_queue_updated = AsyncMock()
    scheduler._direct_turn_runtime = SimpleNamespace(wake=AsyncMock())
    scheduler.cluster_signals = SimpleNamespace(publish=AsyncMock())
    assert not await scheduler.cancel_request("another-conversation", "request-hook-timeout")
    assert await scheduler.cancel_request("conv-hooks", "request-hook-timeout")
    async with factory() as db:
        assert (
            await db.scalar(
                select(DirectTurnRequestRow).where(
                    DirectTurnRequestRow.request_id == "request-hook-timeout",
                )
            )
        ).cancel_requested_at is not None
        assert (
            await db.scalar(
                select(DirectTurnRequestRow).where(
                    DirectTurnRequestRow.request_id == "request-user-newer",
                )
            )
        ).cancel_requested_at is None
    signal = scheduler.cluster_signals.publish.await_args.kwargs["scope"]
    assert signal.direct_request_id == "request-hook-timeout"


@pytest.mark.asyncio
async def test_idle_follow_up_does_not_rearm_from_its_own_completion(factory):
    await assign(
        factory,
        {
            "name": "Idle docs",
            "trigger": "conversation_idle",
            "mode": "follow_up",
            "idle_seconds": 60,
            "prompt": "Review docs",
        },
    )
    service = runtime(factory)
    from cognis.store.direct_turns import DirectTurnPayloadV1, DirectTurnStore

    store = DirectTurnStore(factory)
    foreground = await store.admit(
        conversation_id="conv-hooks",
        session_id="session-hooks",
        agent_id="agent-hooks",
        user_id="owner@hooks.test",
        idempotency_scope="user",
        idempotency_key="source",
        turn_id="source-turn",
        payload=DirectTurnPayloadV1(
            content="Please do the work",
            metadata={"system_initiated": False},
        ).model_dump(mode="json"),
    )
    async with factory() as db:
        request = await db.get(DirectTurnRequestRow, foreground.request.admission_order)
        request.status = "completed"
        request.terminal_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
    await service.scan()
    service.scheduler.submit_turn.assert_awaited_once()
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
    automated = await store.admit(
        conversation_id="conv-hooks",
        session_id="session-hooks",
        agent_id="agent-hooks",
        user_id="owner@hooks.test",
        idempotency_scope="hook",
        idempotency_key=run.run_id,
        turn_id="hook-turn",
        payload=DirectTurnPayloadV1(
            content="Automated hook",
            metadata={"system_initiated": True},
        ).model_dump(mode="json"),
    )
    async with factory() as db:
        request = await db.get(DirectTurnRequestRow, automated.request.admission_order)
        request.status = "completed"
        request.terminal_at = datetime.now(UTC)
        run = await db.get(AgentHookRunRow, run.run_id)
        run.status = "completed"
        source = await db.get(Conversation, "conv-hooks")
        source.last_message_at = datetime.now(UTC) - timedelta(minutes=5)
        await db.commit()
    await service.scan()
    await service.scan()
    service.scheduler.submit_turn.assert_awaited_once()
    async with factory() as db:
        assert len((await db.scalars(select(AgentHookRunRow))).all()) == 1


@pytest.mark.asyncio
async def test_literal_template_metadata_does_not_break_foreground(factory):
    await assign(
        factory,
        {
            "name": "Literal {{",
            "description": "Unfinished {{",
            "parameters": {"note": "{{ work"},
            "trigger": "user_message",
            "prompt": "Static reminder",
        },
    )
    service = runtime(factory)
    result = await service.before_cycle(
        SimpleNamespace(
            conversation=conversation(),
            session=session(),
            turn_id="literal",
            system_initiated=False,
            task_id=None,
            agent=SimpleNamespace(owner_email="owner@hooks.test", agent_id="agent-hooks"),
        ),
        1,
    )
    assert len(result) == 1
    assert "Static reminder" in result[0]["content"]
    service.providers.guardrails.read_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_session_manager_fork_preserves_prefix_and_owner_scope(factory):
    from cognis.core.session import SessionManager
    from cognis.core.session_cache import SessionCache

    await assign(factory, hook_id="system:memory-consolidation")
    service = runtime(factory)
    guardrails = service.providers.guardrails
    guardrails.create_session = AsyncMock()
    guardrails.streams["session-hooks"][:] = [
        {
            "seq": 1,
            "type": "system_message",
            "data": {
                "role": "system",
                "source": "identity",
                "content": "Stable identity",
            },
        },
        {"seq": 2, "type": "context_snapshot", "data": {"entries": [{"seq": 1}]}},
        {"seq": 3, "type": "user_message", "data": {"content": "Keep this decision"}},
    ]
    service.session_cache = SessionCache(guardrails)
    service.session_manager = SessionManager(factory, service.providers, service.session_cache)
    await service.scan()
    service.scheduler.submit_turn.assert_awaited_once()
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        child = await db.get(Conversation, run.child_conversation_id)
        child_session = await db.get(Session, child.active_session_id)
    stream = guardrails.streams[child_session.intaris_session_id or child_session.session_id]
    assert any(event["data"].get("content") == "Stable identity" for event in stream)
    assert any(event["data"].get("event") == "history_copy_completed" for event in stream)
    assert child.context_data["hook_run_id"] == run.run_id
    assert run.status == "running"


@pytest.fixture
async def live(factory):
    service = runtime(factory)
    recovered = asyncio.Event()

    async def recovery():
        recovered.set()

    service.scan = AsyncMock(side_effect=recovery)
    service.start(scheduler=service.scheduler, task_queue=service.task_queue)
    await asyncio.wait_for(recovered.wait(), 2)
    yield service
    await service.stop()
    assert not service._deadlines
    assert not service._jobs


async def drain_jobs(service):
    async with asyncio.timeout(5):
        while service._jobs:
            await asyncio.gather(*list(service._jobs.values()))


@pytest.mark.asyncio
async def test_match_dispatches_without_recovery_scan(factory, live):
    await assign(
        factory,
        {
            "name": "Now",
            "trigger": "user_message",
            "mode": "fresh",
            "prompt": "Check",
        },
    )
    await live.match(
        conversation=conversation(),
        session=session(),
        triggers=["user_message"],
        occurrence="turn:live",
        context={},
    )
    await drain_jobs(live)
    live.scheduler.submit_turn.assert_awaited_once()
    live.scan.assert_awaited_once()
    assert any(key.startswith("hook-timeout:") for key in live._deadlines)


@pytest.mark.asyncio
async def test_fast_task_completion_during_admission_is_not_lost(factory, live):
    await assign(
        factory,
        {
            "name": "Tool",
            "trigger": "turn_completed",
            "mode": "fresh",
            "action": "tool_call",
            "tool_call": {"tool": "builtin:memory_search", "args": {"query": "decisions"}},
        },
    )

    async def submit(**kwargs):
        async with factory() as db:
            db.add(
                Task(
                    task_id=kwargs["task_id"],
                    agent_id="agent-hooks",
                    created_by="owner@hooks.test",
                    title="Hook",
                    status="completed",
                )
            )
            await db.commit()
        await live.event_bus.publish(
            Event(
                type=EventType.TASK_COMPLETED,
                data={"task_id": kwargs["task_id"]},
            )
        )
        return SimpleNamespace(task_id=kwargs["task_id"])

    live.task_queue.submit.side_effect = submit
    await live.match(
        conversation=conversation(),
        session=session(),
        triggers=["turn_completed"],
        occurrence="completed:fast",
        context={},
    )
    await drain_jobs(live)
    live.task_queue.submit.assert_awaited_once()
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        assert run.status == "completed"
        assert "hook-timeout:" + run.run_id not in live._deadlines
    live.scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_sleeping_idle_deadline_has_no_database_work(factory, live):
    await assign(factory, hook_id="system:memory-consolidation")
    async with factory() as db:
        row = await db.get(Conversation, "conv-hooks")
        row.last_message_at = datetime.now(UTC)
        await db.commit()
    # Scheduler publishes completion before releasing its durable admission.
    live.scheduler.durable_running_turn_state.return_value = {"turn_id": "turn-idle"}
    await live._on_completed(
        Event(
            type=EventType.TURN_COMPLETED,
            data={
                "conversation_id": "conv-hooks",
                "session_id": "session-hooks",
                "turn_id": "turn-idle",
            },
        )
    )
    assert "hook-idle:conv-hooks" in live._deadlines
    queries = []
    engine = factory.kw["bind"].sync_engine

    def record(*args):
        queries.append(args[2])

    sql_event.listen(engine, "before_cursor_execute", record)
    try:
        await asyncio.sleep(0.05)
        assert queries == []
        assert RECOVERY_SECONDS == 600
        live.scan.assert_awaited_once()
    finally:
        sql_event.remove(engine, "before_cursor_execute", record)
    await live.event_bus.publish(
        Event(
            type=EventType.USER_MESSAGE,
            data={
                "conversation_id": "conv-hooks",
            },
        )
    )
    assert "hook-idle:conv-hooks" not in live._deadlines
    live.scheduler.cancel_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_unadmitted_dispatch_does_not_spin_on_lifecycle_outage(factory, live):
    await assign(
        factory,
        {
            "name": "Expired",
            "trigger": "user_message",
            "mode": "fresh",
            "prompt": "Check",
        },
    )
    source = runtime(factory)
    await source.match(
        conversation=conversation(),
        session=session(),
        triggers=["user_message"],
        occurrence="turn:expired",
        context={},
    )
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        run.status = "dispatching"
        run.created_at = datetime.now(UTC) - timedelta(hours=1)
        run_id = run.run_id
        await db.commit()
    live.publish = AsyncMock(return_value=False)
    await live._run(run_id)
    await asyncio.sleep(0.02)
    assert "hook-timeout:" + run_id not in live._deadlines
    live.publish.assert_awaited_once()
    live.scheduler.submit_turn.assert_not_awaited()
    async with factory() as db:
        run = await db.get(AgentHookRunRow, run_id)
        assert run.status == "timed_out"
        assert run.lifecycle_pending


@pytest.mark.asyncio
async def test_dispatch_crossing_timeout_is_cancelled_immediately(factory, live):
    await assign(
        factory,
        {
            "name": "Slow",
            "trigger": "user_message",
            "mode": "fresh",
            "prompt": "Check",
        },
    )
    original = live.scheduler.submit_turn.side_effect

    async def slow_admission(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with factory() as db:
            run = await db.scalar(select(AgentHookRunRow))
            # Simulate time spent in successful admission/publication.
            run.created_at = datetime.now(UTC) - timedelta(hours=1)
            await db.commit()
        return result

    live.scheduler.submit_turn.side_effect = slow_admission
    await live.match(
        conversation=conversation(),
        session=session(),
        triggers=["user_message"],
        occurrence="turn:slow",
        context={},
    )
    await drain_jobs(live)
    live.scheduler.cancel_turn.assert_awaited_once_with("child-hooks", clear_queue=False)
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        assert run.status == "timed_out"
        assert "hook-timeout:" + run.run_id not in live._deadlines


@pytest.mark.asyncio
async def test_recovery_interval_claim_skips_other_controllers(factory):
    from cognis.store.coordination import DatabaseLeaseStore

    first, second = runtime(factory), runtime(factory)
    first.lease_store = second.lease_store = DatabaseLeaseStore(factory)
    first.scan, second.scan = AsyncMock(), AsyncMock()
    await first._recover_once()
    # The claim remains after the first scan completes, without renewal.
    await second._recover_once()
    first.scan.assert_awaited_once()
    second.scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_timer_fires_once_without_polling(factory, live):
    await assign(factory, hook_id="system:memory-consolidation")
    async with factory() as db:
        row = await db.get(Conversation, "conv-hooks")
        row.last_message_at = datetime.now(UTC) - timedelta(seconds=899.8)
        await db.commit()
    fired = asyncio.Event()
    submit = live.scheduler.submit_turn.side_effect

    async def observed_submit(*args, **kwargs):
        result = await submit(*args, **kwargs)
        fired.set()
        return result

    live.scheduler.submit_turn.side_effect = observed_submit
    await live._match_idle("conv-hooks")
    await asyncio.wait_for(fired.wait(), 3)
    await drain_jobs(live)
    await live._match_idle("conv-hooks")
    assert "hook-idle:conv-hooks" not in live._deadlines
    live.scheduler.submit_turn.assert_awaited_once()
    live.scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_configuration_event_refreshes_and_removes_local_idle_deadline(factory, live):
    await assign(factory, hook_id="system:memory-consolidation")
    async with factory() as db:
        row = await db.get(Conversation, "conv-hooks")
        row.last_message_at = datetime.now(UTC)
        await db.commit()
    await notify_hook_change(live.event_bus, "hook_assignments_put", {"agent_id": "agent-hooks"})
    await drain_jobs(live)
    assert "hook-idle:conv-hooks" in live._deadlines
    async with factory() as db:
        await manage_hooks(
            db,
            owner="owner@hooks.test",
            action="hook_assignments_remove",
            agent_id="agent-hooks",
            hook_id="system:memory-consolidation",
        )
        await db.commit()
    await notify_hook_change(live.event_bus, "hook_assignments_remove", {"agent_id": "agent-hooks"})
    await drain_jobs(live)
    assert "hook-idle:conv-hooks" not in live._deadlines


@pytest.mark.asyncio
async def test_recovery_rearms_future_deadline(factory, live):
    await assign(factory, hook_id="system:memory-consolidation")
    async with factory() as db:
        row = await db.get(Conversation, "conv-hooks")
        row.last_message_at = datetime.now(UTC)
        await db.commit()
    await HookRuntime.scan(live)
    assert "hook-idle:conv-hooks" in live._deadlines
    live.scheduler.submit_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_idle_queries_for_agents_with_injection_hooks(factory):
    await assign(factory, hook_id="system:recall-reminder")
    service = runtime(factory)
    await service.scan()
    service.scheduler.durable_running_turn_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_is_released_after_work_not_held_during_sleep(factory):
    service = runtime(factory)
    service.lease_store = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace()),
        renew=AsyncMock(),
        release=AsyncMock(),
    )
    operation = AsyncMock()
    await service._leased("hook-test", operation)
    operation.assert_awaited_once()
    service.lease_store.acquire.assert_awaited_once()
    service.lease_store.release.assert_awaited_once()
    service.lease_store.renew.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["read", "edit", "deliverable"])
async def test_self_improvement_is_ready_to_enable_and_executes_fork(factory, evidence):
    await assign(factory, hook_id="system:self-improvement")
    await make_learning_due(factory)
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = learning_events(evidence)
    await service.scan()
    service.session_manager.fork_into_new_conversation.assert_awaited_once()
    prompt = service.scheduler.submit_turn.await_args.args[1]
    for required in (
        "skill_list",
        "skill_get",
        "skill_patch",
        "skill_write",
        "skill_load",
        "attach_to_all_agents=false",
        "Never edit system skills or bundled skills",
        "Verify ownership from skill metadata before any update",
        "Do not request approval to modify a system skill from this hook",
        "without writing",
        "verified",
    ):
        assert required in prompt
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        assert run.hook_id == "system:self-improvement"
        assert run.status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools",
    [
        {"deny_tools": ["builtin:skill_write"]},
        {"disabled_categories": ["skill"]},
        {"builtin_tools": ["skill_list", "skill_get", "skill_load"]},
    ],
)
async def test_learning_requires_canonical_skill_tool_permissions(factory, tools):
    async with factory() as db:
        agent = await db.get(Agent, "agent-hooks")
        agent.tools = tools
        await db.commit()
    with pytest.raises(HookError, match="Self-improvement requires"):
        await assign(factory, hook_id="system:self-improvement")


@pytest.mark.asyncio
async def test_learning_rechecks_revoked_tools_at_dispatch(factory):
    await assign(factory, hook_id="system:self-improvement")
    await make_learning_due(factory)
    service = runtime(factory, legacy_detached=False)
    service.providers.guardrails.streams["session-hooks"] = learning_events("read")
    agent = service.agent_registry.get.return_value
    agent.tools = {"deny_tools": ["builtin:skill_write"]}
    await service.scan()
    service.scheduler.submit_turn.assert_not_awaited()
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))).status == "failed"
    service.session_manager.create_conversation_with_root_session.assert_not_awaited()
    service.session_manager.fork_into_new_conversation.assert_not_awaited()


async def make_learning_due(factory):
    async with factory() as db:
        source = await db.get(Conversation, "conv-hooks")
        source.last_message_at = datetime.now(UTC) - timedelta(minutes=31)
        await db.commit()


def learning_events(evidence):
    events = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"turn_id": "turn-learning", "content": "Investigate this"},
        }
    ]
    if evidence in {"read", "edit", "failed"}:
        events.append(
            {
                "seq": 2,
                "type": "tool_result",
                "data": {
                    "turn_id": "turn-learning",
                    "call_id": "learning-call",
                    "is_error": evidence == "failed",
                    "file_diffs": [{"path": "/src/feature.py"}] if evidence == "edit" else [],
                },
            }
        )
    elif evidence == "deliverable":
        events.append(
            {
                "seq": 2,
                "type": "lifecycle",
                "data": {
                    "turn_id": "turn-learning",
                    "event": "assistant_deliverable",
                    "deliverable_id": "dlv_learning_report",
                    "title": "Investigation",
                },
            }
        )
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["chat", "failed", "older_work"])
async def test_learning_gate_preserves_earlier_work_but_skips_empty_windows(factory, evidence):
    await assign(factory, hook_id="system:self-improvement")
    await make_learning_due(factory)
    service = runtime(factory)
    events = learning_events("read" if evidence == "older_work" else evidence)
    if evidence == "older_work":
        events.append(
            {
                "seq": 3,
                "type": "user_message",
                "data": {"turn_id": "turn-thanks", "content": "Thanks"},
            }
        )
    service.providers.guardrails.streams["session-hooks"] = events
    await service.scan()
    if evidence == "older_work":
        service.scheduler.submit_turn.assert_awaited_once()
        service.session_manager.fork_into_new_conversation.assert_awaited_once()
        return
    service.scheduler.submit_turn.assert_not_awaited()
    service.session_manager.fork_into_new_conversation.assert_not_awaited()
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))) is None
    assert BUNDLED_HOOKS["system:memory-consolidation"].condition is None


@pytest.mark.asyncio
async def test_learning_waits_twenty_five_minutes_but_memory_keeps_fifteen(factory):
    assert BUNDLED_HOOKS["system:self-improvement"].idle_seconds == 1500
    assert BUNDLED_HOOKS["system:memory-consolidation"].idle_seconds == 900
    await assign(factory, hook_id="system:self-improvement")
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = learning_events("read")
    async with factory() as db:
        source = await db.get(Conversation, "conv-hooks")
        source.last_message_at = datetime.now(UTC) - timedelta(minutes=20)
        await db.commit()
    await service.scan()
    service.scheduler.submit_turn.assert_not_awaited()
    async with factory() as db:
        source = await db.get(Conversation, "conv-hooks")
        source.last_message_at = datetime.now(UTC) - timedelta(minutes=31)
        await db.commit()
    await service.scan()
    service.scheduler.submit_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_configuration_refresh_bounds_pre_run_evidence(factory, live):
    await assign(factory, hook_id="system:self-improvement")
    await make_learning_due(factory)
    async with factory() as db:
        for index in range(12):
            cid, sid = f"idle-{index}", f"idle-session-{index}"
            db.add(
                Conversation(
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                    context_type="web",
                    active_session_id=sid,
                    last_message_at=datetime.now(UTC) - timedelta(minutes=31),
                )
            )
            db.add(
                Session(
                    session_id=sid,
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                )
            )
            await db.flush()
            db.add(
                DirectTurnRequestRow(
                    turn_id=f"turn-{cid}",
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_id="owner@hooks.test",
                    idempotency_scope="user",
                    idempotency_key=cid,
                    admission_hash="hash",
                    payload_hash="hash",
                    payload={"metadata": {"system_initiated": False}},
                    status="completed",
                    created_at=datetime(2026, 2, 1, tzinfo=UTC),
                )
            )
            live.providers.guardrails.streams[sid] = learning_events("chat")
        await db.commit()
    live.providers.guardrails.streams["session-hooks"] = learning_events("chat")
    release, saturated = asyncio.Event(), asyncio.Event()
    active = peak = 0
    read = live.providers.guardrails.read_events.side_effect

    async def blocked_read(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active >= 1:
            saturated.set()
        try:
            await release.wait()
            return await read(*args, **kwargs)
        finally:
            active -= 1

    live.providers.guardrails.read_events.side_effect = blocked_read
    try:
        await notify_hook_change(
            live.event_bus, "hook_assignments_put", {"agent_id": "agent-hooks"}
        )
        await asyncio.wait_for(saturated.wait(), 2)
        await asyncio.sleep(0.1)
        assert peak == 1
        assert list(live._jobs) == ["hook-config"]
        async with factory() as db:
            assert (await db.scalar(select(AgentHookRunRow))) is None
    finally:
        release.set()
        await drain_jobs(live)
    assert live.providers.guardrails.read_events.await_count == 13
    live.scheduler.submit_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_matched", [False, True])
async def test_idle_match_checks_eligibility_before_evidence(factory, already_matched):
    await assign(factory, hook_id="system:self-improvement")
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = learning_events("read")
    source = session()
    source.started_at = datetime.now(UTC)
    arguments = {
        "conversation": conversation(),
        "session": source,
        "triggers": ["conversation_idle"],
        "occurrence": "idle:fixture-turn",
        "context": {"idle": {"seconds": 1800 if already_matched else 60}},
    }
    if already_matched:
        await service.match(**arguments)
        service.providers.guardrails.read_events.reset_mock()
        # A restarted runtime has no local evidence cache.
        service = runtime(factory)
    await service.match(**arguments)
    service.providers.guardrails.read_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_recovery_reuses_evidence_until_foreground_occurrence_changes(factory):
    await assign(factory, hook_id="system:self-improvement")
    await make_learning_due(factory)
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = learning_events("chat")
    await service.scan()
    await service.scan()
    service.providers.guardrails.read_events.assert_awaited_once()
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))) is None
        source = await db.get(Conversation, "conv-hooks")
        source.last_message_at = datetime.now(UTC) - timedelta(minutes=32)
        request = await db.scalar(select(DirectTurnRequestRow))
        request.turn_id = "new-foreground-turn"
        await db.commit()
    service.providers.guardrails.streams["session-hooks"] = learning_events("read")
    await service.scan()
    service.scheduler.submit_turn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_idle_evidence_failure_is_not_cached_and_releases_capacity(factory, failure):
    await assign(factory, hook_id="system:self-improvement")
    await make_learning_due(factory)
    service = runtime(factory)
    service._evidence_slots = asyncio.Semaphore(1)
    service.providers.guardrails.streams["session-hooks"] = learning_events("chat")
    read = service.providers.guardrails.read_events.side_effect
    service.providers.guardrails.read_events.side_effect = failure
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await service.scan()
    else:
        await service.scan()
    assert not service._idle_evidence
    service.providers.guardrails.read_events.side_effect = read
    async with asyncio.timeout(2):
        await service.scan()
    assert service.providers.guardrails.read_events.await_count == 2
    assert len(service._idle_evidence) == 1


@pytest.mark.asyncio
async def test_idle_evidence_cache_is_bounded(factory, monkeypatch):
    monkeypatch.setattr("cognis.core.hook_runtime.MAX_IDLE_EVIDENCE", 1)
    await assign(factory, hook_id="system:self-improvement")
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = learning_events("chat")
    for minutes in (31, 32, 31):
        async with factory() as db:
            source = await db.get(Conversation, "conv-hooks")
            source.last_message_at = datetime(2026, 10, 1, tzinfo=UTC) - timedelta(minutes=minutes)
            request = await db.scalar(select(DirectTurnRequestRow))
            request.turn_id = f"foreground-{minutes}"
            await db.commit()
        await service.scan()
        assert len(service._idle_evidence) == 1
    assert service.providers.guardrails.read_events.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "waiting"], ids=["active", "recoverable"])
async def test_idle_evidence_waits_for_settlement_and_includes_late_result(factory, status):
    await assign(factory, hook_id="system:self-improvement")
    await make_learning_due(factory)
    service = runtime(factory)
    stream = service.providers.guardrails.streams["session-hooks"] = learning_events("chat")
    service.scheduler.durable_running_turn_state.return_value = {
        "turn_id": "turn-learning",
        "status": status,
    }
    await service._match_idle("conv-hooks")
    service.providers.guardrails.read_events.assert_not_awaited()
    assert not service._idle_evidence

    # The canonical stream receives a late result while durable ownership remains active.
    stream.append(learning_events("read")[1])
    await service._match_idle("conv-hooks")
    service.providers.guardrails.read_events.assert_not_awaited()
    assert not service._idle_evidence
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))) is None

    service.scheduler.durable_running_turn_state.return_value = None
    await service._match_idle("conv-hooks")
    service.providers.guardrails.read_events.assert_awaited_once()
    evidence = next(iter(service._idle_evidence.values()))
    assert evidence["evidence_turn_id"] == "turn-learning"
    assert evidence["work"]["successful_tool_calls"] == 1
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "preparing",
        "pending",
        "dispatching",
        "running",
        "completed",
        "failed",
        "cancelled",
        "timed_out",
        "skipped",
        "invalid_condition",
    ],
)
async def test_existing_hook_status_suppresses_evidence_after_restart(factory, status):
    await assign(factory, hook_id="system:self-improvement")
    source = session()
    source.started_at = datetime.now(UTC)
    arguments = {
        "conversation": conversation(),
        "session": source,
        "triggers": ["conversation_idle"],
        "occurrence": "idle:fixture-turn",
        "context": {"idle": {"seconds": 1800}},
    }
    original = runtime(factory)
    original.providers.guardrails.streams["session-hooks"] = learning_events("read")
    await original.match(**arguments)
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        run.status = status
        run_id = run.run_id
        await db.commit()

    restarted = runtime(factory)
    restarted._wake_run = MagicMock()
    await restarted.match(**arguments)
    restarted.providers.guardrails.read_events.assert_not_awaited()
    restarted.providers.guardrails.record_events.assert_not_awaited()
    assert not restarted._idle_evidence
    if status == "pending":
        restarted._wake_run.assert_called_once_with(run_id)
    else:
        restarted._wake_run.assert_not_called()
    async with factory() as db:
        runs = (await db.scalars(select(AgentHookRunRow))).all()
        assert len(runs) == 1
        assert runs[0].status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_id", ["system:memory-consolidation", "system:self-improvement"])
async def test_real_manage_enable_does_not_spawn_historical_conversation_jobs(
    factory, live, hook_id
):
    from cognis.core.agent_management import (
        AgentManagementDependencies,
        handle_agent_management_action,
    )

    async with factory() as db:
        for index in range(100):
            db.add(
                Conversation(
                    conversation_id=f"historical-{index}",
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                    context_type="web",
                    active_session_id="session-hooks",
                    last_message_at=datetime.now(UTC) - timedelta(days=30),
                )
            )
        await db.commit()
    spawned = []
    spawn = live._spawn

    def track_spawn(key, operation):
        spawned.append(key)
        spawn(key, operation)

    live._spawn = track_spawn
    result = await handle_agent_management_action(
        deps=AgentManagementDependencies(session_factory=factory, event_bus=live.event_bus),
        actor_email="owner@hooks.test",
        current_agent_id="agent-hooks",
        self_mutation_approved=True,
        arguments={
            "action": "hook_assignments_put",
            "agent_id": "agent-hooks",
            "assignment": {"hook_id": hook_id, "enabled": True},
        },
    )
    assert result["enabled"]
    await drain_jobs(live)
    assert spawned == ["hook-config"]
    await HookRuntime.scan(live)
    await live._match_idle("conv-hooks")
    await live._on_error(Event(type=EventType.TURN_ERROR, data={"conversation_id": "conv-hooks"}))
    await live._on_completed(
        Event(
            type=EventType.TURN_COMPLETED,
            data={
                "conversation_id": "conv-hooks",
                "session_id": "session-hooks",
                "turn_id": "fixture-turn",
            },
        )
    )
    await live.match(
        conversation=conversation(),
        session=session(),
        triggers=["conversation_idle"],
        occurrence="idle:fixture-turn",
        context={"idle": {"seconds": 100000}},
    )
    restarted = runtime(factory)
    await restarted.scan()
    for service in (live, restarted):
        assert not service._deadlines
        service.providers.guardrails.read_events.assert_not_awaited()
        service.providers.guardrails.record_events.assert_not_awaited()
        service.scheduler.submit_turn.assert_not_awaited()
    async with factory() as db:
        assert await db.scalar(select(AgentHookRunRow)) is None


@pytest.mark.asyncio
async def test_activation_changes_only_on_enable_transition(factory):
    hook_id = await assign(factory, hook_id="system:memory-consolidation", prospective=True)
    async with factory() as db:
        row = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        initial = row.activated_at
    for enabled, parameters in [(True, {}), (True, {"topic": "edited"}), (False, {})]:
        async with factory() as db:
            await manage_hooks(
                db,
                owner="owner@hooks.test",
                action="hook_assignments_put",
                agent_id="agent-hooks",
                assignment={"hook_id": hook_id, "enabled": enabled, "parameters": parameters},
            )
            await db.commit()
            assert (
                await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
            ).activated_at == initial
    await assign(factory, hook_id=hook_id, prospective=True)
    async with factory() as db:
        row = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        assert row.activated_at > initial
    restarted = runtime(factory)
    await restarted.scan()
    restarted.providers.guardrails.record_events.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_old_turn_or_legacy_last_message_cannot_cross_activation(factory, legacy):
    await assign(factory, hook_id="system:memory-consolidation", prospective=True)
    async with factory() as db:
        if legacy:
            await db.execute(delete(DirectTurnRequestRow))
        else:
            request = await db.scalar(select(DirectTurnRequestRow))
            # Finishing after enable is not a new foreground admission.
            request.terminal_at = datetime.now(UTC)
        source = await db.get(Conversation, "conv-hooks")
        source.last_message_at = datetime.now(UTC)
        await db.commit()
    service = runtime(factory)
    await service.scan()
    assert not service._deadlines
    service.providers.guardrails.record_events.assert_not_awaited()
    service.scheduler.durable_running_turn_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_conversation", [False, True])
async def test_new_foreground_after_enable_arms_and_recovers_idle_timer(
    factory, live, new_conversation
):
    await assign(factory, hook_id="system:memory-consolidation", prospective=True)
    cid = "new-conversation" if new_conversation else "conv-hooks"
    async with factory() as db:
        if new_conversation:
            db.add(
                Conversation(
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                    context_type="web",
                    active_session_id=f"session-{cid}",
                    last_message_at=datetime.now(UTC),
                )
            )
            await db.flush()
            db.add(
                Session(
                    session_id=f"session-{cid}",
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                )
            )
        db.add(
            DirectTurnRequestRow(
                turn_id="post-enable-turn",
                conversation_id=cid,
                agent_id="agent-hooks",
                user_id="owner@hooks.test",
                idempotency_scope="user",
                idempotency_key="post-enable-turn",
                admission_hash="hash",
                payload_hash="hash",
                payload={"metadata": {"system_initiated": False}},
                status="completed",
                terminal_at=datetime.now(UTC),
            )
        )
        await db.commit()
    await notify_hook_change(live.event_bus, "hook_assignments_put", {"agent_id": "agent-hooks"})
    await drain_jobs(live)
    assert "hook-idle:" + cid in live._deadlines
    live._cancel_deadline("hook-idle:" + cid)
    await HookRuntime.scan(live)
    assert "hook-idle:" + cid in live._deadlines
    live.providers.guardrails.record_events.assert_not_awaited()
    # Recovery after a missed deadline uses the same persisted eligible occurrence.
    async with factory() as db:
        request = await db.scalar(
            select(DirectTurnRequestRow).where(DirectTurnRequestRow.turn_id == "post-enable-turn")
        )
        request.terminal_at = datetime.now(UTC) - timedelta(minutes=16)
        await db.commit()
    restarted = runtime(factory)
    await restarted.scan()
    restarted.scheduler.submit_turn.assert_awaited_once()
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))).occurrence == "idle:post-enable-turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["preparing", "pending", "dispatching"])
async def test_reenable_fences_unadmitted_idle_runs_before_snapshot_reads(factory, status):
    hook_id = await assign(factory, hook_id="system:memory-consolidation")
    service = runtime(factory)
    await service._match_idle("conv-hooks")
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        run.status = status
        assignment = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        assignment.enabled = False
        await db.commit()
    await assign(factory, hook_id=hook_id, prospective=True)
    restarted = runtime(factory)
    await restarted.scan()
    restarted.providers.guardrails.read_events.assert_not_awaited()
    restarted.scheduler.submit_turn.assert_not_awaited()
    async with factory() as db:
        assert (await db.scalar(select(AgentHookRunRow))).status == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize("upgrade_path", ["bootstrap", "alembic"])
async def test_activation_schema_upgrade_fences_legacy_assignments_once(factory, upgrade_path):
    from cognis.bootstrap import run_schema_bootstrap
    from cognis.store.hook_schema import ensure_hook_activation_boundary

    migration = importlib.import_module(
        "cognis.store.migrations.versions.163_hook_activation_boundary"
    )
    await assign(factory, hook_id="system:memory-consolidation")
    engine = factory.kw["bind"]
    async with engine.begin() as connection:

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()

        await connection.run_sync(downgrade)

        def constraints(sync):
            inspector = inspect(sync)
            return (
                inspector.get_pk_constraint("agent_hook_assignments")["constrained_columns"],
                inspector.get_foreign_keys("agent_hook_assignments"),
            )

        original_constraints = await connection.run_sync(constraints)
    before = datetime.now(UTC)
    if upgrade_path == "bootstrap":
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
    else:
        async with engine.begin() as connection:

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()
                    migration.upgrade()
                ensure_hook_activation_boundary(sync)

            await connection.run_sync(upgrade)
    async with factory() as db:
        assignment = await db.get(
            AgentHookAssignmentRow, ("agent-hooks", "system:memory-consolidation")
        )
        activated_at = assignment.activated_at
        aware = activated_at.replace(tzinfo=UTC) if activated_at.tzinfo is None else activated_at
        assert before <= aware <= datetime.now(UTC)
    await run_schema_bootstrap(engine)
    async with engine.connect() as connection:
        assert await connection.run_sync(constraints) == original_constraints
    async with factory() as db:
        assert (
            await db.get(AgentHookAssignmentRow, ("agent-hooks", "system:memory-consolidation"))
        ).activated_at == activated_at
        assert await db.scalar(text("SELECT COUNT(*) FROM agent_hook_assignments")) == 1
    service = runtime(factory)
    await service.scan()
    service.providers.guardrails.record_events.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["automation", "absorbed", "equal", "missing-boundary"])
async def test_only_strictly_new_foreground_admissions_qualify(factory, kind):
    hook_id = await assign(factory, hook_id="system:memory-consolidation", prospective=True)
    async with factory() as db:
        assignment = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        request = await db.scalar(select(DirectTurnRequestRow))
        boundary = assignment.activated_at
        request.created_at = boundary if kind == "equal" else boundary + timedelta(seconds=1)
        if kind == "automation":
            request.payload = {"metadata": {"system_initiated": True}}
        elif kind == "absorbed":
            request.status = "absorbed"
        elif kind == "missing-boundary":
            assignment.activated_at = None
        await db.commit()
    service = runtime(factory)
    await service.scan()
    service.providers.guardrails.read_events.assert_not_awaited()
    service.providers.guardrails.record_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_idle_assignment_has_its_own_boundary(factory):
    await assign(factory, hook_id="system:memory-consolidation")
    await assign(factory, hook_id="system:self-improvement", prospective=True)
    await make_learning_due(factory)
    service = runtime(factory)
    service.providers.guardrails.streams["session-hooks"] = learning_events("read")
    await service._match_idle("conv-hooks")
    service.providers.guardrails.read_events.assert_not_awaited()
    async with factory() as db:
        runs = (await db.scalars(select(AgentHookRunRow))).all()
        assert [run.hook_id for run in runs] == ["system:memory-consolidation"]


@pytest.mark.asyncio
async def test_non_idle_and_definition_edits_preserve_activation_contract(factory):
    hook_id = await assign(factory, prospective=True)
    async with factory() as db:
        assignment = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        initial = assignment.activated_at
        await manage_hooks(
            db,
            owner="owner@hooks.test",
            action="hooks_update",
            hook_id=hook_id,
            expected_revision=1,
            definition={"name": "Edited", "trigger": "user_message", "prompt": "Edited"},
        )
        await db.commit()
        assert assignment.activated_at == initial
    service = runtime(factory)
    # The source turn predates activation: non-idle matching is intentionally unaffected.
    messages = await service.match(
        conversation=conversation(),
        session=session(),
        triggers=["user_message"],
        occurrence="turn:fixture-turn",
        context={},
    )
    assert len(messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["reenable", "claim"])
async def test_activation_and_dispatch_claim_are_transactionally_ordered(factory, winner):
    hook_id = await assign(factory, hook_id="system:memory-consolidation")
    service = runtime(factory)
    await service._match_idle("conv-hooks")
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
    entered, release = asyncio.Event(), asyncio.Event()

    async def reenable():
        async with factory() as db:
            for enabled in (False, True):
                await manage_hooks(
                    db,
                    owner="owner@hooks.test",
                    action="hook_assignments_put",
                    agent_id="agent-hooks",
                    assignment={"hook_id": hook_id, "enabled": enabled},
                )
            await db.commit()

    if winner == "reenable":

        async def before_lock(*args):
            entered.set()
            await release.wait()
            return None

        service.scheduler.durable_running_turn_state.side_effect = before_lock
    else:
        origin = service._idle_origin

        async def under_lock(*args):
            entered.set()
            await release.wait()
            return await origin(*args)

        service._idle_origin = under_lock
    dispatch = asyncio.create_task(service._dispatch(run))
    change = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        change = asyncio.create_task(reenable())
        if winner == "reenable":
            await asyncio.wait_for(asyncio.shield(change), 2)
        else:
            await asyncio.sleep(0.05)
            assert not change.done(), "Activation must wait for the assignment/claim transaction"
    finally:
        release.set()
        await dispatch
        if change is not None:
            await change
    async with factory() as db:
        persisted = await db.get(AgentHookRunRow, run.run_id)
        assert persisted.status == ("skipped" if winner == "reenable" else "running")
    if winner == "reenable":
        service.providers.guardrails.read_events.assert_not_awaited()
        service.scheduler.submit_turn.assert_not_awaited()
    else:
        service.scheduler.submit_turn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "completed", "ambiguous"])
async def test_reenable_reconciles_admitted_or_uncertain_dispatch_without_resubmit(factory, status):
    hook_id = await assign(factory, hook_id="system:memory-consolidation")
    source = runtime(factory)
    await source._match_idle("conv-hooks")
    async with factory() as db:
        run = await db.scalar(select(AgentHookRunRow))
        run.status = "dispatching"
        # The durable request is authoritative even if the hook's link was not recorded.
        db.add(
            DirectTurnRequestRow(
                turn_id="admitted-hook-turn",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_id="owner@hooks.test",
                idempotency_scope="hook",
                idempotency_key=run.run_id,
                admission_hash="hash",
                payload_hash="hash",
                payload={"metadata": {"system_initiated": True}},
                status=status,
            )
        )
        assignment = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        assignment.enabled = False
        await db.commit()
    await assign(factory, hook_id=hook_id, prospective=True)
    recovered = runtime(factory)
    await recovered.scan()
    if status in {"completed", "ambiguous"}:
        # Terminal fork outcomes add bounded capture/publication reads only.
        assert recovered.providers.guardrails.read_events.await_count <= 3
        assert all(
            call.kwargs.get("seqs") or call.kwargs.get("last_n") == 1
            for call in recovered.providers.guardrails.read_events.await_args_list
        )
    else:
        recovered.providers.guardrails.read_events.assert_not_awaited()
    recovered.scheduler.submit_turn.assert_not_awaited()
    recovered.scheduler.cancel_turn.assert_not_awaited()
    async with factory() as db:
        persisted = await db.get(AgentHookRunRow, run.run_id)
        assert persisted.status == (
            "completed"
            if status == "completed"
            else "interrupted"
            if status == "ambiguous"
            else "running"
        )
        assert persisted.child_conversation_id == "conv-hooks"


@pytest.mark.asyncio
async def test_static_true_configuration_refresh_has_global_bound_and_no_handled_backlog(
    factory, live
):
    from cognis.core.hook_runtime import MAX_HOOK_JOBS

    await assign(factory, hook_id="system:memory-consolidation")
    async with factory() as db:
        for index in range(MAX_HOOK_JOBS * 2):
            cid = f"long-lived-{index:03}"
            db.add(
                Conversation(
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                    context_type="web",
                    active_session_id=f"session-{cid}",
                    last_message_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
            await db.flush()
            db.add(
                Session(
                    session_id=f"session-{cid}",
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                )
            )
            db.add(
                DirectTurnRequestRow(
                    turn_id=cid,
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_id="owner@hooks.test",
                    idempotency_scope="user",
                    idempotency_key=cid,
                    admission_hash="hash",
                    payload_hash="hash",
                    payload={},
                    status="completed",
                    created_at=datetime(2026, 2, 1, tzinfo=UTC),
                    terminal_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        await db.commit()
    entered, snapshots, dispatches = asyncio.Event(), asyncio.Event(), asyncio.Event()
    snapshot, submit, spawn = live._snapshot, live.scheduler.submit_turn.side_effect, live._spawn
    peak = 0

    async def blocked_snapshot(*args):
        entered.set()
        await snapshots.wait()
        await snapshot(*args)

    async def blocked_submit(*args, **kwargs):
        await dispatches.wait()
        return await submit(*args, **kwargs)

    def bounded_spawn(*args):
        nonlocal peak
        spawn(*args)
        peak = max(peak, len(live._jobs))

    live._snapshot = AsyncMock(side_effect=blocked_snapshot)
    live.scheduler.submit_turn.side_effect = blocked_submit
    live._spawn = bounded_spawn
    try:
        await notify_hook_change(
            live.event_bus, "hook_assignments_put", {"agent_id": "agent-hooks"}
        )
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(20):
            await notify_hook_change(
                live.event_bus, "hook_assignments_put", {"agent_id": "agent-hooks"}
            )
        assert list(live._jobs) == ["hook-config"]
        assert live._rerun == {"hook-config"}
        snapshots.set()
        async with asyncio.timeout(15):
            while "hook-config" in live._jobs:
                await asyncio.sleep(0.01)
        assert peak == MAX_HOOK_JOBS
        assert live._snapshot.await_count == MAX_HOOK_JOBS * 2 + 1
    finally:
        snapshots.set()
        dispatches.set()
        await drain_jobs(live)
    async with factory() as db:
        await db.execute(
            update(AgentHookRunRow).values(status="completed", lifecycle_pending=False)
        )
        await db.commit()
        assert not (await db.scalars(live._idle_candidates())).all()
    count = live._snapshot.await_count
    for _ in range(5):
        await notify_hook_change(
            live.event_bus, "hook_assignments_put", {"agent_id": "agent-hooks"}
        )
    await drain_jobs(live)
    assert live._snapshot.await_count == count
    assert peak <= MAX_HOOK_JOBS


@pytest.mark.asyncio
async def test_candidate_query_only_returns_latest_unhandled_foreground_occurrence(factory):
    await assign(factory, hook_id="system:memory-consolidation")
    service = runtime(factory)
    await service._match_idle("conv-hooks")
    async with factory() as db:
        assert not (await db.scalars(service._idle_candidates())).all()
        # A newer foreground occurrence must not be hidden by a handled older one.
        db.add(
            DirectTurnRequestRow(
                turn_id="latest-foreground",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_id="owner@hooks.test",
                idempotency_scope="user",
                idempotency_key="latest-foreground",
                admission_hash="hash",
                payload_hash="hash",
                payload={},
                status="completed",
                terminal_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await db.commit()
        assert len((await db.scalars(service._idle_candidates())).all()) == 1
    await service._match_idle("conv-hooks")
    async with factory() as db:
        assert not (await db.scalars(service._idle_candidates())).all()
        assert {run.occurrence for run in (await db.scalars(select(AgentHookRunRow))).all()} == {
            "idle:fixture-turn",
            "idle:latest-foreground",
        }


@pytest.mark.asyncio
async def test_mixed_version_insert_defaults_and_null_repair_preserve_valid_boundaries(factory):
    from cognis.bootstrap import run_schema_bootstrap

    await assign(factory, hook_id="system:memory-consolidation")
    engine = factory.kw["bind"]
    await run_schema_bootstrap(engine)
    async with factory() as db:
        original = (
            await db.get(AgentHookAssignmentRow, ("agent-hooks", "system:memory-consolidation"))
        ).activated_at
        # Match the SQL written by a binary whose model has no activated_at column.
        await db.execute(text("SELECT 1"))
        await asyncio.sleep(0.02)
        before_insert = datetime.now(UTC)
        await db.execute(
            text(
                "INSERT INTO agent_hook_assignments (agent_id, hook_id, enabled, parameters) "
                "VALUES ('agent-hooks', 'old-binary-default', true, '{}')"
            )
        )
        await db.execute(
            text(
                "INSERT INTO agent_hook_assignments (agent_id, hook_id, enabled, parameters, activated_at) "
                "VALUES ('agent-hooks', 'old-binary-null', true, '{}', NULL)"
            )
        )
        await db.commit()
        generated = (
            await db.get(AgentHookAssignmentRow, ("agent-hooks", "old-binary-default"))
        ).activated_at
        assert generated is not None
        aware = generated.replace(tzinfo=UTC) if generated.tzinfo is None else generated
        assert aware >= before_insert, "The default must use insert time, not transaction start"
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    async with factory() as db:
        assert (
            await db.get(AgentHookAssignmentRow, ("agent-hooks", "system:memory-consolidation"))
        ).activated_at == original
        assert (
            await db.get(AgentHookAssignmentRow, ("agent-hooks", "old-binary-default"))
        ).activated_at == generated
        assert (
            await db.get(AgentHookAssignmentRow, ("agent-hooks", "old-binary-null"))
        ).activated_at is not None
        columns = await db.run_sync(
            lambda sync: inspect(sync.connection()).get_columns("agent_hook_assignments")
        )
        assert (
            next(column for column in columns if column["name"] == "activated_at")["default"]
            is not None
        )


@pytest.mark.asyncio
async def test_enabled_put_repairs_missing_activation_once(factory):
    hook_id = await assign(factory, hook_id="system:memory-consolidation")
    async with factory() as db:
        row = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        row.activated_at = None
        await db.commit()
    await assign(factory, hook_id=hook_id, prospective=True)
    async with factory() as db:
        first = (await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))).activated_at
        assert first is not None
    await assign(factory, hook_id=hook_id, prospective=True)
    async with factory() as db:
        assert (
            await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        ).activated_at == first


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [1, 100])
async def test_idle_recovery_resumes_after_timeout_without_starving_later_candidates(
    factory, monkeypatch, page_size
):
    monkeypatch.setattr("cognis.core.hook_runtime.IDLE_PAGE_SIZE", page_size)
    await assign(
        factory,
        {
            "name": "Recovery cursor",
            "trigger": "conversation_idle",
            "mode": "fresh",
            "prompt": "Check",
            "condition": "{{ conversation.id != 'conv-hooks' }}",
        },
    )
    async with factory() as db:
        for cid in ("d-busy", "z-lost-deadline"):
            db.add(
                Conversation(
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                    context_type="web",
                    active_session_id=f"session-{cid}",
                    last_message_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
            await db.flush()
            db.add(
                Session(
                    session_id=f"session-{cid}",
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_email="owner@hooks.test",
                )
            )
            db.add(
                DirectTurnRequestRow(
                    turn_id=cid,
                    conversation_id=cid,
                    agent_id="agent-hooks",
                    user_id="owner@hooks.test",
                    idempotency_scope="user",
                    idempotency_key=cid,
                    admission_hash="hash",
                    payload_hash="hash",
                    payload={},
                    status="completed",
                    created_at=datetime(2026, 2, 1, tzinfo=UTC),
                    terminal_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        await db.commit()
    service = runtime(factory)

    async def running(cid):
        return {"turn_id": "busy"} if cid == "d-busy" else None

    service.scheduler.durable_running_turn_state.side_effect = running
    match = service._match_idle
    visited, cancelled = [], []
    cutoff = "d-busy"

    async def interruptible_match(conversation_id=None, **kwargs):
        if conversation_id is not None:
            visited.append(conversation_id)
            if conversation_id == cutoff:
                # Expire the real timeout at an awaited cancellation point,
                # deterministically, without sleeping or mocking cancellation.
                deadline.reschedule(asyncio.get_running_loop().time() - 1)
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.append(conversation_id)
                    raise
        await match(conversation_id, **kwargs)

    service._match_idle = interruptible_match
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(None) as deadline:
            await service.scan()
    assert visited == ["conv-hooks", "d-busy"]
    assert service._idle_recovery_cursor == "conv-hooks"
    assert cancelled == ["d-busy"]

    visited.clear()
    cutoff = "z-lost-deadline"
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(None) as deadline:
            await service.scan()
    assert visited == ["d-busy", "z-lost-deadline"]
    assert service._idle_recovery_cursor == "d-busy"
    assert cancelled == ["d-busy", "z-lost-deadline"]

    visited.clear()
    cutoff = None
    await service.scan()
    assert visited == ["z-lost-deadline"]
    service.scheduler.submit_turn.assert_awaited_once()
    assert service._idle_recovery_cursor == ""
    async with factory() as db:
        assert [
            run.conversation_id for run in (await db.scalars(select(AgentHookRunRow))).all()
        ] == ["z-lost-deadline"]
        assert {
            row.conversation_id for row in (await db.scalars(service._idle_candidates())).all()
        } == {"conv-hooks", "d-busy"}

    # Exhaustion wrapped recovery back to the persistent false/busy prefix.
    cutoff = "d-busy"
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(None) as deadline:
            await service.scan()
    assert service._idle_recovery_cursor == "conv-hooks"
    visited.clear()
    cutoff = None
    await service._refresh_idle()
    assert visited == ["conv-hooks", "d-busy"]
    assert service._idle_recovery_cursor == "conv-hooks"
    visited.clear()
    await service.scan()
    assert visited == ["d-busy"]
    assert service._idle_recovery_cursor == ""
    visited.clear()
    await service._refresh_idle()
    assert visited == ["conv-hooks", "d-busy"]
    service.scheduler.submit_turn.assert_awaited_once()
    assert runtime(factory)._idle_recovery_cursor == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("intaris_session_id", [None, "canonical-intaris-source"])
@pytest.mark.parametrize("phase", ["snapshot", "lifecycle"])
async def test_hook_event_append_keys_are_target_scoped_and_stable_on_retry(
    factory, intaris_session_id, phase
):
    async with factory() as db:
        source = await db.get(Session, "session-hooks")
        source.intaris_session_id = intaris_session_id
        run = AgentHookRunRow(
            run_id="hrun_event_key",
            hook_id="system:memory-consolidation",
            hook_revision=1,
            owner_email="owner@hooks.test",
            agent_id="agent-hooks",
            conversation_id="conv-hooks",
            source_session_id="session-hooks",
            occurrence="idle:fixture-turn",
            status="preparing" if phase == "snapshot" else "completed",
            lifecycle_pending=True,
            configuration={
                "name": "Event key regression",
                "trigger": "conversation_idle",
                "timeout_seconds": 600,
            },
        )
        db.add(run)
        await db.commit()
    service = runtime(factory)
    guardrails = service.providers.guardrails
    append = guardrails.record_events.side_effect
    lost_reply = True

    async def append_then_lose_reply(*args, **kwargs):
        nonlocal lost_reply
        result = await append(*args, **kwargs)
        if lost_reply:
            lost_reply = False
            raise ConnectionError("Append succeeded but its response was lost")
        return result

    guardrails.record_events.side_effect = append_then_lose_reply
    if phase == "snapshot":
        with pytest.raises(ConnectionError, match="response was lost"):
            await service._snapshot(run, {"prompt": "Pinned input"})
        async with factory() as db:
            assert (await db.get(AgentHookRunRow, run.run_id)).status == "preparing"
        await service._snapshot(run, {"prompt": "Pinned input"})
        await service._snapshot(run, {"prompt": "Pinned input"})
        async with factory() as db:
            assert (await db.get(AgentHookRunRow, run.run_id)).status == "pending"
    else:
        assert not await service.publish(run)
        async with factory() as db:
            assert (await db.get(AgentHookRunRow, run.run_id)).lifecycle_pending
        assert await service.publish(run)
        assert await service.publish(run)
        async with factory() as db:
            assert not (await db.get(AgentHookRunRow, run.run_id)).lifecycle_pending
    target = intaris_session_id or "session-hooks"
    calls = guardrails.record_events.await_args_list
    assert len(calls) == 3
    assert {call.args[0] for call in calls} == {target}
    keys = {call.kwargs["idempotency_key"] for call in calls}
    assert len(keys) == 1
    assert next(iter(keys)).startswith(target + ":" + run.run_id + ":")
    event_name = "hook_snapshot" if phase == "snapshot" else "agent_hook"
    assert (
        len(
            [
                event
                for event in guardrails.streams[target]
                if event.get("data", {}).get("event") == event_name
            ]
        )
        == 1
    )
