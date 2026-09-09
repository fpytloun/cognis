from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core.runtime_selection import (
    persist_runtime_selection,
    resolve_runtime_selection,
)
from cognis.models.agent import AgentDefinition, AgentLLMConfig, AgentRuntimeProfile
from cognis.models.session import ConversationContext, ConversationModel, SessionModel
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, Session, User
from cognis.store.queries import get_session_row


class _Cache:
    def set_model_override(
        self,
        session_id: str,
        model: str | None,
        provider_id: str | None = None,
    ) -> None:
        del session_id, model, provider_id

    def set_reasoning_effort_override(self, session_id: str, effort: str | None) -> None:
        del session_id, effort

    def set_fast_mode_override(self, session_id: str, enabled: bool | None) -> None:
        del session_id, enabled


def _agent() -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        llm_config=AgentLLMConfig(
            provider_id="base-provider",
            model="base-model",
            reasoning_effort="low",
            fast_mode=False,
        ),
        agent_profiles={
            "developer": AgentRuntimeProfile(
                profile_id="developer",
                provider_id="profile-provider",
                model="profile-model",
                reasoning_effort="medium",
                fast_mode=True,
            )
        },
        default_agent_profile_id="developer",
    )


def _conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        agent_profile_id="developer",
        active_session_id="sess-1",
        context=ConversationContext(type="web"),
    )


def _session() -> SessionModel:
    return SessionModel(
        session_id="sess-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        agent_profile_id="developer",
    )


def test_runtime_selection_prefers_session_overrides() -> None:
    session = _session().model_copy(
        update={
            "model_override": "selected-model",
            "model_override_provider_id": "selected-provider",
            "reasoning_effort_override": "high",
            "fast_mode_override": False,
            "runtime_override_revision": 7,
        }
    )

    selection = resolve_runtime_selection(_agent(), session, _conversation())

    assert selection.revision == 7
    assert selection.profile_id == "developer"
    assert selection.model == "selected-model"
    assert selection.provider_id == "selected-provider"
    assert selection.model_source == "session_override"
    assert selection.reasoning_effort == "high"
    assert selection.reasoning_effort_source == "session_override"
    assert selection.fast_mode is False
    assert selection.fast_mode_source == "session_override"


@pytest.mark.asyncio
async def test_runtime_updates_are_durable_and_profile_clear_is_atomic(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/runtime.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add(User(email="user@example.com"))
        await db.commit()
        db.add(Agent(agent_id="agent-1", owner_email="user@example.com", name="Agent"))
        await db.commit()
        db.add(
            Conversation(
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
                agent_profile_id="developer",
                context_type="web",
                context_data={},
                memory_labels={},
            )
        )
        await db.commit()
        db.add(
            Session(
                session_id="sess-1",
                activity_scope_id="sess-1",
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
                agent_profile_id="developer",
            )
        )
        await db.commit()
        conversation_row = await db.get(Conversation, "conv-1")
        assert conversation_row is not None
        conversation_row.active_session_id = "sess-1"
        await db.commit()

    session = _session()
    conversation = _conversation()
    cache = _Cache()
    await persist_runtime_selection(
        session_factory=factory,
        session_cache=cache,
        conversation=conversation,
        session=session,
        model_override="selected-model",
        model_override_provider_id="selected-provider",
    )
    await persist_runtime_selection(
        session_factory=factory,
        session_cache=cache,
        conversation=conversation,
        session=session,
        reasoning_effort_override="high",
        fast_mode_override=False,
    )
    await persist_runtime_selection(
        session_factory=factory,
        session_cache=cache,
        conversation=conversation,
        session=session,
        profile_id="developer",
        clear_overrides=True,
        persist_conversation_profile=True,
    )

    async with factory() as db:
        row = await get_session_row(db, session.session_id)
        assert row is not None
        assert row.runtime_override_revision == 3
        assert row.model_override is None
        assert row.model_override_provider_id is None
        assert row.reasoning_effort_override is None
        assert row.fast_mode_override is None
        assert row.agent_profile_id == "developer"
    assert session.runtime_override_revision == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_update_rejects_a_stale_active_session(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/stale-runtime.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add(User(email="user@example.com"))
        await db.commit()
        db.add(Agent(agent_id="agent-1", owner_email="user@example.com", name="Agent"))
        await db.commit()
        db.add(
            Conversation(
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                context_data={},
                memory_labels={},
            )
        )
        await db.commit()
        db.add(
            Session(
                session_id="sess-1",
                activity_scope_id="sess-1",
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
            )
        )
        db.add(
            Session(
                session_id="sess-other",
                activity_scope_id="sess-other",
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
            )
        )
        await db.commit()
        conversation_row = await db.get(Conversation, "conv-1")
        assert conversation_row is not None
        conversation_row.active_session_id = "sess-other"
        await db.commit()

    with pytest.raises(RuntimeError, match="Active session changed"):
        await persist_runtime_selection(
            session_factory=factory,
            session_cache=SimpleNamespace(),
            conversation=_conversation(),
            session=_session(),
            model_override="selected-model",
            model_override_provider_id="selected-provider",
        )
    async with factory() as db:
        row = await get_session_row(db, "sess-1")
        assert row is not None
        assert row.runtime_override_revision == 0
        assert row.model_override is None
    await engine.dispose()
