"""Durable startup selection under turn admission, cancellation and configuration changes."""

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import schema as sql_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.core.agent_loop import PauseWaiter
from cognis.core.agent_profiles import resolve_conversation_agent_profile
from cognis.core.events import EventBus
from cognis.core.initial_profile_selection import (
    SELECTION_KEY,
    InitialProfileSelector,
    initial_profile_pending,
)
from cognis.core.session import SessionManager, _to_session_model
from cognis.core.turn_scheduler import TurnScheduler, _TurnControl
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext, EventAppendResult
from cognis.store import queries
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, User


def _answer(confidence: float = 0.9) -> dict:
    return {
        "model": "routes/classifier",
        "answers": {
            "profile": {
                "type": "choice",
                "choice": "select:deep",
                "confidence": confidence,
                "probabilities": {"keep_default": 1 - confidence, "select:deep": confidence},
            },
        },
    }


@pytest_asyncio.fixture
async def startup(tmp_path):
    postgres_url = os.environ.get("COGNIS_AUTO_PROFILE_TEST_POSTGRES_URL")
    admin = None
    schema_name = None
    if postgres_url:
        schema_name = f"auto_profile_{uuid.uuid4().hex}"
        admin = create_async_engine(postgres_url)
        async with admin.begin() as connection:
            await connection.execute(sql_schema.CreateSchema(schema_name))
        engine = create_async_engine(
            postgres_url, connect_args={"server_settings": {"search_path": schema_name}}
        )
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/startup.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    agent = AgentDefinition.model_validate(
        {
            "agent_id": "engineering",
            "owner_email": "owner@test.local",
            "name": "Engineering",
            "default_agent_profile_id": "routine",
            "agent_profiles": {
                "routine": {"description": "Routine bounded work.", "agent_switchable": True},
                "deep": {
                    "description": "Concurrency and recovery root causes.",
                    "agent_switchable": True,
                },
                "manual": {"description": "Manual selection only.", "agent_switchable": False},
            },
        }
    )
    async with factory() as db:
        db.add(User(email=agent.owner_email))
        await db.commit()
        db.add(
            Agent(
                agent_id=agent.agent_id,
                owner_email=agent.owner_email,
                name=agent.name,
                status="active",
                default_agent_profile_id=agent.default_agent_profile_id,
                agent_profiles={
                    key: value.model_dump(mode="json")
                    for key, value in agent.agent_profiles.items()
                },
            )
        )
        await db.commit()
    manager = SessionManager(factory, SimpleNamespace(), Mock())
    conversation = await manager.create_conversation(
        user_email=agent.owner_email,
        agent_id=agent.agent_id,
        context=ConversationContext(type="web", platform_data={"retained": True}),
    )
    async with factory() as db:
        row = await queries.create_session(
            db, conversation.conversation_id, agent.owner_email, agent.agent_id
        )
        conversation_row = await queries.get_conversation(db, conversation.conversation_id)
        conversation_row.active_session_id = row.session_id
        await db.commit()
        session = _to_session_model(row)
    conversation.active_session_id = session.session_id
    llm = SimpleNamespace(classify=AsyncMock(return_value=_answer()))
    cache = SimpleNamespace(
        set_model_override=Mock(),
        set_reasoning_effort_override=Mock(),
        set_fast_mode_override=Mock(),
    )
    yield SimpleNamespace(
        factory=factory,
        manager=manager,
        conversation=conversation,
        session=session,
        agent=agent,
        llm=llm,
        cache=cache,
        selector=InitialProfileSelector(factory, llm, cache),
    )
    await engine.dispose()
    if admin is not None:
        async with admin.begin() as connection:
            await connection.execute(sql_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


async def _prepare(startup, **kwargs):
    return await startup.selector.prepare(
        startup.conversation,
        startup.session,
        startup.agent,
        "Fix the data-integrity race during cancellation and recovery.",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_selection_is_atomic_durable_and_not_repeated_for_stale_queued_models(
    startup,
) -> None:
    conversation, session, _ = await _prepare(startup)
    assert conversation.agent_profile_id == session.agent_profile_id == "deep"
    assert session.runtime_override_revision == 1
    assert conversation.context.platform_data["retained"] is True
    assert conversation.context.platform_data[SELECTION_KEY]["status"] == "resolved"
    # A queued admission can still hold the original pending model.
    replay_conversation, replay_session, _ = await _prepare(startup)
    assert replay_conversation.agent_profile_id == replay_session.agent_profile_id == "deep"
    assert replay_session.runtime_override_revision == 1
    startup.llm.classify.assert_awaited_once()
    async with startup.factory() as db:
        saved = await queries.get_conversation(db, conversation.conversation_id)
        assert saved.agent_profile_id == "deep"
        assert saved.context_data[SELECTION_KEY]["confidence"] == 0.9
        assert "user_request" not in str(saved.context_data)


@pytest.mark.asyncio
async def test_fallback_is_also_persisted_once(startup) -> None:
    startup.llm.classify.return_value = _answer(confidence=0.5)
    conversation, session, _ = await _prepare(startup)
    assert conversation.agent_profile_id == session.agent_profile_id == "routine"
    assert conversation.context.platform_data[SELECTION_KEY]["reason"] == "low_confidence"
    await _prepare(startup)
    startup.llm.classify.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_conversation_without_startup_marker_never_classifies(startup) -> None:
    startup.conversation.context.platform_data.pop(SELECTION_KEY)
    conversation, session, _ = await _prepare(startup)
    assert conversation.agent_profile_id is None
    assert session.runtime_override_revision == 0
    startup.llm.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_leaves_no_partial_pin_or_resolved_marker(startup) -> None:
    fence = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await _prepare(startup, assert_current=fence)
    async with startup.factory() as db:
        row = await queries.get_conversation(db, startup.conversation.conversation_id)
        session = await queries.get_session_row(db, startup.session.session_id)
        assert row.agent_profile_id is session.agent_profile_id is None
        assert initial_profile_pending(row.context_data)
        assert session.runtime_override_revision == 0
    startup.cache.set_model_override.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "threshold", "manual_only", "description"])
