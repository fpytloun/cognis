from __future__ import annotations

import uuid

import pytest

from cognis.core.compaction import (
    LONG_LIVED_CHAT_COMPACTION_ADDENDUM,
    CompactionModelContext,
    CompactionStrategy,
    _format_events_for_compaction,
    _mechanical_summary,
)
from cognis.core.compaction.banding import build_compaction_input
from cognis.core.compaction.fallback import build_sliding_window_summary
from cognis.core.compaction.strategy import _split_events
from cognis.core.session_cache import CachedEvent, CachedSessionState, SessionCache
from cognis.models.session import EventAppendResult, SessionModel


class _Cache:
    get_context_snapshot = SessionCache.get_context_snapshot
    _context_snapshot = staticmethod(SessionCache._context_snapshot)

    async def acquire_context_snapshot(self, session: SessionModel):
        return self.get_context_snapshot(session.session_id)

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
async def test_compaction_keeps_snapshot_when_cache_invalidates_during_model_resolution() -> None:
    cache = _Cache()

    class InvalidatingLLM(_LLM):
        async def resolve_model(self, *args: object, **kwargs: object) -> str:
            cache.entry.events.clear()
            cache.entry.canonical_stale = True
            return "test-model"

    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=InvalidatingLLM(),
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )
    result = await strategy.compact(_session())
    assert result.compacted is True
    assert [event.seq for event in result.preserved_tail_events] == [3, 4, 5]


@pytest.mark.asyncio
async def test_compaction_does_not_apply_unacknowledged_summary() -> None:
    from cognis.core.canonical_history import CanonicalHistoryUnavailable

    cache = _Cache()

    class RejectingGuardrails(_Guardrails):
        async def record_events(self, **kwargs: object) -> EventAppendResult:
            return EventAppendResult(ok=False, count=0, first_seq=0, last_seq=0)

    strategy = CompactionStrategy(
        guardrails=RejectingGuardrails(),
        llm=_LLM(),
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )
    with pytest.raises(CanonicalHistoryUnavailable):
        await strategy.compact(_session())
    assert cache.applied == []


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
    assert result.tokens_after > len("summary text")


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
    assert "## Recoverable Tool Evidence" in summary
    assert "call_id='tool-call-3'" in summary
    assert "Use read_tool_output for the full output" in summary


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


def test_compaction_excludes_program_child_events_but_keeps_parent_audit_context() -> None:
    parent_call_id = uuid.uuid4().hex
    child_call_id = uuid.uuid4().hex
    events = [
        CachedEvent(
            seq=1,
            type="tool_call",
            data={"call_id": parent_call_id, "name": "python_execute", "arguments": {}},
        ),
        CachedEvent(
            seq=2,
            type="tool_call",
            data={
                "call_id": child_call_id,
                "name": "web_search",
                "arguments": {"query": "private query"},
                "caller": {"type": "program", "program_id": parent_call_id},
            },
        ),
        CachedEvent(
            seq=3,
            type="tool_result",
            data={
                "call_id": child_call_id,
                "name": "web_search",
                "result": "nested read result should not enter compaction",
                "caller": {"type": "program", "program_id": parent_call_id},
            },
        ),
        CachedEvent(
            seq=4,
            type="tool_result",
            data={"call_id": parent_call_id, "name": "python_execute", "result": "done"},
        ),
    ]

    formatted = _format_events_for_compaction(events)

    assert "python_execute" in formatted
    assert child_call_id not in formatted
    assert "private query" not in formatted
    assert "nested read result should not enter compaction" not in formatted


def test_compaction_formats_tool_call_identifying_args_before_truncation() -> None:
    full_path = "/tmp/worktree/src/example.py"
    events = [
        CachedEvent(
            seq=1,
            type="tool_call",
            data={
                "name": "write",
                "arguments": {
                    "content": "x" * 50_000,
                    "file_path": full_path,
                },
            },
        )
    ]

    formatted = _format_events_for_compaction(events)

    assert f"write file_path='{full_path}' args=" in formatted
    assert full_path in formatted
    assert "truncated for compaction" in formatted


