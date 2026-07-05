from __future__ import annotations

import json

import httpx
import pytest

from cognis.providers.llm.codex import CodexAuth
from cognis.providers.llm.codex_transport import (
    DEFAULT_CODEX_HTTP_TIMEOUT,
    DEFAULT_CODEX_STREAM_HTTP_TIMEOUT,
    DirectCodexResponsesStream,
    _codex_headers,
    _codex_payload,
    _raise_for_status,
)
from cognis.providers.llm.errors import classify_llm_exception, reasoning_summary_rejected
from cognis.providers.llm.retry import with_llm_retry


def test_codex_headers_merge_auth_extra_headers_and_stream_accept() -> None:
    auth = CodexAuth(access_token="token", account_id="account")

    headers = _codex_headers(
        auth,
        {
            "stream": True,
            "extra_headers": {"x-session-affinity": "session-123", "session_id": "session-123"},
        },
    )

    assert headers["Authorization"] == "Bearer token"
    assert headers["ChatGPT-Account-ID"] == "account"
    assert headers["Accept"] == "text/event-stream"
    assert headers["x-session-affinity"] == "session-123"
    assert headers["session_id"] == "session-123"


def test_codex_payload_strips_transport_kwargs() -> None:
    payload, stream = _codex_payload(
        {
            "model": "gpt-5.5",
            "input": [{"role": "user", "content": "hi"}],
            "stream": True,
            "timeout": 30,
            "api_version": "unused",
            "custom_llm_provider": "chatgpt",
            "extra_headers": {"session_id": "session-123"},
            "reasoning": {"effort": "high"},
        }
    )

    assert stream is True
    assert payload == {
        "model": "gpt-5.5",
        "input": [{"role": "user", "content": "hi"}],
        "stream": True,
        "store": False,
        "reasoning": {"effort": "high"},
    }


def test_codex_payload_forces_store_false() -> None:
    payload, _stream = _codex_payload(
        {
            "model": "gpt-5.5",
            "input": [{"role": "user", "content": "hi"}],
            "stream": False,
            "store": True,
        }
    )

    assert payload["store"] is False


def test_default_codex_timeout_does_not_use_httpx_five_second_read_timeout() -> None:
    assert DEFAULT_CODEX_HTTP_TIMEOUT.connect == 30.0
    assert DEFAULT_CODEX_HTTP_TIMEOUT.write == 30.0
    assert DEFAULT_CODEX_HTTP_TIMEOUT.pool == 30.0
    assert DEFAULT_CODEX_HTTP_TIMEOUT.read == 90.0
    assert DEFAULT_CODEX_STREAM_HTTP_TIMEOUT.read is None


def test_direct_codex_bad_request_preserves_error_body_for_fallbacks() -> None:
    response = httpx.Response(
        400,
        json={
            "error": {
                "message": "Unsupported parameter: reasoning.summary",
                "type": "invalid_request_error",
                "param": "reasoning.summary",
            }
        },
        request=httpx.Request("POST", "https://example.invalid"),
    )

    with pytest.raises(Exception) as exc_info:
        _raise_for_status(response)

    assert type(exc_info.value).__name__ == "BadRequestError"
    assert exc_info.value.status_code == 400
    assert exc_info.value.body["error"]["param"] == "reasoning.summary"
    assert reasoning_summary_rejected(classify_llm_exception(exc_info.value)) is True


def test_direct_codex_bad_request_includes_top_level_detail() -> None:
    response = httpx.Response(
        400,
        json={"detail": "Instructions are required"},
        request=httpx.Request("POST", "https://example.invalid"),
    )

    with pytest.raises(Exception) as exc_info:
        _raise_for_status(response)

    assert type(exc_info.value).__name__ == "BadRequestError"
    assert str(exc_info.value) == (
        "Direct Codex request failed: HTTP 400; Instructions are required"
    )


@pytest.mark.asyncio
async def test_direct_codex_5xx_errors_remain_retryable() -> None:
    attempts = 0

    async def _raise_server_error() -> None:
        nonlocal attempts
        attempts += 1
        response = httpx.Response(
            500,
            json={"error": {"message": "The server had an error", "code": "server_error"}},
            request=httpx.Request("POST", "https://example.invalid"),
        )
        _raise_for_status(response)

    with pytest.raises(Exception) as exc_info:
        await with_llm_retry(
            _raise_server_error,
            max_retries=1,
            base_delay=0,
            max_delay=0,
            jitter=False,
            operation="direct_codex_test",
        )

    assert attempts == 2
    assert exc_info.value.status_code == 500
    assert "HTTP 500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_direct_codex_stream_parses_sse_and_closes_resources() -> None:
    closed: list[str] = []

    class _Client:
        async def aclose(self) -> None:
            closed.append("client")

    class _Response:
        async def aiter_lines(self):  # type: ignore[no-untyped-def]
            yield "event: response.output_text.delta"
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': 'hi'})}"
            yield "data: [DONE]"

        async def aclose(self) -> None:
            closed.append("response")

    stream = DirectCodexResponsesStream(
        _Client(),  # type: ignore[arg-type]
        _Response(),  # type: ignore[arg-type]
    )

    events = [event async for event in stream]

    assert events == [{"type": "response.output_text.delta", "delta": "hi"}]
    assert closed == ["response", "client"]


@pytest.mark.asyncio
async def test_direct_codex_stream_closes_real_httpx_response() -> None:
    closed: list[str] = []

    class _Client:
        async def aclose(self) -> None:
            closed.append("client")

    response = httpx.Response(
        200,
        content=b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
        request=httpx.Request("POST", "https://example.invalid"),
    )
    stream = DirectCodexResponsesStream(_Client(), response)  # type: ignore[arg-type]

    events = [event async for event in stream]

    assert events == [{"type": "response.completed", "response": {"status": "completed"}}]
    assert response.is_closed
    assert closed == ["client"]
