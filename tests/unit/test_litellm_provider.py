from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

import cognis.providers.llm.codex as codex_support  # type: ignore[import-not-found]
import cognis.providers.llm.litellm as litellm_provider_module
from cognis.models.config import DEFAULT_MODEL_INFO, ModelInfo
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm import retry as llm_retry
from cognis.providers.llm.errors import (
    MidStreamErrorCategory,
    classify_llm_exception,
    classify_response_failure,
)
from cognis.providers.llm.litellm import (
    LiteLLMProvider,
    OpenAIToolSearchFallbackRequired,
    _apply_chatgpt_affinity_headers,
    _apply_responses_request_defaults,
    _normalize_proxy_model_info,
    _oauth_token_secret_name,
)
from cognis.providers.llm.reasoning import (
    apply_reasoning_config,
    enrich_model_entry,
    reasoning_efforts_for_model,
    remap_reasoning_effort_to_available,
)
from cognis.providers.llm.responses_bridge import responses_request_kwargs
from cognis.providers.llm.retry import is_retryable_error
from cognis.providers.llm.service import LLMService
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider, ModelRouting


async def _session_factory(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)


class _MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], str] = {}

    async def get_secret(self, name: str, user_id: str, agent_id: str | None = None) -> str:
        del agent_id
        key = (user_id, "system", name)
        if key not in self.values:
            raise KeyError(name)
        return self.values[key]

    async def set_secret(
        self,
        name: str,
        value: str,
        user_id: str,
        scope: str = "user",
        agent_id: str | None = None,
        description: str | None = None,
    ) -> None:
        del agent_id, description
        self.values[(user_id, scope, name)] = value

    async def delete_secret(
        self, name: str, user_id: str, scope: str = "user", agent_id: str | None = None
    ) -> bool:
        del agent_id
        return self.values.pop((user_id, scope, name), None) is not None


class _ProviderError(Exception):
    def __init__(
        self, message: str, *, status_code: int | None = None, body: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_classify_llm_exception_artifact_fetch_payload() -> None:
    exc = _ProviderError(
        "Timeout while downloading URL",
        body={
            "error": {
                "param": "url",
                "details": {"url": "https://cognis.fpy.cz/api/v1/artifacts/content/a/b.png"},
            }
        },
    )

    payload = classify_llm_exception(exc)

    assert payload["category"] == MidStreamErrorCategory.ARTIFACT_FETCH.value
    assert payload["artifact_urls"] == ["https://cognis.fpy.cz/api/v1/artifacts/content/a/b.png"]


def test_classify_response_failure_invalid_image_as_attachment_input() -> None:
    payload = classify_response_failure(
        {
            "message": (
                "The image data you provided does not represent a valid image. "
                "Please use one of the supported image formats."
            ),
            "code": "invalid_value",
            "param": "input[0].content[1].image_url.url",
        }
    )

    assert payload["category"] == MidStreamErrorCategory.ATTACHMENT_INPUT.value
    assert payload["param"] == "input[0].content[1].image_url.url"


def test_classify_llm_exception_reasoning_summary_rejection() -> None:
    exc = _ProviderError(
        "Unsupported parameter: reasoning.summary",
        status_code=400,
        body={"error": {"param": "reasoning.summary", "code": "unsupported_parameter"}},
    )

    payload = classify_llm_exception(exc)

    assert payload["category"] == MidStreamErrorCategory.REASONING_SUMMARY_REJECTED.value


def test_classify_llm_exception_usage_limit_reached_is_quota_exhausted() -> None:
    exc = _ProviderError(
        "HTTP 429 usage_limit_reached",
        status_code=429,
        body={"error": {"code": "usage_limit_reached", "message": "Usage limit reached"}},
    )

    payload = classify_llm_exception(exc)

    assert payload["category"] == MidStreamErrorCategory.QUOTA_EXHAUSTED.value


def test_usage_limit_reached_is_not_pre_stream_retryable() -> None:
    exc = _ProviderError(
        "HTTP 429 usage_limit_reached",
        status_code=429,
        body={"error": {"code": "usage_limit_reached", "message": "Usage limit reached"}},
    )

    assert llm_retry.is_retryable_error(exc) is False


def test_chatgpt_responses_defaults_omit_prompt_cache_key_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COGNIS_CHATGPT_PROMPT_CACHE_KEY_ENABLED", raising=False)
    provider = LLMProvider(
        provider_id="codex",
        display_name="Codex",
        location="controller",
        backend="litellm",
        config={"preset": "chatgpt"},
        status="active",
    )

    result = _apply_responses_request_defaults(
        {}, provider=provider, resolved_model="gpt-5.5", instructions="stable prefix"
    )

    assert "prompt_cache_key" not in result
    assert "prompt_cache_retention" not in result
    assert result["store"] is False


def test_direct_codex_responses_defaults_force_store_false() -> None:
    provider = LLMProvider(
        provider_id="codex",
        display_name="Codex",
        location="controller",
        backend="litellm",
        config={"preset": "chatgpt", "chatgpt_transport": "direct_codex"},
        status="active",
    )

    result = _apply_responses_request_defaults(
        {"store": True}, provider=provider, resolved_model="gpt-5.5", instructions="stable prefix"
    )

    assert result["store"] is False
    assert result["instructions"] == "stable prefix"


def test_direct_codex_responses_defaults_supply_instructions_when_missing() -> None:
    provider = LLMProvider(
        provider_id="codex",
        display_name="Codex",
        location="controller",
        backend="litellm",
        config={"preset": "chatgpt", "chatgpt_transport": "direct_codex"},
        status="active",
    )

    result = _apply_responses_request_defaults(
        {}, provider=provider, resolved_model="gpt-5.5", instructions=None
    )

    assert result["store"] is False
    assert "instructions" in result
    assert "helpful assistant" in result["instructions"]


def test_chatgpt_responses_defaults_allow_explicit_prompt_cache_key_opt_in() -> None:
    provider = LLMProvider(
        provider_id="codex",
        display_name="Codex",
        location="controller",
        backend="litellm",
        config={"preset": "chatgpt", "use_prompt_cache_key": True},
        status="active",
    )

    result = _apply_responses_request_defaults(
        {}, provider=provider, resolved_model="gpt-5.5", instructions="stable prefix"
    )

    assert isinstance(result.get("prompt_cache_key"), str)
    assert result["prompt_cache_retention"] == "1h"


def test_chatgpt_affinity_headers_merge_session_id() -> None:
    provider = LLMProvider(
        provider_id="codex",
        display_name="Codex",
        location="controller",
        backend="litellm",
        config={"preset": "chatgpt"},
        status="active",
    )

    result = _apply_chatgpt_affinity_headers(
        {"extra_headers": {"User-Agent": "cognis-test", "x-session-affinity": "old"}},
        provider=provider,
        session_id="sess_123",
    )

    assert result["extra_headers"] == {
        "User-Agent": "cognis-test",
        "x-session-affinity": "sess_123",
        "session_id": "sess_123",
    }


def _jwt_with_claims(claims: Mapping[str, object]) -> str:
    header: Mapping[str, object] = {"alg": "none", "typ": "JWT"}

    def encode(payload: Mapping[str, object]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode(header)}.{encode(claims)}.sig"


@pytest.mark.asyncio
async def test_litellm_provider_resolves_explicit_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    resolved = await provider.resolve_model(explicit_model="gpt-5.4-mini")

    assert resolved == "gpt-5.4-mini"
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_generate_uses_litellm_responses_with_hydrated_oauth(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None
    await secrets.set_secret(
        _oauth_token_secret_name(row),
        json.dumps({"access_token": "valid", "refresh_token": "refresh"}),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    from litellm.llms.chatgpt.authenticator import Authenticator

    monkeypatch.setattr(Authenticator, "get_access_token", lambda _self: "valid")
    monkeypatch.setattr(Authenticator, "get_account_id", lambda _self: "account")

    captured: dict[str, Any] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "hello"}]}
                ],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        captured["env_token_dir"] = os.environ.get("CHATGPT_TOKEN_DIR")
        captured["env_auth_file"] = os.environ.get("CHATGPT_AUTH_FILE")
        captured["env_api_base"] = os.environ.get("OPENAI_CHATGPT_API_BASE")
        return _Response()

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.delitem(
        litellm_provider_module.litellm.model_cost,
        "chatgpt/gpt-5.3-codex",
        raising=False,
    )

    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.3-codex",
        provider_id="chatgpt",
        tools=[{"type": "apply_patch"}],
    )

    assert captured["model"] == "chatgpt/gpt-5.3-codex"
    assert captured["stream"] is False
    assert captured["tools"][0]["type"] == "custom"
    assert captured["tools"][0]["name"] == "apply_patch"
    assert captured["tools"][0]["format"]["type"] == "grammar"
    assert captured["env_token_dir"]
    assert captured["env_auth_file"] == "auth.json"
    assert captured["env_api_base"] is None
    assert "store" not in captured
    assert result["choices"][0]["message"]["content"] == "hello"
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_stream_uses_litellm_responses_with_hydrated_oauth(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None
    await secrets.set_secret(
        _oauth_token_secret_name(row),
        json.dumps({"access_token": "valid", "refresh_token": "refresh"}),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    from litellm.llms.chatgpt.authenticator import Authenticator

    monkeypatch.setattr(Authenticator, "get_access_token", lambda _self: "valid")
    monkeypatch.setattr(Authenticator, "get_account_id", lambda _self: "account")

    captured: dict[str, Any] = {}

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "hello"}
        yield {"type": "response.completed", "response": {"status": "completed"}}

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        captured["env_token_dir"] = os.environ.get("CHATGPT_TOKEN_DIR")
        captured["env_auth_file"] = os.environ.get("CHATGPT_AUTH_FILE")
        captured["env_api_base"] = os.environ.get("OPENAI_CHATGPT_API_BASE")
        return _fake_stream()

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.3-codex",
            provider_id="chatgpt",
            tools=[{"type": "apply_patch"}],
        )
    ]

    assert captured["model"] == "chatgpt/gpt-5.3-codex"
    assert captured["stream"] is True
    assert captured["tools"][0]["type"] == "custom"
    assert captured["tools"][0]["name"] == "apply_patch"
    assert captured["tools"][0]["format"]["type"] == "grammar"
    registered_info = litellm_provider_module.litellm.model_cost["chatgpt/gpt-5.3-codex"]
    assert registered_info["litellm_provider"] == "chatgpt"
    assert registered_info["mode"] == "responses"
    assert registered_info["supports_native_streaming"] is True
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig

    assert (
        ChatGPTResponsesAPIConfig().should_fake_stream(
            model="gpt-5.3-codex", stream=True, custom_llm_provider="chatgpt"
        )
        is False
    )
    assert captured["env_token_dir"]
    assert captured["env_auth_file"] == "auth.json"
    assert captured["env_api_base"] is None
    assert chunks[0]["choices"][0]["delta"]["content"] == "hello"
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_generate_uses_direct_codex_transport_by_default(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, Any] = {}

    async def _fake_auth(self: LiteLLMProvider, row: LLMProvider) -> codex_support.CodexAuth:
        captured["auth_provider_id"] = row.provider_id
        return codex_support.CodexAuth(access_token="token", account_id="account")

    async def _fake_responses(self: Any, **kwargs: object) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
        }

    def _unexpected_oauth_context(self: LiteLLMProvider, row: LLMProvider | None) -> object:
        raise AssertionError("Direct Codex transport must not hydrate LiteLLM OAuth state")

    monkeypatch.setattr(LLMService, "_chatgpt_codex_auth", _fake_auth)
    monkeypatch.setattr(LLMService, "_provider_oauth_token_context", _unexpected_oauth_context)
    monkeypatch.setattr(litellm_provider_module.DirectCodexTransport, "responses", _fake_responses)

    provider = LLMService(session_factory)
    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.3-codex",
        provider_id="chatgpt",
        cognis_session_id="session-123",
    )

    assert captured["auth_provider_id"] == "chatgpt"
    assert captured["model"] == "gpt-5.3-codex"
    assert captured["input"] == [{"role": "user", "content": "hi"}]
    assert captured["store"] is False
    assert result["choices"][0]["message"]["content"] == "hello"
    await engine.dispose()