def test_banding_fast_path_uses_exact_token_count() -> None:
    events = [
        CachedEvent(seq=index, type="user_message", data={"content": "漢字" * 10})
        for index in range(1, 20)
    ]
    calls: list[str] = []

    def count_tokens(text: str, model: str) -> int:
        del model
        calls.append(text)
        return len(text)

    compaction_input = build_compaction_input(
        events,
        max_input_tokens=80,
        count_tokens_fn=count_tokens,
        model="token-dense-model",
    )

    assert calls
    assert compaction_input.dropped_event_count > 0
    assert "compaction band: omitted" in compaction_input.text


def test_banding_drops_verbatim_middle_user_messages_when_exact_budget_requires_it() -> None:
    events = [
        CachedEvent(seq=index, type="user_message", data={"content": f"request {index} " * 12})
        for index in range(1, 45)
    ]

    def count_tokens(text: str, model: str) -> int:
        del model
        return len(text)

    compaction_input = build_compaction_input(
        events,
        max_input_tokens=700,
        count_tokens_fn=count_tokens,
        model="char-token-model",
    )

    assert count_tokens(compaction_input.text, "char-token-model") <= 700
    assert compaction_input.dropped_event_count > 0
    assert "compaction band: omitted" in compaction_input.text
    assert "Dropped-band user messages (verbatim)" not in compaction_input.text


def test_split_events_preserves_tail_with_token_budget_and_cycle_boundary() -> None:
    events = [
        CachedEvent(seq=1, type="user_message", data={"content": "first"}),
        CachedEvent(seq=2, type="assistant_message", data={"content": "first reply"}),
        CachedEvent(seq=3, type="tool_call", data={"name": "read", "arguments": {"path": "a"}}),
        CachedEvent(
            seq=4, type="tool_result", data={"name": "read", "call_id": "c1", "result": "a"}
        ),
        CachedEvent(seq=5, type="user_message", data={"content": "second"}),
        CachedEvent(seq=6, type="assistant_message", data={"content": "second reply"}),
        CachedEvent(
            seq=7, type="tool_result", data={"name": "read", "call_id": "orphan", "result": "b"}
        ),
        CachedEvent(seq=8, type="user_message", data={"content": "third"}),
    ]

    older, preserved = _split_events(
        events,
        preserve_turns=10,
        tail_token_budget=20,
        count_tokens_fn=lambda text, _model: len(text),
        model="test-model",
    )

    assert older
    assert preserved[0].type in {
        "user_message",
        "assistant_message",
        "assistant_thinking",
        "tool_call",
    }
    assert preserved[0].type != "tool_result"


def test_split_events_compacts_tool_heavy_single_turn_at_tool_cycle_boundary() -> None:
    events = [CachedEvent(seq=1, type="user_message", data={"content": "debug this"})]
    for index in range(120):
        call_id = f"call-{index}"
        events.extend(
            [
                CachedEvent(
                    seq=len(events) + 1,
                    type="tool_call",
                    data={"call_id": call_id, "name": "read", "arguments": {}},
                ),
                CachedEvent(
                    seq=len(events) + 1,
                    type="tool_result",
                    data={"call_id": call_id, "name": "read", "result": "x" * 100},
                ),
            ]
        )

    older, preserved = _split_events(events, preserve_turns=10)

    assert older
    assert preserved
    # The active instruction stays verbatim ahead of the preserved cycles.
    assert preserved[0] is events[0]
    assert events[0] not in older
    assert preserved[1].type == "tool_call"
    assert older[-1].type == "tool_result"
    assert len(older) + len(preserved) == len(events)


