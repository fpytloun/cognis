from __future__ import annotations

import pytest

from cognis.core.compaction import CompactionStrategy, _format_events_for_compaction
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
        self.idempotency_keys.append(kwargs.get("idempotency_key"))
        if self.fail:
            raise RuntimeError("write failed")
        return EventAppendResult(ok=True, count=1, first_seq=6, last_seq=6)


class _LLM:
    async def generate(
        self,
        messages: list[dict[str, object]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: object,
    ) -> dict[str, object]:
        del messages, model, task_type, kwargs
        return {"choices": [{"message": {"content": "summary text"}}]}

    async def resolve_model(
        self, explicit_model: str | None = None, task_type: str = "default"
    ) -> str:
        del explicit_model, task_type
        return "test-model"

    def count_tokens(self, text: str, model: str) -> int:
        del model
        return len(text)


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
    strategy = CompactionStrategy(
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        compaction_threshold=0.85,
        preserve_turns=2,
    )

    result = await strategy.compact(_session())

    assert result.compacted is True
    assert result.method == "llm"
    assert cache.applied == [("summary text", 6)]


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
