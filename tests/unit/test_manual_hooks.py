# ruff: noqa: F811
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from cognis.core.commands import CommandDispatcher, is_system_slash_command_message
from cognis.core.hooks import HookError
from cognis.store.models import AgentHookAssignmentRow, AgentHookRow, AgentHookRunRow, Conversation
from tests.unit.test_agent_hooks import assign, conversation, factory, session  # noqa: F401
from tests.unit.test_hook_subsessions import setup_runtime


async def idle_hook(factory, *, enabled=True):
    hook_id = await assign(
        factory,
        {
            "name": "Manual learning",
            "description": "Preserve useful procedures",
            "trigger": "conversation_idle",
            "mode": "fork",
            "condition": "{{ missing.value }}",
            "idle_seconds": 3600,
            "prompt": "Learn from the conversation.",
        },
    )
    async with factory() as db:
        assignment = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        assignment.enabled = enabled
        assignment.activated_at = None if not enabled else assignment.activated_at
        source = await db.get(Conversation, "conv-hooks")
        source.last_message_at = datetime.now(UTC)
        await db.commit()
    return hook_id


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_manual_bypasses_condition_timer_and_consumes_idle(factory, enabled):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory, enabled=enabled)
    timer = Mock()
    service._deadlines["hook-idle:conv-hooks"] = (datetime.now(UTC), timer)
    run = await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    await service._run(run.run_id)
    timer.cancel.assert_called_once()
    again = await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    assert again.run_id == run.run_id
    await service._run(again.run_id)
    await service._match_idle("conv-hooks")
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == "completed"
        assert saved.occurrence == "idle:fixture-turn"
        assert saved.configuration["_manual"] is True
        assignment = await db.get(AgentHookAssignmentRow, ("agent-hooks", hook_id))
        assert assignment.enabled is enabled
    service.agent_loop.run_internal_child_session.assert_awaited_once()
    assert "hook-idle:conv-hooks" not in service._deadlines


@pytest.mark.asyncio
async def test_manual_keeps_other_idle_timer(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory)
    other = await assign(
        factory,
        {
            "name": "Later",
            "trigger": "conversation_idle",
            "mode": "fresh",
            "idle_seconds": 7200,
            "prompt": "Later",
        },
    )
    service._started = True
    try:
        await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
        assert "hook-idle:conv-hooks" in service._deadlines
        async with factory() as db:
            assert (
                await db.get(
                    AgentHookRunRow, service._run_id(other, "conv-hooks", "idle:fixture-turn")
                )
                is None
            )
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_manual_filters_ownership_trigger_and_assignment(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory, enabled=False)
    injection = await assign(factory)
    hooks = await service.manual_hooks(conversation(), "owner@hooks.test")
    assert hook_id in [item[0] for item in hooks]
    assert injection not in [item[0] for item in hooks]
    with pytest.raises(HookError):
        await service.run_manual(conversation(), session(), "owner@hooks.test", injection)
    with pytest.raises(HookError):
        await service.run_manual(conversation(), session(), "other@hooks.test", hook_id)
    service.scheduler.durable_running_turn_state.return_value = {"turn_id": "busy"}
    with pytest.raises(HookError, match="Wait"):
        await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    service.agent_loop.run_internal_child_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, SimpleNamespace(active_session_id=None)])
async def test_manual_fails_closed_when_foreground_disappears(factory, monkeypatch, source):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory)
    available = await service.manual_hooks(conversation(), "owner@hooks.test")
    service.manual_hooks = AsyncMock(return_value=available)
    monkeypatch.setattr("cognis.core.hook_runtime.get_conversation", AsyncMock(return_value=source))
    with pytest.raises(HookError, match="active foreground session"):
        await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    service.agent_loop.run_internal_child_session.assert_not_awaited()
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(AgentHookRunRow)) == 0