@pytest.mark.asyncio
async def test_anthropic_subscription_discover_models_uses_remote_models(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="anthropic-subscription",
                display_name="Claude Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "auth_config": {"mode": "oauth", "provider": "anthropic_subscription"},
                    "default_model": "claude-fable-5",
                    "models": [{"model_id": "claude-sonnet-4-5"}],
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, Any] = {}

    async def _fake_auth(
        self: LiteLLMProvider, row: LLMProvider
    ) -> litellm_provider_module.AnthropicSubscriptionAuth:
        captured["auth_provider_id"] = row.provider_id
        return litellm_provider_module.AnthropicSubscriptionAuth("access-token")

    async def _fake_fetch(
        auth: litellm_provider_module.AnthropicSubscriptionAuth,
    ) -> list[dict[str, Any]]:
        captured["access_token"] = auth.access_token
        return [
            {"model_id": "claude-fable-5", "name": "Claude Fable 5 Remote"},
            {"model_id": "claude-model-only-from-api", "name": "Remote-only model"},
        ]

    monkeypatch.setattr(LLMService, "_anthropic_subscription_auth", _fake_auth)
    monkeypatch.setattr(litellm_provider_module, "fetch_subscription_models", _fake_fetch)

    provider = LLMService(session_factory)
    models = await provider.discover_models("anthropic-subscription")

    by_id = {entry["model_id"]: entry for entry in models}
    assert captured == {
        "auth_provider_id": "anthropic-subscription",
        "access_token": "access-token",
    }
    assert "claude-model-only-from-api" in by_id
    assert by_id["claude-fable-5"]["name"] == "Claude Fable 5 Remote"
    assert by_id["claude-fable-5"]["supports_prompt_caching"] is True
    assert "claude-sonnet-4-5" in by_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_generate_uses_streaming_direct_codex_transport(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, Any] = {}

    async def _fake_auth(self: LiteLLMProvider, row: LLMProvider) -> codex_support.CodexAuth:
        return codex_support.CodexAuth(access_token="token", account_id="account")

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "hel"}
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "lo"}
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            },
        }

    async def _fake_responses(self: Any, **kwargs: object) -> object:
        captured.update(kwargs)
        return _fake_stream()

    monkeypatch.setattr(LLMService, "_chatgpt_codex_auth", _fake_auth)
    monkeypatch.setattr(litellm_provider_module.DirectCodexTransport, "responses", _fake_responses)

    provider = LLMService(session_factory)
    result = await provider.generate(
        messages=[
            {"role": "system", "content": "Answer tersely."},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.3-codex",
        provider_id="chatgpt",
    )

    assert captured["model"] == "gpt-5.3-codex"
    assert captured["stream"] is True
    assert captured["instructions"] == "Answer tersely."
    assert captured["input"] == [{"role": "user", "content": "hi"}]
    assert result["choices"][0]["message"]["content"] == "hello"
    assert result["usage"]["prompt_tokens"] == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_json_generate_uses_streaming_direct_codex_transport(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, Any] = {}

    async def _fake_auth(self: LiteLLMProvider, row: LLMProvider) -> codex_support.CodexAuth:
        return codex_support.CodexAuth(access_token="token", account_id="account")

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": '{"ok":'}
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": " true}"}
        yield {"type": "response.completed", "response": {"status": "completed"}}

    async def _fake_responses(self: Any, **kwargs: object) -> object:
        captured.update(kwargs)
        return _fake_stream()

    monkeypatch.setattr(LLMService, "_chatgpt_codex_auth", _fake_auth)
    monkeypatch.setattr(litellm_provider_module.DirectCodexTransport, "responses", _fake_responses)

    provider = LLMService(session_factory)
    result = await provider.generate(
        messages=[
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": "classify"},
        ],
        model="gpt-5.3-codex",
        provider_id="chatgpt",
        task_type="classification",
        response_format={"type": "json_object"},
    )

    assert captured["stream"] is True
    assert captured["instructions"] == "Return JSON only."
    assert captured["input"] == [
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "classify"},
    ]
    assert result["choices"][0]["message"]["content"] == '{"ok": true}'
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_json_generate_retries_stream_without_reasoning_summary(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                },
                status="active",
            )
        )
        await session.commit()

    calls: list[dict[str, Any]] = []

    async def _fake_auth(self: LiteLLMProvider, row: LLMProvider) -> codex_support.CodexAuth:
        return codex_support.CodexAuth(access_token="token", account_id="account")

    async def _failing_stream() -> object:
        yield {
            "type": "response.failed",
            "response": {"status": "failed"},
            "error": {
                "message": "Unsupported parameter: reasoning.summary",
                "param": "reasoning.summary",
            },
        }

    async def _successful_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": '{"ok": true}',
        }
        yield {"type": "response.completed", "response": {"status": "completed"}}

    async def _fake_responses(self: Any, **kwargs: object) -> object:
        calls.append(kwargs)
        if len(calls) == 1:
            return _failing_stream()
        return _successful_stream()

    monkeypatch.setattr(LLMService, "_chatgpt_codex_auth", _fake_auth)
    monkeypatch.setattr(litellm_provider_module.DirectCodexTransport, "responses", _fake_responses)

    provider = LLMService(session_factory)
    result = await provider.generate(
        messages=[
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": "classify"},
        ],
        model="gpt-5.3-codex",
        provider_id="chatgpt",
        task_type="classification",
        response_format={"type": "json_object"},
    )

    assert len(calls) == 2
    assert "summary" not in calls[1].get("reasoning", {})
    assert result["choices"][0]["message"]["content"] == '{"ok": true}'
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_stream_uses_direct_codex_transport_by_default(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, Any] = {}

    async def _fake_auth(self: LiteLLMProvider, row: LLMProvider) -> codex_support.CodexAuth:
        captured["auth_provider_id"] = row.provider_id
        return codex_support.CodexAuth(access_token="token", account_id="account")

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "hello"}
        yield {"type": "response.completed", "response": {"status": "completed"}}

    async def _fake_responses(self: Any, **kwargs: object) -> object:
        captured.update(kwargs)
        return _fake_stream()

    def _unexpected_oauth_context(self: LiteLLMProvider, row: LLMProvider | None) -> object:
        raise AssertionError("Direct Codex transport must not hydrate LiteLLM OAuth state")

    monkeypatch.setattr(LLMService, "_chatgpt_codex_auth", _fake_auth)
    monkeypatch.setattr(LLMService, "_provider_oauth_token_context", _unexpected_oauth_context)
    monkeypatch.setattr(litellm_provider_module.DirectCodexTransport, "responses", _fake_responses)

    provider = LLMService(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.3-codex",
            provider_id="chatgpt",
            cognis_session_id="session-123",
        )
    ]

    assert captured["auth_provider_id"] == "chatgpt"
    assert captured["model"] == "gpt-5.3-codex"
    assert captured["input"] == [{"role": "user", "content": "hi"}]
    assert captured["stream"] is True
    assert captured["store"] is False
    assert chunks[0]["choices"][0]["delta"]["content"] == "hello"
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_json_stream_uses_direct_codex_transport_json_marker(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, Any] = {}

    async def _fake_auth(self: LiteLLMProvider, row: LLMProvider) -> codex_support.CodexAuth:
        return codex_support.CodexAuth(access_token="token", account_id="account")

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": '{"ok":true}'}
        yield {"type": "response.completed", "response": {"status": "completed"}}

    async def _fake_responses(self: Any, **kwargs: object) -> object:
        captured.update(kwargs)
        return _fake_stream()

    monkeypatch.setattr(LLMService, "_chatgpt_codex_auth", _fake_auth)
    monkeypatch.setattr(litellm_provider_module.DirectCodexTransport, "responses", _fake_responses)

    chunks = [
        chunk
        async for chunk in LLMService(session_factory).stream_generate(
            messages=[{"role": "user", "content": "classify"}],
            model="gpt-5.3-codex",
            provider_id="chatgpt",
            response_format={"type": "json_object"},
        )
    ]

    assert (
        captured["instructions"]
        == "You are a helpful assistant. Follow the user's instructions precisely."
    )
    assert captured["input"] == [
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "classify"},
    ]
    assert chunks[0]["choices"][0]["delta"]["content"] == '{"ok":true}'
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_preset_prefixes_model_and_uses_responses(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None

    model_info = ModelInfo(model_id="gpt-5.3-codex")
    assert provider._apply_model_prefix("gpt-5.3-codex", row) == "chatgpt/gpt-5.3-codex"
    assert provider._should_use_responses_api("gpt-5.3-codex", model_info, row) is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_discovery_uses_codex_catalog_fallback(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={
                "preset": "chatgpt",
                "default_model": "gpt-5.5",
                "models": [{"model_id": "future-codex", "display_name": "Future Codex"}],
            },
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    provider = LiteLLMProvider(session_factory, secrets_provider=_MemorySecrets())
    models = await provider.discover_models("chatgpt")
    by_id = {model["model_id"]: model for model in models}

    assert "gpt-5.5" in by_id
    assert by_id["gpt-5.5"]["source"] == "codex_catalog"
    assert by_id["gpt-5.5"]["context_window"] == 400_000
    assert by_id["gpt-5.5"]["max_context_window"] == 400_000
    assert by_id["gpt-5.5"]["max_input_tokens"] == 272_000
    assert by_id["gpt-5.5"]["max_output_tokens"] == 128_000
    assert by_id["future-codex"]["source"] == "configured"
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_owned_anthropic_provider_visible_with_actor_scope(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="meridian-claude",
                owner_email="filip@example.com",
                display_name="Meridian Claude",
                location="executor",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "default_model": "claude-opus-4-7",
                    "models": [{"model_id": "claude-opus-4-7"}],
                    "base_url": "http://127.0.0.1:8090",
                    "api_base": "http://127.0.0.1:8090",
                    "auth_config": {"mode": "none"},
                    "executor_id": "maitrea",
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    with pytest.raises(ValueError, match="not visible"):
        await provider.resolve_model_target(explicit_provider_id="meridian-claude")

    model, provider_id = await provider.resolve_model_target(
        explicit_provider_id="meridian-claude",
        acting_user_email="filip@example.com",
    )
    model_info = await provider.get_model_info(
        model,
        provider_id=provider_id,
        acting_user_email="filip@example.com",
    )

    assert model == "claude-opus-4-7"
    assert provider_id == "meridian-claude"
    assert model_info.model_id == "claude-opus-4-7"
    assert model_info.context_window >= 200_000
    await engine.dispose()


def test_codex_usage_payload_normalization() -> None:
    payload = {
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 42,
                "limit_window_seconds": 3600,
                "reset_after_seconds": 600,
                "reset_at": 1700000000,
            },
            "secondary_window": {
                "used_percent": 84,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 1200,
                "reset_at": 1700001200,
            },
        },
        "credits": {"has_credits": True, "unlimited": False, "balance": "10"},
        "rate_limit_reached_type": {"type": "rate_limit_reached"},
    }

    result = codex_support._normalize_usage_payload(payload)

    assert result["plan_type"] == "pro"
    assert result["primary"]["used_percent"] == 42.0
    assert result["primary"]["window_duration_mins"] == 60
    assert result["secondary"]["window_duration_mins"] == 10080
    assert result["credits"]["balance"] == "10"
    assert result["rate_limit_reached_type"] == "rate_limit_reached"


@pytest.mark.asyncio
async def test_chatgpt_oauth_context_hydrates_and_persists_encrypted_secret(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None
    secret_name = _oauth_token_secret_name(row)
    await secrets.set_secret(
        secret_name,
        json.dumps({"access_token": "old", "refresh_token": "refresh"}),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    from litellm.llms.chatgpt.authenticator import Authenticator

    get_access_token_calls = 0

    def fake_get_access_token(self: object) -> str:
        nonlocal get_access_token_calls
        get_access_token_calls += 1
        auth_path = Path(os.environ["CHATGPT_TOKEN_DIR"]) / os.environ["CHATGPT_AUTH_FILE"]
        payload = json.loads(auth_path.read_text())
        assert payload["access_token"] == "old"
        auth_path.write_text(json.dumps({"access_token": "fresh", "refresh_token": "refresh"}))
        return "fresh"

    def fake_get_account_id(_self: object) -> None:
        return None

    def fail_device_login(_self: object) -> None:
        raise AssertionError("LiteLLM device login must not be started")

    monkeypatch.setattr(Authenticator, "get_access_token", fake_get_access_token)
    monkeypatch.setattr(Authenticator, "get_account_id", fake_get_account_id)
    monkeypatch.setattr(Authenticator, "_login_device_code", fail_device_login)

    async with provider._provider_oauth_token_context(row):
        token_dir = os.environ["CHATGPT_TOKEN_DIR"]
        auth_file = os.environ["CHATGPT_AUTH_FILE"]
        auth_path = Path(token_dir) / auth_file
        assert "OPENAI_CHATGPT_API_BASE" not in os.environ
        assert json.loads(auth_path.read_text())["access_token"] == "fresh"
        auth_path.write_text(json.dumps({"access_token": "new", "refresh_token": "refresh"}))

    assert get_access_token_calls == 1
    assert (
        json.loads(secrets.values[(SYSTEM_USER_EMAIL, "system", secret_name)])["access_token"]
        == "new"
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_oauth_context_uses_user_owned_secret_scope(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt-user",
            display_name="User ChatGPT",
            location="controller",
            backend="litellm",
            owner_email="owner@example.com",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt-user")
    assert row is not None
    secret_name = _oauth_token_secret_name(row)
    await secrets.set_secret(
        secret_name,
        json.dumps({"access_token": "old", "refresh_token": "refresh"}),
        "owner@example.com",
        scope="system",
    )

    from litellm.llms.chatgpt.authenticator import Authenticator

    def fake_get_access_token(self: object) -> str:
        auth_path = Path(os.environ["CHATGPT_TOKEN_DIR"]) / os.environ["CHATGPT_AUTH_FILE"]
        auth_path.write_text(json.dumps({"access_token": "new", "refresh_token": "refresh"}))
        return "new"

    monkeypatch.setattr(Authenticator, "get_access_token", fake_get_access_token)
    monkeypatch.setattr(Authenticator, "get_account_id", lambda _self: None)
    monkeypatch.setattr(
        Authenticator,
        "_login_device_code",
        lambda _self: (_ for _ in ()).throw(AssertionError("device login must not start")),
    )

    async with provider._provider_oauth_token_context(row):
        pass

    assert (
        json.loads(secrets.values[("owner@example.com", "system", secret_name)])["access_token"]
        == "new"
    )
    assert (SYSTEM_USER_EMAIL, "system", secret_name) not in secrets.values
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_oauth_context_rejects_pending_state(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None
    await secrets.set_secret(
        _oauth_token_secret_name(row),
        json.dumps({"status": "pending", "user_code": "ABCD-EFGH"}),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    with pytest.raises(RuntimeError, match="not authorized"):
        async with provider._provider_oauth_token_context(row):
            pass
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_oauth_context_requires_authorized_token(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    provider = LiteLLMProvider(session_factory, secrets_provider=_MemorySecrets())
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None

    with pytest.raises(RuntimeError, match="not authorized"):
        async with provider._provider_oauth_token_context(row):
            pass
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_oauth_context_fails_fast_when_litellm_would_login(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None
    await secrets.set_secret(
        _oauth_token_secret_name(row),
        json.dumps({"access_token": "expired", "refresh_token": "refresh"}),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    from litellm.llms.chatgpt.authenticator import Authenticator
    from litellm.llms.chatgpt.common_utils import GetAccessTokenError

    login_calls = 0

    def fake_login(_self: object) -> None:
        nonlocal login_calls
        login_calls += 1
        raise GetAccessTokenError(message="blocked", status_code=401)

    def fake_get_access_token(self: object) -> str:
        return cast(dict[str, str], cast(Any, self)._login_device_code())["access_token"]

    monkeypatch.setattr(Authenticator, "get_access_token", fake_get_access_token)
    monkeypatch.setattr(Authenticator, "_login_device_code", fake_login)

    with pytest.raises(RuntimeError, match="complete Cognis provider OAuth first"):
        async with provider._provider_oauth_token_context(row):
            pass
    assert login_calls == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_provider_test_uses_direct_codex_health_check(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None
    await secrets.set_secret(
        _oauth_token_secret_name(row),
        json.dumps(
            {
                "access_token": _jwt_with_claims(
                    {
                        "exp": int(time.time()) + 3600,
                        "https://api.openai.com/auth": {"chatgpt_account_id": "account"},
                    }
                ),
                "refresh_token": "refresh",
            }
        ),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    from litellm.llms.chatgpt.authenticator import Authenticator

    def fail_authenticator_init(_self: object) -> None:
        raise AssertionError("provider tests must not instantiate LiteLLM Authenticator")

    calls: list[tuple[str, float]] = []

    async def fake_test(
        auth: codex_support.CodexAuth, *, model: str, timeout: float = 15.0
    ) -> None:
        assert auth.account_id == "account"
        calls.append((model, timeout))

    async def fail_resolve_kwargs(_provider: object) -> dict[str, object]:
        raise AssertionError("provider tests must not resolve LiteLLM request kwargs")

    def fail_model_info(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("provider tests must not call LiteLLM model metadata")

    monkeypatch.setattr(Authenticator, "__init__", fail_authenticator_init)
    monkeypatch.setattr(provider, "_resolve_provider_kwargs", fail_resolve_kwargs)
    monkeypatch.setattr(litellm_provider_module.litellm, "get_model_info", fail_model_info)
    monkeypatch.setattr(litellm_provider_module, "test_codex_responses", fake_test)

    result = await provider.test_provider("chatgpt", timeout_seconds=7)

    assert result["ok"] is True
    assert calls == [("gpt-5.3-codex", 7.0)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_model_info_uses_codex_catalog_without_litellm(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory, secrets_provider=_MemorySecrets())

    def fail_model_info(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("ChatGPT metadata must not call LiteLLM model metadata")

    async def fail_resolve_kwargs(_provider: object) -> dict[str, object]:
        raise AssertionError("ChatGPT metadata must not resolve LiteLLM request kwargs")

    monkeypatch.setattr(litellm_provider_module.litellm, "get_model_info", fail_model_info)
    monkeypatch.setattr(provider, "_resolve_provider_kwargs", fail_resolve_kwargs)

    model_info = await provider.get_model_info("gpt-5.3-codex", provider_id="chatgpt")

    assert model_info.model_id == "gpt-5.3-codex"
    assert model_info.supports_responses_api is True
    assert model_info.supports_openai_apply_patch is True
    assert model_info.openai_apply_patch_tool_type == "freeform"
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_unknown_codex_model_defaults_apply_patch_subtype_to_freeform(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={"preset": "chatgpt", "default_model": "gpt-5.3-codex-spark"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory, secrets_provider=_MemorySecrets())

    def fail_model_info(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("ChatGPT metadata must not call LiteLLM model metadata")

    async def fail_resolve_kwargs(_provider: object) -> dict[str, object]:
        raise AssertionError("ChatGPT metadata must not resolve LiteLLM request kwargs")

    monkeypatch.setattr(litellm_provider_module.litellm, "get_model_info", fail_model_info)
    monkeypatch.setattr(provider, "_resolve_provider_kwargs", fail_resolve_kwargs)

    model_info = await provider.get_model_info("gpt-5.3-codex-spark", provider_id="chatgpt")

    assert model_info.model_id == "gpt-5.3-codex-spark"
    assert model_info.context_window == 272_000
    assert model_info.max_context_window == 272_000
    assert model_info.max_input_tokens == 128_000
    assert model_info.max_output_tokens == 128_000
    assert model_info.supports_responses_api is True
    assert model_info.supports_openai_apply_patch is True
    assert model_info.openai_apply_patch_tool_type == "freeform"
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_codex_auth_refreshes_without_litellm_authenticator(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None
    token_secret_name = _oauth_token_secret_name(row)
    await secrets.set_secret(
        token_secret_name,
        json.dumps(
            {
                "access_token": _jwt_with_claims({"exp": int(time.time()) - 3600}),
                "refresh_token": "refresh-old",
            }
        ),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    from litellm.llms.chatgpt.authenticator import Authenticator

    def fail_authenticator_init(_self: object) -> None:
        raise AssertionError("Codex auth refresh must not instantiate LiteLLM Authenticator")

    class _RefreshResponse:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {
                "access_token": _jwt_with_claims({"exp": int(time.time()) + 3600}),
                "refresh_token": "refresh-new",
                "id_token": _jwt_with_claims(
                    {"https://api.openai.com/auth": {"chatgpt_account_id": "account-new"}}
                ),
            }

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, *args: object, **kwargs: object) -> _RefreshResponse:
            return _RefreshResponse()

    monkeypatch.setattr(Authenticator, "__init__", fail_authenticator_init)
    monkeypatch.setattr(litellm_provider_module.httpx, "AsyncClient", _Client)

    auth = await provider._chatgpt_codex_auth(row)

    assert auth.account_id == "account-new"
    stored = json.loads(await secrets.get_secret(token_secret_name, SYSTEM_USER_EMAIL))
    assert stored["refresh_token"] == "refresh-new"
    assert stored["account_id"] == "account-new"
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_oauth_start_preserves_existing_authorized_token(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="controller",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None
    token_secret_name = _oauth_token_secret_name(row)
    await secrets.set_secret(
        token_secret_name,
        json.dumps({"access_token": "working", "refresh_token": "refresh"}),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    async def _fake_device_code() -> dict[str, object]:
        return {"device_auth_id": "device", "user_code": "ABCD-EFGH", "interval": 5}

    monkeypatch.setattr(provider, "_request_chatgpt_device_code", _fake_device_code)

    status = await provider.start_chatgpt_oauth("chatgpt")

    assert status["status"] == "pending"
    assert (
        json.loads(secrets.values[(SYSTEM_USER_EMAIL, "system", token_secret_name)])["access_token"]
        == "working"
    )
    assert (SYSTEM_USER_EMAIL, "system", f"{token_secret_name}_pending") in secrets.values
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_oauth_start_uses_user_owned_secret_scope(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt-user",
            display_name="User ChatGPT",
            location="controller",
            backend="litellm",
            owner_email="owner@example.com",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)

    async def _fake_device_code() -> dict[str, object]:
        return {"device_auth_id": "device", "user_code": "ABCD-EFGH", "interval": 5}

    monkeypatch.setattr(provider, "_request_chatgpt_device_code", _fake_device_code)

    status = await provider.start_chatgpt_oauth("chatgpt-user")

    assert status["status"] == "pending"
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt-user")
    assert row is not None
    pending_secret_name = f"{_oauth_token_secret_name(row)}_pending"
    assert ("owner@example.com", "system", pending_secret_name) in secrets.values
    assert (SYSTEM_USER_EMAIL, "system", pending_secret_name) not in secrets.values
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_owned_provider_secret_auth_uses_provider_owner_scope(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="user-openai",
            display_name="User OpenAI Compatible",
            location="controller",
            backend="litellm",
            owner_email="owner@example.com",
            config={
                "preset": "openai_compatible",
                "auth_config": {"mode": "secret", "secret_name": "provider-api-key"},
            },
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    secrets = _MemorySecrets()
    await secrets.set_secret("provider-api-key", "system-key", SYSTEM_USER_EMAIL, scope="system")
    await secrets.set_secret("provider-api-key", "owner-key", "owner@example.com", scope="system")
    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)

    async with session_factory() as session:
        row = await session.get(LLMProvider, "user-openai")
    kwargs = await provider._resolve_provider_kwargs(row)

    assert kwargs["api_key"] == "owner-key"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_drops_invalid_provider_request_kwargs(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="bad-openai",
            display_name="Bad OpenAI Compatible",
            location="controller",
            backend="litellm",
            config={
                "preset": "openai_compatible",
                "api_base": ["http://127.0.0.1:8090"],
                "base_url": ["http://127.0.0.1:8090"],
                "timeout": [30],
                "api_version": "2024-10-21",
            },
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    async with session_factory() as session:
        row = await session.get(LLMProvider, "bad-openai")
    kwargs = await provider._resolve_provider_kwargs(row)

    assert kwargs == {"api_version": "2024-10-21"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_oauth_rejects_executor_location(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        provider_row = LLMProvider(
            provider_id="chatgpt",
            display_name="ChatGPT Subscription",
            location="executor",
            backend="litellm",
            config={"preset": "chatgpt", "default_model": "gpt-5.3-codex"},
            status="active",
        )
        session.add(provider_row)
        await session.commit()

    provider = LiteLLMProvider(session_factory, secrets_provider=_MemorySecrets())
    async with session_factory() as session:
        row = await session.get(LLMProvider, "chatgpt")
    assert row is not None

    with pytest.raises(RuntimeError, match="must run on the controller"):
        async with provider._provider_oauth_token_context(row):
            pass
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_uses_model_routing_entry(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(ModelRouting(task_type="default", provider_id=None, model="gpt-4o-mini"))
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.resolve_model(task_type="default") == "gpt-4o-mini"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_resolves_stream_idle_config_precedence(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                is_default=True,
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "stream_idle_timeout_seconds": 45,
                    "stream_max_retries": 2,
                    "models": [
                        {
                            "model_id": "gpt-5.4",
                            "stream_idle_timeout_seconds": 15,
                            "stream_max_retries": 5,
                        }
                    ],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.resolve_stream_idle_config(
        provider_id="openai",
        model_id="gpt-5.4",
        default_idle_timeout_seconds=60,
        default_max_retries=3,
    ) == {"idle_timeout_seconds": 15, "max_retries": 5}
    assert await provider.resolve_stream_idle_config(
        provider_id="openai",
        model_id="gpt-4o-mini",
        default_idle_timeout_seconds=60,
        default_max_retries=3,
    ) == {"idle_timeout_seconds": 45, "max_retries": 2}
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_uses_bounded_chatgpt_stream_idle_defaults(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="codex",
                display_name="Codex",
                location="controller",
                backend="litellm",
                is_default=True,
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.5",
                    "models": [{"model_id": "gpt-5.5"}],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.resolve_stream_idle_config(
        provider_id="codex",
        model_id="gpt-5.5",
        default_idle_timeout_seconds=300,
        default_max_retries=3,
    ) == {"idle_timeout_seconds": 90, "max_retries": 3}
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_preserves_explicit_chatgpt_stream_idle_config(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="codex",
                display_name="Codex",
                location="controller",
                backend="litellm",
                is_default=True,
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.5",
                    "stream_idle_timeout_seconds": 180,
                    "stream_max_retries": 2,
                    "models": [{"model_id": "gpt-5.5"}],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.resolve_stream_idle_config(
        provider_id="codex",
        model_id="gpt-5.5",
        default_idle_timeout_seconds=300,
        default_max_retries=3,
    ) == {"idle_timeout_seconds": 180, "max_retries": 2}
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_applies_route_reasoning_effort_when_not_explicit(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                is_default=True,
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "models": [
                        {
                            "model_id": "gpt-5.4",
                            "supports_reasoning": True,
                            "reasoning_efforts": [
                                "default",
                                "none",
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                            ],
                        }
                    ],
                },
                status="active",
            )
        )
        session.add(
            ModelRouting(
                task_type="classifier",
                provider_id="openai",
                model="gpt-5.4",
                config={"reasoning_effort": "xhigh"},
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    captured: dict[str, object] = {}

    monkeypatch.setattr(provider, "_should_route_to_executor", lambda *_args: True)

    async def _fake_executor_generate(
        model: str,
        messages: list[dict[str, object]],
        provider_row: LLMProvider,
        *,
        request_kwargs: dict[str, object],
    ) -> dict[str, object]:
        del model, messages, provider_row
        captured.update(request_kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(provider, "_executor_generate", _fake_executor_generate)

    response = await provider.generate([{"role": "user", "content": "hi"}], task_type="classifier")

    assert response["choices"][0]["message"]["content"] == "ok"
    assert captured["reasoning_effort"] == "xhigh"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_explicit_reasoning_effort_overrides_route_default(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                is_default=True,
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "models": [
                        {
                            "model_id": "gpt-5.4",
                            "supports_reasoning": True,
                            "reasoning_efforts": [
                                "default",
                                "none",
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                            ],
                        }
                    ],
                },
                status="active",
            )
        )
        session.add(
            ModelRouting(
                task_type="classifier",
                provider_id="openai",
                model="gpt-5.4",
                config={"reasoning_effort": "xhigh"},
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    captured: dict[str, object] = {}

    monkeypatch.setattr(provider, "_should_route_to_executor", lambda *_args: True)

    async def _fake_executor_generate(
        model: str,
        messages: list[dict[str, object]],
        provider_row: LLMProvider,
        *,
        request_kwargs: dict[str, object],
    ) -> dict[str, object]:
        del model, messages, provider_row
        captured.update(request_kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(provider, "_executor_generate", _fake_executor_generate)

    await provider.generate(
        [{"role": "user", "content": "hi"}],
        task_type="classifier",
        reasoning_effort="medium",
    )

    assert captured["reasoning_effort"] == "medium"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_does_not_inject_hidden_route_reasoning_default(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                is_default=True,
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "models": [
                        {
                            "model_id": "gpt-5.4",
                            "supports_reasoning": True,
                            "reasoning_efforts": [
                                "default",
                                "none",
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                            ],
                        }
                    ],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    captured: dict[str, object] = {}

    monkeypatch.setattr(provider, "_should_route_to_executor", lambda *_args: True)

    async def _fake_executor_generate(
        model: str,
        messages: list[dict[str, object]],
        provider_row: LLMProvider,
        *,
        request_kwargs: dict[str, object],
    ) -> dict[str, object]:
        del model, messages, provider_row
        captured.update(request_kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(provider, "_executor_generate", _fake_executor_generate)

    await provider.generate([{"role": "user", "content": "hi"}], task_type="classifier")

    assert "reasoning_effort" not in captured
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_infers_image_generation_capability_from_model_name(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                is_default=True,
                config={
                    "preset": "openai",
                    "default_model": "gpt-image-1",
                    "models": [{"model_id": "gpt-image-1"}],
                },
                status="active",
            )
        )
        await session.commit()

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.get_model_info", lambda **_: {})
    provider = LiteLLMProvider(session_factory)

    model_info = await provider.get_model_info("gpt-image-1", provider_id="openai")

    assert model_info.supports_image_generation is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_infers_embedding_capability_from_model_name(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                is_default=True,
                config={
                    "preset": "openai",
                    "default_model": "text-embedding-3-small",
                    "models": [{"model_id": "text-embedding-3-small"}],
                },
                status="active",
            )
        )
        await session.commit()

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.get_model_info", lambda **_: {})
    provider = LiteLLMProvider(session_factory)

    model_info = await provider.get_model_info("text-embedding-3-small", provider_id="openai")

    assert model_info.supports_embedding is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_falls_back_to_default_provider_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"default_model": "gpt-4o-mini"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.resolve_model(task_type="default") == "gpt-4o-mini"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_raises_when_no_model_is_configured(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    with pytest.raises(ValueError, match="No LLM model configured"):
        await provider.resolve_model(task_type="default")

    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_raises_when_model_route_provider_is_missing(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return False

        async def get(self, model, key):
            if model is LLMProvider and key == "missing":
                return None
            return None

        async def execute(self, stmt):
            del stmt

            class _Result:
                def scalar_one_or_none(self) -> object:
                    return ModelRouting(
                        task_type="default", provider_id="missing", model="gpt-4o-mini"
                    )

            return _Result()

    provider = LiteLLMProvider(session_factory)
    provider.session_factory = lambda: _FakeSession()  # type: ignore[assignment]

    with pytest.raises(ValueError, match="references missing provider"):
        await provider.resolve_model(task_type="default")

    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_image_generate_omits_response_format_for_gpt_image_1(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "default_model": "gpt-image-1"},
                status="active",
            )
        )
        await session.commit()
    provider = LiteLLMProvider(session_factory)

    captured: dict[str, object] = {}

    class _Response:
        data = [{"b64_json": "YWJj"}]
        usage = None

    async def fake_with_llm_retry(_func: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr("cognis.providers.llm.retry.with_llm_retry", fake_with_llm_retry)

    await provider.image_generate(prompt="draw", model="gpt-image-1")

    assert "response_format" not in captured
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_image_generate_omits_response_format_for_gpt_image_2(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "default_model": "gpt-image-2"},
                status="active",
            )
        )
        await session.commit()
    provider = LiteLLMProvider(session_factory)

    captured: dict[str, object] = {}

    class _Response:
        data = [{"b64_json": "YWJj"}]
        usage = None

    async def fake_with_llm_retry(_func: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr("cognis.providers.llm.retry.with_llm_retry", fake_with_llm_retry)

    await provider.image_generate(prompt="draw", model="gpt-image-2")

    assert "response_format" not in captured
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_image_generate_keeps_response_format_for_other_models(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "default_model": "dall-e-3"},
                status="active",
            )
        )
        await session.commit()
    provider = LiteLLMProvider(session_factory)

    captured: dict[str, object] = {}

    class _Response:
        data = [{"b64_json": "YWJj"}]
        usage = None

    async def fake_with_llm_retry(_func: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr("cognis.providers.llm.retry.with_llm_retry", fake_with_llm_retry)

    await provider.image_generate(prompt="draw", model="dall-e-3")

    assert captured["response_format"] == "b64_json"
    await engine.dispose()


def test_litellm_provider_normalizes_gemini_content_part_images() -> None:
    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "data:image/png;base64,YWJj"},
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }

    result = LiteLLMProvider._normalize_gemini_image_response(_Response(), "gemini-image")

    assert len(result.images) == 1
    assert result.images[0].b64_json == "YWJj"
    assert result.images[0].content_type == "image/png"


@pytest.mark.asyncio
async def test_litellm_provider_transcribe_routes_to_executor(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="exec-openai",
                display_name="Executor OpenAI",
                location="executor",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-4o-mini",
                    "executor_labels": {"location": "local"},
                },
                status="active",
            )
        )
        session.add(
            ModelRouting(
                task_type="speech_to_text",
                provider_id="exec-openai",
                model="gpt-4o-mini-transcribe",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Router:
        async def route_transcribe(self, **kwargs: object):
            captured.update(kwargs)
            return SimpleNamespace(text="hello", model="gpt-4o-mini-transcribe")

    provider = LiteLLMProvider(session_factory, inference_router=_Router())

    result = await provider.transcribe(
        b"audio-bytes",
        mime_type="audio/ogg",
        filename="voice.ogg",
    )

    assert result.text == "hello"
    assert captured["model"] == "gpt-4o-mini-transcribe"
    assert captured["provider_preset"] == "openai"
    assert captured["executor_labels"] == {"location": "local"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_returns_model_info_from_provider_config(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "default_model": "gpt-4o-mini",
                    "models": [
                        {
                            "model_id": "gpt-4o-mini",
                            "context_window": 12345,
                            "max_output_tokens": 678,
                        }
                    ],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-4o-mini")

    assert model_info.context_window == 12345
    assert model_info.max_output_tokens == 678
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_infers_anthropic_capabilities(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="anthropic",
                display_name="Anthropic",
                location="controller",
                backend="litellm",
                config={"default_model": "claude-sonnet-4-20250514"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("claude-sonnet-4-20250514")

    assert model_info.supports_defer_loading is True
    assert model_info.supports_prompt_caching is True
    await engine.dispose()


def test_reasoning_translation_maps_openai_max_to_xhigh() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "max"},
        model_id="gpt-5.4",
        provider_preset="openai",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert prepared.request_kwargs["reasoning_effort"] == "xhigh"
    assert prepared.effective_effort == "xhigh"


def test_reasoning_translation_uses_adaptive_default_for_claude_46() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "default"},
        model_id="claude-opus-4.6",
        provider_preset="anthropic",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert prepared.request_kwargs["thinking"] == {"type": "adaptive"}
    assert prepared.effective_effort == "adaptive"


def test_reasoning_translation_drops_default_for_openai_models() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "default", "temperature": 0.2},
        model_id="gpt-5.4",
        provider_preset="openai",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert "reasoning_effort" not in prepared.request_kwargs
    assert "temperature" not in prepared.request_kwargs


def test_reasoning_translation_keeps_none_for_gpt5_models() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "none"},
        model_id="gpt-5.4",
        provider_preset="openai",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert prepared.request_kwargs["reasoning_effort"] == "none"
    assert prepared.effective_effort == "none"


def test_reasoning_translation_maps_none_to_google_thinking_budget_zero() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "none"},
        model_id="gemini-2.5-pro",
        provider_preset="gemini",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert prepared.request_kwargs["thinking_config"] == {"thinking_budget": 0}


def test_reasoning_translation_strips_sampling_and_translates_max_tokens() -> None:
    prepared = apply_reasoning_config(
        {
            "reasoning_effort": "low",
            "temperature": 0.3,
            "top_p": 0.9,
            "max_tokens": 222,
        },
        model_id="gpt-5.4",
        provider_preset="openai",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert "temperature" not in prepared.request_kwargs
    assert "top_p" not in prepared.request_kwargs
    assert "max_tokens" not in prepared.request_kwargs
    assert prepared.request_kwargs["max_completion_tokens"] == 222
    assert prepared.translated_max_tokens is True


def test_reasoning_translation_enforces_anthropic_budget_floor() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "medium", "max_tokens": 4000},
        model_id="claude-sonnet-4-20250514",
        provider_preset="anthropic",
        model_info=DEFAULT_MODEL_INFO.model_copy(
            update={
                "supports_reasoning": True,
                "supports_extended_thinking": True,
                "max_output_tokens": 20000,
            }
        ),
    )

    assert prepared.request_kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert prepared.request_kwargs["max_tokens"] == 12288


def test_reasoning_translation_preserves_anthropic_sampling_when_thinking_off() -> None:
    prepared = apply_reasoning_config(
        {"temperature": 0.3, "top_p": 0.9, "max_tokens": 4000},
        model_id="claude-sonnet-4-20250514",
        provider_preset="anthropic",
        model_info=DEFAULT_MODEL_INFO.model_copy(
            update={"supports_reasoning": True, "supports_extended_thinking": True}
        ),
    )

    assert prepared.request_kwargs["temperature"] == 0.3
    assert prepared.request_kwargs["top_p"] == 0.9
    assert prepared.request_kwargs["max_tokens"] == 4000
    assert prepared.effective_effort is None


def test_reasoning_translation_supports_haiku_when_model_info_is_explicit() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "low"},
        model_id="claude-3-5-haiku-20241022",
        provider_preset="anthropic",
        model_info=DEFAULT_MODEL_INFO.model_copy(
            update={"supports_reasoning": True, "supports_extended_thinking": True}
        ),
    )

    assert prepared.request_kwargs["thinking"] == {"type": "enabled", "budget_tokens": 2048}


def test_reasoning_translation_uses_output_config_for_claude_47() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "xhigh"},
        model_id="claude-opus-4-7",
        provider_preset="anthropic",
        model_info=DEFAULT_MODEL_INFO.model_copy(
            update={
                "supports_reasoning": True,
                "supports_extended_thinking": True,
                "display_name": "Claude Opus 4.7",
            }
        ),
    )

    assert prepared.family == "anthropic_adaptive"
    assert prepared.request_kwargs["thinking"] == {"type": "adaptive"}
    assert prepared.request_kwargs["output_config"] == {"effort": "xhigh"}
    assert prepared.effective_effort == "xhigh"


def test_reasoning_translation_uses_provider_preset_for_aliased_openai_reasoning_models() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "low", "max_tokens": 222},
        model_id="internal-reasoner",
        provider_preset="openai",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert prepared.family == "openai"
    assert prepared.request_kwargs["reasoning_effort"] == "low"
    assert prepared.request_kwargs["max_completion_tokens"] == 222


def test_reasoning_translation_skips_non_reasoning_claude_models() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "low"},
        model_id="claude-3-5-haiku-latest",
        provider_preset="anthropic",
        model_info=DEFAULT_MODEL_INFO.model_copy(
            update={"supports_reasoning": False, "supports_extended_thinking": False}
        ),
    )

    assert "thinking" not in prepared.request_kwargs
    assert prepared.effective_effort is None


def test_reasoning_translation_uses_adaptive_default_for_aliased_claude_46_models() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "default"},
        model_id="internal-claude",
        provider_preset="anthropic",
        model_info=DEFAULT_MODEL_INFO.model_copy(
            update={
                "supports_reasoning": True,
                "supports_extended_thinking": True,
                "display_name": "Claude Opus 4.6 Alias",
            }
        ),
    )

    assert prepared.family == "anthropic_adaptive"
    assert prepared.request_kwargs["thinking"] == {"type": "adaptive"}


def test_reasoning_translation_respects_explicit_false_for_matching_model_ids() -> None:
    prepared = apply_reasoning_config(
        {"reasoning_effort": "low", "temperature": 0.2, "top_p": 0.7},
        model_id="gpt-5.4",
        provider_preset="openai",
        model_info=DEFAULT_MODEL_INFO.model_copy(
            update={"model_id": "gpt-5.4", "supports_reasoning": False}
        ),
    )

    assert prepared.request_kwargs["temperature"] == 0.2
    assert prepared.request_kwargs["top_p"] == 0.7
    assert "reasoning_effort" not in prepared.request_kwargs


def test_reasoning_efforts_for_reasoning_model_return_normalized_levels() -> None:
    assert reasoning_efforts_for_model(
        "gpt-5.4", provider_preset="openai", supports_reasoning=True
    ) == ["default", "none", "low", "medium", "high", "xhigh"]


def test_reasoning_efforts_for_claude_46_exclude_xhigh() -> None:
    assert reasoning_efforts_for_model(
        "claude-opus-4-6", provider_preset="anthropic", supports_reasoning=True
    ) == ["default", "none", "low", "medium", "high", "max"]


def test_reasoning_efforts_for_claude_47_include_xhigh() -> None:
    assert reasoning_efforts_for_model(
        "claude-opus-4-7", provider_preset="anthropic", supports_reasoning=True
    ) == ["default", "none", "low", "medium", "high", "xhigh", "max"]


def test_reasoning_efforts_for_openai_alias_use_display_name_when_available() -> None:
    assert reasoning_efforts_for_model(
        "internal-reasoner",
        provider_preset="openai",
        model_info=DEFAULT_MODEL_INFO.model_copy(
            update={"supports_reasoning": True, "display_name": "GPT 5.4 Alias"}
        ),
        supports_reasoning=True,
    ) == ["default", "none", "low", "medium", "high", "xhigh"]


def test_reasoning_efforts_for_generic_reasoning_model_include_none() -> None:
    assert reasoning_efforts_for_model(
        "openrouter/kimi-k2-thinking",
        provider_preset="openrouter",
        supports_reasoning=True,
    ) == ["default", "none", "low", "medium", "high"]


def test_enrich_model_entry_infers_embedding_capability() -> None:
    entry = enrich_model_entry({"model_id": "text-embedding-3-small"}, provider_preset="openai")

    assert entry["supports_embedding"] is True


def test_enrich_model_entry_preserves_explicit_embedding_false() -> None:
    entry = enrich_model_entry(
        {"model_id": "text-embedding-3-small", "supports_embedding": False},
        provider_preset="openai",
    )

    assert entry["supports_embedding"] is False


def test_enrich_model_entry_infers_reasoning_efforts() -> None:
    entry = enrich_model_entry(
        {"model_id": "claude-sonnet-4-5"},
        provider_preset="openai_compatible",
    )

    assert entry["supports_reasoning"] is True
    assert "none" in entry["reasoning_efforts"]


def test_remap_reasoning_effort_to_available_prefers_closest_supported_level() -> None:
    assert (
        remap_reasoning_effort_to_available(
            "max",
            available_efforts=["default", "none", "low", "medium", "high", "xhigh"],
        )
        == "xhigh"
    )


def test_reasoning_efforts_respect_explicit_false() -> None:
    assert (
        reasoning_efforts_for_model("gpt-5.4", provider_preset="openai", supports_reasoning=False)
        == []
    )


def test_responses_request_kwargs_preserves_namespace_and_tool_search_tools() -> None:
    result = responses_request_kwargs(
        {
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp_github",
                    "description": "Deferred tools loaded from MCP server 'github'.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "mcp_github__search_issues",
                            "description": "search",
                            "parameters": {"type": "object", "properties": {}},
                            "defer_loading": True,
                        }
                    ],
                },
                {"type": "tool_search"},
            ]
        }
    )

    assert result["tools"][0]["type"] == "namespace"
    assert result["tools"][0]["tools"][0]["defer_loading"] is True
    assert result["tools"][1] == {"type": "tool_search"}


@pytest.mark.asyncio
async def test_litellm_provider_infers_openai_responses_capabilities_for_proxy_model(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                is_default=True,
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-5.4")

    assert model_info.supports_responses_api is True
    assert model_info.supports_tool_search is True
    assert model_info.supports_openai_namespace_tools is True
    assert model_info.supports_openai_allowed_tools is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_does_not_infer_tool_search_for_gpt5_mini(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "default_model": "gpt-5-mini"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-5-mini", provider_id="openai")

    assert model_info.supports_responses_api is True
    assert model_info.supports_tool_search is False
    assert model_info.supports_openai_namespace_tools is False
    assert model_info.supports_openai_allowed_tools is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_does_not_infer_native_apply_patch_for_compatible_proxy(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="compatible",
                display_name="Compatible",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai_compatible",
                    "default_model": "gpt-5.1-codex",
                    "models": [{"model_id": "gpt-5.1-codex", "supports_responses_api": True}],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-5.1-codex", provider_id="compatible")

    assert model_info.supports_responses_api is True
    assert model_info.supports_openai_apply_patch is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_infers_native_apply_patch_for_gpt55_direct_openai(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "default_model": "gpt-5.5"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-5.5", provider_id="openai")

    assert model_info.supports_responses_api is True
    assert model_info.supports_openai_apply_patch is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_resolves_native_apply_patch_toggle(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "use_native_apply_patch": False,
                    "models": [
                        {
                            "model_id": "gpt-5.1-codex",
                            "supports_responses_api": True,
                            "supports_openai_apply_patch": True,
                            "use_native_apply_patch": True,
                        }
                    ],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-5.1-codex", provider_id="openai")
    contract = await provider.resolve_tool_exposure_contract(
        model_id="gpt-5.1-codex",
        model_info=model_info,
        provider_id="openai",
        allow_tool_search=False,
    )

    assert model_info.supports_openai_apply_patch is True
    assert contract.native_apply_patch is True
    assert contract.native_apply_patch_reason == "enabled_by_config"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_explicitly_forces_native_apply_patch_without_capability(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "use_native_apply_patch": True,
                    "models": [
                        {
                            "model_id": "gpt-5.1-codex",
                            "supports_responses_api": True,
                            "supports_openai_apply_patch": False,
                        }
                    ],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-5.1-codex", provider_id="openai")
    contract = await provider.resolve_tool_exposure_contract(
        model_id="gpt-5.1-codex",
        model_info=model_info,
        provider_id="openai",
        allow_tool_search=False,
    )

    assert contract.native_apply_patch is True
    assert contract.native_apply_patch_reason == "enabled_by_config"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_applies_gpt5_metadata_floor_when_lookup_fails(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    monkeypatch.setattr(
        "cognis.providers.llm.litellm.litellm.get_model_info",
        lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-5.4", provider_id="openai")

    assert model_info.context_window == 1_050_000
    assert model_info.max_context_window == 1_050_000
    assert model_info.max_input_tokens == 922_000
    assert model_info.max_output_tokens == 128_000
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_returns_default_model_info_when_missing(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    model_info = await provider.get_model_info("missing-model")

    assert model_info == DEFAULT_MODEL_INFO
    await engine.dispose()


def test_litellm_provider_count_messages_tokens_falls_back_when_token_counter_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    provider = LiteLLMProvider(object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.litellm.token_counter",
        lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    count = provider.count_messages_tokens(
        [{"role": "user", "content": "hello world"}],
        "gpt-4o-mini",
    )

    assert count > 0


def test_litellm_provider_count_tokens_uses_litellm_for_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LiteLLMProvider(object())  # type: ignore[arg-type]
    captured: dict[str, object] = {}

    def _fake_counter(**kwargs: object) -> int:
        captured.update(kwargs)
        return 42

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.token_counter", _fake_counter)

    count = provider.count_tokens("hello world", "claude-sonnet-4-20250514")

    assert count == 42
    assert captured["model"] == "claude-sonnet-4-20250514"
    assert captured["messages"] == [{"role": "user", "content": "hello world"}]


def test_litellm_provider_count_tokens_falls_back_for_gemini_on_counter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LiteLLMProvider(object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.litellm.token_counter",
        lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    count = provider.count_tokens("hello world", "gemini-2.5-pro")

    assert count == max(1, len("hello world") // 4)


@pytest.mark.asyncio
async def test_litellm_provider_test_provider_sanitizes_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"default_model": "gpt-4o-mini"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_completion(**_: object) -> dict[str, object]:
        raise RuntimeError("api_key=secret-value sk-secret123")

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_completion)
    provider = LiteLLMProvider(session_factory)
    result = await provider.test_provider("default")

    assert result["ok"] is False
    assert "secret-value" not in str(result["error_detail"])
    assert "sk-secret123" not in str(result["error_detail"])
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_routes_executor_location_via_inference_router(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="remote",
                display_name="Remote OpenAI",
                location="executor",
                backend="litellm",
                config={
                    "default_model": "gpt-4o-mini",
                    "executor_id": "maitrea",
                    "executor_labels": {"location": "local"},
                },
                status="active",
            )
        )
        await session.commit()

    class Router:
        async def route_generate(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["model"] == "gpt-4o-mini"
            assert kwargs["executor_id"] == "maitrea"
            assert kwargs["executor_labels"] == {"location": "local"}
            return {"choices": [{"message": {"content": "hello"}}], "usage": {}}

    provider = LiteLLMProvider(session_factory, inference_router=Router())
    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o-mini",
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_test_reports_executor_backend_metadata(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="meridian-claude",
                display_name="Meridian Claude",
                location="executor",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "default_model": "claude-opus-4-7",
                    "executor_id": "olorin",
                    "base_url": "http://127.0.0.1:8090",
                    "api_base": "http://127.0.0.1:8090",
                    "auth_config": {"mode": "none"},
                },
                status="active",
            )
        )
        await session.commit()

    class Router:
        async def route_generate(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["backend"] == "litellm"
            assert kwargs["executor_id"] == "olorin"
            return {"choices": [{"message": {"content": "hello"}}], "usage": {}}

    provider = LiteLLMProvider(session_factory, inference_router=Router())

    result = await provider.test_provider("meridian-claude")
    assert result["ok"] is True, result
    assert result["executor_routed"] is True
    assert result["executor_id"] == "olorin"
    assert result["executor_backend"] == "litellm"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_merges_extra_headers(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="Anthropic",
                location="controller",
                backend="litellm",
                config={
                    "default_model": "claude-sonnet-4-20250514",
                    "extra_headers": {"x-provider": "configured"},
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    async def _fake_completion(**kwargs: object) -> object:
        captured.update(kwargs)

        class _Response:
            def model_dump(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "ok"}}]}

        return _Response()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_completion)
    try:
        provider = LiteLLMProvider(session_factory)
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-20250514",
            extra_headers={"anthropic-beta": "tool-search-tool-2025-10-19"},
        )
    finally:
        monkeypatch.undo()

    assert captured["extra_headers"] == {
        "x-provider": "configured",
        "anthropic-beta": "tool-search-tool-2025-10-19",
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_generate_applies_anthropic_cache_hint_to_first_message(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="Anthropic",
                location="controller",
                backend="litellm",
                config={"default_model": "claude-sonnet-4-20250514"},
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    async def _fake_completion(**kwargs: object) -> object:
        captured.update(kwargs)

        class _Response:
            def model_dump(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "ok"}}]}

        return _Response()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_completion)
    try:
        provider = LiteLLMProvider(session_factory)
        await provider.generate(
            messages=[
                {"role": "system", "content": "immutable prefix"},
                {"role": "system", "content": "mutable environment"},
                {"role": "user", "content": "hi"},
            ],
            model="claude-sonnet-4-20250514",
            cache_breakpoint_index=0,
        )
    finally:
        monkeypatch.undo()

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == [
        {
            "type": "text",
            "text": "immutable prefix",
            "cache_control": {"type": "ephemeral", "ttl": "5m"},
        }
    ]
    assert messages[1] == {"role": "system", "content": "mutable environment"}
    assert messages[2] == {"role": "user", "content": "hi"}
    await engine.dispose()


def test_apply_message_cache_hints_accepts_ordered_breakpoint_ttls() -> None:
    model_info = ModelInfo(
        model_id="anthropic/claude-sonnet-4-5",
        display_name="Claude Sonnet 4.5",
        supports_prompt_caching=True,
    )
    messages = [
        {"role": "system", "content": "prefix"},
        {"role": "user", "content": "prior turn"},
        {"role": "assistant", "content": "current"},
    ]

    result = litellm_provider_module._apply_message_cache_hints(
        messages,
        "anthropic/claude-sonnet-4-5",
        model_info,
        [
            {"index": 0, "ttl": "1h"},
            {"index": 1, "ttl": "5m"},
            {"index": 2, "ttl": "1h"},
        ],
    )

    assert result[0]["content"][-1]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
    }
    assert result[1]["content"][-1]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "5m",
    }
    assert result[2]["content"][-1]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "5m",
    }
    assert messages[0]["content"] == "prefix"


def test_prompt_cache_rejection_requires_400_class_status() -> None:
    class ProviderError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            super().__init__("unknown parameter: prompt_cache_key")

    assert litellm_provider_module._is_prompt_cache_key_rejected(ProviderError(400))
    assert not litellm_provider_module._is_prompt_cache_key_rejected(ProviderError(503))


def test_retryable_error_status_substring_requires_status_context() -> None:
    assert is_retryable_error(Exception("HTTP status 500 from provider"))
    assert not is_retryable_error(Exception("model-5000 exceeded an internal counter"))


@pytest.mark.asyncio
async def test_litellm_provider_projects_anthropic_developer_notices_to_hidden_user(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="anthropic",
                display_name="Anthropic",
                location="controller",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "default_model": "claude-sonnet-4-20250514",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    async def _fake_completion(**kwargs: object) -> object:
        captured.update(kwargs)

        class _Response:
            def model_dump(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "ok"}}]}

        return _Response()

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_completion)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix", "_immutable_prefix": True},
            {"role": "developer", "content": "operator instruction"},
            {
                "role": "system",
                "content": '<follow_up_event status="failed">Recover.</follow_up_event>',
                "_follow_up_context": True,
                "_audit_source": "follow_up_boundary",
                "_audit_role": "developer",
            },
        ],
        model="claude-sonnet-4-20250514",
        provider_id="anthropic",
        cache_breakpoint_index=0,
    )

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == [
        {
            "type": "text",
            "text": "immutable prefix",
            "cache_control": {"type": "ephemeral", "ttl": "5m"},
        }
    ]
    assert all(message["role"] != "developer" for message in messages)
    projected_users = [message for message in messages if message["role"] == "user"]
    assert len(projected_users) == 2
    assert projected_users[0]["content"].startswith("<system-notice")
    assert "operator instruction" in projected_users[0]["content"]
    assert "follow_up_boundary" in projected_users[1]["content"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_explicit_anthropic_projection_for_openai_compatible_provider(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="meridian",
                display_name="Meridian Claude",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai_compatible",
                    "message_projection_policy": "anthropic_messages",
                    "default_model": "claude-via-meridian",
                    "base_url": "http://127.0.0.1:8090",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    async def _fake_completion(**kwargs: object) -> object:
        captured.update(kwargs)

        class _Response:
            def model_dump(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "ok"}}]}

        return _Response()

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_completion)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "developer", "content": "operator instruction"},
            {"role": "user", "content": "hi"},
        ],
        model="claude-via-meridian",
        provider_id="meridian",
    )

    assert captured["model"] == "openai/claude-via-meridian"
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert all(message["role"] != "developer" for message in messages)
    assert messages[-2]["role"] == "user"
    assert messages[-2]["content"].startswith("<system-notice")
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_generate_skips_cache_hint_when_capability_disabled(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="Anthropic-ish Proxy",
                location="controller",
                backend="litellm",
                config={
                    "default_model": "claude-sonnet-4-20250514",
                    "models": [
                        {
                            "model_id": "claude-sonnet-4-20250514",
                            "supports_prompt_caching": False,
                        }
                    ],
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    async def _fake_completion(**kwargs: object) -> object:
        captured.update(kwargs)

        class _Response:
            def model_dump(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "ok"}}]}

        return _Response()

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_completion)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix"},
            {"role": "user", "content": "hi"},
        ],
        model="claude-sonnet-4-20250514",
        cache_breakpoint_index=0,
    )

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["content"] == [
        {
            "type": "text",
            "text": "immutable prefix",
            "cache_control": {"type": "ephemeral", "ttl": "5m"},
        }
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_tool_exposure_contract_marks_anthropic_preset_schema_compatible(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="meridian",
                display_name="Meridian",
                location="controller",
                backend="litellm",
                config={"preset": "anthropic", "default_model": "local-meridian-alias"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    contract = await provider.resolve_tool_exposure_contract(
        model_id="local-meridian-alias",
        model_info=ModelInfo(model_id="local-meridian-alias", supports_defer_loading=False),
        provider_id="meridian",
        allow_tool_search=True,
    )

    assert contract.anthropic_schema_compatible is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_tool_exposure_contract_disables_anthropic_defer_loading_for_custom_base_url(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="meridian",
                display_name="Meridian",
                location="controller",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "default_model": "local-meridian-alias",
                    "base_url": "http://127.0.0.1:8090",
                    "api_base": "http://127.0.0.1:8090",
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    contract = await provider.resolve_tool_exposure_contract(
        model_id="local-meridian-alias",
        model_info=ModelInfo(model_id="local-meridian-alias", supports_defer_loading=True),
        provider_id="meridian",
        allow_tool_search=True,
    )

    assert contract.anthropic_defer_loading is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_tool_exposure_contract_honors_custom_base_url_explicit_anthropic_defer_loading_true(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="meridian",
                display_name="Meridian",
                location="controller",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "default_model": "local-meridian-alias",
                    "base_url": "http://127.0.0.1:8090",
                    "api_base": "http://127.0.0.1:8090",
                    "anthropic_defer_loading": True,
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    contract = await provider.resolve_tool_exposure_contract(
        model_id="local-meridian-alias",
        model_info=ModelInfo(model_id="local-meridian-alias", supports_defer_loading=True),
        provider_id="meridian",
        allow_tool_search=True,
    )

    assert contract.anthropic_defer_loading is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_tool_exposure_contract_respects_explicit_anthropic_defer_loading_false(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="meridian",
                display_name="Meridian",
                location="controller",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "default_model": "local-meridian-alias",
                    "base_url": "http://127.0.0.1:8090",
                    "api_base": "http://127.0.0.1:8090",
                    "anthropic_defer_loading": False,
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    contract = await provider.resolve_tool_exposure_contract(
        model_id="local-meridian-alias",
        model_info=ModelInfo(model_id="local-meridian-alias", supports_defer_loading=True),
        provider_id="meridian",
        allow_tool_search=True,
    )

    assert contract.anthropic_defer_loading is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_tool_exposure_contract_enables_anthropic_defer_loading_for_standard_anthropic_base_url(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="anthropic",
                display_name="Anthropic",
                location="controller",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "default_model": "claude-sonnet-4",
                    "base_url": "https://api.anthropic.com",
                    "api_base": "https://api.anthropic.com",
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    contract = await provider.resolve_tool_exposure_contract(
        model_id="claude-sonnet-4",
        model_info=ModelInfo(model_id="claude-sonnet-4", supports_defer_loading=True),
        provider_id="anthropic",
        allow_tool_search=True,
    )

    assert contract.anthropic_defer_loading is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_generate_uses_responses_bridge_for_supported_model(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "hello"}]}
                ],
                "usage": {"total_tokens": 3},
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    result = await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix"},
            {"role": "system", "content": "mutable environment"},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.4",
        max_tokens=123,
    )

    assert captured["input"] == [
        {"role": "system", "content": "immutable prefix"},
        {"role": "system", "content": "mutable environment"},
        {"role": "user", "content": "hi"},
    ]
    assert captured["max_output_tokens"] == 123
    assert result["choices"][0]["message"]["content"] == "hello"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_responses_sets_cache_key_and_store_false(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "prompt_cache_retention": "24h",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "hello"}]}
                ],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix"},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.4",
        max_tokens=32,
        cache_breakpoint_index=0,
    )

    assert captured["instructions"] == "immutable prefix"
    assert captured["input"] == [{"role": "user", "content": "hi"}]
    assert captured["store"] is False
    assert captured["include"] == ["reasoning.encrypted_content"]
    assert str(captured["prompt_cache_key"]).startswith("cognis-")
    assert captured["prompt_cache_retention"] == "24h"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_responses_respects_provider_cache_overrides(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "responses_store": True,
                    "prompt_cache_key": "provider-cache-key",
                    "prompt_cache_retention": "1h",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix"},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.4",
        max_tokens=32,
        cache_breakpoint_index=0,
    )

    assert captured["store"] is True
    assert captured["prompt_cache_key"] == "provider-cache-key"
    assert captured["prompt_cache_retention"] == "1h"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_chatgpt_responses_sets_default_cache_retention(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="codex",
                display_name="Codex",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.5",
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.delenv("COGNIS_CHATGPT_PROMPT_CACHE_RETENTION", raising=False)
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr(
        "cognis.providers.llm.litellm._looks_like_chatgpt_oauth_provider",
        lambda provider: provider is not None and provider.provider_id == "codex",
    )
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.LiteLLMProvider._provider_oauth_token_context",
        lambda self, provider: contextlib.nullcontext(),
    )

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix"},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.5",
        max_tokens=32,
        cache_breakpoint_index=0,
    )

    assert "prompt_cache_key" not in captured
    assert "prompt_cache_retention" not in captured
    assert "store" not in captured
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_chatgpt_responses_cache_retention_env_override(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="codex",
                display_name="Codex",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.5",
                    "use_prompt_cache_key": True,
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setenv("COGNIS_CHATGPT_PROMPT_CACHE_KEY_ENABLED", "true")
    monkeypatch.setenv("COGNIS_CHATGPT_PROMPT_CACHE_RETENTION", "24h")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr(
        "cognis.providers.llm.litellm._looks_like_chatgpt_oauth_provider",
        lambda provider: provider is not None and provider.provider_id == "codex",
    )
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.LiteLLMProvider._provider_oauth_token_context",
        lambda self, provider: contextlib.nullcontext(),
    )

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix"},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.5",
        max_tokens=32,
        cache_breakpoint_index=0,
    )

    assert captured["prompt_cache_retention"] == "24h"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_responses_respects_use_prompt_cache_key_false(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "prompt_cache_key": "provider-cache-key",
                    "prompt_cache_retention": "1h",
                    "use_prompt_cache_key": False,
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix"},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.4",
        max_tokens=32,
        cache_breakpoint_index=0,
    )

    assert "prompt_cache_key" not in captured
    assert "prompt_cache_retention" not in captured
    await engine.dispose()


