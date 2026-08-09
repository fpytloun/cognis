from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.channels.managed import build_managed_channel_developer_instruction
from cognis.core.agent_loop import AgentLoop, SessionLock
from cognis.core.agent_profiles import resolve_conversation_agent_profile
from cognis.core.context import ContextAssembler
from cognis.core.session import (
    SessionManager,
    SessionRotationConflictError,
    _explicit_profile_for_fork,
    _map_cognis_to_intaris_status,
)
from cognis.core.session_cache import CachedEvent, SessionCache
from cognis.models.agent import AgentCapabilities, AgentDefinition, AgentLLMConfig
from cognis.models.config import ModelInfo
from cognis.models.session import (
    ConversationContext,
    ConversationLineage,
    ConversationModel,
    EventAppendResult,
    EventReadResult,
    SessionEvent,
    SessionModel,
    SessionTransition,
)
from cognis.providers.memory.policy import resolve_memory_policy
from cognis.runtime_context import current_agent_id, current_agent_owner_email, current_user_email
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Conversation, Session, StepRun, Task, User
from cognis.store.queries import (
    create_conversation,
    create_session,
    get_child_session_continuation_chain,
    get_conversation,
    get_root_session_chain,
    get_session_row,
    list_conversation_sessions,
    list_conversation_todos,
    list_session_todos,
    replace_conversation_todos,
    replace_session_todos,
)


def test_fork_preserves_only_target_agent_explicit_profile() -> None:
    conversation = ConversationModel(
        conversation_id="conv",
        user_email="user@example.com",
        agent_id="primary",
        agent_profile_id="primary-chat",
        context=ConversationContext(type="web"),
    )
    primary_session = SessionModel(
        session_id="sess-primary",
        conversation_id="conv",
        user_email="user@example.com",
        agent_id="primary",
        agent_profile_id="session-chat",
    )
    secondary_session = primary_session.model_copy(
        update={"session_id": "sess-secondary", "agent_id": "secondary"}
    )

    assert (
        _explicit_profile_for_fork(
            primary_session,
            conversation,
            target_agent_id="primary",
        )
        == "session-chat"
    )
    assert (
        _explicit_profile_for_fork(
            secondary_session,
            conversation,
            target_agent_id="primary",
        )
        is None
    )


class _Guardrails:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str | None]] = []
        self.status_calls: list[tuple[str, str, str | None]] = []
        self.last_details: dict | None = None
        self.last_policy: dict | None = None
        self.policy_updates: list[tuple[str, dict | None, dict | None]] = []
        self.recorded_events: list[tuple[str, list[SessionEvent], str | None]] = []
        self.record_event_contexts: list[tuple[str | None, str | None, str | None]] = []

    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict | None = None,
        details: dict | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("intaris unavailable")
        self.last_details = dict(details) if details is not None else None
        self.last_policy = dict(policy) if policy is not None else None
        self.calls.append((session_id, agent_id, parent_session_id))

    async def update_session_status(
        self,
        session_id: str,
        status: str,
        status_reason: str | None = None,
        user_email: str | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("intaris unavailable")
        self.status_calls.append((session_id, status, status_reason))
        self.status_context = (
            user_email,
            current_user_email.get(),
            current_agent_id.get(),
            current_agent_owner_email.get(),
        )

    async def update_session_policy(
        self,
        session_id: str,
        *,
        agent_id: str,
        user_id: str | None = None,
        details: dict | None = None,
        policy: dict | None = None,
    ) -> None:
        del agent_id, user_id
        if self.fail:
            raise RuntimeError("intaris unavailable")
        self.last_details = dict(details) if details is not None else None
        self.last_policy = dict(policy) if policy is not None else None
        self.policy_updates.append((session_id, self.last_details, self.last_policy))

    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
        **_: object,
    ) -> object:
        del source
        self.recorded_events.append((session_id, events, idempotency_key))
        self.record_event_contexts.append(
            (current_user_email.get(), current_agent_id.get(), current_agent_owner_email.get())
        )
        return type(
            "_AppendResult",
            (),
            {"ok": True, "count": len(events), "first_seq": 1, "last_seq": len(events)},
        )()


class _NonAppendingGuardrails(_Guardrails):
    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
        **_: object,
    ) -> object:
        del source
        self.recorded_events.append((session_id, events, idempotency_key))
        self.record_event_contexts.append(
            (current_user_email.get(), current_agent_id.get(), current_agent_owner_email.get())
        )
        return EventAppendResult(ok=False, count=0, first_seq=0, last_seq=0)


class _OptionalAppendFailingGuardrails(_Guardrails):
    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
        **_: object,
    ) -> object:
        del source
        self.recorded_events.append((session_id, events, idempotency_key))
        self.record_event_contexts.append(
            (current_user_email.get(), current_agent_id.get(), current_agent_owner_email.get())
        )
        if idempotency_key and idempotency_key.endswith(":rotation_seed"):
            return EventAppendResult(
                ok=True,
                count=len(events),
                first_seq=1,
                last_seq=len(events),
            )
        return EventAppendResult(ok=False, count=0, first_seq=0, last_seq=0)


class _SlowGuardrails(_Guardrails):
    def __init__(self) -> None:
        super().__init__(fail=False)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict | None = None,
        details: dict | None = None,
    ) -> None:
        del intention, user_id, policy, details
        self.calls.append((session_id, agent_id, parent_session_id))
        self.entered.set()
        await self.release.wait()


class _Providers:
    def __init__(self, fail: bool = False) -> None:
        self.guardrails = _Guardrails(fail=fail)
        self.memory = object()


class _StreamGuardrails(_Guardrails):
    def __init__(self) -> None:
        super().__init__()
        self.streams: dict[str, list[dict]] = {}
        self.idempotent_results: dict[tuple[str, str], EventAppendResult] = {}

    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict | None = None,
        details: dict | None = None,
    ) -> None:
        await super().create_session(
            session_id,
            intention,
            agent_id,
            user_id,
            parent_session_id,
            policy,
            details,
        )
        self.streams.setdefault(session_id, [])

    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
        **kwargs: object,
    ) -> EventAppendResult:
        del kwargs
        key = (session_id, idempotency_key or "")
        if idempotency_key and key in self.idempotent_results:
            return self.idempotent_results[key]
        stream = self.streams.setdefault(session_id, [])
        first_seq = len(stream) + 1
        for index, event in enumerate(events):
            stream.append(
                {
                    "seq": first_seq + index,
                    "type": event.type,
                    "data": dict(event.data),
                    "source": source,
                }
            )
        result = EventAppendResult(
            ok=True,
            count=len(events),
            first_seq=first_seq,
            last_seq=first_seq + len(events) - 1,
        )
        if idempotency_key:
            self.idempotent_results[key] = result
        return result

    async def read_events(
        self,
        session_id: str,
        after_seq: int = 0,
        **kwargs: object,
    ) -> EventReadResult:
        del kwargs
        events = [
            dict(event)
            for event in self.streams.get(session_id, [])
            if int(event["seq"]) > after_seq
        ]
        return EventReadResult(
            events=events,
            last_seq=max((int(event["seq"]) for event in events), default=after_seq),
            has_more=False,
        )

    async def get_session(self, session_id: str) -> object:
        del session_id
        return SimpleNamespace(
            intention="Resolve the participant request.",
            title="Managed participant",
            updated_at=None,
        )