def test_split_events_keeps_parallel_tool_call_batch_together() -> None:
    events = [CachedEvent(seq=1, type="user_message", data={"content": "inspect"})]
    for batch in range(60):
        first_call = f"call-{batch}-a"
        second_call = f"call-{batch}-b"
        events.extend(
            [
                CachedEvent(
                    seq=len(events) + 1,
                    type="tool_call",
                    data={"call_id": first_call, "name": "read", "arguments": {}},
                ),
                CachedEvent(
                    seq=len(events) + 1,
                    type="tool_call",
                    data={"call_id": second_call, "name": "read", "arguments": {}},
                ),
                CachedEvent(
                    seq=len(events) + 1,
                    type="tool_result",
                    data={"call_id": first_call, "name": "read", "result": "a"},
                ),
                CachedEvent(
                    seq=len(events) + 1,
                    type="tool_result",
                    data={"call_id": second_call, "name": "read", "result": "b"},
                ),
            ]
        )

    older, preserved = _split_events(events, preserve_turns=10)

    assert older
    assert preserved[0] is events[0]
    assert preserved[1].type == "tool_call"
    assert preserved[2].type == "tool_call"
    preserved_call_ids = {
        event.data.get("call_id") for event in preserved if event.type == "tool_call"
    }
    assert all(
        event.data.get("call_id") in preserved_call_ids
        for event in preserved
        if event.type == "tool_result"
    )


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

    assert "## Recoverable Tool Evidence" in summary
    assert "call_id='tool-call-7'" in summary
    assert "call_id='tool-call-11'" in summary
    assert "call_id='tool-call-0'" not in summary
    assert "Use read_tool_output for the full output" in summary


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
async def test_same_session_compaction_requires_resolved_model_context() -> None:
    class _SameSessionLLM(_LLM):
        async def resolve_model(
            self, explicit_model: str | None = None, task_type: str = "default", **kwargs: object
        ) -> str:
            del explicit_model, task_type, kwargs
            return "__same_session_model__"

    cache = _Cache()
    llm = _SameSessionLLM()
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
        fallback_enabled=True,
    )

    with pytest.raises(RuntimeError, match="__same_session_model__"):
        await strategy.compact(_session(), model_context=CompactionModelContext())

    assert llm.kwargs == []
    assert cache.applied == []


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


def test_mechanical_fallback_keeps_previous_summary_original_request_and_deliverable_args() -> None:
    events = [
        CachedEvent(seq=1, type="user_message", data={"content": "Build the report"}),
        CachedEvent(seq=2, type="user_message", data={"content": "Use the compact format"}),
        CachedEvent(
            seq=3,
            type="tool_call",
            data={
                "name": "write_deliverable",
                "arguments": {"content": "# Final deliverable\nThis is the real artifact."},
            },
        ),
        CachedEvent(
            seq=4,
            type="tool_result",
            data={"name": "write_deliverable", "result": "receipt-only"},
        ),
    ]

    summary = build_sliding_window_summary(
        events,
        previous_summary="## Anchors\n- Keep this previous summary.",
    )

    assert "## Previous anchored summary (verbatim):" in summary
    assert "Keep this previous summary." in summary
    assert "## Original request (verbatim):" in summary
    assert "Build the report" in summary
    assert "# Final deliverable" in summary
    assert "receipt-only" not in summary


@pytest.mark.asyncio
async def test_compaction_max_input_uses_compaction_route_provider_only_for_same_session() -> None:
    class _ModelInfo:
        max_input_tokens = 2_000

    class _RoutedLLM(_LLM):
        def __init__(self) -> None:
            super().__init__()
            self.model_info_provider_ids: list[str | None] = []

        async def resolve_model(
            self, explicit_model: str | None = None, task_type: str = "default", **kwargs: object
        ) -> str:
            del explicit_model, task_type, kwargs
            return "compaction-model"

        async def get_model_info(
            self,
            model: str,
            provider_id: str | None = None,
            **kwargs: object,
        ) -> _ModelInfo:
            del model, kwargs
            self.model_info_provider_ids.append(provider_id)
            return _ModelInfo()

    llm = _RoutedLLM()
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
        ),
    )

    assert result.compacted is True
    assert llm.model_info_provider_ids == [None]


