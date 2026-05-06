from __future__ import annotations

import pytest

from cognis.providers.memory.mnemory import MnemoryProvider
from cognis.runtime_context import scoped_runtime_context


class _AuthProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str], str | None]] = []

    def sign_service_jwt(
        self,
        user_email: str,
        agent_id: str,
        audience: list[str],
        *,
        agent_owner_email: str | None = None,
    ) -> str:
        self.calls.append((user_email, agent_id, audience, agent_owner_email))
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
        self.last_json: dict[str, object] | None = None
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    async def post(self, path: str, json: dict[str, object], headers: dict[str, str]) -> _Response:
        del path, headers
        self.last_json = json
        return _Response(self.payload)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
        headers: dict[str, str],
    ) -> _Response:
        del json, params
        self.requests.append((method, path, headers))
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


def test_headers_include_agent_owner_when_context_sets_it() -> None:
    auth = _AuthProvider()
    provider = MnemoryProvider("https://mnemory.test", auth)

    with scoped_runtime_context(
        user_email="guest@example.com",
        agent_id="agent-1",
        agent_owner_email="owner@example.com",
    ):
        headers = provider._headers()

    assert headers["X-Agent-Id"] == "agent-1"
    assert headers["X-Agent-Owner"] == "owner@example.com"
    assert auth.calls[-1] == (
        "guest@example.com",
        "agent-1",
        ["mnemory"],
        "owner@example.com",
    )


@pytest.mark.asyncio
async def test_remember_accepts_explicit_agent_owner() -> None:
    auth = _AuthProvider()
    provider = MnemoryProvider("https://mnemory.test", auth)
    provider.client = _Client({"accepted": True})

    await provider.remember(
        session_id="mem-1",
        messages=[{"role": "assistant", "content": "done"}],
        user_email="guest@example.com",
        agent_id="agent-1",
        agent_owner_email="owner@example.com",
    )

    assert auth.calls[-1] == (
        "guest@example.com",
        "agent-1",
        ["mnemory"],
        "owner@example.com",
    )


@pytest.mark.asyncio
async def test_recall_truncates_oversized_query_payload() -> None:
    provider = MnemoryProvider("https://mnemory.test", _AuthProvider())
    client = _Client(
        {
            "session_id": "mem-existing",
            "search_results": [],
            "stats": {},
        }
    )
    provider.client = client
    long_query = "a" * 20_000

    with scoped_runtime_context(user_email="user@example.com", agent_id="agent-1"):
        await provider.recall(query=long_query, session_id="mem-existing", managed=True)

    assert client.last_json is not None
    bounded_query = client.last_json["query"]
    assert isinstance(bounded_query, str)
    assert len(bounded_query) < len(long_query)
    # Must stay within Mnemory's schema limit of 10_000 chars
    assert len(bounded_query) <= 10_000
    assert "middle truncated" in bounded_query
    assert client.last_json["messages"] == [{"role": "user", "content": bounded_query}]


@pytest.mark.asyncio
async def test_recall_truncates_oversized_context_payload() -> None:
    """context field is also capped to Mnemory's 10_000-char schema limit."""
    provider = MnemoryProvider("https://mnemory.test", _AuthProvider())
    client = _Client(
        {
            "session_id": "mem-existing",
            "search_results": [],
            "stats": {},
        }
    )
    provider.client = client
    long_context = "b" * 20_000

    with scoped_runtime_context(user_email="user@example.com", agent_id="agent-1"):
        await provider.recall(
            query="hello",
            session_id="mem-existing",
            managed=True,
            context=long_context,
        )

    assert client.last_json is not None
    sent_context = client.last_json["context"]
    assert isinstance(sent_context, str)
    assert len(sent_context) < len(long_context)
    assert len(sent_context) <= 10_000


@pytest.mark.asyncio
async def test_delete_memory_tool_calls_mnemory_delete_endpoint() -> None:
    auth = _AuthProvider()
    provider = MnemoryProvider("https://mnemory.test", auth)
    client = _Client({})
    provider.client = client

    await provider.delete_memory_tool(
        "mem_123",
        agent_id="agent-1",
        user_email="user@example.com",
    )

    assert client.requests == [
        (
            "DELETE",
            "/api/memories/mem_123",
            {
                "Authorization": "Bearer token",
                "X-Agent-Id": "agent-1",
                "X-Agent-Owner": "user@example.com",
            },
        )
    ]
    assert auth.calls == [("user@example.com", "agent-1", ["mnemory"], "user@example.com")]