class _StreamProviders:
    def __init__(self) -> None:
        self.guardrails = _StreamGuardrails()
        self.memory = object()


class _ContextLLM:
    async def resolve_model(
        self, explicit_model: str | None = None, task_type: str = "default"
    ) -> str:
        del task_type
        return explicit_model or "test-model"

    async def get_model_info(self, model_id: str, **_: object) -> ModelInfo:
        return ModelInfo(
            model_id=model_id,
            context_window=20_000,
            max_output_tokens=256,
        )

    def count_tokens(self, text: str, model: str) -> int:
        del model
        return max(1, len(text) // 4)

    def count_messages_tokens(self, messages: list[dict], model: str) -> int:
        del model
        return sum(max(1, len(str(message.get("content", ""))) // 4) for message in messages)


class _Cache:
    def __init__(self) -> None:
        self.evicted: list[str] = []
        self.appended_events: list[tuple[SessionModel, list[SessionEvent], object]] = []

    async def evict(self, session_id: str) -> bool:
        self.evicted.append(session_id)
        return True

    async def append_recorded_events(
        self,
        session: SessionModel,
        events: list[SessionEvent],
        append_result: object,
    ) -> None:
        self.appended_events.append((session, events, append_result))


@pytest.mark.asyncio
async def test_seed_rotated_tail_events_skips_non_appendable_event_types(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    cache = _Cache()
    manager = SessionManager(session_factory, providers, cache)
    new_session = SessionModel(
        session_id="new-session",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id="intaris-new-session",
    )
    tail_events = [
        SimpleNamespace(
            seq=10,
            type="user_message",
            data={"content": "keep", "intention_eligible": False},
        ),
        SimpleNamespace(seq=11, type="tool_result_chunk", data={"content": "skip"}),
        SimpleNamespace(seq=12, type="assistant_message", data={"content": "keep too"}),
    ]

    await manager._seed_rotated_tail_events(
        new_session,
        tail_events=tail_events,
        previous_session_id="old-session",
    )

    assert len(providers.guardrails.recorded_events) == 1
    session_id, recorded_events, idempotency_key = providers.guardrails.recorded_events[0]
    assert session_id == "intaris-new-session"
    assert idempotency_key == "new-session:compaction_tail:old-session"
    assert [event.type for event in recorded_events] == ["user_message", "assistant_message"]
    assert [event.data["source_seq"] for event in recorded_events] == [10, 12]
    assert recorded_events[0].data["intention_eligible"] is False
    assert all(event.data["compaction_tail"] is True for event in recorded_events)
    assert all(event.data["source_session_id"] == "old-session" for event in recorded_events)
    assert len(cache.appended_events) == 1
    assert cache.appended_events[0][1] == recorded_events

    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_rotated_tail_events_accepts_legacy_user_message_without_eligibility(
    tmp_path,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())
    new_session = SessionModel(
        session_id="new-session",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
    )

    await manager._seed_rotated_tail_events(
        new_session,
        tail_events=[SimpleNamespace(seq=1, type="user_message", data={"content": "legacy"})],
        previous_session_id="old-session",
    )

    recorded_event = providers.guardrails.recorded_events[0][1][0]
    assert "intention_eligible" not in recorded_event.data

    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_rotated_tail_events_batches_large_event_sets(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    cache = _Cache()
    manager = SessionManager(session_factory, providers, cache)
    new_session = SessionModel(
        session_id="new-session",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id="intaris-new-session",
    )
    tail_events = [
        SimpleNamespace(seq=index, type="user_message", data={"content": f"message {index}"})
        for index in range(1001)
    ]

    await manager._seed_rotated_tail_events(
        new_session,
        tail_events=tail_events,
        previous_session_id="old-session",
    )

    assert len(providers.guardrails.recorded_events) == 2
    assert [len(events) for _, events, _ in providers.guardrails.recorded_events] == [1000, 1]
    assert [key for _, _, key in providers.guardrails.recorded_events] == [
        "new-session:compaction_tail:old-session:batch:1",
        "new-session:compaction_tail:old-session:batch:2",
    ]
    assert len(cache.appended_events) == 2
    assert [len(events) for _, events, _ in cache.appended_events] == [1000, 1]
    assert cache.appended_events[1][1][0].data["source_seq"] == 1000

    await engine.dispose()


async def _session_factory(tmp_path) -> object:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}")
    session_factory = create_session_factory(engine)
    from cognis.bootstrap import run_schema_bootstrap

    await run_schema_bootstrap(engine)
    async with session_factory() as session:
        session.add(User(email="user@example.com", name="User", password_hash="x", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent One",
                description="Helpful assistant",
            )
        )
        await session.commit()
    return engine, session_factory


@pytest.mark.asyncio
async def test_session_manager_creates_conversation_and_root_session_atomically(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Test conversation",
    )

    assert conversation.active_session_id == root_session.session_id
    assert root_session.intaris_session_id == root_session.session_id

    async with session_factory() as session:
        stored_conversation = await session.get(Conversation, conversation.conversation_id)
        stored_session = await session.get(Session, root_session.session_id)
        assert stored_conversation is not None
        assert stored_conversation.active_session_id == root_session.session_id
        assert stored_session is not None
        assert stored_session.intaris_session_id == root_session.session_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_generic_creation_never_trusts_lineage_context_data(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    conversation, root = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(
            type="web",
            platform_data={
                "forked_from": "conversation",
                "forked_from_conversation_id": "conv-source",
                "forked_from_session_id": "session-source",
            },
        ),
        title="Forged lineage",
    )

    async with session_factory() as session:
        stored_conversation = await session.get(Conversation, conversation.conversation_id)
        stored_session = await session.get(Session, root.session_id)
        assert stored_conversation is not None
        assert stored_conversation.context_data["forked_from"] == "conversation"
        assert stored_conversation.lineage_kind is None
        assert stored_conversation.fork_source_conversation_id is None
        assert stored_conversation.fork_source_session_id is None
        assert stored_session is not None
        assert stored_session.source_session_id is None
        direct = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="web",
            context_data={
                "forked_from": "task_step",
                "task_id": "task-forged",
                "step_run_id": "step-forged",
                "source_session_id": root.session_id,
            },
        )
        await session.commit()
        assert direct.lineage_kind is None
        assert direct.fork_source_session_id is None
        assert direct.lineage_task_id is None
        assert direct.lineage_step_run_id is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_trusted_lineage_creation_validates_conversation_task_and_step_edges(
    tmp_path,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())
    source_conversation, source_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="task", ref="task-1"),
        title="Source",
    )
    async with session_factory() as session:
        session.add(
            Task(
                task_id="task-1",
                title="Task",
                status="running",
                priority=0,
                created_by="user@example.com",
                agent_id="agent-1",
                source_type="api",
                delivery_mode="same_conversation",
                queue_name="default",
            )
        )
        await session.flush()
        session.add(
            StepRun(
                step_run_id="step-1",
                task_id="task-1",
                step_name="implement",
                step_type="run",
                status="completed",
                agent_id="agent-1",
                conversation_id=source_conversation.conversation_id,
                session_id=source_session.session_id,
            )
        )
        await session.commit()

    lineages = [
        ConversationLineage(
            kind="conversation",
            source_conversation_id=source_conversation.conversation_id,
            source_session_id=source_session.session_id,
        ),
        ConversationLineage(
            kind="task",
            source_conversation_id=source_conversation.conversation_id,
            source_session_id=source_session.session_id,
            task_id="task-1",
            step_run_id="step-1",
        ),
        ConversationLineage(
            kind="task_step",
            source_conversation_id=source_conversation.conversation_id,
            source_session_id=source_session.session_id,
            task_id="task-1",
            step_run_id="step-1",
        ),
    ]
    created: list[tuple[ConversationModel, SessionModel]] = []
    for lineage in lineages:
        created.append(
            await manager.create_conversation_with_root_session(
                user_email="user@example.com",
                agent_id="agent-1",
                context=ConversationContext(type="web"),
                title=f"{lineage.kind} continuation",
                lineage=lineage,
            )
        )

    async with session_factory() as session:
        for lineage, (conversation, root) in zip(lineages, created, strict=True):
            stored_conversation = await session.get(Conversation, conversation.conversation_id)
            stored_session = await session.get(Session, root.session_id)
            assert stored_conversation is not None
            assert stored_conversation.lineage_kind == lineage.kind
            assert stored_conversation.fork_source_session_id == source_session.session_id
            assert stored_session is not None
            assert stored_session.source_session_id == source_session.session_id
        assert (
            await session.get(Conversation, created[0][0].conversation_id)
        ).fork_source_conversation_id == source_conversation.conversation_id
        assert (await session.get(Conversation, created[1][0].conversation_id)).lineage_task_id == (
            "task-1"
        )
        assert (
            await session.get(Conversation, created[2][0].conversation_id)
        ).lineage_step_run_id == "step-1"

    await engine.dispose()


@pytest.mark.asyncio
async def test_canonical_conversation_fork_persists_trusted_lineage(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())
    source_conversation, source_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Source",
    )
    fork_events = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "cognis.core.session.fork_session_events",
        fork_events,
    )

    forked_conversation, forked_session, copied = await manager.fork_into_new_conversation(
        source_session=source_session,
        source_conversation=source_conversation,
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent One",
        ),
        user_email="user@example.com",
    )

    assert copied is True
    async with session_factory() as session:
        stored_conversation = await session.get(Conversation, forked_conversation.conversation_id)
        stored_session = await session.get(Session, forked_session.session_id)
        assert stored_conversation is not None
        assert stored_conversation.lineage_kind == "conversation"
        assert (
            stored_conversation.fork_source_conversation_id == source_conversation.conversation_id
        )
        assert stored_conversation.fork_source_session_id == source_session.session_id
        assert stored_session is not None
        assert stored_session.source_session_id == source_session.session_id
        assert stored_session.activity_scope_id == stored_session.session_id
        assert stored_session.activity_scope_id != source_session.activity_scope_id
    event_filter = fork_events.await_args.kwargs["event_filter"]
    assert event_filter(CachedEvent(seq=1, type="user_message", data={})) is True
    assert event_filter(CachedEvent(seq=2, type="tool_call", data={})) is False
    assert event_filter(CachedEvent(seq=3, type="todo_state", data={})) is False
    assert event_filter(CachedEvent(seq=4, type="assistant_deliverable", data={})) is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_trusted_lineage_rejects_cross_owner_and_mismatched_task_sources(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())
    source_conversation, source_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="task", ref="task-1"),
        title="Source",
    )
    other_conversation, other_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Other source",
    )
    async with session_factory() as session:
        session.add(User(email="other@example.com", name="Other", password_hash="x", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-other",
                owner_email="other@example.com",
                name="Other agent",
                description="Other",
            )
        )
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv-other-owner",
                user_email="other@example.com",
                agent_id="agent-other",
                context_type="web",
                title_source="unset",
            )
        )
        await session.flush()
        session.add(
            Session(
                session_id="session-other-owner",
                conversation_id="conv-other-owner",
                user_email="other@example.com",
                agent_id="agent-other",
                delegation_metadata={},
            )
        )
        await session.flush()
        session.add(
            Task(
                task_id="task-1",
                title="Task",
                status="running",
                priority=0,
                created_by="user@example.com",
                agent_id="agent-1",
                source_type="api",
                delivery_mode="same_conversation",
                queue_name="default",
            )
        )
        await session.flush()
        session.add(
            StepRun(
                step_run_id="step-1",
                task_id="task-1",
                step_name="implement",
                step_type="run",
                status="completed",
                agent_id="agent-1",
                conversation_id=source_conversation.conversation_id,
                session_id=source_session.session_id,
            )
        )
        await session.commit()

    invalid_lineages = [
        ConversationLineage(
            kind="conversation",
            source_conversation_id="conv-other-owner",
            source_session_id="session-other-owner",
        ),
        ConversationLineage(
            kind="task",
            source_conversation_id=other_conversation.conversation_id,
            source_session_id=other_session.session_id,
            task_id="task-1",
            step_run_id="step-1",
        ),
        ConversationLineage(
            kind="task_step",
            source_conversation_id=source_conversation.conversation_id,
            source_session_id=source_session.session_id,
            task_id="task-mismatch",
            step_run_id="step-1",
        ),
    ]
    for lineage in invalid_lineages:
        with pytest.raises(PermissionError, match="lineage source"):
            await manager.create_conversation_with_root_session(
                user_email="user@example.com",
                agent_id="agent-1",
                context=ConversationContext(type="web"),
                lineage=lineage,
            )