def test_response_cache_observation_status() -> None:
    from cognis.providers.llm.litellm import _response_cache_observation_status

    assert (
        _response_cache_observation_status(
            cached_tokens=0,
            explicit_cache_key_present=True,
        )
        == "miss"
    )
    assert (
        _response_cache_observation_status(
            cached_tokens=128,
            explicit_cache_key_present=True,
        )
        == "hit_with_explicit_key"
    )
    assert (
        _response_cache_observation_status(
            cached_tokens=128,
            explicit_cache_key_present=False,
        )
        == "hit_without_explicit_key"
    )


def test_exception_detail_redacts_provider_error_body_preview() -> None:
    from cognis.providers.llm.litellm import _exception_detail

    class _Error(Exception):
        def __init__(self) -> None:
            super().__init__("provider failed")
            self.body = (
                "api_key=sk-secret123 access_token=tok-secret "
                "Authorization: Bearer bearer-secret https://user:pass@example.test/path"
            )

    detail = _exception_detail(_Error())
    preview = str(detail["provider_error_body_preview"])

    assert "sk-secret123" not in preview
    assert "tok-secret" not in preview
    assert "bearer-secret" not in preview
    assert "user:pass" not in preview
    assert "[redacted" in preview


@pytest.mark.asyncio
async def test_litellm_provider_chatgpt_response_diagnostics_include_cache_status(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="codex",
                display_name="Codex",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.5",
                    "use_prompt_cache_key": True,
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 1,
                    "total_tokens": 101,
                    "input_tokens_details": {"cached_tokens": 80},
                },
            },
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    caplog.set_level(logging.INFO, logger="cognis.providers.llm.litellm")
    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr(
        "cognis.providers.llm.litellm._looks_like_chatgpt_oauth_provider",
        lambda provider: provider is not None and provider.provider_id == "codex",
    )
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.LiteLLMProvider._provider_oauth_token_context",
        lambda self, provider: contextlib.nullcontext(),
    )

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[
                {"role": "system", "content": "immutable prefix"},
                {"role": "user", "content": "hi"},
            ],
            model="gpt-5.5",
            max_tokens=32,
            cache_breakpoint_index=0,
        )
    ]

    assert chunks
    completion_records = [
        record for record in caplog.records if record.message == "LLM stream request completed"
    ]
    assert completion_records
    payload = completion_records[-1].__dict__["extra_data"]
    assert payload["request_diagnostics_stage"] == "responses_build"
    assert payload["prompt_cache_key_status"] == "sent"
    assert payload["cache_observation_status"] == "hit_with_explicit_key"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_prompt_cache_rejection_updates_diagnostics(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="codex",
                display_name="Codex",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex-spark",
                    "use_prompt_cache_key": True,
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    calls: list[dict[str, object]] = []

    async def _fake_stream() -> object:
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
            },
        }

    async def _fake_aresponses(**kwargs: object) -> object:
        calls.append(kwargs)
        if "prompt_cache_key" in kwargs:
            raise _ProviderError("Unknown parameter: 'prompt_cache_key'", status_code=400)
        return _fake_stream()

    caplog.set_level(logging.INFO, logger="cognis.providers.llm.litellm")
    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr(
        "cognis.providers.llm.litellm._looks_like_chatgpt_oauth_provider",
        lambda provider: provider is not None and provider.provider_id == "codex",
    )
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.LiteLLMProvider._provider_oauth_token_context",
        lambda self, provider: contextlib.nullcontext(),
    )

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[
                {"role": "system", "content": "immutable prefix"},
                {"role": "user", "content": "hi"},
            ],
            model="gpt-5.3-codex-spark",
            max_tokens=32,
            cache_breakpoint_index=0,
        )
    ]

    assert chunks
    assert len(calls) == 2
    assert "prompt_cache_key" in calls[0]
    assert "prompt_cache_key" not in calls[1]
    completion_records = [
        record for record in caplog.records if record.message == "LLM stream request completed"
    ]
    assert completion_records
    payload = completion_records[-1].__dict__["extra_data"]
    assert payload["prompt_cache_key_present"] is False
    assert payload["prompt_cache_key_status"] == "disabled_after_backend_rejection"
    assert "prompt_cache_key_hash" not in payload
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_logs_responses_event_diagnostics(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="codex",
                display_name="Codex",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.5",
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {"type": "response.created", "response": {"status": "in_progress"}}
        yield {"type": "response.reasoning_summary_text.delta", "delta": "thinking"}
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
            },
        }

    async def _fake_aresponses(**kwargs: object) -> object:
        return _fake_stream()

    caplog.set_level(logging.INFO, logger="cognis.providers.llm.litellm")
    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr(
        "cognis.providers.llm.litellm._looks_like_chatgpt_oauth_provider",
        lambda provider: provider is not None and provider.provider_id == "codex",
    )
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.LiteLLMProvider._provider_oauth_token_context",
        lambda self, provider: contextlib.nullcontext(),
    )

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.5",
            max_tokens=32,
        )
    ]

    assert chunks
    completion_records = [
        record for record in caplog.records if record.message == "LLM stream request completed"
    ]
    assert completion_records
    payload = completion_records[-1].__dict__["extra_data"]
    assert payload["provider_event_counts"]["response.completed"] == 1
    assert payload["provider_event_counts"]["response.reasoning_summary_text.delta"] == 1
    assert payload["recent_provider_event_types"][-1] == "response.completed"
    assert payload["response_completed_seen"] is True
    assert payload["response_failed_seen"] is False
    assert payload["reasoning_chunk_count"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_serializes_tool_call_models(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="anthropic",
                display_name="Anthropic",
                location="controller",
                backend="litellm",
                config={"preset": "anthropic", "default_model": "claude-opus-4-7"},
                status="active",
            )
        )
        await session.commit()

    class ToolCall:
        def model_dump(self, **_: object) -> dict[str, object]:
            return {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
            }

    async def _fake_stream() -> object:
        yield {"choices": [{"delta": {"tool_calls": [ToolCall()]}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {}}

    async def _fake_acompletion(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            [{"role": "user", "content": "hi"}], model="claude-opus-4-7"
        )
    ]

    json.dumps(chunks[0])
    assert chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "bash"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_responses_respects_call_cache_overrides(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "responses_store": False,
                    "prompt_cache_key": "provider-cache-key",
                    "prompt_cache_retention": "1h",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "immutable prefix"},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.4",
        max_tokens=32,
        cache_breakpoint_index=0,
        store=True,
        prompt_cache_key="call-cache-key",
        prompt_cache_retention="2h",
    )

    assert captured["store"] is True
    assert captured["prompt_cache_key"] == "call-cache-key"
    assert captured["prompt_cache_retention"] == "2h"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_responses_bridge_translates_tools_shape(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {"status": "completed", "output": []}

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_tools",
                    "description": "Search tools",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert captured["tools"] == [
        {
            "type": "function",
            "name": "search_tools",
            "description": "Search tools",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_normalizes_responses_events(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Hello"}
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search_tools",
            },
        }
        yield {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '{"query":"docs"}',
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 7}},
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
        )
    ]

    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello"
    assert chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "search_tools"
    assert (
        chunks[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        == '{"query":"docs"}'
    )
    assert chunks[-1]["usage"]["total_tokens"] == 7
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_suppresses_unbound_responses_output_text(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {"type": "response.output_text.delta", "delta": "Need commit."}
        yield {"type": "response.output_text.done", "text": "Need commit."}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 4}},
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
        )
    ]

    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )
    assert chunks[-1]["usage"]["total_tokens"] == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_emits_message_item_text_without_output_delta(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello from item"}],
            },
        }
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Hello from item"}],
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
        )
    ]

    choices_chunks = [chunk for chunk in chunks if chunk.get("choices")]
    assert choices_chunks[0]["choices"][0]["delta"]["content"] == "Hello from item"
    assert chunks[-1]["usage"]["total_tokens"] == 5
    assert len(choices_chunks) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_emits_output_text_done_without_delta(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_done", "content": []},
        }
        yield {
            "type": "response.output_text.done",
            "item_id": "msg_done",
            "text": "Hello from done",
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 4}},
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
        )
    ]

    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello from done"
    assert chunks[-1]["usage"]["total_tokens"] == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_normalizes_enum_style_event_types(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {
            "type": "ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED",
            "item": {"type": "message", "id": "msg_enum", "content": []},
        }
        yield {
            "type": "ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA",
            "item_id": "msg_enum",
            "delta": "Hello",
        }
        yield {
            "type": "ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE",
            "item_id": "msg_enum",
            "text": "Hello",
        }
        yield {
            "type": "ResponsesAPIStreamEvents.RESPONSE_COMPLETED",
            "response": {"status": "completed", "usage": {"total_tokens": 4}},
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
        )
    ]

    text_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("choices") and chunk["choices"][0]["delta"].get("content")
    ]
    assert len(text_chunks) == 1
    assert text_chunks[0]["choices"][0]["delta"]["content"] == "Hello"
    assert chunks[-1]["usage"]["total_tokens"] == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_does_not_duplicate_output_text_done(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_dedupe", "content": []},
        }
        yield {"type": "response.output_text.delta", "item_id": "msg_dedupe", "delta": "Hello"}
        yield {"type": "response.output_text.done", "item_id": "msg_dedupe", "text": "Hello"}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 4}},
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
        )
    ]

    text_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("choices") and chunk["choices"][0]["delta"].get("content")
    ]
    assert len(text_chunks) == 1
    assert text_chunks[0]["choices"][0]["delta"]["content"] == "Hello"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_emits_content_part_done_text(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_part", "content": []},
        }
        yield {
            "type": "response.content_part.done",
            "item_id": "msg_part",
            "part": {"type": "output_text", "text": "Hello from content part"},
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 4}},
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
        )
    ]

    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello from content part"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_stream_generate_emits_reasoning_text(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_stream() -> object:
        yield {"type": "response.reasoning_text.delta", "delta": '{"decision":"revise"}'}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 4}},
        }

    async def _fake_aresponses(**_: object) -> object:
        return _fake_stream()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
        )
    ]

    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == '{"decision":"revise"}'
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_responses_bridge_maps_response_format_to_text_format(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"ok": true}'},
                        ],
                    }
                ],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        response_format={"type": "json_object"},
    )

    assert captured["text"] == {"format": {"type": "json_object"}}
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_responses_bridge_enables_reasoning_summary_by_default(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "ok"},
                        ],
                    }
                ],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        reasoning_effort="low",
    )

    assert captured["reasoning"] == {"effort": "low", "summary": "auto"}
    assert "reasoning_effort" not in captured
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_responses_respects_disabled_reasoning_summary(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                    "models": [
                        {
                            "model_id": "gpt-5.4",
                            "default_reasoning_summary": "none",
                        }
                    ],
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        reasoning_effort="low",
    )

    assert captured["reasoning"] == {"effort": "low"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_generate_preserves_reasoning_only_payload(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": '{"decision":"revise"}'}],
                    }
                ],
            }

    async def _fake_aresponses(**_: object) -> object:
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
    )

    assert result["choices"][0]["message"]["content"] is None
    assert result["choices"][0]["message"]["reasoning_content"] == '{"decision":"revise"}'
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_logs_do_not_include_header_values(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="Anthropic",
                location="controller",
                backend="litellm",
                config={
                    "default_model": "claude-sonnet-4-20250514",
                    "extra_headers": {"x-provider": "secret-header-value"},
                },
                status="active",
            )
        )
        await session.commit()

    async def _fake_completion(**_: object) -> object:
        class _Response:
            def model_dump(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "ok"}}]}

        return _Response()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_completion)
    caplog.set_level("DEBUG")
    try:
        provider = LiteLLMProvider(session_factory)
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-20250514",
        )
    finally:
        monkeypatch.undo()

    assert "secret-header-value" not in caplog.text
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_discover_models_rejects_executor_location(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="remote",
                display_name="Remote OpenAI",
                location="executor",
                backend="litellm",
                config={"default_model": "gpt-4o-mini"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    with pytest.raises(ValueError, match="controller-side providers"):
        await provider.discover_models("remote")
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_health_reports_unhealthy_without_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    health = await provider.health()

    assert health.status == "unhealthy"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_cached_resolution_expires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(ModelRouting(task_type="default", provider_id=None, model="gpt-4o-mini"))
        await session.commit()

    time_points = iter([1.0, 1.0, 70.0, 70.0, 70.0])
    monkeypatch.setattr("cognis.providers.llm.litellm.monotonic", lambda: next(time_points))

    provider = LiteLLMProvider(session_factory)
    assert await provider.resolve_model(task_type="default") == "gpt-4o-mini"

    async with session_factory() as session:
        row = (
            await session.execute(select(ModelRouting).where(ModelRouting.task_type == "default"))
        ).scalar_one_or_none()
        assert row is not None
        row.model = "gpt-5.4-mini"
        await session.commit()

    assert await provider.resolve_model(task_type="default") == "gpt-5.4-mini"
    await engine.dispose()


# ---------------------------------------------------------------------------
# _apply_model_prefix tests
# ---------------------------------------------------------------------------


def _make_provider_row(preset: str) -> LLMProvider:
    """Create a minimal LLMProvider ORM instance with a given preset."""
    return LLMProvider(
        provider_id="test",
        display_name="Test",
        location="controller",
        backend="litellm",
        config={"preset": preset, "default_model": "some-model"},
        status="active",
    )


def test_apply_model_prefix_returns_unchanged_when_provider_is_none() -> None:
    assert LiteLLMProvider._apply_model_prefix("gpt-4o", None) == "gpt-4o"


def test_apply_model_prefix_returns_unchanged_when_model_contains_slash() -> None:
    provider = _make_provider_row("openai_compatible")
    assert LiteLLMProvider._apply_model_prefix("ollama/llama3", provider) == "ollama/llama3"


def test_apply_model_prefix_adds_openai_prefix_for_openai_compatible() -> None:
    provider = _make_provider_row("openai_compatible")
    assert LiteLLMProvider._apply_model_prefix("gpt-oss-120b", provider) == "openai/gpt-oss-120b"


def test_apply_model_prefix_adds_litellm_proxy_prefix() -> None:
    provider = _make_provider_row("litellm_proxy")
    assert (
        LiteLLMProvider._apply_model_prefix("gpt-oss-120b", provider)
        == "litellm_proxy/gpt-oss-120b"
    )


def test_apply_model_prefix_no_prefix_for_standard_openai() -> None:
    provider = _make_provider_row("openai")
    assert LiteLLMProvider._apply_model_prefix("gpt-4o", provider) == "gpt-4o"


def test_apply_model_prefix_no_prefix_for_anthropic() -> None:
    provider = _make_provider_row("anthropic")
    assert (
        LiteLLMProvider._apply_model_prefix("claude-sonnet-4-20250514", provider)
        == "claude-sonnet-4-20250514"
    )


def test_apply_model_prefix_no_prefix_for_unknown_preset() -> None:
    provider = _make_provider_row("some_future_preset")
    assert LiteLLMProvider._apply_model_prefix("my-model", provider) == "my-model"


def test_apply_model_prefix_no_prefix_when_preset_missing() -> None:
    provider = LLMProvider(
        provider_id="test",
        display_name="Test",
        location="controller",
        backend="litellm",
        config={"default_model": "my-model"},
        status="active",
    )
    assert LiteLLMProvider._apply_model_prefix("my-model", provider) == "my-model"


def test_transcription_wire_model_preserves_prefixed_model_for_litellm_proxy() -> None:
    assert (
        LiteLLMProvider._transcription_wire_model(
            "openai/gpt-4o-transcribe",
            "litellm_proxy",
        )
        == "openai/gpt-4o-transcribe"
    )


def test_transcription_wire_model_strips_openai_prefix_for_openai() -> None:
    assert (
        LiteLLMProvider._transcription_wire_model(
            "openai/gpt-4o-transcribe",
            "openai",
        )
        == "gpt-4o-transcribe"
    )


# ---------------------------------------------------------------------------
# Proxy model info tests
# ---------------------------------------------------------------------------


def test_normalize_proxy_model_info_maps_fields() -> None:
    raw = {
        "max_input_tokens": 1048576,
        "max_output_tokens": 32768,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_audio_input": False,
        "supports_image_generation": True,
        "supports_pdf_input": True,
        "supports_reasoning": False,
        "supports_extended_thinking": True,
        "supports_prompt_caching": True,
        "supports_openai_namespace_tools": True,
        "input_cost_per_token": 0.0000025,
        "output_cost_per_token": 0.00001,
    }
    result = _normalize_proxy_model_info(raw)
    assert result["context_window"] == 1048576
    assert result["max_input_tokens"] == 1048576
    assert result["max_output_tokens"] == 32768
    assert result["supports_tools"] is True
    assert result["supports_vision"] is True
    assert result["supports_audio_input"] is False
    assert result["supports_image_generation"] is True
    assert result["supports_pdf_input"] is True
    assert result["supports_reasoning"] is False
    assert result["supports_extended_thinking"] is True
    assert result["supports_prompt_caching"] is True
    assert result["supports_openai_namespace_tools"] is True
    assert result["input_cost_per_mtok"] == 2.5
    assert result["output_cost_per_mtok"] == 10.0


def test_normalize_proxy_model_info_cost_rounding() -> None:
    raw = {"input_cost_per_token": 0.0000003, "output_cost_per_token": 0.0000012}
    result = _normalize_proxy_model_info(raw)
    assert result["input_cost_per_mtok"] == 0.3
    assert result["output_cost_per_mtok"] == 1.2


def test_normalize_proxy_model_info_empty_input() -> None:
    result = _normalize_proxy_model_info({})
    assert result == {}


def test_normalize_proxy_model_info_max_tokens_fallback() -> None:
    raw = {"max_tokens": 4096}
    result = _normalize_proxy_model_info(raw)
    assert result["context_window"] == 4096


def test_normalize_proxy_model_info_preserves_split_limits() -> None:
    raw = {
        "max_context_window_tokens": 400000,
        "max_input_tokens": 272000,
        "max_output_tokens": 128000,
    }
    result = _normalize_proxy_model_info(raw)
    assert result["context_window"] == 400000
    assert result["max_context_window"] == 400000
    assert result["max_input_tokens"] == 272000
    assert result["max_output_tokens"] == 128000


@pytest.mark.asyncio
async def test_fetch_proxy_model_info_success(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    async def _fake_get(self, url, **kwargs):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": [
                        {
                            "model_name": "gpt-5.4",
                            "model_info": {
                                "max_input_tokens": 1048576,
                                "max_output_tokens": 32768,
                                "supports_function_calling": True,
                                "supports_vision": True,
                            },
                        },
                        {
                            "model_name": "gpt-4o-mini",
                            "model_info": {
                                "max_input_tokens": 128000,
                                "max_output_tokens": 16384,
                            },
                        },
                    ]
                }

        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    result = await provider._fetch_proxy_model_info("http://localhost:4000", "test-key")
    assert "gpt-5.4" in result
    assert result["gpt-5.4"]["context_window"] == 1048576
    assert result["gpt-5.4"]["max_output_tokens"] == 32768
    assert "gpt-4o-mini" in result
    assert result["gpt-4o-mini"]["context_window"] == 128000
    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_proxy_model_info_failure_returns_empty(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    async def _fake_get(self, url, **kwargs):
        raise ConnectionError("proxy down")

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    result = await provider._fetch_proxy_model_info("http://localhost:4000", "test-key")
    assert result == {}
    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_proxy_model_info_negative_cache(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    call_count = 0

    async def _fake_get(self, url, **kwargs):
        nonlocal call_count
        call_count += 1
        raise ConnectionError("proxy down")

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    # First call: makes HTTP request, fails, caches empty result
    result1 = await provider._fetch_proxy_model_info("http://localhost:4000", "key")
    assert result1 == {}
    assert call_count == 1

    # Second call: should hit negative cache, no HTTP request
    result2 = await provider._fetch_proxy_model_info("http://localhost:4000", "key")
    assert result2 == {}
    assert call_count == 1  # No additional HTTP call
    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_proxy_model_info_cache_isolated_by_api_key(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    call_count = 0

    async def _fake_get(self, url, **kwargs):
        nonlocal call_count
        call_count += 1

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"model_name": "m1", "model_info": {"max_input_tokens": 100}}]}

        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    await provider._fetch_proxy_model_info("http://localhost:4000", "key-a")
    await provider._fetch_proxy_model_info("http://localhost:4000", "key-b")

    assert call_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_proxy_model_info_bypass_cache(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    call_count = 0

    async def _fake_get(self, url, **kwargs):
        nonlocal call_count
        call_count += 1

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"model_name": "m1", "model_info": {"max_input_tokens": 100}}]}

        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    # First call populates cache
    await provider._fetch_proxy_model_info("http://localhost:4000", "key")
    assert call_count == 1

    # Second call with bypass_cache=True should make another HTTP request
    await provider._fetch_proxy_model_info("http://localhost:4000", "key", bypass_cache=True)
    assert call_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_merge_proxy_overrides_litellm_static(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proxy /model/info data should override litellm static data."""
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={
                    "preset": "litellm_proxy",
                    "default_model": "gpt-5.4",
                    "base_url": "http://localhost:4000",
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    # Mock proxy to return 1M context window
    async def _fake_get(self, url, **kwargs):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": [
                        {
                            "model_name": "gpt-5.4",
                            "model_info": {
                                "max_input_tokens": 1048576,
                                "max_output_tokens": 65536,
                                "supports_function_calling": True,
                                "supports_vision": True,
                            },
                        }
                    ]
                }

        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    model_info = await provider.get_model_info("gpt-5.4")

    # Proxy data (1M) should override whatever litellm static returns
    assert model_info.context_window == 1048576
    assert model_info.max_input_tokens == 1048576
    assert model_info.max_output_tokens == 65536
    assert model_info.supports_vision is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_anthropic_custom_base_url_discovers_openai_compatible_models(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)
    requested_urls: list[str] = []

    async def _fake_get(self, url, **kwargs):
        del self, kwargs
        requested_urls.append(str(url))

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "claude-opus-4-7"}]}

        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    models = await provider._discover_models_remote(
        "anthropic",
        "http://localhost:4000",
        "key",
    )

    assert requested_urls == ["http://localhost:4000/v1/models"]
    assert models[0]["model_id"] == "claude-opus-4-7"
    await engine.dispose()


@pytest.mark.asyncio
async def test_merge_user_config_overrides_proxy(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User-configured overrides in DB should win over proxy data."""
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={
                    "preset": "litellm_proxy",
                    "default_model": "gpt-5.4",
                    "base_url": "http://localhost:4000",
                    "models": [{"model_id": "gpt-5.4", "context_window": 500000}],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    # Mock proxy to return 1M context window
    async def _fake_get(self, url, **kwargs):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": [
                        {
                            "model_name": "gpt-5.4",
                            "model_info": {"max_input_tokens": 1048576},
                        }
                    ]
                }

        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    model_info = await provider.get_model_info("gpt-5.4")

    # User-configured 500k should win over proxy's 1M
    assert model_info.context_window == 500000
    await engine.dispose()


