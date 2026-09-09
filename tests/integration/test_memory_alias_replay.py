from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.context import ContextAssembler
from cognis.core.session_cache import SessionCache
from cognis.core.tool_result_settlement import append_tool_result_once
from cognis.core.tool_router import ToolRouter
from cognis.models.session import EventAppendResult, EventReadResult, SessionEvent
from cognis.models.tool import ToolCall
from cognis.tools.builtin.memory import MEMORY_UPDATE_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.unit.test_context_assembler import (
    _LLM,
    _agent,
    _conversation,
    _Guardrails,
    _Memory,
    _session,
    _SessionCache,
    _SessionManager,
)

MEMORY_ID = "01234567-89ab-4def-8123-456789abcdef"


class _DurableIntaris:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.results: dict[str, EventAppendResult] = {}

    async def record_events(
        self,
        *,
        events: list[SessionEvent],
        idempotency_key: str,
        **_: object,
    ) -> EventAppendResult:
        existing = self.results.get(idempotency_key)
        if existing is not None:
            return existing
        first_seq = len(self.events) + 1
        for event in events:
            self.events.append(
                {"seq": len(self.events) + 1, "type": event.type, "data": event.data}
            )
        result = EventAppendResult(
            ok=True,
            count=len(events),
            first_seq=first_seq,
            last_seq=len(self.events),
        )
        self.results[idempotency_key] = result
        return result

    async def read_events(self, **_: object) -> EventReadResult:
        return EventReadResult(
            events=list(self.events),
            last_seq=len(self.events),
            has_more=False,
        )


class _MemoryWithRecord(_Memory):
    async def recall(self, **kwargs: object) -> dict[str, object]:
        result = await super().recall(**kwargs)
        result["search_results"] = [
            {
                "id": MEMORY_ID,
                "memory": "Uses pytest",
                "score": 0.9,
                "metadata": {"revision_id": "revision-1"},
            }
        ]
        return result


@pytest.mark.asyncio
async def test_context_tool_settlement_and_fresh_cache_replay_preserve_aliases() -> None:
    context_cache = _SessionCache()
    assembled = await ContextAssembler(
        memory=_MemoryWithRecord(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=context_cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    ).assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="remembered context?",
        tool_definitions=[],
    )
    audit = next(item for item in assembled.audit_messages if item["source"] == "memory_search")
    durable = _DurableIntaris()
    recall_event = SessionEvent(
        type="developer_message",
        data={
            "role": audit["role"],
            "source": audit["source"],
            "content": audit["content"],
            **audit["metadata"],
        },
    )
    await durable.record_events(
        events=[recall_event],
        idempotency_key="session-1:memory-recall",
    )

    replay_cache = SessionCache(durable)
    replay_entry = await replay_cache.refresh(_session())
    assert replay_entry.memory_aliases.resolve("m1") == MEMORY_ID

    memory = AsyncMock()
    memory.get_memories_by_ids_tool.side_effect = [
        {
            "results": [
                {
                    "id": MEMORY_ID,
                    "memory": "Uses pytest",
                    "metadata": {"revision_id": "revision-1"},
                }
            ]
        },
        {
            "results": [
                {
                    "id": MEMORY_ID,
                    "memory": "Uses pytest daily",
                    "metadata": {"revision_id": "revision-2"},
                }
            ]
        },
    ]
    memory.update_memory_tool.return_value = {"memory_id": MEMORY_ID}
    router = ToolRouter(
        guardrails=SimpleNamespace(),
        memory=memory,
        session_cache=replay_cache,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=MEMORY_UPDATE_TOOL))
    tool_call = ToolCall(
        call_id="memory-update-1",
        name="memory_update",
        arguments={"memory_id": "m1", "content": "Uses pytest daily"},
        runtime_metadata={"memory_policy_enabled": True},
    )
    result = await router.execute(
        tool_call,
        _session(),
        _agent(),
        registry,
        executor=SimpleNamespace(),
    )
    assert not result.is_error
    assert memory.update_memory_tool.await_args.kwargs["expected_revision"] == "revision-1"
    assert result.metadata is not None
    alias_delta = result.metadata["memory_aliases"]
    result_event = SessionEvent(
        type="tool_result",
        data={
            "call_id": tool_call.call_id,
            "name": tool_call.name,
            "turn_id": "turn-1",
            "result": result.output,
            "is_error": False,
            "memory_aliases": alias_delta,
        },
    )
    first = await append_tool_result_once(
        durable,
        session_id=_session().session_id,
        turn_id="turn-1",
        call_id=tool_call.call_id,
        tool_name=tool_call.name,
        event=result_event,
        known_absent=True,
        verify_after_append=False,
    )
    retried = await append_tool_result_once(
        durable,
        session_id=_session().session_id,
        turn_id="turn-1",
        call_id=tool_call.call_id,
        tool_name=tool_call.name,
        event=result_event,
        known_absent=True,
        verify_after_append=False,
    )
    assert first.append_result == retried.append_result

    fresh_cache = SessionCache(durable)
    fresh_entry = await fresh_cache.refresh(_session())
    assert fresh_entry.memory_aliases.resolve("m2") == MEMORY_ID
    assert fresh_entry.memory_aliases.snapshot()["bindings"][1]["alias"] == "m2"