@pytest.mark.asyncio
async def test_managed_policy_snapshot_rotates_once_from_creation_flow(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _StreamProviders()
    cache = SessionCache(providers.guardrails)
    manager = SessionManager(session_factory, providers, cache)
    conversation, managed_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(
            type="channel",
            ref="mcb_test",
            platform_data={"managed_channel": True},
        ),
        title="Managed participant",
    )
    loop = object.__new__(AgentLoop)
    loop.providers = providers
    loop.session_cache = cache
    loop.session_lock = SessionLock()
    policy = build_managed_channel_developer_instruction(
        objective="Resolve the participant request.",
        participant="Participant",
        channel_type="signal",
        safety_guidance="Do not disclose private controller context.",
    )

    await loop._record_persisted_developer_context(
        session=managed_session,
        content=policy,
        source="managed_channel_policy",
        target_agent_id="agent-1",
    )

    source_entries = cache.get_prefix_entries(managed_session.session_id)
    assert [
        entry.source for entry in source_entries if entry.source == "managed_channel_policy"
    ] == ["managed_channel_policy"]
    source_snapshots = [
        event
        for event in providers.guardrails.streams[managed_session.session_id]
        if event["type"] == "context_snapshot"
    ]
    assert len(source_snapshots) == 1
    assert [
        ref["source"]
        for ref in source_snapshots[0]["data"]["entries"]
        if ref["source"] == "managed_channel_policy"
    ] == ["managed_channel_policy"]

    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        system_prompt="You are helpful.",
        llm_config=AgentLLMConfig(model="test-model", max_tokens=128),
        capabilities=AgentCapabilities(memory_backend="none"),
    )
    memory_policy = resolve_memory_policy(
        agent,
        resolve_conversation_agent_profile(agent, managed_session, conversation),
    )
    assembler = ContextAssembler(
        memory=providers.memory,
        guardrails=providers.guardrails,
        llm=_ContextLLM(),
        session_cache=cache,
        session_manager=manager,
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    assembled = await assembler.assemble(
        session=managed_session,
        conversation=conversation,
        agent=agent,
        user_message="Start the managed participant conversation.",
        tool_definitions=[],
        memory_policy=memory_policy,
    )
    assembled_prompt = "\n".join(
        str(message.get("content") or "") for message in assembled.messages
    )
    assert assembled_prompt.count(policy) == 1
    initialized_entries = cache.get_prefix_entries(managed_session.session_id)
    assert [
        entry.source for entry in initialized_entries if entry.source == "managed_channel_policy"
    ] == ["managed_channel_policy"]
    assert cache.get_entry(managed_session.session_id).memory_policy_fingerprint == (
        memory_policy.policy_fingerprint
    )

    rotated = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=managed_session,
        intention="Continue the managed participant conversation.",
        compaction_summary="The managed participant conversation remains active.",
    )

    rotated_policy_events = [
        event
        for event in providers.guardrails.streams[rotated.session_id]
        if event["type"] == "developer_message"
        and event["data"].get("source") == "managed_channel_policy"
    ]
    assert len(rotated_policy_events) == 1
    rotated_snapshots = [
        event
        for event in providers.guardrails.streams[rotated.session_id]
        if event["type"] == "context_snapshot"
    ]
    assert len(rotated_snapshots) == 1
    assert [
        ref["source"]
        for ref in rotated_snapshots[0]["data"]["entries"]
        if ref["source"] == "managed_channel_policy"
    ] == ["managed_channel_policy"]
    assert (
        sum(
            entry.source == "managed_channel_policy"
            for entry in cache.get_prefix_entries(rotated.session_id)
        )
        == 1
    )

    await cache.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_session_manager_rolls_back_when_intaris_creation_fails(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(fail=True), _Cache())

    with pytest.raises(RuntimeError, match="intaris unavailable"):
        await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Broken conversation",
        )

    async with session_factory() as session:
        assert (await session.execute(Session.__table__.select())).all() == []
        assert (await session.execute(Conversation.__table__.select())).all() == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_session_manager_recovery_uses_updated_at_not_started_at(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            Conversation(
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
            )
        )
        session.add_all(
            [
                Session(
                    session_id="fresh-session",
                    conversation_id="conv-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=2),
                    updated_at=now,
                ),
                Session(
                    session_id="stale-parent",
                    conversation_id="conv-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(minutes=20),
                ),
                Session(
                    session_id="stale-child",
                    conversation_id="conv-1",
                    parent_session_id="stale-parent",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=1),
                    updated_at=now - timedelta(minutes=20),
                ),
                Session(
                    session_id="stale-grandchild",
                    conversation_id="conv-1",
                    parent_session_id="stale-child",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=1),
                    updated_at=now - timedelta(minutes=20),
                ),
            ]
        )
        await session.commit()

    recovered_ids = await manager.recover_stale_sessions(stale_after_seconds=300)

    assert set(recovered_ids) == {"stale-parent", "stale-child", "stale-grandchild"}
    async with session_factory() as session:
        fresh = await session.get(Session, "fresh-session")
        stale_parent = await session.get(Session, "stale-parent")
        stale_child = await session.get(Session, "stale-child")
        stale_grandchild = await session.get(Session, "stale-grandchild")
        assert fresh is not None and fresh.status == "active"
        assert stale_parent is not None and stale_parent.status == "idle"
        assert stale_parent.idle_since is not None
        assert stale_child is not None and stale_child.status == "failed"
        assert stale_grandchild is not None and stale_grandchild.status == "failed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_rotate_session_creates_new_root_and_marks_old_completed(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    cache = _Cache()
    manager = SessionManager(session_factory, providers, cache)

    # Create an initial conversation + root session
    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Rotation test",
    )

    # Rotate
    new_session = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=root_session,
        intention="Continued after compaction",
        completion_reason="compacted",
        compaction_summary="Summary of older turns.",
        compaction_summary_event_data={
            "method": "llm",
            "turns_compacted": 5,
            "status": "compacted",
        },
    )

    # Verify new session
    assert new_session.session_id != root_session.session_id
    assert new_session.conversation_id == conversation.conversation_id

    # Verify old session is completed
    async with session_factory() as db:
        old_row = await db.get(Session, root_session.session_id)
        assert old_row is not None
        assert old_row.status == "completed"
        assert old_row.completion_reason == "compacted"
        assert old_row.completed_at is not None

    # Verify new session is linked via previous_session_id
    async with session_factory() as db:
        new_row = await db.get(Session, new_session.session_id)
        assert new_row is not None
        assert new_row.previous_session_id == root_session.session_id

    # Verify conversation root updated
    async with session_factory() as db:
        conv = await db.get(Conversation, conversation.conversation_id)
        assert conv is not None
        assert conv.active_session_id == new_session.session_id

    # Verify old session cache was evicted
    assert root_session.session_id in cache.evicted

    # Verify Intaris session was created for new root
    assert len(providers.guardrails.calls) == 2  # original + rotation
    assert providers.guardrails.calls[1][0] == new_session.session_id
    assert any(
        key == f"{new_session.session_id}:rotation_seed"
        and events
        and getattr(events[0], "type", None) == "lifecycle"
        and events[0].data["event"] == "session_rotated"
        for session_id, events, key in providers.guardrails.recorded_events
        if session_id == new_session.session_id
    )
    assert any(
        key == f"{new_session.session_id}:compaction_summary:rotation"
        and events
        and getattr(events[0], "type", None) == "compaction_summary"
        and events[0].data["session_id"] == new_session.session_id
        and events[0].data["source_session_id"] == root_session.session_id
        and events[0].data["method"] == "llm"
        and events[0].data["timeline_visible"] is True
        and events[0].data["turns_compacted"] == 5
        and events[0].data["status"] == "compacted"
        for session_id, events, key in providers.guardrails.recorded_events
        if session_id == new_session.session_id
    )
    assert (
        "user@example.com",
        "agent-1",
        "user@example.com",
    ) in providers.guardrails.record_event_contexts

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transition", "preserves_scope", "preserves_todos", "preserves_context"),
    [
        (SessionTransition.COMPACT, True, True, True),
        (SessionTransition.RENEW, False, False, True),
        (SessionTransition.RESET, False, False, False),
    ],
)
async def test_rotate_session_transition_matrix(
    tmp_path,
    transition: SessionTransition,
    preserves_scope: bool,
    preserves_todos: bool,
    preserves_context: bool,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())
    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Transition matrix",
    )
    todos = [{"content": "Finish work", "status": "in_progress"}]
    async with session_factory() as db:
        await replace_conversation_todos(db, conversation.conversation_id, todos)
        await replace_session_todos(db, root_session.session_id, todos)
        await db.commit()

    tail = [SimpleNamespace(seq=2, type="user_message", data={"content": "recent"})]
    successor = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=root_session,
        intention="Continue",
        completion_reason=("user_reset" if transition is SessionTransition.RESET else "compacted"),
        transition=transition,
        compaction_summary="Prior conversational summary",
        tail_events=tail,
    )

    assert (successor.activity_scope_id == root_session.activity_scope_id) is preserves_scope
    async with session_factory() as db:
        assert bool(await list_conversation_todos(db, conversation.conversation_id)) is (
            preserves_todos
        )
        assert bool(await list_session_todos(db, successor.session_id)) is preserves_todos

    recorded_keys = {
        key
        for session_id, _events, key in providers.guardrails.recorded_events
        if session_id == successor.session_id
    }
    assert any("compaction_summary" in key for key in recorded_keys) is preserves_context
    assert any("compaction_tail" in key for key in recorded_keys) is preserves_context
    await engine.dispose()