@pytest.mark.asyncio
async def test_enrich_model_info_public_method(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "default_model": "gpt-4o-mini"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.enrich_model_info("gpt-4o-mini", provider_id="openai")

    assert model_info.model_id == "gpt-4o-mini"
    # Should have some reasonable context window (not the 8192 default)
    assert model_info.context_window > 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_enrich_model_info_preview_mode(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    model_info = await provider.enrich_model_info("gpt-4o-mini", preset="openai")

    assert model_info.model_id == "gpt-4o-mini"
    assert model_info.context_window > 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_find_provider_for_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-4o-mini",
                    "models": [{"model_id": "gpt-4o-mini"}, {"model_id": "gpt-4o"}],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.find_provider_for_model("gpt-4o-mini") == "openai"
    assert await provider.find_provider_for_model("gpt-4o") == "openai"
    assert await provider.find_provider_for_model("nonexistent") is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_find_provider_for_model_honors_owner_visibility(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add_all(
            [
                LLMProvider(
                    provider_id="private",
                    display_name="Private",
                    location="controller",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={"preset": "openai", "models": [{"model_id": "private-model"}]},
                    status="active",
                ),
                LLMProvider(
                    provider_id="shared",
                    display_name="Shared",
                    location="controller",
                    backend="litellm",
                    owner_email=SYSTEM_USER_EMAIL,
                    config={"preset": "openai", "models": [{"model_id": "shared-model"}]},
                    status="active",
                ),
            ]
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert (
        await provider.find_provider_for_model(
            "private-model", acting_user_email="owner@example.com"
        )
        == "private"
    )
    assert (
        await provider.find_provider_for_model(
            "private-model", acting_user_email="other@example.com"
        )
        is None
    )
    assert (
        await provider.find_provider_for_model(
            "shared-model", acting_user_email="other@example.com"
        )
        == "shared"
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_find_provider_for_model_ignores_disabled_provider(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add_all(
            [
                LLMProvider(
                    provider_id="disabled",
                    display_name="Disabled",
                    location="controller",
                    backend="litellm",
                    config={"preset": "openai", "models": [{"model_id": "shared-model"}]},
                    is_default=True,
                    status="disabled",
                ),
                LLMProvider(
                    provider_id="active",
                    display_name="Active",
                    location="controller",
                    backend="litellm",
                    config={"preset": "openai", "models": [{"model_id": "shared-model"}]},
                    status="active",
                ),
            ]
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.find_provider_for_model("shared-model") == "active"
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_model_default_id_fallback_honors_owner_visibility(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="Private Default",
                location="controller",
                backend="litellm",
                owner_email="owner@example.com",
                config={"preset": "openai", "default_model": "private-model"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert (
        await provider.resolve_model(task_type="default", acting_user_email="owner@example.com")
        == "private-model"
    )
    with pytest.raises(ValueError, match="No LLM model configured"):
        await provider.resolve_model(task_type="default", acting_user_email="other@example.com")
    await engine.dispose()


@pytest.mark.asyncio
async def test_find_provider_for_model_prefers_single_default_provider(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add_all(
            [
                LLMProvider(
                    provider_id="zzz",
                    display_name="Zed",
                    location="controller",
                    backend="litellm",
                    config={"preset": "openai", "models": [{"model_id": "gpt-5.4"}]},
                    status="active",
                ),
                LLMProvider(
                    provider_id="aaa",
                    display_name="Aye",
                    location="controller",
                    backend="litellm",
                    config={"preset": "openai", "models": [{"model_id": "gpt-5.4"}]},
                    is_default=True,
                    status="active",
                ),
            ]
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.find_provider_for_model("gpt-5.4") == "aaa"
    await engine.dispose()


@pytest.mark.asyncio
async def test_find_provider_for_model_rejects_ambiguous_duplicates(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add_all(
            [
                LLMProvider(
                    provider_id="aaa",
                    display_name="Aye",
                    location="controller",
                    backend="litellm",
                    config={"preset": "openai", "models": [{"model_id": "shared-model"}]},
                    status="active",
                ),
                LLMProvider(
                    provider_id="zzz",
                    display_name="Zed",
                    location="controller",
                    backend="litellm",
                    config={"preset": "openai", "models": [{"model_id": "shared-model"}]},
                    status="active",
                ),
            ]
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.find_provider_for_model("shared-model") is None
    with pytest.raises(ValueError, match="ambiguous"):
        await provider.resolve_model_reference("shared-model")
    assert await provider.resolve_model_reference("aaa/shared-model") == (
        "shared-model",
        "aaa",
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_model_references_uses_provider_qualified_values(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="anthropic-sub",
                display_name="Claude Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "anthropic",
                    "default_model": "claude-sonnet-5",
                    "models": [{"model_id": "claude-fable-5"}],
                },
                is_default=True,
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    refs = await provider.list_model_references()
    assert refs == [
        {
            "provider_id": "anthropic-sub",
            "provider_display_name": "Claude Subscription",
            "model_id": "claude-sonnet-5",
            "value": "anthropic-sub/claude-sonnet-5",
            "is_default_provider": True,
            "is_default_model": True,
        },
        {
            "provider_id": "anthropic-sub",
            "provider_display_name": "Claude Subscription",
            "model_id": "claude-fable-5",
            "value": "anthropic-sub/claude-fable-5",
            "is_default_provider": True,
            "is_default_model": False,
        },
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_find_provider_for_model_recovers_from_stale_provider_cache(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "models": [{"model_id": "gpt-5.4"}]},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    await provider._set_cached_provider_id("gpt-5.4", SYSTEM_USER_EMAIL, "missing-provider")

    async with session_factory() as session:
        row = await provider._find_provider_for_model(session, "gpt-5.4")

    assert row is not None
    assert row.provider_id == "openai"
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolved_model_cache_preserves_pinned_route_provider(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add_all(
            [
                LLMProvider(
                    provider_id="provider-a",
                    display_name="Provider A",
                    location="controller",
                    backend="litellm",
                    config={"preset": "openai", "models": [{"model_id": "shared-model"}]},
                    status="active",
                ),
                LLMProvider(
                    provider_id="provider-b",
                    display_name="Provider B",
                    location="controller",
                    backend="litellm",
                    config={"preset": "litellm_proxy", "models": [{"model_id": "shared-model"}]},
                    status="active",
                ),
                ModelRouting(task_type="default", provider_id="provider-b", model="shared-model"),
            ]
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    first = await provider.resolve_model_target(task_type="default")
    second = await provider.resolve_model_target(task_type="default")

    assert first == ("shared-model", "provider-b")
    assert second == ("shared-model", "provider-b")
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_model_target_honors_explicit_provider_id(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="provider-b",
                display_name="Provider B",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "shared-model"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    resolved = await provider.resolve_model_target(
        task_type="default", explicit_provider_id="provider-b"
    )

    assert resolved == ("shared-model", "provider-b")
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_model_info_uses_provider_default_model_metadata_when_models_list_omits_entry(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="provider-b",
                display_name="Provider B",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "shared-model"},
                status="active",
            )
        )
        await session.commit()

    monkeypatch.setattr(
        "cognis.providers.llm.litellm.litellm.get_model_info",
        lambda **_: {
            "max_input_tokens": 123456,
            "max_output_tokens": 7890,
            "supports_reasoning": True,
            "supports_prompt_caching": True,
        },
    )

    provider = LiteLLMProvider(session_factory)

    model_info = await provider.get_model_info("shared-model", provider_id="provider-b")

    assert model_info.context_window == 123456
    assert model_info.max_output_tokens == 7890
    assert model_info.supports_prompt_caching is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_model_target_recovers_from_stale_cached_provider_id(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add_all(
            [
                LLMProvider(
                    provider_id="provider-b",
                    display_name="Provider B",
                    location="controller",
                    backend="litellm",
                    config={"preset": "litellm_proxy", "models": [{"model_id": "shared-model"}]},
                    status="active",
                ),
                ModelRouting(task_type="default", provider_id="provider-b", model="shared-model"),
            ]
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    await provider._set_cached_resolved_model("default", "shared-model", "missing-provider")

    resolved = await provider.resolve_model_target(task_type="default")

    assert resolved == ("shared-model", "provider-b")
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_model_info_is_scoped_by_provider_id(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add_all(
            [
                LLMProvider(
                    provider_id="provider-a",
                    display_name="Provider A",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "shared-model",
                        "models": [
                            {
                                "model_id": "shared-model",
                                "supports_responses_api": False,
                                "supports_prompt_caching": False,
                            }
                        ],
                    },
                    status="active",
                ),
                LLMProvider(
                    provider_id="provider-b",
                    display_name="Provider B",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "litellm_proxy",
                        "default_model": "shared-model",
                        "models": [
                            {
                                "model_id": "shared-model",
                                "supports_responses_api": True,
                                "supports_prompt_caching": True,
                            }
                        ],
                    },
                    status="active",
                ),
            ]
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    info_a = await provider.get_model_info("shared-model", provider_id="provider-a")
    info_b = await provider.get_model_info("shared-model", provider_id="provider-b")

    assert info_a.supports_responses_api is False
    assert info_b.supports_responses_api is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_model_info_with_missing_provider_id_does_not_fall_back(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="provider-a",
                display_name="Provider A",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "models": [{"model_id": "shared-model", "supports_responses_api": True}],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    model_info = await provider.get_model_info("shared-model", provider_id="missing-provider")

    assert model_info == DEFAULT_MODEL_INFO
    await engine.dispose()


# ---------------------------------------------------------------------------
# "auto" max_tokens auto-fill
# ---------------------------------------------------------------------------


async def _groq_provider_session(
    tmp_path: object,
    *,
    config_extra: dict[str, object] | None = None,
    model_info_extra: dict[str, object] | None = None,
) -> tuple[object, object]:
    """Seed a Groq-like litellm_proxy provider with known model metadata."""

    engine, session_factory = await _session_factory(tmp_path)
    model_info: dict[str, object] = {
        "model_id": "llama-3.3-70b-versatile",
        "max_output_tokens": 32768,
        "context_window": 131072,
    }
    if model_info_extra:
        model_info.update(model_info_extra)
    config: dict[str, object] = {
        "preset": "litellm_proxy",
        "default_model": "llama-3.3-70b-versatile",
        "models": [model_info],
    }
    if config_extra:
        config.update(config_extra)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="groq",
                display_name="Groq",
                location="controller",
                backend="litellm",
                config=config,
                is_default=True,
                status="active",
            )
        )
        await session.commit()
    return engine, session_factory


class _CallCaptureResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, object]:
        return self._payload


@pytest.mark.asyncio
async def test_autofill_max_tokens_when_caller_does_not_set_it(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _groq_provider_session(tmp_path)
    captured: dict[str, object] = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return _CallCaptureResponse(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)
    provider = LiteLLMProvider(session_factory)
    await provider.generate(messages=[{"role": "user", "content": "hi"}])

    assert captured.get("max_tokens") == 32768
    await engine.dispose()


@pytest.mark.asyncio
async def test_autofill_max_tokens_respects_caller_override(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _groq_provider_session(tmp_path)
    captured: dict[str, object] = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return _CallCaptureResponse(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)
    provider = LiteLLMProvider(session_factory)
    await provider.generate(messages=[{"role": "user", "content": "hi"}], max_tokens=2048)

    assert captured.get("max_tokens") == 2048
    await engine.dispose()


@pytest.mark.asyncio
async def test_autofill_max_tokens_respects_max_completion_tokens_override(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _groq_provider_session(tmp_path)
    captured: dict[str, object] = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return _CallCaptureResponse(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)
    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[{"role": "user", "content": "hi"}], max_completion_tokens=1024
    )

    assert "max_tokens" not in captured
    assert captured.get("max_completion_tokens") == 1024
    await engine.dispose()


@pytest.mark.asyncio
async def test_autofill_max_tokens_honors_provider_ceiling(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _groq_provider_session(
        tmp_path, config_extra={"max_tokens_ceiling": 8192}
    )
    captured: dict[str, object] = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return _CallCaptureResponse(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)
    provider = LiteLLMProvider(session_factory)
    await provider.generate(messages=[{"role": "user", "content": "hi"}])

    assert captured.get("max_tokens") == 8192
    await engine.dispose()


@pytest.mark.asyncio
async def test_autofill_max_tokens_uses_safe_fallback_for_unknown_model(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        # Provider without any model metadata — get_model_info returns the
        # DEFAULT_MODEL_INFO (max_output_tokens=4096) which we deem "unknown"
        # and replace with the safe fallback of 16384.
        session.add(
            LLMProvider(
                provider_id="unknown",
                display_name="Unknown",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "mystery-model"},
                is_default=True,
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return _CallCaptureResponse(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    # Stub out litellm.get_model_info so the live lookup doesn't leak real
    # token limits into the test.
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.litellm.get_model_info",
        lambda model: {"max_tokens": 0, "max_output_tokens": 0},
    )
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)
    provider = LiteLLMProvider(session_factory)
    await provider.generate(messages=[{"role": "user", "content": "hi"}])

    assert captured.get("max_tokens") == 16384
    await engine.dispose()


# ---------------------------------------------------------------------------
# JSON-mode transport fallback (empty Responses reply / BadRequest validator)
# ---------------------------------------------------------------------------


class _FakeBadRequestError(Exception):
    """Stand-in for litellm.BadRequestError (matched by class name)."""


# Rename so _is_json_validator_bad_request matches by class name.
_FakeBadRequestError.__name__ = "BadRequestError"


@pytest.mark.asyncio
async def test_json_mode_fallback_when_responses_api_returns_empty(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    # Responses API returns a completed-but-empty payload.
    class _EmptyResponse:
        def model_dump(self) -> dict[str, object]:
            return {"status": "completed", "output": []}

    acompletion_kwargs: dict[str, object] = {}

    async def _fake_aresponses(**_: object) -> object:
        return _EmptyResponse()

    async def _fake_acompletion(**kwargs: object) -> object:
        acompletion_kwargs.update(kwargs)
        return _CallCaptureResponse(
            {
                "choices": [
                    {
                        "message": {"content": '{"decision":"approved"}'},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        response_format={"type": "json_object"},
    )

    # Fallback kicked in and we got the chat-completions JSON payload.
    assert result["choices"][0]["message"]["content"] == '{"decision": "approved"}'
    # response_format was preserved on the fallback call.
    assert acompletion_kwargs.get("response_format") == {"type": "json_object"}
    # The (provider, model) pair is now cached as broken for JSON mode.
    assert ("proxy", "gpt-5.4") in provider._json_mode_broken_keys

    await engine.dispose()


@pytest.mark.asyncio
async def test_json_mode_fallback_when_responses_api_returns_invalid_json(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    class _InvalidJSONResponse:
        def model_dump(self) -> dict[str, object]:
            return {"status": "completed", "output_text": "not json"}

    acompletion_calls: list[dict[str, object]] = []

    async def _fake_aresponses(**_: object) -> object:
        return _InvalidJSONResponse()

    async def _fake_acompletion(**kwargs: object) -> object:
        acompletion_calls.append(dict(kwargs))
        return _CallCaptureResponse(
            {
                "choices": [
                    {
                        "message": {"content": '```json\n{"ok": true}\n```'},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        response_format={"type": "json_object"},
    )

    assert result["choices"][0]["message"]["content"] == '{"ok": true}'
    assert acompletion_calls[0].get("response_format") == {"type": "json_object"}
    assert ("proxy", "gpt-5.4") in provider._json_mode_broken_keys
    await engine.dispose()


@pytest.mark.asyncio
async def test_json_mode_cached_broken_routes_directly_to_chat_completions(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    aresponses_calls = 0

    async def _fake_aresponses(**_: object) -> object:
        nonlocal aresponses_calls
        aresponses_calls += 1
        raise AssertionError("aresponses must not be called when cache marks model broken")

    async def _fake_acompletion(**_: object) -> object:
        return _CallCaptureResponse(
            {
                "choices": [
                    {
                        "message": {"content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    provider._json_mode_broken_keys.add(("proxy", "gpt-5.4"))

    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        response_format={"type": "json_object"},
    )

    assert aresponses_calls == 0
    assert result["choices"][0]["message"]["content"] == '{"ok": true}'
    await engine.dispose()


@pytest.mark.asyncio
async def test_json_mode_cached_response_format_broken_strips_response_format(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _groq_provider_session(tmp_path)
    acompletion_kwargs: dict[str, object] = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        acompletion_kwargs.update(kwargs)
        return _CallCaptureResponse(
            {
                "choices": [
                    {
                        "message": {"content": 'Result: {"ok": true}'},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    provider._json_response_format_broken_keys.add(("groq", "llama-3.3-70b-versatile"))

    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )

    assert "response_format" not in acompletion_kwargs
    assert result["choices"][0]["message"]["content"] == '{"ok": true}'
    await engine.dispose()


@pytest.mark.asyncio
async def test_non_json_call_still_uses_responses_api_on_cached_broken_model(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    aresponses_called = {"flag": False}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "hello"}],
                    }
                ],
            }

    async def _fake_aresponses(**_: object) -> object:
        aresponses_called["flag"] = True
        return _Response()

    async def _fake_acompletion(**_: object) -> object:
        raise AssertionError("acompletion must not be called for non-JSON requests")

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    provider._json_mode_broken_keys.add(("proxy", "gpt-5.4"))

    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
    )

    assert aresponses_called["flag"] is True
    assert result["choices"][0]["message"]["content"] == "hello"
    await engine.dispose()


@pytest.mark.asyncio
async def test_plain_text_empty_responses_falls_back_to_chat_and_caches(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                is_default=True,
                status="active",
            )
        )
        await session.commit()

    class _EmptyResponse:
        def model_dump(self) -> dict[str, object]:
            return {"status": "completed", "output": []}

    acompletion_calls = 0

    async def _fake_aresponses(**_: object) -> object:
        return _EmptyResponse()

    async def _fake_acompletion(**_: object) -> object:
        nonlocal acompletion_calls
        acompletion_calls += 1
        return _CallCaptureResponse(
            {"choices": [{"message": {"content": "Generated personality"}}]}
        )

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    result = await provider.generate(messages=[{"role": "user", "content": "agent field"}])

    assert result["choices"][0]["message"]["content"] == "Generated personality"
    assert acompletion_calls == 1
    assert ("proxy", "gpt-5.4") in provider._plain_text_responses_broken_keys
    await engine.dispose()


@pytest.mark.asyncio
async def test_json_mode_fallback_on_json_validator_bad_request(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _groq_provider_session(tmp_path)
    acompletion_call_count = {"count": 0}
    acompletion_kwargs_history: list[dict[str, object]] = []

    async def _fake_acompletion(**kwargs: object) -> object:
        acompletion_call_count["count"] += 1
        acompletion_kwargs_history.append(dict(kwargs))
        if acompletion_call_count["count"] == 1:
            # First call: JSON validator rejects truncated output.
            raise _FakeBadRequestError(
                "GroqException - Failed to generate JSON. "
                "See 'failed_generation' for more details. code=json_validate_failed"
            )
        return _CallCaptureResponse(
            {
                "choices": [
                    {
                        "message": {"content": '{"rationale":"ok","steps":[]}'},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    result = await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )

    assert acompletion_call_count["count"] == 2
    # First call carried response_format; second (fallback) dropped it.
    assert acompletion_kwargs_history[0].get("response_format") == {"type": "json_object"}
    assert "response_format" not in acompletion_kwargs_history[1]
    assert result["choices"][0]["message"]["content"] == '{"rationale": "ok", "steps": []}'
    assert ("groq", "llama-3.3-70b-versatile") in provider._json_response_format_broken_keys
    await engine.dispose()


@pytest.mark.asyncio
async def test_other_bad_request_errors_propagate_unchanged(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _groq_provider_session(tmp_path)

    async def _fake_acompletion(**_: object) -> object:
        raise _FakeBadRequestError("model_not_found: unknown_model")

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    with pytest.raises(_FakeBadRequestError, match="model_not_found"):
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
        )

    # Non-JSON-validator BadRequestErrors must not populate the cache.
    assert ("groq", "llama-3.3-70b-versatile") not in provider._json_mode_broken_keys
    await engine.dispose()


@pytest.mark.asyncio
async def test_context_overflow_bad_request_raises_typed_error_without_retry(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _groq_provider_session(tmp_path)
    call_count = {"count": 0}

    async def _fake_acompletion(**_: object) -> object:
        call_count["count"] += 1
        raise _FakeBadRequestError("context_length_exceeded: maximum context length is 8192 tokens")

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(session_factory)
    with pytest.raises(llm_retry.LLMContextOverflowError) as exc_info:
        await provider.generate(messages=[{"role": "user", "content": "hi"}])

    assert call_count["count"] == 1
    assert exc_info.value.reason == "context_length_exceeded"
    assert exc_info.value.provider_id == "groq"
    assert ("groq", "llama-3.3-70b-versatile") not in provider._json_mode_broken_keys
    assert ("groq", "llama-3.3-70b-versatile") not in provider._json_response_format_broken_keys
    await engine.dispose()


def test_context_overflow_classifier_ignores_token_rate_limit() -> None:
    assert (
        llm_retry.context_overflow_reason("Rate limit exceeded: too many tokens per minute") is None
    )


@pytest.mark.asyncio
async def test_invalidate_json_mode_cache_clears_matching_provider(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)
    provider._json_mode_broken_keys.add(("proxy-a", "model-1"))
    provider._json_mode_broken_keys.add(("proxy-a", "model-2"))
    provider._json_mode_broken_keys.add(("proxy-b", "model-1"))

    provider.invalidate_json_mode_cache_for_provider("proxy-a")

    assert provider._json_mode_broken_keys == {("proxy-b", "model-1")}
    await engine.dispose()


@pytest.mark.asyncio
async def test_stream_generate_marks_native_openai_tool_search_broken_and_raises_retry_signal(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="proxy",
                display_name="LiteLLM Proxy",
                location="controller",
                backend="litellm",
                config={"preset": "litellm_proxy", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_aresponses(**_: object) -> object:
        raise _FakeBadRequestError("Unknown parameter: 'tool_choice.tools'.")

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)

    with pytest.raises(
        OpenAIToolSearchFallbackRequired, match="native OpenAI Responses tool search"
    ):
        async for _ in provider.stream_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
            provider_id="proxy",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "Read",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                {"type": "tool_search"},
            ],
            tool_choice={
                "type": "allowed_tools",
                "mode": "auto",
                "tools": [{"type": "function", "name": "read"}],
            },
        ):
            pass

    assert ("proxy", "gpt-5.4") in provider._openai_tool_search_broken_keys
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_tool_exposure_runtime_fallbacks_masks_cached_native_openai_tool_search(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)
    provider._openai_tool_search_broken_keys.add(("proxy", "gpt-5.4"))

    adjusted = provider.apply_tool_exposure_runtime_fallbacks(
        ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
        ),
        provider_id="proxy",
        model_id="gpt-5.4",
    )

    assert adjusted.supports_openai_allowed_tools is False
    assert adjusted.supports_openai_namespace_tools is False
    assert adjusted.supports_tool_search is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalidate_runtime_capability_cache_clears_json_and_tool_search_entries(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)
    provider._json_mode_broken_keys.add(("proxy-a", "model-1"))
    provider._openai_tool_search_broken_keys.add(("proxy-a", "model-2"))
    provider._json_mode_broken_keys.add(("proxy-b", "model-1"))
    provider._openai_tool_search_broken_keys.add(("proxy-b", "model-2"))

    provider.invalidate_runtime_capability_cache_for_provider("proxy-a")

    assert provider._json_mode_broken_keys == {("proxy-b", "model-1")}
    assert provider._openai_tool_search_broken_keys == {("proxy-b", "model-2")}
    await engine.dispose()


@pytest.mark.asyncio
async def test_hosted_instruction_drift_is_cached_per_provider_model(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    provider._maybe_note_hosted_instruction_drift(
        SimpleNamespace(provider_id="proxy"),
        "gpt-5.4",
        sent_instructions="You are an executive assistant.",
        response_instructions="You are Codex, based on GPT-5.",
    )

    assert provider.has_hosted_instruction_drift("proxy", "gpt-5.4") is True
    assert (
        provider.hosted_instruction_drift_reason("proxy", "gpt-5.4")
        == "server_returned_different_instructions"
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalidate_runtime_capability_cache_clears_hosted_instruction_drift(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)
    provider._hosted_instruction_drift_keys[("proxy-a", "model-1")] = "reason-a"
    provider._hosted_instruction_drift_keys[("proxy-b", "model-2")] = "reason-b"

    provider.invalidate_runtime_capability_cache_for_provider("proxy-a")

    assert provider._hosted_instruction_drift_keys == {("proxy-b", "model-2"): "reason-b"}
    await engine.dispose()


# ---------------------------------------------------------------------------
# ChatGPT OAuth provider caching tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chatgpt_oauth_provider_attaches_cache_key(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ChatGPT OAuth providers can opt into prompt_cache_key in Responses kwargs."""
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt-test",
                display_name="ChatGPT",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "codex-mini-latest",
                    "use_prompt_cache_key": True,
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    # Simulate the OAuth token being available so the provider doesn't try to
    # authenticate interactively.
    import json as _json

    from cognis.ownership import SYSTEM_USER_EMAIL

    secrets = _MemorySecrets()
    await secrets.set_secret(
        "llm_oauth_chatgpt_chatgpt-test",
        _json.dumps(
            {
                "access_token": _jwt_with_claims({"sub": "user", "exp": 9999999999}),
                "refresh_token": "refresh-token",
                "account_id": "acct_test",
            }
        ),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    await provider.generate(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hello"},
        ],
        model="codex-mini-latest",
        max_tokens=32,
        cache_breakpoint_index=0,
    )

    assert "prompt_cache_key" in captured, "prompt_cache_key must be sent for ChatGPT OAuth"
    assert str(captured["prompt_cache_key"]).startswith("cognis-")
    # store must NOT be set for ChatGPT (backend forces False)
    assert "store" not in captured
    await engine.dispose()


@pytest.mark.asyncio
async def test_chatgpt_oauth_provider_omits_cache_key_when_disabled(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COGNIS_CHATGPT_PROMPT_CACHE_KEY_ENABLED=false suppresses the cache key."""
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt-test",
                display_name="ChatGPT",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "codex-mini-latest",
                    "codex_transport": "litellm",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        captured.update(kwargs)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setenv("COGNIS_CHATGPT_PROMPT_CACHE_KEY_ENABLED", " false ")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    import json as _json

    from cognis.ownership import SYSTEM_USER_EMAIL

    secrets = _MemorySecrets()
    await secrets.set_secret(
        "llm_oauth_chatgpt_chatgpt-test",
        _json.dumps(
            {
                "access_token": _jwt_with_claims({"sub": "user", "exp": 9999999999}),
                "refresh_token": "refresh-token",
                "account_id": "acct_test",
            }
        ),
        SYSTEM_USER_EMAIL,
        scope="system",
    )

    provider = LiteLLMProvider(session_factory, secrets_provider=secrets)
    await provider.generate(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hello"},
        ],
        model="codex-mini-latest",
        max_tokens=32,
        cache_breakpoint_index=0,
        cognis_session_id="sess_chatgpt_123",
    )

    assert "prompt_cache_key" not in captured
    assert captured["extra_headers"] == {
        "x-session-affinity": "sess_chatgpt_123",
        "session_id": "sess_chatgpt_123",
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_prompt_cache_key_capability_fallback(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the backend rejects prompt_cache_key, the provider retries without it."""
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                },
                status="active",
            )
        )
        await session.commit()

    call_count = 0
    captured_kwargs: list[dict[str, object]] = []

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            }

    async def _fake_aresponses(**kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        captured_kwargs.append(dict(kwargs))
        if call_count == 1:
            # Simulate backend rejecting the cache key param.
            raise _ProviderError("Unknown parameter: 'prompt_cache_key'", status_code=400)
        return _Response()

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    await provider.generate(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hello"},
        ],
        model="gpt-5.4",
        max_tokens=32,
        cache_breakpoint_index=0,
    )

    assert call_count == 2, "Should retry once after rejection"
    assert "prompt_cache_key" in captured_kwargs[0], "First call must include cache key"
    assert "prompt_cache_key" not in captured_kwargs[1], "Retry must omit cache key"
    assert ("openai", "gpt-5.4") in provider._prompt_cache_key_broken_keys
    await engine.dispose()


@pytest.mark.asyncio
async def test_prompt_cache_key_fallback_preserves_context_overflow_classification(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the no-cache retry overflows, Cognis still raises the structured error."""
    from cognis.providers.llm.retry import LLMContextOverflowError

    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"preset": "openai", "default_model": "gpt-5.4"},
                status="active",
            )
        )
        await session.commit()

    call_count = 0

    async def _fake_aresponses(**_: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _ProviderError("Unknown parameter: 'prompt_cache_key'", status_code=400)
        raise Exception("maximum context length exceeded")

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)

    provider = LiteLLMProvider(session_factory)
    with pytest.raises(LLMContextOverflowError):
        await provider.generate(
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "hello"},
            ],
            model="gpt-5.4",
            max_tokens=32,
            cache_breakpoint_index=0,
        )

    assert call_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalidate_runtime_capability_cache_clears_prompt_cache_key_broken(
    tmp_path: object,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)
    provider._prompt_cache_key_broken_keys.add(("prov-a", "model-1"))
    provider._prompt_cache_key_broken_keys.add(("prov-b", "model-2"))

    provider.invalidate_runtime_capability_cache_for_provider("prov-a")

    assert provider._prompt_cache_key_broken_keys == {("prov-b", "model-2")}
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_diagnostics_includes_instructions_hash(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream diagnostics include instructions_hash and prompt_cache_key_present."""
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="openai",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-5.4",
                },
                status="active",
            )
        )
        await session.commit()

    observed_diagnostics: list[dict[str, object]] = []

    async def _fake_stream() -> object:
        yield {
            "choices": [{"delta": {"content": "hello"}, "finish_reason": None}],
            "provider_event": "responses",
            "provider_event_type": "response.output_text.delta",
        }
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "response_status": "completed",
            "provider_event": "responses",
            "provider_event_type": "response.completed",
        }

    async def _fake_aresponses(**kwargs: object) -> object:
        return _fake_stream()

    import cognis.providers.llm.litellm as _llm_mod

    original_observe = _llm_mod._observe_provider_phase

    def _capturing_observe(**kwargs: object) -> None:
        extra = kwargs.get("extra_data")
        if isinstance(extra, dict) and "responses_instructions_hash" in extra:
            observed_diagnostics.append(dict(extra))
        original_observe(**kwargs)

    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "on")
    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.aresponses", _fake_aresponses)
    monkeypatch.setattr("cognis.providers.llm.litellm._observe_provider_phase", _capturing_observe)

    provider = LiteLLMProvider(session_factory)
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "hello"},
            ],
            model="gpt-5.4",
            cache_breakpoint_index=0,
        )
    ]

    assert chunks, "Should yield at least one chunk"
    assert observed_diagnostics, "Should have captured diagnostics with instructions_hash"
    diag = observed_diagnostics[0]
    assert "responses_instructions_hash" in diag
    assert "prompt_cache_key_present" in diag
    assert diag["prompt_cache_key_present"] is True
    await engine.dispose()


# ---------------------------------------------------------------------------
# chatgpt_patches module tests
# ---------------------------------------------------------------------------


def test_chatgpt_patches_cache_passthrough_is_idempotent() -> None:
    """install_chatgpt_responses_cache_passthrough is safe to call multiple times."""
    from cognis.providers.llm.chatgpt_patches import (
        install_chatgpt_responses_cache_passthrough,
    )

    # First call may or may not succeed depending on whether litellm is installed.
    install_chatgpt_responses_cache_passthrough()
    result2 = install_chatgpt_responses_cache_passthrough()

    # Second call must return False (already installed or not importable).
    assert result2 is False, "Second call must be a no-op"


def test_chatgpt_patches_cache_passthrough_preserves_cache_params() -> None:
    """Wrapped ChatGPT Responses transform preserves Cognis cache params."""
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig

    from cognis.providers.llm.chatgpt_patches import install_chatgpt_responses_cache_passthrough

    install_chatgpt_responses_cache_passthrough()

    request = ChatGPTResponsesAPIConfig().transform_responses_api_request(
        model="gpt-5.3-codex",
        input=[{"role": "user", "content": "hello"}],
        response_api_optional_request_params={
            "instructions": "You are a helpful assistant.",
            "prompt_cache_key": "cache-key",
            "prompt_cache_retention": "24h",
        },
        litellm_params={},
        headers={},
    )

    assert request["prompt_cache_key"] == "cache-key"
    assert request["prompt_cache_retention"] == "24h"


def test_chatgpt_patches_suppress_instructions_uses_setdefault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """suppress_chatgpt_default_instructions does not override an existing env var."""
    from cognis.providers.llm.chatgpt_patches import suppress_chatgpt_default_instructions

    monkeypatch.setenv("CHATGPT_DEFAULT_INSTRUCTIONS", "custom-instructions")
    result = suppress_chatgpt_default_instructions()

    assert result is False, "Should not override an existing env var"
    assert os.environ["CHATGPT_DEFAULT_INSTRUCTIONS"] == "custom-instructions"


def test_chatgpt_patches_suppress_instructions_sets_space_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """suppress_chatgpt_default_instructions sets a truthy env override when absent."""
    from cognis.providers.llm.chatgpt_patches import suppress_chatgpt_default_instructions

    monkeypatch.delenv("CHATGPT_DEFAULT_INSTRUCTIONS", raising=False)
    result = suppress_chatgpt_default_instructions()

    assert result in {True, False}
    assert os.environ.get("CHATGPT_DEFAULT_INSTRUCTIONS") == " "


def test_is_prompt_cache_key_rejected_matches_known_messages() -> None:
    from cognis.providers.llm.litellm import _is_prompt_cache_key_rejected

    assert _is_prompt_cache_key_rejected(
        _ProviderError("Unknown parameter: 'prompt_cache_key'", status_code=400)
    )
    assert _is_prompt_cache_key_rejected(
        _ProviderError("Unsupported parameter: prompt_cache_retention", status_code=400)
    )
    assert not _is_prompt_cache_key_rejected(
        _ProviderError("Unknown parameter: 'max_tokens'", status_code=400)
    )
    assert not _is_prompt_cache_key_rejected(
        _ProviderError("Unknown parameter: 'prompt_cache_key'", status_code=503)
    )
    assert not _is_prompt_cache_key_rejected(Exception("Some other error"))
    assert not _is_prompt_cache_key_rejected(ValueError("prompt_cache_key is fine here"))