async def test_configuration_is_revalidated_after_classification(startup, change: str) -> None:
    async def classify(*args, **kwargs):
        async with startup.factory() as db:
            row = await queries.get_agent(db, startup.agent.agent_id)
            if change == "disable":
                row.execution = {"auto_profile_selection": {"enabled": False}}
            elif change == "threshold":
                row.execution = {"auto_profile_selection": {"min_confidence": 0.95}}
            else:
                profiles = {key: dict(value) for key, value in row.agent_profiles.items()}
                profiles["deep"][
                    "agent_switchable" if change == "manual_only" else "description"
                ] = False if change == "manual_only" else "Changed routing contract"
                row.agent_profiles = profiles
            await db.commit()
        return _answer()

    startup.llm.classify.side_effect = classify
    conversation, session, _ = await _prepare(startup)
    assert conversation.agent_profile_id == session.agent_profile_id == "routine"
    assert conversation.context.platform_data[SELECTION_KEY]["reason"] == "configuration_changed"


@pytest.mark.asyncio
async def test_manual_runtime_selection_during_classification_wins(startup) -> None:
    async def classify(*args, **kwargs):
        async with startup.factory() as db:
            row = await queries.get_session_row(db, startup.session.session_id)
            row.agent_profile_id = "manual"
            row.model_override = "explicit-model"
            row.runtime_override_revision = 1
            conversation = await queries.get_conversation(db, startup.conversation.conversation_id)
            conversation.agent_profile_id = "manual"
            await db.commit()
        return _answer()

    startup.llm.classify.side_effect = classify
    conversation, session, _ = await _prepare(startup)
    assert conversation.agent_profile_id == session.agent_profile_id == "manual"
    assert session.model_override == "explicit-model"
    assert session.runtime_override_revision == 1
    assert conversation.context.platform_data[SELECTION_KEY]["reason"] == "explicit_selection"