@pytest.mark.asyncio
async def test_rotate_child_preserves_lane_identity_and_root_visibility(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())
    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Child rotation test",
    )
    metadata = {"task_id": "task-1", "depth": 1}
    async with session_factory() as db:
        child_row = await create_session(
            db,
            conversation.conversation_id,
            "user@example.com",
            "agent-1",
            agent_profile_id="developer",
            parent_session_id=root_session.session_id,
            delegation_mode="execute",
            delegation_task="Implement the fix",
            delegation_metadata=metadata,
        )
        await db.commit()
        child = SessionModel.model_validate(child_row, from_attributes=True)

    first_successor = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=child,
        intention="Continue child",
        compaction_summary="Child summary",
    )
    second_successor = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=first_successor,
        intention="Continue child again",
        compaction_summary="Second child summary",
    )

    async with session_factory() as db:
        conv = await db.get(Conversation, conversation.conversation_id)
        first_row = await db.get(Session, first_successor.session_id)
        second_row = await db.get(Session, second_successor.session_id)
        root_chain, truncated = await get_root_session_chain(
            db,
            conversation.conversation_id,
            root_session.session_id,
        )
        child_chain, child_truncated = await get_child_session_continuation_chain(
            db, child.session_id
        )

    assert conv is not None
    assert conv.active_session_id == root_session.session_id
    assert first_row is not None
    assert second_row is not None
    for row in (first_row, second_row):
        assert row.parent_session_id == root_session.session_id
        assert row.agent_profile_id == "developer"
        assert row.delegation_mode == "execute"
        assert row.delegation_task == "Implement the fix"
        assert row.delegation_metadata == metadata
    assert first_row.previous_session_id == child.session_id
    assert second_row.previous_session_id == first_successor.session_id
    assert [row.session_id for row in root_chain] == [root_session.session_id]
    assert truncated is False
    assert [row.session_id for row in child_chain] == [
        child.session_id,
        first_successor.session_id,
        second_successor.session_id,
    ]
    assert child_truncated is False
    assert providers.guardrails.calls[-2][2] == root_session.session_id
    assert providers.guardrails.calls[-1][2] == root_session.session_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_root_rotation_has_clean_conflict(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())
    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Root rotation conflict test",
    )

    winner = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=root_session,
        intention="Winner",
    )
    create_calls = len(manager.providers.guardrails.calls)
    event_calls = len(manager.providers.guardrails.recorded_events)
    with pytest.raises(
        SessionRotationConflictError,
        match="Conversation active session changed",
    ):
        await manager.rotate_session(
            conversation_id=conversation.conversation_id,
            current_session=root_session,
            intention="Stale loser",
        )
    assert len(manager.providers.guardrails.calls) == create_calls
    assert len(manager.providers.guardrails.recorded_events) == event_calls

    async with session_factory() as db:
        conv = await db.get(Conversation, conversation.conversation_id)
        rows = await list_conversation_sessions(db, conversation.conversation_id)

    assert conv is not None
    assert conv.active_session_id == winner.session_id
    assert {row.session_id for row in rows} == {root_session.session_id, winner.session_id}

    await engine.dispose()


