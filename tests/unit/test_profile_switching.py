from types import SimpleNamespace

import pytest

from cognis.core.profile_switching import persist_agent_profile_switch


class _DatabaseSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _SessionFactory:
    def __init__(self, db: _DatabaseSession) -> None:
        self.db = db

    def __call__(self):
        db = self.db

        class _Context:
            async def __aenter__(self) -> _DatabaseSession:
                return db

            async def __aexit__(self, *_args: object) -> bool:
                return False

        return _Context()


class _SessionCache:
    def __init__(self) -> None:
        self.model_overrides: list[tuple[str, None]] = []
        self.reasoning_overrides: list[tuple[str, None]] = []
        self.tool_runtime_info: list[tuple[str, None]] = []

    def set_model_override(self, session_id: str, value: None) -> None:
        self.model_overrides.append((session_id, value))

    def set_reasoning_effort_override(self, session_id: str, value: None) -> None:
        self.reasoning_overrides.append((session_id, value))

    def update_tool_runtime_info(self, session_id: str, value: None) -> None:
        self.tool_runtime_info.append((session_id, value))


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_conversation", [True, False])
async def test_persist_agent_profile_switch_respects_conversation_scope(
    monkeypatch: pytest.MonkeyPatch,
    persist_conversation: bool,
) -> None:
    stored_conversations: list[tuple[str, str]] = []
    stored_sessions: list[tuple[str, str]] = []

    async def _set_conversation(_db: object, conversation_id: str, profile_id: str) -> None:
        stored_conversations.append((conversation_id, profile_id))

    async def _set_session(_db: object, session_id: str, profile_id: str) -> None:
        stored_sessions.append((session_id, profile_id))

    monkeypatch.setattr(
        "cognis.store.queries.set_conversation_agent_profile_id",
        _set_conversation,
    )
    monkeypatch.setattr(
        "cognis.store.queries.set_session_agent_profile_id",
        _set_session,
    )
    db = _DatabaseSession()
    cache = _SessionCache()
    conversation = SimpleNamespace(conversation_id="conv-1", agent_profile_id="developer")
    session = SimpleNamespace(session_id="sess-1", agent_profile_id="developer")

    await persist_agent_profile_switch(
        session_factory=_SessionFactory(db),
        session_cache=cache,
        conversation=conversation,
        session=session,
        profile_id="senior",
        persist_conversation=persist_conversation,
    )

    assert stored_sessions == [("sess-1", "senior")]
    assert stored_conversations == ([("conv-1", "senior")] if persist_conversation else [])
    assert session.agent_profile_id == "senior"
    assert conversation.agent_profile_id == ("senior" if persist_conversation else "developer")
    assert cache.model_overrides == [("sess-1", None)]
    assert cache.reasoning_overrides == [("sess-1", None)]
    assert cache.tool_runtime_info == [("sess-1", None)]
    assert db.commits == 1
    assert db.rollbacks == 0
