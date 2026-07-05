from __future__ import annotations

import json

import httpx
import pytest

from cognis.providers.llm import anthropic_subscription as subject
from cognis.providers.llm.anthropic_subscription import (
    AnthropicSubscriptionAuth,
    AnthropicSubscriptionTransport,
    parse_callback_input,
)


def test_parse_callback_input_accepts_url_and_code_state() -> None:
    assert parse_callback_input(
        "https://platform.claude.com/oauth/code/callback?code=abc&state=def"
    ) == ("abc", "def")
    assert parse_callback_input("abc#def") == ("abc", "def")
    assert parse_callback_input("code=abc&state=def") == ("abc", "def")
    assert parse_callback_input("not enough") is None


@pytest.mark.asyncio
async def test_direct_transport_rewrites_auth_headers_and_tool_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "msg_123",
                "model": "claude-sonnet-4-5",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "mcp_Bash",
                        "input": {"command": "pwd"},
                    }
                ],
            },
        )

    original_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(subject.httpx, "AsyncClient", client_factory)

    transport = AnthropicSubscriptionTransport(AnthropicSubscriptionAuth("access-token"))
    result = await transport.completion(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "run pwd"}],
        stream=False,
        max_tokens=32,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run shell",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        extra_headers={"anthropic-beta": "tool-search-tool-2025-10-19", "x-api-key": "nope"},
    )

    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer access-token"
    assert "x-api-key" not in headers
    assert "oauth-2025-04-20" in headers["anthropic-beta"]
    assert "tool-search-tool-2025-10-19" in headers["anthropic-beta"]

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["tools"][0]["name"] == "mcp_Bash"
    assert payload["stream"] is False
    assert payload["system"][0]["text"].startswith("x-anthropic-billing-header:")
    assert payload["system"][1]["text"] == subject.CLAUDE_CODE_IDENTITY
    assert payload["system"][2]["text"] == subject.CLAUDE_CODE_IDENTITY_BRIDGE

    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"


@pytest.mark.asyncio
async def test_direct_transport_preserves_cache_control_images_and_thinking_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "msg_123",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 5,
                },
                "content": [{"type": "text", "text": "done"}],
            },
        )

    original_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(subject.httpx, "AsyncClient", client_factory)

    transport = AnthropicSubscriptionTransport(AnthropicSubscriptionAuth("access-token"))
    result = await transport.completion(
        model="claude-sonnet-4-5",
        messages=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "stable prefix",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,aGVsbG8=",
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "_anthropic_thinking_blocks": [
                    {
                        "type": "thinking",
                        "thinking": "Need a tool.",
                        "signature": "sig-1",
                    },
                    {"type": "redacted_thinking", "data": "opaque"},
                ],
                "tool_calls": [
                    {
                        "id": "toolu_123",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
                    }
                ],
            },
        ],
        stream=False,
        max_tokens=32,
    )

    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert "extended-cache-ttl-2025-04-11" in headers["anthropic-beta"]

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert payload["messages"][0]["content"] == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "aGVsbG8=",
            },
        }
    ]
    assistant_blocks = payload["messages"][1]["content"]
    assert [block["type"] for block in assistant_blocks] == [
        "thinking",
        "redacted_thinking",
        "tool_use",
    ]
    assert assistant_blocks[0]["signature"] == "sig-1"
    assert assistant_blocks[2]["id"] == "toolu_123"
    usage = result["usage"]
    assert usage["cache_read_input_tokens"] == 7
    assert usage["cache_creation_input_tokens"] == 5


def test_bundled_anthropic_model_catalog_includes_current_aliases() -> None:
    ids = {entry["model_id"] for entry in subject.bundled_anthropic_model_entries()}

    assert "claude-fable-5" in ids
    assert "claude-mythos-5" in ids
    assert "claude-opus-4-8" in ids
    assert "claude-sonnet-5" in ids
    assert "claude-haiku-4-5" in ids


@pytest.mark.asyncio
async def test_fetch_subscription_models_uses_models_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "claude-fable-5",
                        "display_name": "Claude Fable 5",
                        "created_at": "2026-06-09T00:00:00Z",
                    }
                ],
                "has_more": False,
            },
        )

    original_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(subject.httpx, "AsyncClient", client_factory)

    entries = await subject.fetch_subscription_models(AnthropicSubscriptionAuth("access-token"))

    assert seen["url"] == "https://api.anthropic.com/v1/models?limit=100"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer access-token"
    assert headers["anthropic-version"] == "2023-06-01"
    assert entries[0]["model_id"] == "claude-fable-5"
    assert entries[0]["supports_prompt_caching"] is True


@pytest.mark.asyncio
async def test_exchange_authorization_code_validates_state_and_persists_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = subject.generate_authorization_state()
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"access_token": "access", "refresh_token": "refresh", "expires_in": 3600},
        )

    original_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(subject.httpx, "AsyncClient", client_factory)

    record = await subject.exchange_authorization_code(
        callback_input=f"code-value#{pending['state']}",
        pending=pending,
    )

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["grant_type"] == "authorization_code"
    assert payload["code_verifier"] == pending["code_verifier"]
    assert record["access_token"] == "access"
    assert record["refresh_token"] == "refresh"
    assert record["expires_at"] > pending["created_at"]
