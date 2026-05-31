"""Tests for automatic post-turn compaction in the agent loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    AgentLoop,
    CompactionRunContext,
    PauseWaiter,
    SessionLock,
    StepContext,
)
from cognis.core.compaction import CompactionResult
from cognis.core.events import Event, EventBus
from cognis.core.session_cache import CachedEvent, CachedSessionState
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext, ConversationModel, SessionModel
from cognis.models.workflow import StepDefinition

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCompactionStrategy:
    def __init__(
        self,
        *,
        result: CompactionResult | None = None,
        fail: bool = False,
        slow: float = 0.0,
    ) -> None:
        self.calls: list[tuple[str, str, bool]] = []  # (session_id, trigger, long_lived_chat)
        self.preserve_turns = 2
        self._result = result or CompactionResult(
            compacted=True,
            method="llm",
            summary="Auto-compaction summary text",
            compaction_seq=10,
            turns_compacted=5,
        )
        self._fail = fail
        self._slow = slow
        self.fallback_calls: list[tuple[str, str]] = []

    async def compact(
        self,
        session: SessionModel,
        *,
        trigger: str = "manual",
        model_context: object | None = None,
        long_lived_chat: bool = False,
    ) -> CompactionResult:
        del model_context
        self.calls.append((session.session_id, trigger, long_lived_chat))
        if self._slow:
            await asyncio.sleep(self._slow)
        if self._fail:
            raise RuntimeError("compaction failed")
        return self._result

    async def compact_with_fallback(
        self,
        session: SessionModel,
        *,
        trigger: str = "manual",
        model_context: object | None = None,
    ) -> CompactionResult:
        del model_context
        self.fallback_calls.append((session.session_id, trigger))
        return CompactionResult(
            compacted=True,
            method="mechanical",
            summary="Mechanical fallback summary",
            compaction_seq=20,
            turns_compacted=5,
        )


class _FakeGuardrails:
    async def record_events(self, *, session_id: str, events: list, **_: Any) -> Any:
        return type("AppendResult", (), {"ok": True, "first_seq": 1, "last_seq": len(events)})()


class _FakeSessionManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.rotations: list[dict[str, Any]] = []
        self._fail = fail

    async def rotate_session(
        self,
        *,
        conversation_id: str,
        current_session: SessionModel,
        intention: str,
        completion_reason: str = "compacted",
        compaction_summary: str | None = None,
        tail_events: list[Any] | None = None,
    ) -> SessionModel:
        del compaction_summary, tail_events
        self.rotations.append(
            {
                "conversation_id": conversation_id,
                "old_session_id": current_session.session_id,
                "intention": intention,
                "completion_reason": completion_reason,
            }
        )
        if self._fail:
            raise RuntimeError("rotation failed")
        return SessionModel(
            session_id="new-session-1",
            conversation_id=conversation_id,
            user_email=current_session.user_email,
            agent_id=current_session.agent_id,
            intaris_session_id="new-session-1",
        )


class _FakeSessionCache:
    def __init__(self, *, entry: CachedSessionState | None = None) -> None:
        self._entry = entry
        self.refreshed: list[str] = []
        self.compactions: list[tuple[str, str, int]] = []  # (session_id, summary, seq)

    def get_entry(self, session_id: str) -> CachedSessionState | None:
        if self._entry and self._entry.session_id == session_id:
            return self._entry
        return None

    async def refresh(self, session: SessionModel) -> CachedSessionState:
        self.refreshed.append(session.session_id)
        return CachedSessionState(
            session_id=session.session_id,
            intaris_session_id=session.intaris_session_id or session.session_id,
            initialized=True,
        )

    async def apply_compaction(
        self, session: SessionModel, *, summary: str, compaction_seq: int
    ) -> None:
        self.compactions.append((session.session_id, summary, compaction_seq))

    def get_events_since_compaction(
        self, session_id: str, types: list[str] | None = None
    ) -> list[CachedEvent]:
        if self._entry is None or self._entry.session_id != session_id:
            return []
        events = self._entry.events
        if types is None:
            return list(events)
        allowed = set(types)
        return [event for event in events if event.type in allowed]

    async def append_recorded_events(
        self, session: SessionModel, events: list, result: Any
    ) -> None:
        pass


@dataclass
class _FakeContextResult:
    messages: list[dict[str, Any]] = field(default_factory=list)
    recommend_compaction: bool = False
    cache_breakpoint_index: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(session_id: str = "session-1") -> SessionModel:
    return SessionModel(
        session_id=session_id,
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id=session_id,
        mnemory_session_id="mnemory-1",
    )


def _conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        active_session_id="session-1",
    )


def _cache_entry_with_events(user_event_count: int) -> CachedSessionState:
    events: list[CachedEvent] = []
    seq = 0
    for i in range(user_event_count):
        seq += 1
        events.append(CachedEvent(seq=seq, type="user_message", data={"content": f"msg {i}"}))
        seq += 1
        events.append(
            CachedEvent(seq=seq, type="assistant_message", data={"content": f"reply {i}"})
        )
    return CachedSessionState(
        session_id="session-1",
        intaris_session_id="session-1",
        events=events,
        last_event_seq=seq,
        initialized=True,
    )


# ---------------------------------------------------------------------------
# Tests — _auto_compact method
# ---------------------------------------------------------------------------

# We test _auto_compact directly by constructing the required objects.
# This avoids the complexity of wiring up the full agent loop.


def _minimal_agent_loop(
    *,
    compaction: _FakeCompactionStrategy | None = None,
    session_manager: _FakeSessionManager | None = None,
    session_cache: _FakeSessionCache | None = None,
    event_bus: EventBus | None = None,
) -> AgentLoop:
    """Create an AgentLoop with only the fields needed for _auto_compact."""
    loop = AgentLoop(
        providers=type("P", (), {"llm": None, "guardrails": _FakeGuardrails()})(),
        session_manager=session_manager or _FakeSessionManager(),
        session_cache=session_cache or _FakeSessionCache(),
        context_assembler=None,
        compaction_strategy=compaction or _FakeCompactionStrategy(),
        tool_router=None,
        remember_queue=type("RQ", (), {"enqueue": staticmethod(lambda _: None)})(),
        event_bus=event_bus or EventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    return loop


def _step_context(session: SessionModel | None = None) -> StepContext:
    return StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=session or _session(),
        conversation=_conversation(),
        agent=AgentDefinition(
            agent_id="agent-1",
            name="Test Agent",
            owner_email="user@example.com",
        ),
        policy=CHAT_POLICY,
    )


@pytest.mark.asyncio
async def test_auto_compact_triggers_rotation_and_caches() -> None:
    """Full happy path: compaction + rotation + cache pre-population."""
    compaction = _FakeCompactionStrategy()
    session_mgr = _FakeSessionManager()
    cache = _FakeSessionCache(entry=_cache_entry_with_events(5))
    published: list[Event] = []
    bus = EventBus()

    async def capture(event: Event) -> None:
        published.append(event)

    bus.subscribe_all(capture)

    loop = _minimal_agent_loop(
        compaction=compaction,
        session_manager=session_mgr,
        session_cache=cache,
        event_bus=bus,
    )

    ctx = _step_context()
    result = await loop._auto_compact(ctx)
    assert result is not None
    await loop._rotate_after_compaction(ctx, result, trigger="automatic")

    # Compaction was called with trigger="automatic"
    assert len(compaction.calls) == 1
    assert compaction.calls[0] == ("session-1", "automatic", False)

    # Session was rotated
    assert len(session_mgr.rotations) == 1
    assert session_mgr.rotations[0]["old_session_id"] == "session-1"
    assert session_mgr.rotations[0]["completion_reason"] == "compacted"

    # Cache was refreshed for the new session; the durable summary event is
    # recorded by SessionManager during rotation.
    assert cache.refreshed == ["new-session-1"]
    assert cache.compactions == []

    # Event was published by rotation.
    assert len(published) == 1
    assert published[0].type.value == "session_compacted"
    assert published[0].data["previous_session_id"] == "session-1"
    assert published[0].data["session_id"] == "new-session-1"


@pytest.mark.asyncio
async def test_auto_compact_skips_when_few_events() -> None:
    """Early exit when user event count <= preserve_turns."""
    compaction = _FakeCompactionStrategy()
    compaction.preserve_turns = 5
    cache = _FakeSessionCache(entry=_cache_entry_with_events(3))

    loop = _minimal_agent_loop(compaction=compaction, session_cache=cache)
    ctx = _step_context()
    result = await loop._auto_compact(ctx)

    # Compaction should not be called
    assert result is None
    assert len(compaction.calls) == 0


@pytest.mark.asyncio
async def test_auto_compact_skips_when_noop() -> None:
    """Compaction returns noop — no rotation should happen."""
    compaction = _FakeCompactionStrategy(
        result=CompactionResult(compacted=False, method="noop"),
    )
    session_mgr = _FakeSessionManager()
    cache = _FakeSessionCache(entry=_cache_entry_with_events(5))

    loop = _minimal_agent_loop(
        compaction=compaction,
        session_manager=session_mgr,
        session_cache=cache,
    )
    ctx = _step_context()
    result = await loop._auto_compact(ctx)

    assert result is not None
    assert result.compacted is False
    assert len(compaction.calls) == 1
    assert len(session_mgr.rotations) == 0


@pytest.mark.asyncio
async def test_auto_compact_compaction_failure_returns_none() -> None:
    """Compaction failure should return None.

    compact() now handles its own retry and mechanical fallback internally.
    When compact() raises, _auto_compact surfaces the failure cleanly by
    returning None rather than attempting a second fallback call.
    """
    compaction = _FakeCompactionStrategy(fail=True)
    session_mgr = _FakeSessionManager()
    cache = _FakeSessionCache(entry=_cache_entry_with_events(5))

    loop = _minimal_agent_loop(
        compaction=compaction,
        session_manager=session_mgr,
        session_cache=cache,
    )
    ctx = _step_context()

    # Should not raise; compact() already handled its own fallback internally.
    result = await loop._auto_compact(ctx)

    assert result is None
    assert len(compaction.calls) == 1
    # No rotation attempted when compaction failed.
    assert len(session_mgr.rotations) == 0


@pytest.mark.asyncio
async def test_auto_compact_rotation_failure_is_graceful() -> None:
    """Rotation failure after successful compaction should not raise."""
    compaction = _FakeCompactionStrategy()
    session_mgr = _FakeSessionManager(fail=True)
    cache = _FakeSessionCache(entry=_cache_entry_with_events(5))

    loop = _minimal_agent_loop(
        compaction=compaction,
        session_manager=session_mgr,
        session_cache=cache,
    )
    ctx = _step_context()

    # Should not raise
    result = await loop._auto_compact(ctx)
    assert result is not None
    rotated = await loop._rotate_after_compaction(ctx, result, trigger="automatic")

    assert len(compaction.calls) == 1
    assert rotated is None
    assert len(session_mgr.rotations) == 1
    assert len(cache.compactions) == 0  # Cache not populated after rotation failure


@pytest.mark.asyncio
async def test_auto_compact_timeout_is_graceful() -> None:
    """Slow compaction should time out without raising."""
    compaction = _FakeCompactionStrategy(slow=20.0)  # > AUTO_COMPACTION_TIMEOUT_SECONDS
    cache = _FakeSessionCache(entry=_cache_entry_with_events(5))

    published: list[Event] = []
    bus = EventBus()

    async def capture(event: Event) -> None:
        published.append(event)

    bus.subscribe_all(capture)
    loop = _minimal_agent_loop(compaction=compaction, session_cache=cache, event_bus=bus)
    ctx = _step_context()
    run = CompactionRunContext(trigger="automatic", reason="test_timeout")

    # Monkey-patch timeout to be very short for test speed
    import cognis.core.agent_loop as agent_loop_mod

    original_timeout = agent_loop_mod.AUTO_COMPACTION_TIMEOUT_SECONDS
    agent_loop_mod.AUTO_COMPACTION_TIMEOUT_SECONDS = 0.05
    try:
        result = await loop._auto_compact(ctx, run=run)
    finally:
        agent_loop_mod.AUTO_COMPACTION_TIMEOUT_SECONDS = original_timeout

    # Compaction was attempted, timed out, then used fallback.
    assert result is not None
    # The fake strategy returns "mechanical"; real strategy returns "mechanical_sliding_window".
    assert result.method in ("mechanical", "mechanical_sliding_window")
    assert run.used_timeout_fallback is True
    assert len(compaction.calls) == 1
    assert compaction.fallback_calls == [("session-1", "automatic_timeout_fallback")]
    assert any(event.type.value == "system_notice" for event in published)


@pytest.mark.asyncio
async def test_auto_compact_no_cache_entry_runs_normally() -> None:
    """When no cache entry exists, skip early-exit check and proceed."""
    compaction = _FakeCompactionStrategy()
    session_mgr = _FakeSessionManager()
    cache = _FakeSessionCache(entry=None)

    loop = _minimal_agent_loop(
        compaction=compaction,
        session_manager=session_mgr,
        session_cache=cache,
    )
    ctx = _step_context()
    result = await loop._auto_compact(ctx)
    assert result is not None
    await loop._rotate_after_compaction(ctx, result, trigger="automatic")

    # Should still run compaction (no early-exit)
    assert len(compaction.calls) == 1
    assert len(session_mgr.rotations) == 1


@pytest.mark.asyncio
async def test_idle_checkpoint_compacts_with_ambient_prompt_and_trigger() -> None:
    compaction = _FakeCompactionStrategy()
    session_mgr = _FakeSessionManager()
    cache = _FakeSessionCache(entry=_cache_entry_with_events(11))
    published: list[Event] = []
    bus = EventBus()

    async def capture(event: Event) -> None:
        published.append(event)

    bus.subscribe_all(capture)
    loop = _minimal_agent_loop(
        compaction=compaction,
        session_manager=session_mgr,
        session_cache=cache,
        event_bus=bus,
    )

    new_session = await loop.run_idle_checkpoint_compaction(
        conversation=_conversation(),
        session=_session(),
        agent=AgentDefinition(
            agent_id="agent-1",
            name="Test Agent",
            owner_email="user@example.com",
        ),
        min_events=20,
    )

    assert new_session is not None
    assert compaction.calls == [("session-1", "idle_checkpoint", True)]
    assert len(session_mgr.rotations) == 1
    assert published[0].data["trigger"] == "idle_checkpoint"


@pytest.mark.asyncio
async def test_idle_checkpoint_skips_below_min_events() -> None:
    compaction = _FakeCompactionStrategy()
    session_mgr = _FakeSessionManager()
    cache = _FakeSessionCache(entry=_cache_entry_with_events(9))
    loop = _minimal_agent_loop(
        compaction=compaction,
        session_manager=session_mgr,
        session_cache=cache,
    )

    new_session = await loop.run_idle_checkpoint_compaction(
        conversation=_conversation(),
        session=_session(),
        agent=AgentDefinition(
            agent_id="agent-1",
            name="Test Agent",
            owner_email="user@example.com",
        ),
        min_events=20,
    )

    assert new_session is None
    assert compaction.calls == []
    assert session_mgr.rotations == []
