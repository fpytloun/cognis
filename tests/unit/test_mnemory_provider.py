from __future__ import annotations

import pytest

from cognis.providers.memory.mnemory import MnemoryProvider
from cognis.runtime_context import scoped_runtime_context


class _AuthProvider:
    def sign_service_jwt(self, user_email: str, agent_id: str, audience: list[str]) -> str:
        del user_email, agent_id, audience
        return "token"


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return dict(self._payload)


class _Client:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def post(self, path: str, json: dict[str, object], headers: dict[str, str]) -> _Response:
        del path, json, headers
        return _Response(self.payload)


@pytest.mark.asyncio
async def test_recall_flags_forged_session_ids() -> None:
    provider = MnemoryProvider("https://mnemory.test", _AuthProvider())
    provider.client = _Client(
        {
            "session_id": "mem-new",
            "instructions": "use memory tools",
            "core_memories": "prefers python",
            "stats": {},
        }
    )

    with scoped_runtime_context(user_email="user@example.com", agent_id="agent-1"):
        result = await provider.recall(
            query="hello",
            session_id="mem-old",
            managed=True,
            include_instructions=True,
        )

    assert result["session_id"] == "mem-new"
    assert result["_session_forged"] is True


@pytest.mark.asyncio
async def test_recall_keeps_matching_session_ids_unflagged() -> None:
    provider = MnemoryProvider("https://mnemory.test", _AuthProvider())
    provider.client = _Client(
        {
            "session_id": "mem-existing",
            "search_results": [],
            "stats": {},
        }
    )

    with scoped_runtime_context(user_email="user@example.com", agent_id="agent-1"):
        result = await provider.recall(query="hello", session_id="mem-existing", managed=True)

    assert result["session_id"] == "mem-existing"
    assert result["_session_forged"] is False