@pytest.mark.asyncio
async def test_manual_command_listing_suggestions_and_execution(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory, enabled=False)
    dispatcher = CommandDispatcher(
        session_factory=factory,
        session_manager=None,
        session_cache=None,
        compaction_strategy=None,
        providers=None,
        pause_waiter=None,
        notification_service=None,
        hook_runtime=service,
    )
    kwargs = dict(
        conversation=conversation(), session=session(), agent=None, user_email="owner@hooks.test"
    )
    assert is_system_slash_command_message("/hook")
    assert is_system_slash_command_message("/hook Manual learning")
    result = await dispatcher.dispatch("/hook", **kwargs)
    assert hook_id in result.text
    assert "disabled — manual only" in result.text
    assert "Preserve useful procedures" in result.text
    suggestions = await dispatcher.suggest("/hook Manual l", **kwargs)
    assert len(suggestions) == 1
    assert suggestions[0].label == "Manual learning (disabled)"
    assert suggestions[0].insert_text == "/hook Manual learning"
    assert suggestions[0].value == "Manual learning"
    result = await dispatcher.dispatch("/hook manual learning", **kwargs)
    assert result.type == "hook_started"
    assert not result.text
    assert result.data["hook_run_id"]


@pytest.mark.asyncio
async def test_manual_timer_race_uses_one_durable_occurrence(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory)
    await asyncio.gather(
        service.run_manual(conversation(), session(), "owner@hooks.test", hook_id),
        service.run_manual(conversation(), session(), "owner@hooks.test", hook_id),
    )
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(AgentHookRunRow)) == 1
    await service.scan()
    service.agent_loop.run_internal_child_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_rejects_channel_origin(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory)
    async with factory() as db:
        source = await db.get(Conversation, "conv-hooks")
        source.context_type = "signal"
        await db.commit()
    with pytest.raises(HookError, match="web"):
        await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)


@pytest.mark.asyncio
async def test_manual_recovers_disabled_preparation(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory, enabled=False)
    service._wake_run = Mock()
    run = await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        saved.status = "preparing"
        await db.commit()
    await service._run(run.run_id)
    await service._run(run.run_id)
    async with factory() as db:
        assert (await db.get(AgentHookRunRow, run.run_id)).status == "completed"


@pytest.mark.asyncio
async def test_manual_overrides_automatic_invalid_condition(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory)
    await service.match(
        conversation=conversation(),
        session=session(),
        triggers=["conversation_idle"],
        occurrence="idle:fixture-turn",
        context={"idle": {"seconds": 10000}},
    )
    async with factory() as db:
        auto = await db.get(
            AgentHookRunRow, service._run_id(hook_id, "conv-hooks", "idle:fixture-turn")
        )
        assert auto.status == "invalid_condition"
    run = await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    assert run.occurrence == "manual:idle:fixture-turn"
    await service._run(run.run_id)
    async with factory() as db:
        assert (await db.get(AgentHookRunRow, run.run_id)).status == "completed"


