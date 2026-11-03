# ruff: noqa: F811
"""Hook executions are one-shot; only their nonterminal session can recover."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from cognis.core.execution_metadata import persist_execution_metadata
from cognis.core.hook_dispatch import HookDispatchJournal
from cognis.core.hook_subsessions import internal_execution
from cognis.core.session import SessionManager
from cognis.store.models import Agent, AgentHookRunRow, Conversation, Session
from tests.unit.test_agent_hooks import conversation, factory, session  # noqa: F401
from tests.unit.test_hook_subsessions import queue, setup_runtime


@pytest.mark.asyncio
async def test_failure_is_terminal_and_next_occurrence_can_run(factory):
    service = await setup_runtime(factory)
    first = await queue(service, factory, "completed:first")
    service.agent_loop.run_internal_child_session.side_effect = RuntimeError("fatal failure")
    await service._run(first.run_id)
    await service._run(first.run_id)
    assert service.agent_loop.run_internal_child_session.await_count == 1
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, first.run_id)
        assert saved.status == "failed"
        assert saved.fork_slot is None
        assert internal_execution(saved)["phase"] == "terminal"
    second = await queue(service, factory, "completed:second")
    assert second is not None and second.run_id != first.run_id


@pytest.mark.asyncio
async def test_occurrences_queue_durably_while_one_session_is_running(factory):
    service = await setup_runtime(factory)
    first = await queue(service, factory, "completed:first")
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.agent_loop.run_internal_child_session.side_effect

    async def hold(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    service.agent_loop.run_internal_child_session.side_effect = hold
    worker = asyncio.create_task(service._run(first.run_id))
    await entered.wait()
    second = await queue(service, factory, "completed:second")
    third = await queue(service, factory, "completed:third")
    await service._run(second.run_id)
    await service._run(third.run_id)
    assert service.agent_loop.run_internal_child_session.await_count == 1
    release.set()
    await worker
    service.agent_loop.run_internal_child_session.side_effect = original
    await service._run(third.run_id)
    assert service.agent_loop.run_internal_child_session.await_count == 1
    await service._run(second.run_id)
    await service._run(third.run_id)
    assert service.agent_loop.run_internal_child_session.await_count == 3
    async with factory() as db:
        rows = (await db.scalars(select(AgentHookRunRow))).all()
        assert all(row.status == "completed" and row.fork_slot is None for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["reserved", "executing"])
async def test_elapsed_original_deadline_fails_without_inference(factory, phase):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:expired")
    service._started = True
    service._wake_run = lambda run_id: None
    await service._dispatch(run)
    service._started = False
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        saved.handoff = {
            **saved.handoff,
            "execution": {
                **internal_execution(saved),
                "phase": phase,
                "deadline_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
        }
        await db.commit()
    await service._run(run.run_id)
    await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.assert_not_awaited()
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == "failed" and saved.fork_slot is None
        assert internal_execution(saved)["failure_reason"] == "timeout"


@pytest.mark.asyncio
async def test_timeout_cancels_child_once_and_never_retries(factory):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:timeout")
    service._started = True
    service._wake_run = lambda run_id: None
    await service._dispatch(run)
    service._started = False
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        saved.handoff = {
            **saved.handoff,
            "execution": {
                **internal_execution(saved),
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
            },
        }
        deadline = internal_execution(saved)["deadline_at"]
        await db.commit()
    cancelled = asyncio.Event()

    async def hang(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.agent_loop.run_internal_child_session.side_effect = hang
    await service._run(run.run_id)
    await service._run(run.run_id)
    assert cancelled.is_set()
    service.agent_loop.run_internal_child_session.assert_awaited_once()
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == "failed" and saved.fork_slot is None
        assert internal_execution(saved)["deadline_at"] == deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["retry_wait", "blocked", "executing"])
async def test_legacy_retry_runs_are_retired_without_inference(factory, phase):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:legacy")
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        saved.status = "running"
        saved.handoff = {
            "execution": {
                "kind": "hook",
                "phase": phase,
                "session_id": "sess-hook-retired",
                "operator_paused": True,
            }
        }
        await db.commit()
    await service._run(run.run_id)
    await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.assert_not_awaited()
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == "failed" and saved.fork_slot is None
        assert internal_execution(saved)["phase"] == "terminal"


@pytest.mark.asyncio
async def test_execution_lease_prevents_duplicate_workers(factory):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:leased")
    original = service.agent_loop.run_internal_child_session.side_effect
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        started.set()
        await release.wait()
        return await original(**kwargs)

    service.agent_loop.run_internal_child_session.side_effect = slow
    worker = asyncio.create_task(service._run(run.run_id))
    await started.wait()
    await service._run(run.run_id)
    assert service.agent_loop.run_internal_child_session.await_count == 1
    release.set()
    await worker


@pytest.mark.asyncio
@pytest.mark.parametrize("reconciliation_error", [False, True])
async def test_restart_reconciles_tools_before_same_session_can_continue(
    factory, reconciliation_error
):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:journal")
    original = service.agent_loop.run_internal_child_session.side_effect
    service.scheduler.recover_internal_tool_batch = AsyncMock()

    async def interrupted(**kwargs):
        child, fence = kwargs["child_session"], kwargs["execution_fence"]
        call = {
            "session_id": child.session_id,
            "turn_id": run.run_id,
            "descriptors": [{"call_id": "call-hook-write", "tool_name": "write", "arguments": {}}],
        }
        await fence.record_tool_dispatch(**call)
        await fence.record_tool_dispatch(**call)
        await HookDispatchJournal(fence, service.providers.guardrails).bind(
            "call-hook-write",
            "executor-hook",
            None,
            session_id=child.session_id,
            turn_id=run.run_id,
            dispatch_state="accepted",
        )
        raise asyncio.CancelledError

    service.agent_loop.run_internal_child_session.side_effect = interrupted
    with pytest.raises(asyncio.CancelledError):
        await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.side_effect = original
    if reconciliation_error:
        service.scheduler.recover_internal_tool_batch.side_effect = RuntimeError(
            "outcome uncertain"
        )
    await service._run(run.run_id)
    service.scheduler.recover_internal_tool_batch.assert_awaited_once()
    assert service.agent_loop.run_internal_child_session.await_count == (
        1 if reconciliation_error else 2
    )
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == ("failed" if reconciliation_error else "completed")
        assert saved.fork_slot is None
    if not reconciliation_error:
        calls = service.agent_loop.run_internal_child_session.await_args_list
        assert (
            calls[0].kwargs["child_session"].session_id
            == calls[1].kwargs["child_session"].session_id
        )


@pytest.mark.asyncio
async def test_terminal_delivery_failure_releases_slot_and_never_repeats_inference(factory):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:delivery")
    settle = service.handoffs.settle
    service.handoffs.settle = AsyncMock(side_effect=RuntimeError("publication unavailable"))
    await service._run(run.run_id)
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == "completed" and saved.fork_slot is None
        assert internal_execution(saved)["phase"] == "terminal"
        assert saved.handoff["response"]
    service.handoffs.settle = settle
    service._match_idle = AsyncMock()
    await service.scan()
    service.agent_loop.run_internal_child_session.assert_awaited_once()
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.handoff["published"]
        assert not saved.handoff["settlement_pending"]


@pytest.mark.asyncio
async def test_crash_after_response_commit_never_reexecutes_completed_session(factory):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:response-crash")
    finalize = service.subsessions._terminalize
    service.subsessions._terminalize = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await service._run(run.run_id)
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == "completed" and saved.fork_slot is None
        assert saved.handoff["response"]
        assert internal_execution(saved)["phase"] == "terminal"
    service.subsessions._terminalize = finalize
    await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.assert_awaited_once()
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        child = await db.get(Session, internal_execution(saved)["session_id"])
        assert child.status == "completed"
        assert saved.handoff["published"]


@pytest.mark.asyncio
@pytest.mark.parametrize("appended", [False, True])
async def test_crash_during_final_response_append_never_reenters_inference(factory, appended):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:append-crash")
    record = service.providers.guardrails.record_events.side_effect

    async def interrupt(session_id, events, **kwargs):
        if events[0].data.get("event") == "hook_response":
            if appended:
                await record(session_id, events, **kwargs)
            raise asyncio.CancelledError
        return await record(session_id, events, **kwargs)

    service.providers.guardrails.record_events.side_effect = interrupt
    try:
        await service._run(run.run_id)
    except asyncio.CancelledError:
        pass
    else:
        async with factory() as db:
            saved = await db.get(AgentHookRunRow, run.run_id)
            pytest.fail(f"Append interruption was not reached: {internal_execution(saved)}")
    service.providers.guardrails.record_events.side_effect = record
    await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.assert_awaited_once()
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == ("completed" if appended else "failed")
        assert saved.fork_slot is None and saved.handoff["published"]


@pytest.mark.asyncio
async def test_successor_uses_predecessor_response_while_delivery_is_unavailable(factory):
    service = await setup_runtime(factory)
    first = await queue(service, factory, "completed:predecessor")
    settle = service.handoffs.settle
    service.handoffs.settle = AsyncMock(side_effect=RuntimeError("delivery unavailable"))
    await service._run(first.run_id)
    service.handoffs.settle = settle
    second = await queue(service, factory, "completed:successor")
    await service._run(second.run_id)
    async with factory() as db:
        before = await db.get(AgentHookRunRow, first.run_id)
        after = await db.get(AgentHookRunRow, second.run_id)
        assert not before.handoff.get("published")
        assert after.status == "completed"
        assert after.handoff["previous_run_id"] == first.run_id
        assert after.handoff["previous_boundary"] == before.handoff["boundary"]
        assert after.handoff["previous_response_ref"] == before.handoff["response"]
    from cognis.runtime_context import scoped_runtime_context

    with scoped_runtime_context(
        user_email="owner@hooks.test",
        agent_id="agent-hooks",
        agent_owner_email="owner@hooks.test",
    ):
        context = await service.handoffs.input(after, session())
        assert "vault/Research.md" in context


@pytest.mark.asyncio
async def test_different_hooks_share_one_conversation_execution_slot(factory):
    from tests.unit.test_agent_hooks import assign

    service = await setup_runtime(factory)
    await assign(
        factory,
        {
            "name": "Second hook",
            "trigger": "turn_completed",
            "mode": "fresh",
            "prompt": "Inspect the work",
            "timeout_seconds": 60,
        },
    )
    first = await queue(service, factory, "completed:cross-hook")
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.agent_loop.run_internal_child_session.side_effect

    async def hold(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    service.agent_loop.run_internal_child_session.side_effect = hold
    worker = asyncio.create_task(service._run(first.run_id))
    await entered.wait()
    async with factory() as db:
        second = await db.scalar(
            select(AgentHookRunRow).where(
                AgentHookRunRow.run_id != first.run_id,
            )
        )
    assert second is not None and second.hook_id != first.hook_id
    await service._run(second.run_id)
    assert service.agent_loop.run_internal_child_session.await_count == 1
    release.set()
    await worker
    service.agent_loop.run_internal_child_session.side_effect = original
    await service._run(second.run_id)
    assert service.agent_loop.run_internal_child_session.await_count == 2


@pytest.mark.asyncio
async def test_card_follows_source_session_rotation(factory):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:rotation")
    async with factory() as db:
        db.add(
            Session(
                session_id="session-rotated",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_email="owner@hooks.test",
            )
        )
        source = await db.get(Conversation, "conv-hooks")
        source.active_session_id = "session-rotated"
        await db.commit()
    await service._run(run.run_id)
    cards = [
        event["data"]
        for event in service.providers.guardrails.streams["session-rotated"]
        if event["data"].get("event") == "agent_hook"
    ]
    assert cards[-1]["status"] == "completed"
    assert cards[-1]["child_session_id"]
    assert "vault/Research.md" in cards[-1]["outcome"]


def real_child_manager(service, factory):
    service.providers.guardrails.get_session = AsyncMock(
        return_value=SimpleNamespace(
            policy={"evaluation_policy_version": "hybrid_v1", "profile": "balanced"}
        )
    )
    service.providers.guardrails.create_session = AsyncMock()
    return SessionManager(factory, service.providers, service.session_cache)


@pytest.mark.asyncio
async def test_restart_keeps_child_identity_profile_metadata_and_original_deadline(factory):
    service = await setup_runtime(factory)
    manager = real_child_manager(service, factory)
    service.session_manager.create_child_session = AsyncMock(
        side_effect=manager.create_child_session
    )
    run = await queue(service, factory, "completed:identity")
    original = service.agent_loop.run_internal_child_session.side_effect

    async def interrupted(**kwargs):
        sid = kwargs["child_session"].session_id
        await persist_execution_metadata(
            factory,
            session_id=sid,
            user_email="owner@hooks.test",
            model="gpt-5",
            provider_id="openai",
            profile_id="developer",
            reasoning_effort="medium",
            reasoning_mode=None,
        )
        async with factory() as db:
            child = await db.get(Session, sid)
            child.agent_profile_id = "developer"
            source = await db.get(Session, session().session_id)
            source.agent_profile_id = "developer-deep"
            await db.commit()
        raise asyncio.CancelledError

    service.agent_loop.run_internal_child_session.side_effect = interrupted
    with pytest.raises(asyncio.CancelledError):
        await service._run(run.run_id)
    async with factory() as db:
        before = dict(internal_execution(await db.get(AgentHookRunRow, run.run_id)))
    service.agent_loop.run_internal_child_session.side_effect = original
    await service._run(run.run_id)
    service.providers.guardrails.create_session.assert_awaited_once()
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        assert saved.status == "completed"
        assert internal_execution(saved)["deadline_at"] == before["deadline_at"]
        assert internal_execution(saved)["session_id"] == before["session_id"]
        child = await db.get(Session, before["session_id"])
        assert child.delegation_metadata["execution_runtime"]["model"] == "gpt-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "completed", "cancelled"])
async def test_restart_never_reopens_terminal_child(factory, status):
    service = await setup_runtime(factory)
    run = await queue(service, factory, "completed:terminal-child")
    service.agent_loop.run_internal_child_session.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service._run(run.run_id)
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        child = await db.get(Session, internal_execution(saved)["session_id"])
        child.status = status
        await db.commit()
    await service._run(run.run_id)
    service.agent_loop.run_internal_child_session.assert_awaited_once()
    async with factory() as db:
        saved = await db.get(AgentHookRunRow, run.run_id)
        child = await db.get(Session, internal_execution(saved)["session_id"])
        assert saved.status == "failed"
        assert child.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("conversation_id", "other-conversation"),
        ("parent_session_id", "other-session"),
        ("user_email", "other@hooks.test"),
        ("agent_id", "other-agent"),
        ("delegation_mode", "delegate"),
        ("delegation_metadata", {"hook_run_id": "other-run", "internal": True}),
        ("delegation_metadata", {"hook_run_id": "run-reserved", "internal": False}),
    ],
)
async def test_reservation_still_rejects_changed_ownership_or_hook_identity(factory, field, value):
    service = await setup_runtime(factory)
    manager = real_child_manager(service, factory)
    arguments = {
        "mode": "hook",
        "task_description": "Background hook: Debrief",
        "agent_id": "agent-hooks",
        "effective_agent_id": "agent-hooks",
        "session_id": "sess-hook-reserved",
        "delegation_metadata": {"hook_run_id": "run-reserved", "internal": True},
    }
    await manager.create_child_session(session(), **arguments)
    async with factory() as db:
        db.add(Agent(agent_id="other-agent", owner_email="owner@hooks.test", name="Other"))
        db.add(
            Conversation(
                conversation_id="other-conversation",
                agent_id="agent-hooks",
                user_email="owner@hooks.test",
                context_type="web",
            )
        )
        db.add(
            Session(
                session_id="other-session",
                conversation_id="conv-hooks",
                agent_id="agent-hooks",
                user_email="owner@hooks.test",
            )
        )
        await db.flush()
        reserved = await db.get(Session, arguments["session_id"])
        setattr(reserved, field, value)
        await db.commit()
    with pytest.raises(ValueError, match="Child reservation identity mismatch"):
        await manager.create_child_session(session(), **arguments)
    service.providers.guardrails.create_session.assert_awaited_once()
