from __future__ import annotations

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