@pytest.mark.asyncio
async def test_rotate_session_keeps_new_root_when_compaction_summary_append_fails(
    tmp_path,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    providers.guardrails = _OptionalAppendFailingGuardrails()
    cache = _Cache()
    manager = SessionManager(session_factory, providers, cache)

    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Rotation append failure test",
    )

    new_session = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=root_session,
        intention="Continued after compaction",
        completion_reason="compacted",
        compaction_summary="Summary of older turns.",
    )

    async with session_factory() as db:
        conv = await db.get(Conversation, conversation.conversation_id)
        old_row = await db.get(Session, root_session.session_id)
        new_row = await db.get(Session, new_session.session_id)

    assert conv is not None
    assert conv.active_session_id == new_session.session_id
    assert old_row is not None
    assert old_row.status == "completed"
    assert old_row.completion_reason == "compacted"
    assert new_row is not None
    assert new_row.status == "active"
    assert new_row.previous_session_id == root_session.session_id
    assert providers.guardrails.recorded_events
    assert len(cache.appended_events) == 1
    assert cache.appended_events[0][1][0].type == "lifecycle"

    await engine.dispose()


@pytest.mark.asyncio
async def test_rotate_session_does_not_activate_unseeded_intaris_stream(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    providers.guardrails = _NonAppendingGuardrails()
    cache = _Cache()
    manager = SessionManager(session_factory, providers, cache)

    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Rotation seed failure test",
    )

    with pytest.raises(RuntimeError, match="Could not seed rotated Intaris session stream"):
        await manager.rotate_session(
            conversation_id=conversation.conversation_id,
            current_session=root_session,
            intention="Continued after compaction",
            completion_reason="compacted",
            compaction_summary="Summary of older turns.",
        )

    async with session_factory() as db:
        conv = await db.get(Conversation, conversation.conversation_id)
        old_row = await db.get(Session, root_session.session_id)

    assert conv is not None
    assert conv.active_session_id == root_session.session_id
    assert old_row is not None
    assert old_row.status == "active"
    assert old_row.completion_reason is None
    assert root_session.session_id not in cache.evicted

    await engine.dispose()