def _long_single_turn_events(
    *, cycles: int = 60, result_chars: int = 6_000, with_retry_notice: bool = True
) -> list[CachedEvent]:
    """One user turn extended by many tool cycles, as system continuations do."""
    events = [
        CachedEvent(
            seq=1,
            type="developer_message",
            data={"content": "context seed", "context_injection": True},
        ),
        CachedEvent(
            seq=2,
            type="user_message",
            data={"content": "Work it out fully until QA ready", "turn_id": "turn-1"},
        ),
    ]
    if with_retry_notice:
        events.append(
            CachedEvent(
                seq=3,
                type="system_message",
                data={
                    "event": "system_notice",
                    "kind": "model_recovery",
                    "scope": "turn",
                    "retry_source_turn_id": "turn-1",
                    "turn_id": "turn-2",
                },
            )
        )
    for index in range(cycles):
        call_id = f"call-{index}"
        events.extend(
            [
                CachedEvent(
                    seq=len(events) + 1,
                    type="assistant_message",
                    data={"content": f"step {index}", "turn_id": "turn-2"},
                ),
                CachedEvent(
                    seq=len(events) + 2,
                    type="tool_call",
                    data={"call_id": call_id, "name": "bash", "arguments": {"command": "x"}},
                ),
                CachedEvent(
                    seq=len(events) + 3,
                    type="tool_result",
                    data={"call_id": call_id, "name": "bash", "result": "r" * result_chars},
                ),
            ]
        )
    return events


def _char_tokens(text: str, _model: str) -> int:
    return len(text)


def test_split_events_cuts_inside_oversized_turn_and_pins_admission() -> None:
    from cognis.core.compaction.strategy import _event_prompt_text

    events = _long_single_turn_events()
    budget = 40_000

    older, preserved = _split_events(
        events,
        preserve_turns=10,
        tail_token_budget=budget,
        count_tokens_fn=_char_tokens,
        model="test-model",
    )

    # Admission anchors stay verbatim, in order, ahead of the contiguous tail.
    assert [event.seq for event in preserved[:2]] == [2, 3]
    assert all(event.seq not in {2, 3} for event in older)
    suffix = preserved[2:]
    assert suffix[0].type in {"assistant_message", "tool_call"}
    assert [event.seq for event in suffix] == list(range(suffix[0].seq, events[-1].seq + 1))
    assert len(older) + len(preserved) == len(events)
    # The tail is sized on raw replay cost, not the truncated compaction text.
    assert sum(len(_event_prompt_text(event)) for event in suffix) <= budget
    assert any(event.type == "tool_result" for event in older)


def test_split_events_keeps_whole_latest_turn_when_it_fits() -> None:
    events = _long_single_turn_events(cycles=3, result_chars=10)

    older, preserved = _split_events(
        events,
        preserve_turns=10,
        tail_token_budget=100_000,
        count_tokens_fn=_char_tokens,
        model="test-model",
    )

    assert [event.seq for event in older] == [1]
    assert preserved == events[1:]


def test_split_events_keeps_last_cycle_when_it_alone_exceeds_budget() -> None:
    events = _long_single_turn_events(cycles=4, result_chars=50_000, with_retry_notice=False)

    older, preserved = _split_events(
        events,
        preserve_turns=10,
        tail_token_budget=1_000,
        count_tokens_fn=_char_tokens,
        model="test-model",
    )

    assert preserved[0].seq == 2
    # The tool exchange stays complete even though it alone exceeds the budget.
    assert [event.type for event in preserved[1:]] == ["tool_call", "tool_result"]
    assert preserved[-1] is events[-1]
    assert older


