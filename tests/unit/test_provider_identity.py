from __future__ import annotations

import httpx
import pytest

from cognis.models.session import SessionEvent
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.providers.memory.mnemory import MnemoryProvider
from cognis.runtime_context import current_user_email


class _AuthProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str], str | None]] = []

    def sign_service_jwt(
        self,
        subject: str,
        agent_id: str,
        audience: list[str],
        *,
        agent_owner_email: str | None = None,
    ) -> str:
        self.calls.append((subject, agent_id, audience, agent_owner_email))
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
        assert mnemory_headers["X-Agent-Owner"] == "user@example.com"
        assert intaris_headers["X-Agent-Owner"] == "user@example.com"
        assert auth.calls[0] == ("user@example.com", "agent-a", ["mnemory"], "user@example.com")
        assert auth.calls[1] == ("user@example.com", "agent-b", ["intaris"], "user@example.com")
    finally:
        current_user_email.reset(token)


def test_mnemory_headers_require_explicit_identity() -> None:
    auth = _AuthProvider()
    mnemory = MnemoryProvider("http://localhost:8050", auth)

    with pytest.raises(RuntimeError, match="requires explicit user identity"):
        mnemory._headers(agent_id="agent-a")


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
        "context": {},
    }


@pytest.mark.asyncio
async def test_intaris_call_mcp_tool_normalizes_rest_content_blocks() -> None:
    auth = _AuthProvider()
    intaris = IntarisProvider("http://localhost:8060", auth)

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "content": [
                    {"type": "text", "text": "Issue created"},
                    {"type": "text", "text": "#42"},
                ],
                "isError": False,
                "decision": "approve",
                "call_id": "call-1",
                "latency_ms": 1088,
            }

    async def _fake_post(*_: object, **__: object) -> _Response:
        return _Response()

    token = current_user_email.set("user@example.com")
    try:
        intaris.client.post = _fake_post  # type: ignore[method-assign]
        result = await intaris.call_mcp_tool(
            session_id="sess-1",
            server_name="github",
            tool_name="create_issue",
            arguments={"title": "Bug"},
        )
    finally:
        current_user_email.reset(token)
        await intaris.client.aclose()

    assert result.output == "Issue created\n#42"
    assert result.is_error is False
    assert result.duration_ms == 1088
    assert result.metadata == {
        "decision": "approve",
        "call_id": "call-1",
        "latency_ms": 1088,
    }


@pytest.mark.asyncio
async def test_intaris_report_reasoning_falls_back_for_older_intaris_nodes() -> None:
    auth = _AuthProvider()
    intaris = IntarisProvider("http://localhost:8060", auth)
    calls: list[dict[str, object]] = []

    class _Response:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                request = httpx.Request("POST", "http://localhost:8060/api/v1/reasoning")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("boom", request=request, response=response)

        def json(self) -> dict[str, object]:
            return self._payload

    async def _fake_post(
        path: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
    ) -> _Response:
        del path, headers
        calls.append(json)
        if len(calls) == 1:
            return _Response(422, {"detail": "extra fields not permitted"})
        return _Response(200, {"ok": True, "call_id": "call-1"})

    token = current_user_email.set("user@example.com")
    try:
        intaris.client.post = _fake_post  # type: ignore[method-assign]
        result = await intaris.report_reasoning(
            session_id="sess-1",
            from_events=True,
            wait_for_intention=True,
            wait_timeout_ms=1500,
        )
    finally:
        current_user_email.reset(token)
        await intaris.client.aclose()

    assert result.call_id == "call-1"
    assert calls[0]["wait_for_intention"] is True
    assert calls[0]["wait_timeout_ms"] == 1500
    assert "wait_for_intention" not in calls[1]
    assert "wait_timeout_ms" not in calls[1]


@pytest.mark.asyncio
async def test_intaris_report_reasoning_omits_wait_timeout_without_bootstrap_wait() -> None:
    auth = _AuthProvider()
    intaris = IntarisProvider("http://localhost:8060", auth)
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"ok": True, "call_id": "call-2"}

    async def _fake_post(
        path: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
    ) -> _Response:
        del path, headers
        captured.update(json)
        return _Response()

    token = current_user_email.set("user@example.com")
    try:
        intaris.client.post = _fake_post  # type: ignore[method-assign]
        result = await intaris.report_reasoning(
            session_id="sess-2",
            from_events=True,
            wait_for_intention=False,
            wait_timeout_ms=1500,
        )
    finally:
        current_user_email.reset(token)
        await intaris.client.aclose()

    assert result.call_id == "call-2"
    assert captured["from_events"] is True
    assert "wait_for_intention" not in captured
    assert "wait_timeout_ms" not in captured


@pytest.mark.asyncio
async def test_intaris_record_events_retries_missing_session_when_requested() -> None:
    auth = _AuthProvider()
    intaris = IntarisProvider("http://localhost:8060", auth)
    calls = 0

    class _Response:
        def __init__(self, status_code: int, payload: dict[str, object] | None = None) -> None:
            self.status_code = status_code
            self._payload = payload or {}
            self.request = httpx.Request(
                "POST", "http://localhost:8060/api/v1/session/sess-1/events"
            )

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "boom",
                    request=self.request,
                    response=httpx.Response(self.status_code, request=self.request),
                )

        def json(self) -> dict[str, object]:
            return self._payload

    async def _fake_post(*_: object, **__: object) -> _Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(404)
        return _Response(200, {"ok": True, "count": 1, "first_seq": 1, "last_seq": 1})

    async def _no_sleep(_: float) -> None:
        return None

    token = current_user_email.set("user@example.com")
    try:
        intaris.client.post = _fake_post  # type: ignore[method-assign]
        import cognis.providers.guardrails.intaris as intaris_module

        original_sleep = intaris_module.asyncio.sleep
        intaris_module.asyncio.sleep = _no_sleep
        try:
            result = await intaris.record_events(
                session_id="sess-1",
                events=[SessionEvent(type="user_message", data={"content": "hello"})],
                retry_missing_session=True,
            )
        finally:
            intaris_module.asyncio.sleep = original_sleep
    finally:
        current_user_email.reset(token)
        await intaris.client.aclose()

    assert calls == 2
    assert result.ok is True
    assert result.last_seq == 1