@pytest.mark.asyncio
async def test_rotate_session_resets_mnemory_session_id(tmp_path) -> None:
    """Rotated sessions start with mnemory_session_id=None.

    Session rotation creates a new context window.  The first recall in
    the new session creates a fresh Mnemory session and reconstructs the
    full immutable prefix (core memories + instructions) from scratch.
    """
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Mnemory reset test",
    )

    # Set a mnemory_session_id on the root session
    async with session_factory() as db:
        row = await db.get(Session, root_session.session_id)
        assert row is not None
        row.mnemory_session_id = "mnemory-abc-123"
        await db.commit()

    root_session.mnemory_session_id = "mnemory-abc-123"

    new_session = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=root_session,
        intention="Test",
    )

    # The new session should NOT carry forward mnemory_session_id —
    # it starts fresh so the first recall triggers is_first_call=True
    # in Mnemory, which returns core memories and instructions.
    async with session_factory() as db:
        new_row = await db.get(Session, new_session.session_id)
        assert new_row is not None
        assert new_row.mnemory_session_id is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_root_session_is_single_winner(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    providers.guardrails = _SlowGuardrails()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Race conversation",
    )

    first = asyncio.create_task(
        manager.ensure_root_session(
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            intention="first bootstrap",
        )
    )
    await providers.guardrails.entered.wait()
    second = asyncio.create_task(
        manager.ensure_root_session(
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            intention="second bootstrap",
        )
    )
    providers.guardrails.release.set()

    first_session, second_session = await asyncio.gather(first, second)

    assert first_session.session_id == second_session.session_id

    async with session_factory() as db:
        stored_conversation = await db.get(Conversation, conversation.conversation_id)
        stored_sessions = await list_conversation_sessions(db, conversation.conversation_id)
        assert stored_conversation is not None
        assert stored_conversation.active_session_id == first_session.session_id
        assert [row.session_id for row in stored_sessions] == [first_session.session_id]

    await engine.dispose()


# ------------------------------------------------------------------
# Status mapping tests
# ------------------------------------------------------------------


def test_map_cognis_to_intaris_status_direct_mappings() -> None:
    for status in ("active", "idle", "completed", "suspended", "terminated"):
        intaris_status, _ = _map_cognis_to_intaris_status(status)
        assert intaris_status == status


def test_map_cognis_to_intaris_status_failed_maps_to_terminated() -> None:
    intaris_status, reason = _map_cognis_to_intaris_status("failed")
    assert intaris_status == "terminated"
    assert reason == "source_status=failed"


def test_map_cognis_to_intaris_status_cancelled_maps_to_terminated() -> None:
    intaris_status, reason = _map_cognis_to_intaris_status("cancelled")
    assert intaris_status == "terminated"
    assert reason == "source_status=cancelled"


def test_map_cognis_to_intaris_status_completed_with_reason() -> None:
    intaris_status, reason = _map_cognis_to_intaris_status(
        "completed", completion_reason="compacted"
    )
    assert intaris_status == "completed"
    assert reason == "completion_reason=compacted"


def test_map_cognis_to_intaris_status_reason_truncated() -> None:
    _, reason = _map_cognis_to_intaris_status("suspended", reason="x" * 600)
    assert reason is not None
    assert len(reason) <= 500