def test_split_events_does_not_snap_over_unbudgeted_parallel_batch_call() -> None:
    """A later call in a parallel batch is not a boundary on its own."""
    events = _long_single_turn_events(cycles=10, result_chars=10, with_retry_notice=False)
    seq = events[-1].seq
    events.extend(
        [
            CachedEvent(
                seq=seq + 1,
                type="tool_call",
                data={"call_id": "big", "name": "write", "arguments": {"content": "a" * 50_000}},
            ),
            CachedEvent(seq=seq + 2, type="tool_call", data={"call_id": "small", "name": "read"}),
            CachedEvent(seq=seq + 3, type="tool_result", data={"call_id": "big", "result": "ok"}),
            CachedEvent(seq=seq + 4, type="tool_result", data={"call_id": "small", "result": "ok"}),
            CachedEvent(seq=seq + 5, type="assistant_message", data={"content": "next"}),
            CachedEvent(seq=seq + 6, type="tool_call", data={"call_id": "last", "name": "read"}),
            CachedEvent(seq=seq + 7, type="tool_result", data={"call_id": "last", "result": "ok"}),
        ]
    )

    older, preserved = _split_events(
        events,
        preserve_turns=10,
        tail_token_budget=5_000,
        count_tokens_fn=_char_tokens,
        model="test-model",
    )

    # The oversized batch is summarized; compaction is not a no-op.
    assert any(event.data.get("call_id") == "big" for event in older)
    assert any(event.data.get("call_id") == "small" for event in older)
    assert preserved[0].seq == 2
    assert [event.seq for event in preserved[1:]] == [seq + 5, seq + 6, seq + 7]


def test_pinned_admission_satisfies_continuation_validation_after_rotation() -> None:
    from cognis.core.canonical_history import CanonicalHistoryUnavailable
    from cognis.core.session_cache import CanonicalContextSnapshot

    events = _long_single_turn_events()
    older, preserved = _split_events(
        events,
        preserve_turns=10,
        tail_token_budget=40_000,
        count_tokens_fn=_char_tokens,
        model="test-model",
    )
    assert older

    def rotated_snapshot(tail: list[CachedEvent]) -> CanonicalContextSnapshot:
        # Rotation re-appends the tail in order under fresh sequence numbers.
        reseeded = [
            CachedEvent(seq=index + 10, type=event.type, data=dict(event.data))
            for index, event in enumerate(tail)
        ]
        return CanonicalContextSnapshot(
            events=reseeded,
            prefix_entries=[],
            last_compaction_summary="summary",
            last_event_seq=reseeded[-1].seq,
            last_compaction_seq=9,
            projection_revision=1,
        )

    # The retry execution resolves its admission through the pinned notice.
    rotated_snapshot(preserved).require_turn("turn-2", user_event=True, profile_switch=False)
    rotated_snapshot(preserved).require_turn("turn-1", user_event=True, profile_switch=False)
    unpinned_tail = [event for event in preserved if event.seq > older[-1].seq]
    with pytest.raises(CanonicalHistoryUnavailable):
        rotated_snapshot(unpinned_tail).require_turn(
            "turn-2", user_event=True, profile_switch=False
        )


