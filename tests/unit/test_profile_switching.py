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

    async def flush(self) -> None:
        return None


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

    def set_model_override(
        self, session_id: str, value: None, provider_id: str | None = None
    ) -> None:
        del provider_id
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
    db = _DatabaseSession()
    cache = _SessionCache()
    conversation = SimpleNamespace(
        conversation_id="conv-1",
        active_session_id="sess-1",
        agent_profile_id="developer",
        updated_at=None,
    )
    session = SimpleNamespace(
        session_id="sess-1",
        conversation_id="conv-1",
        agent_profile_id="developer",
        model_override="old-model",
        model_override_provider_id="old-provider",
        reasoning_effort_override="high",
        fast_mode_override=True,
        runtime_override_revision=3,
        updated_at=None,
    )

    async def _get_conversation_for_update(_db: object, conversation_id: str) -> object:
        assert conversation_id == conversation.conversation_id
        return conversation

    async def _get_session_for_update(_db: object, session_id: str) -> object:
        assert session_id == session.session_id
        return session

    monkeypatch.setattr(
        "cognis.store.queries.get_conversation_for_update",
        _get_conversation_for_update,
    )
    monkeypatch.setattr(
        "cognis.store.queries.get_session_for_update",
        _get_session_for_update,
    )

    await persist_agent_profile_switch(
        session_factory=_SessionFactory(db),
        session_cache=cache,
        conversation=conversation,
        session=session,
        profile_id="senior",
        persist_conversation=persist_conversation,
    )

    assert session.agent_profile_id == "senior"
    assert conversation.agent_profile_id == ("senior" if persist_conversation else "developer")
    assert session.model_override is None
    assert session.model_override_provider_id is None
    assert session.reasoning_effort_override is None
    assert session.fast_mode_override is None
    assert session.runtime_override_revision == 4
    assert cache.model_overrides == [("sess-1", None)]
    assert cache.reasoning_overrides == [("sess-1", None)]
    assert cache.tool_runtime_info == [("sess-1", None)]
    assert db.commits == 1
    assert db.rollbacks == 0
