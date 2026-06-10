from __future__ import annotations

import pytest

from cognis.core.compaction import (
    LONG_LIVED_CHAT_COMPACTION_ADDENDUM,
    CompactionModelContext,
    CompactionStrategy,
    _format_events_for_compaction,
    _mechanical_summary,
)
from cognis.core.session_cache import CachedEvent, CachedSessionState
from cognis.models.session import EventAppendResult, SessionModel


class _Cache:
    def __init__(self) -> None:
        self.entry = CachedSessionState(
            session_id="session-1",
            intaris_session_id="session-1",
            events=[
                CachedEvent(seq=1, type="user_message", data={"content": "task one"}),
                CachedEvent(seq=2, type="assistant_message", data={"content": "answer one"}),
                CachedEvent(seq=3, type="user_message", data={"content": "task two"}),
                CachedEvent(seq=4, type="assistant_message", data={"content": "answer two"}),
                CachedEvent(seq=5, type="user_message", data={"content": "task three"}),
            ],
            initialized=True,
        )
        self.applied: list[tuple[str, int]] = []

    def get_entry(self, session_id: str) -> CachedSessionState:
        del session_id
        return self.entry

    async def refresh(self, session: SessionModel) -> CachedSessionState:
        del session
        return self.entry

    async def apply_compaction(
        self, session: SessionModel, *, summary: str, compaction_seq: int
    ) -> None:
        self.applied.append((summary, compaction_seq))


class _Guardrails:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.idempotency_keys: list[str | None] = []

    async def record_events(self, **kwargs: object) -> EventAppendResult:
        self.calls += 1
        idempotency_value = kwargs.get("idempotency_key")
        idempotency_key: str | None = (
            idempotency_value if isinstance(idempotency_value, str) else None
        )
        self.idempotency_keys.append(idempotency_key)
        if self.fail:
            raise RuntimeError("write failed")
        return EventAppendResult(ok=True, count=1, first_seq=6, last_seq=6)


class _LLM:
    def __init__(self) -> None:
        self.messages: list[list[dict[str, object]]] = []
        self.kwargs: list[dict[str, object]] = []

    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        self.messages.append(messages)
        self.kwargs.append({"model": model, "task_type": task_type, **dict(kwargs)})
        return {"choices": [{"message": {"content": "summary text"}}]}

    async def resolve_model(
        self, explicit_model: str | None = None, task_type: str = "default", **kwargs: object
    ) -> str:
        del explicit_model, task_type, kwargs
        return "test-model"

    def count_tokens(self, text: str, model: str) -> int:
        del model
        return len(text)


class _FailingLLM(_LLM):
    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        self.messages.append(messages)
        self.kwargs.append({"model": model, "task_type": task_type, **dict(kwargs)})
        raise ValueError("LLM failed")


def _session() -> SessionModel:
    return SessionModel(
        session_id="session-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id="session-1",
    )


@pytest.mark.asyncio
async def test_compaction_records_summary_and_updates_cache() -> None:
    cache = _Cache()
    llm = _LLM()
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    result = await strategy.compact(_session())

    assert result.compacted is True
    assert result.method == "llm"
    assert cache.applied == [("summary text", 6)]
    assert llm.kwargs[0]["cognis_session_id"] == "session-1"
    assert llm.kwargs[0]["model"] is None
    assert llm.kwargs[0]["task_type"] == "compaction"
    assert result.tail_start_seq == 3
    assert [event.seq for event in result.preserved_tail_events] == [3, 4, 5]


