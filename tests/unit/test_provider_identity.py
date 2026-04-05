from __future__ import annotations

import pytest

from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.providers.memory.mnemory import MnemoryProvider
from cognis.runtime_context import current_user_email


class _AuthProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []

    def sign_service_jwt(self, subject: str, agent_id: str, audience: list[str]) -> str:
        self.calls.append((subject, agent_id, audience))
        return "token"


def test_provider_headers_use_request_user_context() -> None:
    auth = _AuthProvider()
    token = current_user_email.set("user@example.com")
    try:
        mnemory = MnemoryProvider("http://localhost:8050", auth)
        intaris = IntarisProvider("http://localhost:8060", auth)

        mnemory_headers = mnemory._headers(agent_id="agent-a")
        intaris_headers = intaris._headers(agent_id="agent-b")

        assert mnemory_headers["Authorization"] == "Bearer token"
        assert intaris_headers["Authorization"] == "Bearer token"
        assert auth.calls[0] == ("user@example.com", "agent-a", ["mnemory"])
        assert auth.calls[1] == ("user@example.com", "agent-b", ["intaris"])
    finally:
        current_user_email.reset(token)


@pytest.mark.asyncio
async def test_intaris_call_mcp_tool_uses_server_and_tool_fields() -> None:
    auth = _AuthProvider()
    intaris = IntarisProvider("http://localhost:8060", auth)
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"output": "ok", "is_error": False}

    async def _fake_post(
        path: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: float,
    ) -> _Response:
        captured["path"] = path
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Response()

    token = current_user_email.set("user@example.com")
    try:
        intaris.client.post = _fake_post  # type: ignore[method-assign]
        result = await intaris.call_mcp_tool(
            session_id="sess-1",
            server_name="github",
            tool_name="search/issues",
            arguments={"q": "bug"},
        )
    finally:
        current_user_email.reset(token)
        await intaris.client.aclose()

    assert result.output == "ok"
    assert captured["path"] == "/api/v1/mcp/call"
    assert captured["json"] == {
        "session_id": "sess-1",
        "server": "github",
        "tool": "search/issues",
        "arguments": {"q": "bug"},
    }
