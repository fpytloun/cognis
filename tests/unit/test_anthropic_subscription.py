from __future__ import annotations

import json

import httpx
import pytest

from cognis.providers.llm import anthropic_subscription as subject
from cognis.providers.llm.anthropic_subscription import (
    AnthropicSubscriptionAuth,
    parse_callback_input,
)


def test_parse_callback_input_accepts_url_and_code_state() -> None:
    assert parse_callback_input(
        "https://platform.claude.com/oauth/code/callback?code=abc&state=def"
    ) == ("abc", "def")
    assert parse_callback_input("abc#def") == ("abc", "def")
    assert parse_callback_input("code=abc&state=def") == ("abc", "def")
    assert parse_callback_input("not enough") is None


def test_bundled_anthropic_model_catalog_includes_current_aliases() -> None:
    entries = subject.bundled_anthropic_model_entries()
    ids = {entry["model_id"] for entry in entries}

    assert "claude-fable-5" in ids
    assert "claude-mythos-5" in ids
    assert "claude-opus-4-8" in ids
    assert "claude-sonnet-5" in ids
    assert "claude-haiku-4-5" in ids
    assert all(entry["supports_strict_tools"] is True for entry in entries)
    assert all(entry["supports_pause_turn"] is True for entry in entries)


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