# ------------------------------------------------------------------
# SessionManager Intaris sync tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_completed_syncs_to_intaris(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Sync test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    await manager.mark_completed(
        root.session_id,
        result_content="Full durable delegate result",
        completion_reason="compacted",
    )

    assert len(providers.guardrails.status_calls) == 1
    sid, status, reason = providers.guardrails.status_calls[0]
    assert sid == root.session_id
    assert status == "completed"
    assert reason == "completion_reason=compacted"
    async with session_factory() as db_session:
        row = await get_session_row(db_session, root.session_id)
    assert row is not None
    assert row.result_content == "Full durable delegate result"

    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_active_syncs_to_intaris_and_clears_idle(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Active test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    await manager.mark_idle(root.session_id)
    await manager.mark_active(root.session_id)

    async with session_factory() as db_session:
        row = await get_session_row(db_session, root.session_id)
        assert row is not None
        assert row.status == "active"
        assert row.idle_since is None

    assert len(providers.guardrails.status_calls) == 2
    sid, status, reason = providers.guardrails.status_calls[-1]
    assert sid == root.session_id
    assert status == "active"
    assert reason is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_failed_syncs_terminated_to_intaris(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Fail test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    await manager.mark_failed(root.session_id, result_summary="boom")

    assert len(providers.guardrails.status_calls) == 1
    sid, status, reason = providers.guardrails.status_calls[0]
    assert sid == root.session_id
    assert status == "terminated"
    assert reason == "source_status=failed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_cancelled_syncs_terminated_to_intaris(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Cancel test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    await manager.mark_cancelled(root.session_id, result_summary="user cancelled")

    assert len(providers.guardrails.status_calls) == 1
    sid, status, reason = providers.guardrails.status_calls[0]
    assert sid == root.session_id
    assert status == "terminated"
    assert reason == "source_status=cancelled"

    await engine.dispose()


@pytest.mark.asyncio
async def test_intaris_sync_failure_does_not_block_mark(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers_ok = _Providers(fail=False)
    manager_ok = SessionManager(session_factory, providers_ok, _Cache())

    conversation = await manager_ok.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Degraded test",
    )
    root = await manager_ok.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    # Now use a failing provider for mark_idle
    providers_fail = _Providers(fail=True)
    manager_fail = SessionManager(session_factory, providers_fail, _Cache())
    updated = await manager_fail.mark_idle(root.session_id)
    assert updated  # DB update succeeded despite Intaris failure

    await engine.dispose()


@pytest.mark.asyncio
async def test_status_sync_uses_authoritative_session_identity_after_restart(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers(fail=False)
    manager = SessionManager(session_factory, providers, _Cache())
    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Restart identity",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )
    async with session_factory() as session:
        row = await session.get(Session, root.session_id)
        assert row is not None
        row.intaris_session_id = "intaris-session-1"
        await session.commit()

    await manager.mark_idle(root.session_id)

    assert providers.guardrails.status_calls[-1][:2] == ("intaris-session-1", "idle")
    assert providers.guardrails.status_context == (
        "user@example.com",
        "user@example.com",
        "agent-1",
        "user@example.com",
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_intaris_sync_failure_logs_safe_diagnostics(tmp_path, caplog) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers_ok = _Providers(fail=False)
    manager_ok = SessionManager(session_factory, providers_ok, _Cache())

    conversation = await manager_ok.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Diagnostics test",
    )
    root = await manager_ok.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    providers_fail = _Providers(fail=True)
    manager_fail = SessionManager(session_factory, providers_fail, _Cache())
    await manager_fail.mark_idle(root.session_id)

    matching = [
        record
        for record in caplog.records
        if record.message == "session: failed to sync status to Intaris"
    ]
    assert matching
    extra = matching[-1].__dict__.get("extra_data") or {}
    assert extra["session_id"] == root.session_id
    assert extra["target_session_id"] == root.session_id
    assert extra["uses_intaris_session_id"] is False
    assert extra["has_user_email"] is True
    assert extra["user_email_hash"]
    assert "user@example.com" not in str(extra)

    await engine.dispose()


@pytest.mark.asyncio
async def test_archive_conversation_clears_active_session_and_marks_session_completed(
    tmp_path,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Archive test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    archived = await manager.archive_conversation(conversation.conversation_id)

    assert archived is True
    assert providers.guardrails.status_calls[-1] == (
        root.session_id,
        "completed",
        "completion_reason=conversation_archived",
    )

    async with session_factory() as session:
        stored_conversation = await get_conversation(session, conversation.conversation_id)
        stored_session = await get_session_row(session, root.session_id)
        assert stored_conversation is not None
        assert stored_conversation.status == "archived"
        assert stored_conversation.active_session_id is None
        assert stored_session is not None
        assert stored_session.status == "completed"
        assert stored_session.completion_reason == "conversation_archived"

    await engine.dispose()


@pytest.mark.asyncio
async def test_soft_delete_conversation_clears_active_session_and_marks_session_completed(
    tmp_path,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Delete test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    deleted = await manager.soft_delete_conversation(conversation.conversation_id)

    assert deleted is True
    assert providers.guardrails.status_calls[-1] == (
        root.session_id,
        "completed",
        "completion_reason=conversation_deleted",
    )

    async with session_factory() as session:
        stored_conversation = await get_conversation(session, conversation.conversation_id)
        stored_session = await get_session_row(session, root.session_id)
        assert stored_conversation is not None
        assert stored_conversation.status == "deleted"
        assert stored_conversation.active_session_id is None
        assert stored_session is not None
        assert stored_session.status == "completed"
        assert stored_session.completion_reason == "conversation_deleted"

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_root_session_passes_workdir_and_allow_paths_to_intaris(tmp_path) -> None:
    """Executor-visible working directory must reach Intaris on session create."""

    from cognis.runtime_context import scoped_runtime_context

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    workdir = "/home/user/projects/cognis"
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        effective_working_directory=workdir,
    ):
        conversation, root = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Wired",
        )
    del conversation, root

    assert providers.guardrails.last_details is not None
    assert providers.guardrails.last_details["working_directory"] == workdir
    assert providers.guardrails.last_details["source"] == "cognis"
    assert providers.guardrails.last_policy is not None
    allow_paths = providers.guardrails.last_policy["allow_paths"]
    assert f"{workdir}/*" in allow_paths
    assert not any(path.endswith("/.cognis/*") for path in allow_paths)

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_root_session_falls_back_to_executor_cwd_when_context_unset(
    tmp_path,
) -> None:
    """Chat sessions must allow the executor cwd even without platform path data."""

    from types import SimpleNamespace

    from cognis.runtime_context import scoped_runtime_context

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    executor_env = SimpleNamespace(home="/home/user", cwd="/home/user/src/cognis")
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        executor_environment=executor_env,
    ):
        conversation, root = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="No workdir",
        )
    del conversation, root

    assert providers.guardrails.last_details is not None
    assert providers.guardrails.last_details["working_directory"] == "/home/user/src/cognis"
    assert providers.guardrails.last_policy is not None
    assert "/tmp/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/private/tmp/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/var/tmp/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/home/user/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/home/user/src/cognis/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/home/user/.local/share/cognis/*" in providers.guardrails.last_policy["allow_paths"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_intaris_session_policy_includes_project_source_paths(tmp_path) -> None:
    """Project source local_path entries must be added to allow_paths."""

    from cognis.runtime_context import scoped_runtime_context
    from cognis.store.queries import create_project_source

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    async with session_factory() as session:
        from cognis.store.models import ProjectRow

        session.add(
            ProjectRow(
                project_id="proj-1",
                name="Test Project",
                owner_email="user@example.com",
            )
        )
        await session.flush()
        await create_project_source(
            session,
            project_id="proj-1",
            name="cognis",
            local_path="/home/user/src/cognis",
            remote_url=None,
            default_branch="main",
        )
        await create_project_source(
            session,
            project_id="proj-1",
            name="intaris",
            local_path="/home/user/src/intaris",
            remote_url=None,
            default_branch="main",
        )
        await session.commit()

    workdir = "/home/user/projects/work"
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        effective_working_directory=workdir,
    ):
        conversation = await manager.create_conversation(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Project conv",
            project_id="proj-1",
        )
        await manager.create_root_session(
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            intention="work",
        )

    allow_paths = providers.guardrails.last_policy["allow_paths"]
    assert "/tmp/*" in allow_paths
    assert "/private/tmp/*" in allow_paths
    assert "/var/tmp/*" in allow_paths
    assert "/home/user/src/cognis/*" in allow_paths
    assert "/home/user/src/intaris/*" in allow_paths
    assert f"{workdir}/*" in allow_paths

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_conversation_with_root_session_uses_project_source_paths(
    tmp_path,
) -> None:
    """Task step conversations must allow every configured project source."""

    from cognis.runtime_context import scoped_runtime_context
    from cognis.store.queries import create_project_source

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    async with session_factory() as session:
        from cognis.store.models import ProjectRow

        session.add(
            ProjectRow(
                project_id="proj-1",
                name="Test Project",
                owner_email="user@example.com",
            )
        )
        await session.flush()
        await create_project_source(
            session,
            project_id="proj-1",
            name="cognis",
            local_path="/home/user/src/cognis",
            remote_url=None,
            default_branch="main",
        )
        await create_project_source(
            session,
            project_id="proj-1",
            name="intaris",
            local_path="/home/user/src/intaris",
            remote_url=None,
            default_branch="main",
        )
        await session.commit()

    narrowed_workdir = "/home/user/src/cognis/ui/src/lib"
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        effective_working_directory=narrowed_workdir,
    ):
        conversation, _ = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="task", ref="task-1"),
            title="Task step",
            project_id="proj-1",
        )

    assert conversation.project_id == "proj-1"
    assert providers.guardrails.last_details == {
        "source": "cognis",
        "working_directory": narrowed_workdir,
    }
    allow_paths = providers.guardrails.last_policy["allow_paths"]
    assert "/tmp/*" in allow_paths
    assert "/var/tmp/*" in allow_paths
    assert "/home/user/src/cognis/*" in allow_paths
    assert "/home/user/src/intaris/*" in allow_paths
    assert f"{narrowed_workdir}/*" in allow_paths

    await engine.dispose()