@pytest.mark.parametrize("split_mode", ["tokens", "turn_count"])
@pytest.mark.parametrize("with_retry", [False, True])
def test_compaction_keeps_execution_admission_before_later_user_messages(
    split_mode: str, with_retry: bool
) -> None:
    """Question answers and absorbed messages do not replace execution admission."""
    from cognis.core.session_cache import CanonicalContextSnapshot

    events = _long_single_turn_events(cycles=60, with_retry_notice=with_retry)
    execution_id = "turn-2" if with_retry else "turn-1"
    # Match production tool events: they belong to the running execution even
    # after a question answer or an absorbed user message starts a new chat turn.
    for event in events[3:]:
        event.data["turn_id"] = execution_id
    later_user = CachedEvent(
        seq=0,
        type="user_message",
        data={"source": "question_answer", "content": "Approved"},
    )
    if split_mode == "turn_count":
        later_user.data["turn_id"] = "absorbed-turn"
        events.append(later_user)
        events.append(CachedEvent(seq=0, type="assistant_message", data={"turn_id": execution_id}))
    else:
        events.insert(30, later_user)
    for seq, event in enumerate(events, 1):
        event.seq = seq

    older, preserved = _split_events(
        events,
        preserve_turns=1 if split_mode == "turn_count" else 10,
        tail_token_budget=40_000 if split_mode == "tokens" else None,
        count_tokens_fn=_char_tokens,
        model="test-model",
    )
    assert older
    assert any(event.type == "tool_result" for event in older)
    assert preserved == sorted(preserved, key=lambda event: event.seq)
    assert len({id(event) for event in [*older, *preserved]}) == len(events)
    assert len(older) + len(preserved) == len(events)
    assert later_user in preserved
    # Cold replay has only the rotated tail, not the original in-memory prompt.
    snapshot = CanonicalContextSnapshot(
        events=[
            CachedEvent(seq=index + 10, type=event.type, data=dict(event.data))
            for index, event in enumerate(preserved)
        ],
        prefix_entries=[],
        last_compaction_summary="summary",
        last_event_seq=len(preserved) + 9,
        last_compaction_seq=9,
        projection_revision=1,
    )
    snapshot.require_turn(execution_id, user_event=True, profile_switch=False)


class _CapturingGuardrails(_Guardrails):
    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[object] = []

    async def record_events(self, **kwargs: object) -> EventAppendResult:
        events = kwargs.get("events")
        if isinstance(events, list):
            self.recorded.extend(events)
        return await super().record_events(**kwargs)


@pytest.mark.asyncio
async def test_compaction_summarizes_inside_long_single_turn() -> None:
    cache = _Cache()
    cache.entry.events = _long_single_turn_events()
    guardrails = _CapturingGuardrails()
    llm = _LLM()
    strategy = CompactionStrategy(
        guardrails=guardrails,
        llm=llm,
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=10,
    )
    # 30% of this budget is the preserved-tail budget.
    strategy.max_input_tokens = 130_000

    result = await strategy.compact(_session(), trigger="tool_loop_pressure")

    assert result.compacted is True
    assert result.turns_compacted == 0
    assert result.events_compacted > 0
    assert [event.seq for event in result.preserved_tail_events[:2]] == [2, 3]
    suffix_start = result.preserved_tail_events[2].seq
    assert result.tail_start_seq == suffix_start
    marker = guardrails.recorded[-1]
    marker_data = getattr(marker, "data", {})
    assert marker_data["tail_pinned_seqs"] == [2, 3]
    assert marker_data["tail_start_seq"] == suffix_start
    assert marker_data["events_compacted"] == result.events_compacted
    assert marker_data["turns_compacted"] == 0
    # The summarizer still reads the active instruction it reports progress on.
    summary_input = str(llm.messages[0][1]["content"])
    assert "Work it out fully until QA ready" in summary_input


@pytest.mark.asyncio
async def test_compaction_is_noop_before_recording_when_only_seeds_would_be_summarized() -> None:
    cache = _Cache()
    cache.entry.events = _long_single_turn_events(cycles=3, result_chars=10)
    guardrails = _CapturingGuardrails()
    llm = _LLM()
    strategy = CompactionStrategy(
        guardrails=guardrails,
        llm=llm,
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=10,
    )
    strategy.max_input_tokens = 1_000_000

    result = await strategy.compact(_session(), trigger="tool_loop_pressure")

    assert result.compacted is False
    assert result.method == "noop"
    assert llm.messages == []
    assert guardrails.calls == 0
    assert cache.applied == []