@pytest.mark.asyncio
async def test_idle_countdown_starts_after_attached_work_settles(factory, monkeypatch):
    service = await setup_runtime(factory)
    await idle_hook(factory)
    pending = AsyncMock(return_value={"conv-hooks": ["managed_conversation"]})
    monkeypatch.setattr(
        "cognis.core.hook_readiness.list_pending_notification_types_by_conversation", pending
    )
    service._started = True
    try:
        await service._match_idle("conv-hooks")
        assert "hook-idle:conv-hooks" not in service._deadlines
        with pytest.raises(HookError, match="pending notifications"):
            await service.run_manual(
                conversation(), session(), "owner@hooks.test", "Manual learning"
            )
        pending.return_value = {}
        before = datetime.now(UTC)
        await service._match_idle("conv-hooks")
        deadline = service._deadlines["hook-idle:conv-hooks"][0]
        assert (deadline - before).total_seconds() >= 3599
        async with factory() as db:
            source = await db.get(Conversation, "conv-hooks")
            assert source.context_data["hook_idle_boundary"]["ready_at"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_manual_completed_hook_allowed_but_pre_triggers_excluded(factory):
    service = await setup_runtime(factory)
    hook_id = await assign(
        factory,
        {
            "name": "Post turn",
            "trigger": "turn_completed",
            "mode": "fresh",
            "prompt": "Review the completed turn.",
            "condition": "{{ false }}",
        },
    )
    run = await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    assert run.occurrence == "completed:fixture-turn"
    await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_dispatch_reads_manual_upgrade_under_claim_lock(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory, enabled=False)
    run = await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    # Simulate the other controller's old pre-upgrade object, not the stored row.
    run.configuration = {key: value for key, value in run.configuration.items() if key != "_manual"}
    await service._dispatch(run)
    async with factory() as db:
        assert (await db.get(AgentHookRunRow, run.run_id)).status == "completed"
    service.agent_loop.run_internal_child_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepared_automatic_hook_waits_for_restarted_countdown(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory)
    async with factory() as db:
        hook = await db.get(AgentHookRow, hook_id)
        hook.definition = {**hook.definition, "condition": None}
        await db.commit()
    await service.match(
        conversation=conversation(),
        session=session(),
        triggers=["conversation_idle"],
        occurrence="idle:fixture-turn",
        context={"idle": {"seconds": 10000}},
    )
    async with factory() as db:
        run = await db.get(
            AgentHookRunRow, service._run_id(hook_id, "conv-hooks", "idle:fixture-turn")
        )
    real_ready = service.idle_start_ready
    service.idle_start_ready = AsyncMock(return_value=False)
    await service._dispatch(run)
    service.idle_start_ready = real_ready
    async with factory() as db:
        source = await db.get(Conversation, "conv-hooks")
        source.context_data = {
            "hook_idle_boundary": {
                "turn_id": "fixture-turn",
                "ready_at": datetime.now(UTC).isoformat(),
            }
        }
        await db.commit()
    await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.assert_not_awaited()
    async with factory() as db:
        source = await db.get(Conversation, "conv-hooks")
        source.context_data = {
            "hook_idle_boundary": {
                "turn_id": "fixture-turn",
                "ready_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            }
        }
        await db.commit()
    await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_actual_timer_manual_race(factory):
    service = await setup_runtime(factory)
    hook_id = await idle_hook(factory)
    await asyncio.gather(
        service.match(
            conversation=conversation(),
            session=session(),
            triggers=["conversation_idle"],
            occurrence="idle:fixture-turn",
            context={"idle": {"seconds": 10000}},
        ),
        service.run_manual(conversation(), session(), "owner@hooks.test", hook_id),
    )
    await service.scan()
    service.agent_loop.run_internal_child_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_deferred_follow_up_timeout_starts_at_execution(factory):
    service = await setup_runtime(factory)
    hook_id = await assign(
        factory,
        {
            "name": "Follow up",
            "trigger": "conversation_idle",
            "mode": "follow_up",
            "prompt": "Review results",
            "idle_seconds": 60,
            "timeout_seconds": 600,
        },
    )
    run = await service.run_manual(conversation(), session(), "owner@hooks.test", hook_id)
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        saved.configuration = {
            key: value for key, value in saved.configuration.items() if key != "_manual"
        }
        saved.created_at = datetime.now(UTC) - timedelta(hours=2)
        source = await db.get(Conversation, "conv-hooks")
        source.context_data = {
            "hook_idle_boundary": {
                "turn_id": "fixture-turn",
                "ready_at": (datetime.now(UTC) - timedelta(seconds=61)).isoformat(),
            }
        }
        await db.commit()
    await service._run(run.run_id)
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.configuration["_execution_started_at"]
        assert saved.status == "running"
    await service._run(run.run_id)
    async with factory() as db:
        assert (await db.get(AgentHookRunRow, run.run_id)).status == "running"
    service.scheduler.cancel_request.assert_not_awaited()