@pytest.mark.asyncio
async def test_intaris_session_policy_expands_tilde_with_executor_home(tmp_path) -> None:
    """Project sources using ~ must be expanded with the executor home directory."""

    from types import SimpleNamespace

    from cognis.runtime_context import scoped_runtime_context
    from cognis.store.queries import create_project_source

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    async with session_factory() as session:
        from cognis.store.models import ProjectRow

        session.add(
            ProjectRow(
                project_id="proj-tilde",
                name="Tilde Project",
                owner_email="user@example.com",
            )
        )
        await session.flush()
        await create_project_source(
            session,
            project_id="proj-tilde",
            name="cognis",
            local_path="~/src/cognis",
            remote_url=None,
            default_branch="main",
        )
        await create_project_source(
            session,
            project_id="proj-tilde",
            name="intaris",
            local_path="~/src/intaris",
            remote_url=None,
            default_branch="main",
        )
        await session.commit()

    executor_env = SimpleNamespace(home="/home/executor", cwd="/home/executor/src/cognis")
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        effective_working_directory="~/src/cognis/api",
        executor_environment=executor_env,
    ):
        conversation, _ = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="task", ref="task-tilde"),
            title="Tilde task step",
            project_id="proj-tilde",
        )

    assert conversation.project_id == "proj-tilde"
    assert providers.guardrails.last_details == {
        "source": "cognis",
        "working_directory": "~/src/cognis/api",
    }
    allow_paths = providers.guardrails.last_policy["allow_paths"]
    assert "~/src/cognis/*" not in allow_paths
    assert "~/src/intaris/*" not in allow_paths
    assert "/home/executor/src/cognis/*" in allow_paths
    assert "/home/executor/src/intaris/*" in allow_paths
    assert "/home/executor/src/cognis/api/*" in allow_paths
    assert "/home/executor/.local/share/cognis/*" in allow_paths
    assert "/tmp/*" in allow_paths
    assert "/var/tmp/*" in allow_paths

    await engine.dispose()


@pytest.mark.asyncio
async def test_child_session_does_not_inherit_implicit_runtime_workdir(tmp_path) -> None:
    """Delegated child sessions stay neutral unless the caller scopes paths explicitly."""

    from types import SimpleNamespace

    from cognis.runtime_context import scoped_runtime_context

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    executor_env = SimpleNamespace(home="/home/user", cwd="/home/user/src/cognis")
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        executor_environment=executor_env,
        workspace_root="/home/user/src/codex",
        effective_working_directory="/home/user/src/codex/codex-rs/protocol/src",
    ):
        conversation, parent = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Parent",
        )
        child = await manager.create_child_session(
            parent,
            mode="sync",
            task_description="Inspect policy",
            agent_id="agent-1",
            effective_agent_id="agent-1",
        )
    assert child.activity_scope_id == parent.activity_scope_id
    del conversation, parent, child

    assert providers.guardrails.last_policy is not None
    assert (
        "/home/user/src/codex/codex-rs/protocol/src/*"
        not in providers.guardrails.last_policy["allow_paths"]
    )
    assert providers.guardrails.last_details is not None
    assert "working_directory" not in providers.guardrails.last_details

    await engine.dispose()


@pytest.mark.asyncio
async def test_child_session_uses_explicit_delegation_workdir(tmp_path) -> None:
    """Explicit delegation paths are still reflected in Intaris details and policy."""

    from cognis.runtime_context import scoped_runtime_context

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
    ):
        _, parent = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Parent",
        )
        child = await manager.create_child_session(
            parent,
            mode="sync",
            task_description="Inspect policy",
            agent_id="agent-1",
            effective_agent_id="agent-1",
            workspace_root="/home/user/src/cognis",
            working_directory="/home/user/src/cognis/cognis/core",
        )
    del parent, child

    assert providers.guardrails.last_policy is not None
    assert "/home/user/src/cognis/cognis/core/*" in providers.guardrails.last_policy["allow_paths"]
    assert providers.guardrails.last_details is not None
    assert (
        providers.guardrails.last_details["working_directory"]
        == "/home/user/src/cognis/cognis/core"
    )

    await engine.dispose()
