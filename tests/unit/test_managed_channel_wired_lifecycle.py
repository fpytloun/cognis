"""Managed-channel acceptance through production runtime wiring.

Only external providers are replaced. SQLite and the application lifecycle,
inbound pipeline, scheduler, agent loop, and outbox remain real.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from cognis.api import app as app_module
from cognis.models.channel import ChannelAccountConfig, ChannelCapabilities, InboundMessage
from cognis.models.config import ModelInfo
from cognis.models.session import ConversationContext
from cognis.models.tool import ToolCall
from cognis.store import queries
from cognis.store.models import (
    ChannelDeliveryOutboxRow,
    DirectTurnRequestRow,
    ManagedConversationSignal,
)
from cognis.tools.builtin.orchestration import OrchestrationMode
from tests.unit.test_agent_loop import (
    _background_work_agent_loop,
    _background_work_ctx,
    _FakeReminderLLM,
    _IdleWaitScheduler,
    _RecordingGuardrails,
)
from tests.unit.test_managed_channel_foundation import _seed_channel_link


class _ScriptedLLM(_FakeReminderLLM):
    async def stream_generate(self, messages, **kwargs):
        self.calls.append(messages)
        self.offered_tools = {tool["function"]["name"] for tool in kwargs.get("tools") or []}
        if "agent_conversation_send_controller" not in self.offered_tools:
            yield {"choices": [{"delta": {"content": "Controller control unavailable."}}]}
            return
        if len(self.calls) == 1:
            yield {"choices": [{"delta": {"reasoning_content": "PRIVATE reasoning marker"}}]}
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-controller-question",
                                    "type": "function",
                                    "function": {
                                        "name": "agent_conversation_send_controller",
                                        "arguments": json.dumps(
                                            {"message": "PRIVATE question marker", "wait": True}
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        elif len(self.calls) == 2:
            yield {"choices": [{"delta": {"content": "Public final answer."}}]}
        else:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-complete",
                                    "type": "function",
                                    "function": {
                                        "name": "agent_conversation_complete",
                                        "arguments": json.dumps(
                                            {
                                                "status": "completed",
                                                "summary": "PRIVATE completion marker",
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }


@pytest.mark.parametrize("mode", list(OrchestrationMode))
@pytest.mark.parametrize("kind", ["channel", "ordinary", "missing", "malformed"])
def test_child_control_surface_is_fixed_and_fail_closed(mode, kind):
    loop = _background_work_agent_loop(SimpleNamespace(), _IdleWaitScheduler())
    ctx = _background_work_ctx("child")
    ctx.orchestration_mode = mode
    ids = [
        "builtin:agent_conversation_send_controller",
        "builtin:agent_conversation_complete",
    ]
    data = {"kind": "agent_work", "managed_depth": 2}
    if kind != "ordinary":
        data["managed_conversation_kind"] = "channel"
    if kind == "channel":
        data["managed_creation_policy_snapshot"] = {
            "tool_ids": ids,
            "explicit_tool_allowlist": ids,
        }
    elif kind == "malformed":
        data["managed_creation_policy_snapshot"] = None
    ctx.conversation.context = ConversationContext(type="agent_work", platform_data=data)
    schemas = loop._build_controller_tool_exposure(ctx).schemas
    controls = {
        s["function"]["name"]: s
        for s in schemas
        if s["function"]["name"]
        in {
            "agent_conversation_send_controller",
            "agent_conversation_complete",
        }
    }
    assert bool(controls) == (kind == "channel")
    if controls:
        assert len(controls) == 2
        assert (
            "wait"
            in controls["agent_conversation_send_controller"]["function"]["parameters"][
                "properties"
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["agent_conversation_send_controller", "agent_conversation_complete"]
)
@pytest.mark.parametrize("spoofed", [False, True])
async def test_spoofed_child_controls_require_durable_lineage(tmp_path, name, spoofed):
    from cognis.store.database import create_engine, create_session_factory
    from cognis.store.models import Base

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'spoof.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    loop = _background_work_agent_loop(create_session_factory(engine), _IdleWaitScheduler())
    ctx = _background_work_ctx("spoofed-child")
    if spoofed:
        ctx.conversation.context = ConversationContext(
            type="agent_work",
            platform_data={"kind": "agent_work", "managed_conversation_kind": "channel"},
        )
    try:
        result = await loop._handle_managed_conversation_tool(
            ToolCall(
                call_id="spoofed-control",
                name=name,
                arguments={"message": "forged", "wait": True, "status": "completed"},
            ),
            ctx=ctx,
        )
        assert result.is_error
        if spoofed:
            assert json.loads(result.output)["code"] == "managed_lineage_unavailable"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [False, True])
async def test_child_signal_retains_explicit_wait_semantics(tmp_path, wait):
    from cognis.store.database import create_engine, create_session_factory
    from cognis.store.models import Base

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'wait.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        link, _, _, target = await _seed_channel_link(session)
        await session.commit()
    loop = _background_work_agent_loop(factory, _IdleWaitScheduler())
    ctx = _background_work_ctx(target.conversation_id)
    ctx.session.user_email = "owner@example.com"
    ctx.agent.agent_id = "target"
    ctx.agent.owner_email = "owner@example.com"
    ctx.conversation.context = ConversationContext(
        type="agent_work",
        platform_data={"kind": "agent_work", "managed_conversation_kind": "channel"},
    )
    try:
        result = await loop._handle_managed_conversation_tool(
            ToolCall(
                call_id="notify-controller",
                name="agent_conversation_send_controller",
                arguments={"message": "Private signal", "wait": wait},
            ),
            ctx=ctx,
        )
        assert not result.is_error, result.output
        assert json.loads(result.output)["status"] == ("waiting_controller" if wait else "notified")
        async with factory() as session:
            signal = (await session.execute(select(ManagedConversationSignal))).scalar_one()
            binding = await queries.get_managed_channel_binding_for_link(session, link.link_id)
            assert signal.wait == wait
            assert binding.state == ("waiting_controller" if wait else "waiting_external")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_failure", "restart_pending", "cancel_transport"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (False, False, True),
    ],
)
async def test_wired_managed_channel_lifecycle(
    tmp_path, monkeypatch, delivery_failure, restart_pending, cancel_transport
):
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'wired.db'}")
    monkeypatch.setenv("COGNIS_SERVE_UI", "false")
    monkeypatch.setenv("COGNIS_REDIS_URL", "")
    build_registry = app_module.build_provider_registry
    llm = _ScriptedLLM()
    llm.set_artifact_store = lambda store: None
    llm.close = AsyncMock()
    llm.aclose = AsyncMock()
    llm.resolve_model = AsyncMock(return_value="test-model")
    llm.get_model_info = AsyncMock(return_value=ModelInfo(model_id="test-model"))
    llm.count_messages_tokens = lambda messages, *args, **kwargs: len(str(messages)) // 4
    guardrails = _RecordingGuardrails()
    guardrails.client = SimpleNamespace(aclose=AsyncMock())
    original_record_events = guardrails.record_events

    async def record_events(*args, **kwargs):
        previous = len(guardrails.recorded_events)
        result = await original_record_events(*args, **kwargs)
        return result.model_copy(
            update={
                "first_seq": previous + 1,
                "last_seq": len(guardrails.recorded_events),
            }
        )

    guardrails.record_events = record_events

    async def read_events(**kwargs):
        after = kwargs.get("after_seq") or 0
        events = [
            {"seq": index, **event.model_dump(mode="json")}
            for index, event in enumerate(guardrails.recorded_events, 1)
            if index > after
        ]
        return SimpleNamespace(
            events=events,
            last_seq=len(guardrails.recorded_events),
            has_more=False,
            missing_stream_fallback_used=False,
        )

    guardrails.read_events = read_events
    guardrails.add_event_append_listener = lambda listener: None
    guardrails.remove_event_append_listener = lambda listener: None
    guardrails.close = AsyncMock()
    guardrails.set_status = AsyncMock()
    guardrails.update_session = AsyncMock()
    memory = AsyncMock()
    memory.health.return_value = SimpleNamespace(status="healthy")
    transport_started = asyncio.Event()
    transport_finished = asyncio.Event()
    release_transport = asyncio.Event()

    async def send_message(message):
        from cognis.channels.signal_failures import SignalDeliveryFailure, sanitize_signal_failure

        transport_started.set()
        await release_transport.wait()
        await asyncio.sleep(1.2)
        transport_finished.set()
        if delivery_failure:
            raise SignalDeliveryFailure(sanitize_signal_failure(rpc_code=-5))
        return "platform-final"

    adapter = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        sync_profile=AsyncMock(),
        send_message=AsyncMock(side_effect=send_message),
        capabilities=ChannelCapabilities(),
        channel_type="signal",
    )
    monkeypatch.setattr("cognis.channels.manager._create_adapter", lambda _: adapter)

    def external_providers(*args, **kwargs):
        providers = build_registry(*args, **kwargs)
        providers.llm = llm
        providers.guardrails = guardrails
        providers.memory = memory
        return providers

    monkeypatch.setattr(app_module, "build_provider_registry", external_providers)
    app = app_module.create_app()
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        factory = app.state.session_factory
        async with factory() as session:
            link, controller, _, target = await _seed_channel_link(session)
            controls = [
                "builtin:agent_conversation_send_controller",
                "builtin:agent_conversation_complete",
            ]
            link.creation_policy_snapshot = {
                "tool_ids": controls,
                "explicit_tool_allowlist": controls,
            }
            binding = await queries.get_managed_channel_binding_for_link(session, link.link_id)
            binding.explicit_tool_allowlist = controls
            executor = await queries.create_executor(
                session,
                name="Isolated test executor",
                owner_email="owner@example.com",
                enabled_tools=[],
                enabled_tool_groups=[],
                config={"workspace_root": str(tmp_path)},
            )
            agent = await queries.get_agent(session, "target")
            agent.execution = {"executor_id": executor.executor_id}
            agent.capabilities = {"memory_backend": "none", "guardrails_backend": "none"}
            agent.tools = {"builtin_tools": [], "tool_groups": [], "mcp_servers": []}
            target.context_type = "agent_work"
            target.context_data = {
                "kind": "agent_work",
                "controller_agent_id": "controller",
                "controller_conversation_id": controller.conversation_id,
                "controller_session_id": "controller-session",
                "assistant_delivery_mode": "final_only",
                "managed_conversation_kind": "channel",
                "managed_creation_policy_snapshot": link.creation_policy_snapshot,
            }
            agent.llm_config = {"provider_id": "test", "model": "test-model"}
            target_session = await queries.create_session(
                session,
                session_id="target-session",
                conversation_id=target.conversation_id,
                user_email="owner@example.com",
                agent_id="target",
            )
            target.active_session_id = target_session.session_id
            target_session.context_type = "agent_work"
            target_session.context_data = target.context_data
            await session.commit()
        await app.state.channel_manager._inbound_pipeline.process(
            InboundMessage(
                channel_type="signal",
                account_id="account-1",
                chat_id="chat-1",
                sender_id="sender-1",
                message_id="participant-first",
                content="Please resolve my request.",
                timestamp=datetime.now(UTC),
            ),
            ChannelAccountConfig(
                display_name="Isolated Signal",
                account_id="account-1",
                channel_type="signal",
                user_email="owner@example.com",
                agent_id="target",
                settings={},
                dm_policy="open",
            ),
        )
        async with asyncio.timeout(15):
            while True:
                if llm.calls:
                    assert llm.offered_tools == {
                        "agent_conversation_send_controller",
                        "agent_conversation_complete",
                    }
                async with factory() as session:
                    signals = (
                        (await session.execute(select(ManagedConversationSignal))).scalars().all()
                    )
                if signals and not app.state.turn_scheduler.has_active_turn(target.conversation_id):
                    break
                await asyncio.sleep(0.02)
        async with factory() as session:
            signal = (await session.execute(select(ManagedConversationSignal))).scalar_one()
            assert signal.state == "waiting_controller"
            assert not (await session.execute(select(ChannelDeliveryOutboxRow))).scalars().all()
            requests = (await session.execute(select(DirectTurnRequestRow))).scalars().all()
            assert requests and requests[0].status == "completed"
        adapter.send_message.assert_not_awaited()
        await stack.aclose()
        app = app_module.create_app()
        await stack.enter_async_context(app.router.lifespan_context(app))
        factory = app.state.session_factory
        adapter.send_message.assert_not_awaited()
        if restart_pending:
            await app.state.channel_delivery.stop()
        ctx = _background_work_ctx(controller.conversation_id)
        ctx.session.user_email = "owner@example.com"
        ctx.agent.agent_id = "controller"
        ctx.agent.owner_email = "owner@example.com"
        result = await app.state.agent_loop._handle_managed_conversation_tool(
            ToolCall(
                call_id="resume-child",
                name="agent_conversation_send",
                arguments={
                    "conversation_id": target.conversation_id,
                    "message": "PRIVATE approval marker",
                },
            ),
            ctx=ctx,
        )
        assert not result.is_error, result.output
        async with asyncio.timeout(15):
            while True:
                async with factory() as session:
                    finals = (
                        (await session.execute(select(ChannelDeliveryOutboxRow))).scalars().all()
                    )
                if finals and not app.state.turn_scheduler.has_active_turn(target.conversation_id):
                    break
                await asyncio.sleep(0.02)
        async with factory() as session:
            rows = (await session.execute(select(ChannelDeliveryOutboxRow))).scalars().all()
            assert len(rows) == 1
            assert rows[0].fallback_text == "Public final answer."
        assert not transport_finished.is_set()
        if cancel_transport:
            async with asyncio.timeout(15):
                await transport_started.wait()
            await stack.aclose()
            async with factory() as session:
                row = (await session.execute(select(ChannelDeliveryOutboxRow))).scalar_one()
                assert row.status == "sending"
                assert row.inflight_chunk_index == 0
                row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            app = app_module.create_app()
            await stack.enter_async_context(app.router.lifespan_context(app))
            factory = app.state.session_factory
            async with asyncio.timeout(15):
                while True:
                    async with factory() as session:
                        row = (await session.execute(select(ChannelDeliveryOutboxRow))).scalar_one()
                        binding = await queries.get_managed_channel_binding_for_link(
                            session, link.link_id
                        )
                    if row.status == "uncertain" and binding.state == "delivery_failed":
                        break
                    await asyncio.sleep(0.02)
            assert row.last_error == "stale_non_idempotent_send"
            assert row.lease_token is None
            assert binding.delivery_lease_token is None
            assert adapter.send_message.await_count == 1
            assert not transport_finished.is_set()
            return
        if restart_pending:
            adapter.send_message.assert_not_awaited()
            await stack.aclose()
            app = app_module.create_app()
            await stack.enter_async_context(app.router.lifespan_context(app))
            factory = app.state.session_factory
        release_transport.set()
        async with asyncio.timeout(15):
            while not adapter.send_message.await_count:
                await asyncio.sleep(0.02)
        assert adapter.send_message.await_count == 1
        assert "PRIVATE" not in str(adapter.send_message.call_args)
        async with asyncio.timeout(15):
            while True:
                async with factory() as session:
                    row = (await session.execute(select(ChannelDeliveryOutboxRow))).scalar_one()
                    final_link = await queries.get_managed_conversation_link(session, link.link_id)
                if row.status == ("uncertain" if delivery_failure else "sent"):
                    break
                await asyncio.sleep(0.02)
        assert transport_started.is_set()
        assert transport_finished.is_set()
        assert row.lease_token is None
        assert row.lease_expires_at is None
        assert row.last_error != "stale_non_idempotent_send"
        async with factory() as session:
            binding = await queries.get_managed_channel_binding_for_link(session, link.link_id)
        assert binding.delivery_lease_token is None
        if delivery_failure:
            assert binding.state == "delivery_failed"
            await stack.aclose()
            app = app_module.create_app()
            await stack.enter_async_context(app.router.lifespan_context(app))
            await asyncio.sleep(0.1)
            assert adapter.send_message.await_count == 1
            return
        async with factory() as session:
            signal = (await session.execute(select(ManagedConversationSignal))).scalar_one()
            assert signal.state == "consumed"
            final_link = await queries.get_managed_conversation_link(session, link.link_id)
            assert final_link.conversation_state == "open"
            assert len(llm.calls) == 2
        await app.state.channel_manager._inbound_pipeline.process(
            InboundMessage(
                channel_type="signal",
                account_id="account-1",
                chat_id="chat-1",
                sender_id="sender-1",
                message_id="participant-done",
                content="Thank you, that resolves it.",
                timestamp=datetime.now(UTC),
            ),
            ChannelAccountConfig(
                display_name="Isolated Signal",
                account_id="account-1",
                channel_type="signal",
                user_email="owner@example.com",
                agent_id="target",
                settings={},
                dm_policy="open",
            ),
        )
        async with asyncio.timeout(15):
            while True:
                async with factory() as session:
                    final_link = await queries.get_managed_conversation_link(session, link.link_id)
                if len(llm.calls) == 3 and not app.state.turn_scheduler.has_active_turn(
                    target.conversation_id
                ):
                    break
                await asyncio.sleep(0.02)
        async with factory() as session:
            binding = await queries.get_managed_channel_binding_for_link(session, link.link_id)
        assert final_link.conversation_state == "completed", (
            final_link.turn_state,
            final_link.last_result_turn_id,
            final_link.control_metadata,
            binding.state,
            binding.version,
        )
        assert len(llm.calls) == 3
        assert adapter.send_message.await_count == 1
        await stack.aclose()
        app = app_module.create_app()
        await stack.enter_async_context(app.router.lifespan_context(app))
        await asyncio.sleep(0.1)
        assert adapter.send_message.await_count == 1