@pytest.mark.asyncio
async def test_channel_profile_is_explicit_and_preserved(startup) -> None:
    startup.session.channel_default_agent_profile_id = "manual"
    _, session, _ = await _prepare(startup)
    assert session.channel_default_agent_profile_id == "manual"
    startup.llm.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_conversation_default_mode_and_agent_opt_out(startup) -> None:
    for mode in ["default", None]:
        if mode is None:
            async with startup.factory() as db:
                row = await queries.get_agent(db, startup.agent.agent_id)
                row.execution = {"auto_profile_selection": {"enabled": False}}
                await db.commit()
        conversation = await startup.manager.create_conversation(
            user_email=startup.agent.owner_email,
            agent_id=startup.agent.agent_id,
            agent_profile_mode=mode,
            context=ConversationContext(type="web"),
        )
        assert conversation.context.platform_data[SELECTION_KEY] == {
            "mode": "default",
            "status": "resolved",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_synthetic_default_does_not_pin_a_real_manual_or_disabled_default(
    startup, enabled: bool
) -> None:
    async with startup.factory() as db:
        row = await queries.get_agent(db, startup.agent.agent_id)
        row.default_agent_profile_id = None
        row.agent_profiles = {
            "default": {
                "enabled": enabled,
                "agent_switchable": False,
                "description": "Manual-only profile must not replace the synthetic baseline.",
            },
        }
        await db.commit()

    conversation, session, agent = await _prepare(startup)
    assert conversation.agent_profile_id is None
    assert session.agent_profile_id is None
    assert conversation.context.platform_data[SELECTION_KEY]["profile_id"] is None
    assert conversation.context.platform_data[SELECTION_KEY]["status"] == "resolved"
    assert resolve_conversation_agent_profile(agent, session, conversation).synthetic
    startup.llm.classify.assert_not_awaited()
    # A cold/queued continuation must retain the same implicit default.
    conversation, session, agent = await _prepare(startup)
    assert resolve_conversation_agent_profile(agent, session, conversation).synthetic
    startup.llm.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_agent_selectable_default_is_a_distinct_eligible_alternative(startup) -> None:
    async with startup.factory() as db:
        row = await queries.get_agent(db, startup.agent.agent_id)
        row.default_agent_profile_id = None
        row.agent_profiles = {
            "default": {
                "enabled": True,
                "agent_switchable": True,
                "description": "Deep race analysis.",
            },
        }
        await db.commit()
    startup.llm.classify.return_value = {
        "model": "routes/classifier",
        "answers": {
            "profile": {
                "type": "choice",
                "choice": "select:default",
                "confidence": 0.9,
                "probabilities": {"keep_default": 0.1, "select:default": 0.9},
            }
        },
    }
    conversation, session, agent = await _prepare(startup)
    assert session.agent_profile_id == "default"
    assert not resolve_conversation_agent_profile(agent, session, conversation).synthetic
    startup.llm.classify.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_selects_before_main_context_skips_system_and_does_not_reclassify(
    startup,
) -> None:
    observed: list[tuple[str | None, str, int]] = []

    async def main_boundary(**kwargs):
        session = kwargs["session"]
        conversation = kwargs["conversation"]
        observed.append(
            (
                session.agent_profile_id,
                conversation.context.platform_data[SELECTION_KEY]["status"],
                startup.llm.classify.await_count,
            )
        )
        # Stop at the workflow's context/inference boundary without a live main model.
        raise asyncio.CancelledError

    async def record_events(**kwargs):
        count = len(kwargs["events"])
        return EventAppendResult(ok=True, count=count, first_seq=42, last_seq=41 + count)

    startup.cache.append_recorded_events = AsyncMock()
    workflow = SimpleNamespace(run_direct_turn=AsyncMock(side_effect=main_boundary))
    scheduler = TurnScheduler(
        session_factory=startup.factory,
        workflow_engine=workflow,
        decision_engine=SimpleNamespace(
            decide=AsyncMock(return_value=SimpleNamespace(decision="chat"))
        ),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(refresh_intaris_session_policy=AsyncMock()),
        session_cache=startup.cache,
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(guardrails=SimpleNamespace(record_events=record_events)),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
        initial_profile_selector=startup.selector,
    )
    scheduler._persist_admitted_user_message = AsyncMock(return_value=(False, None))
    scheduler._touch_conversation = AsyncMock()
    scheduler._clear_follow_up_pending = AsyncMock()
    scheduler._suppress_absorbed_channel_delivery_intents = AsyncMock()

    for index, system_initiated in enumerate([True, False, False]):
        await scheduler._run_turn(
            conversation=startup.conversation,
            session=startup.session,
            agent=startup.agent,
            content="Diagnose a cross-session race",
            user_email=startup.agent.owner_email,
            attachments=[],
            outbound_attachments=None,
            attachment_notice=None,
            attachment_context=None,
            system_initiated=system_initiated,
            follow_up=None,
            channel_deliverable=False,
            delivery_id=None,
            delivery_fallback_text=None,
            bootstrap_wait_for_intention=False,
            cancel_event=asyncio.Event(),
            turn_control=_TurnControl(),
            turn_id=f"startup-turn-{index}",
        )
    assert observed == [
        (None, "pending", 0),
        ("deep", "resolved", 1),
        ("deep", "resolved", 1),
    ]
    assert workflow.run_direct_turn.await_count == 3
    startup.llm.classify.assert_awaited_once()