@pytest.mark.asyncio
async def test_compaction_preview_summary_does_not_record_or_update_cache() -> None:
    cache = _Cache()
    guardrails = _Guardrails()
    llm = _LLM()
    strategy = CompactionStrategy(
        guardrails=guardrails,
        llm=llm,
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    result = await strategy.preview_summary(_session())

    assert result.compacted is True
    assert result.method == "llm"
    assert result.summary == "summary text"
    assert result.compaction_seq is None
    assert result.turns_compacted == 1
    assert result.tail_start_seq == 3
    assert guardrails.calls == 0
    assert cache.applied == []


@pytest.mark.asyncio
async def test_compaction_preview_mechanical_fallback_does_not_record_or_update_cache() -> None:
    cache = _Cache()
    guardrails = _Guardrails()
    strategy = CompactionStrategy(
        guardrails=guardrails,
        llm=_FailingLLM(),
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    result = await strategy.preview_summary(_session())

    assert result.compacted is True
    assert result.method == "mechanical_sliding_window"
    assert result.summary
    assert result.compaction_seq is None
    assert guardrails.calls == 0
    assert cache.applied == []


@pytest.mark.asyncio
async def test_compaction_prompt_adds_long_lived_chat_addendum() -> None:
    cache = _Cache()
    llm = _LLM()
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    await strategy.compact(_session(), long_lived_chat=True)

    assert LONG_LIVED_CHAT_COMPACTION_ADDENDUM in str(llm.messages[0][0]["content"])


@pytest.mark.asyncio
async def test_compaction_prompt_default_omits_long_lived_chat_addendum() -> None:
    cache = _Cache()
    llm = _LLM()
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    await strategy.compact(_session())

    assert LONG_LIVED_CHAT_COMPACTION_ADDENDUM not in str(llm.messages[0][0]["content"])


@pytest.mark.asyncio
async def test_llm_compaction_appends_recoverable_tool_handles() -> None:
    cache = _Cache()
    cache.entry.events = [
        CachedEvent(seq=1, type="user_message", data={"content": "inspect files"}),
        CachedEvent(
            seq=2,
            type="tool_result",
            data={
                "call_id": "tool-call-3",
                "recovery_call_id": "tool-call-3",
                "name": "read",
                "result": "file contents",
                "has_full_output": True,
            },
        ),
        CachedEvent(seq=3, type="user_message", data={"content": "continue"}),
    ]
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=1,
    )

    result = await strategy.compact(_session())

    assert result.compacted is True
    assert cache.applied
    summary, _seq = cache.applied[0]
    assert summary.startswith("summary text")
    assert "Recoverable tool outputs before compaction:" in summary
    assert "read_tool_output(call_id='tool-call-3')" in summary


@pytest.mark.asyncio
async def test_compaction_write_failure_leaves_cache_unchanged() -> None:
    cache = _Cache()
    strategy = CompactionStrategy(
        guardrails=_Guardrails(fail=True),
        llm=_LLM(),
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    with pytest.raises(RuntimeError, match="write failed"):
        await strategy.compact(_session())

    assert cache.applied == []


def test_compaction_formats_attachment_notes() -> None:
    events = [
        type(
            "Event",
            (),
            {
                "seq": 1,
                "type": "assistant_message",
                "data": {
                    "content": "",
                    "attachments": [
                        {"filename": "report.pdf", "kind": "pdf"},
                        {"filename": "diagram.png", "kind": "image"},
                    ],
                },
            },
        )()
    ]

    formatted = _format_events_for_compaction(events)

    assert "report.pdf (pdf)" in formatted
    assert "diagram.png (image)" in formatted


def test_compaction_formats_tool_results_with_recovery_metadata() -> None:
    events = [
        CachedEvent(
            seq=7,
            type="tool_result",
            data={
                "call_id": "tool-call-1",
                "recovery_call_id": "tool-call-1",
                "name": "read",
                "result": "x" * 2500,
                "output_size": 2500,
                "has_full_output": True,
            },
        )
    ]

    formatted = _format_events_for_compaction(events)

    assert "call_id='tool-call-1'" in formatted
    assert "recovery_call_id='tool-call-1'" in formatted
    assert "output_size=2500" in formatted
    assert "has_full_output=true" in formatted
    assert "read_tool_output(call_id='tool-call-1')" in formatted
    assert "search_tool_output(call_id='tool-call-1', pattern='keyword')" in formatted
    assert "truncated for compaction: omitted 500 chars" in formatted


def test_mechanical_summary_keeps_recoverable_tool_handles() -> None:
    events = []
    for index in range(12):
        events.append(
            CachedEvent(
                seq=index + 1,
                type="tool_result",
                data={
                    "call_id": f"tool-call-{index}",
                    "recovery_call_id": f"tool-call-{index}",
                    "name": "grep",
                    "result": "many matches",
                    "has_full_output": True,
                },
            )
        )

    summary = _mechanical_summary(events)

    assert "Recoverable tool outputs before compaction:" in summary
    assert "grep: recover with read_tool_output(call_id='tool-call-0')" in summary
    assert "grep: recover with read_tool_output(call_id='tool-call-11')" in summary


@pytest.mark.asyncio
async def test_compaction_uses_idempotency_key() -> None:
    """Compaction passes an idempotency key to record_events.

    Retry logic is now in the Intaris provider (exponential backoff),
    so compaction calls record_events once.  The idempotency key
    ensures safe retries at the provider level.
    """
    cache = _Cache()
    guardrails = _Guardrails()
    strategy = CompactionStrategy(
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    result = await strategy.compact(_session())

    assert result.compacted is True
    assert len(guardrails.idempotency_keys) == 1
    assert guardrails.idempotency_keys[0] is not None


@pytest.mark.asyncio
async def test_compaction_updates_previous_summary_anchor() -> None:
    cache = _Cache()
    cache.entry.last_compaction_summary = "## Goal\n- old goal"
    llm = _LLM()
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    result = await strategy.compact(_session())

    assert result.compacted is True
    raw_prompt = llm.messages[0][1]["content"]
    assert isinstance(raw_prompt, str)
    prompt = raw_prompt
    assert "<previous-summary>" in prompt
    assert "old goal" in prompt
    assert "<new-history>" in prompt


@pytest.mark.asyncio
async def test_same_session_compaction_forwards_model_context() -> None:
    class _SameSessionLLM(_LLM):
        async def resolve_model(
            self, explicit_model: str | None = None, task_type: str = "default", **kwargs: object
        ) -> str:
            del explicit_model, task_type, kwargs
            return "__same_session_model__"

    llm = _SameSessionLLM()
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=_Cache(),
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    result = await strategy.compact(
        _session(),
        model_context=CompactionModelContext(
            model="agent-model",
            provider_id="agent-provider",
            reasoning_effort="none",
        ),
    )

    assert result.compacted is True
    assert llm.kwargs[0]["model"] == "agent-model"
    assert llm.kwargs[0]["provider_id"] == "agent-provider"
    assert llm.kwargs[0]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_compaction_route_resolution_failure_preserves_route_behavior() -> None:
    class _FailingResolveLLM(_LLM):
        async def resolve_model(
            self, explicit_model: str | None = None, task_type: str = "default", **kwargs: object
        ) -> str:
            del explicit_model, task_type, kwargs
            raise RuntimeError("route lookup failed")

    llm = _FailingResolveLLM()
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=_Cache(),
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    result = await strategy.compact(
        _session(),
        model_context=CompactionModelContext(
            model="agent-model",
            provider_id="agent-provider",
            reasoning_effort="none",
        ),
    )

    assert result.compacted is True
    assert llm.kwargs[0]["model"] is None
    assert llm.kwargs[0]["task_type"] == "compaction"
    assert llm.kwargs[0]["provider_id"] is None
    assert llm.kwargs[0]["reasoning_effort"] is None
