from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.models.config import DEFAULT_MODEL_INFO
from cognis.providers.llm.litellm import LiteLLMProvider, _normalize_proxy_model_info
from cognis.providers.llm.reasoning import apply_reasoning_config, reasoning_efforts_for_model
from cognis.providers.llm.responses_bridge import responses_request_kwargs
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider, ModelRouting


async def _session_factory(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)


@pytest.mark.asyncio
async def test_litellm_provider_resolves_explicit_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    resolved = await provider.resolve_model(explicit_model="gpt-5.4-mini")

    assert resolved == "gpt-5.4-mini"
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
async def test_litellm_provider_image_generate_omits_response_format_for_gpt_image_1(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
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
async def test_litellm_provider_image_generate_keeps_response_format_for_other_models(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
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
    kwargs = apply_reasoning_config(
        {"reasoning_effort": "max"},
        model_id="gpt-5.4",
        provider_preset="openai",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert kwargs["reasoning_effort"] == "xhigh"


def test_reasoning_translation_uses_adaptive_default_for_claude_46() -> None:
    kwargs = apply_reasoning_config(
        {"reasoning_effort": "default"},
        model_id="claude-opus-4.6",
        provider_preset="anthropic",
        model_info=DEFAULT_MODEL_INFO.model_copy(update={"supports_reasoning": True}),
    )

    assert kwargs["thinking"] == {"type": "adaptive"}


def test_reasoning_efforts_for_reasoning_model_return_normalized_levels() -> None:
    assert reasoning_efforts_for_model(
        "gpt-5.4", provider_preset="openai", supports_reasoning=True
    ) == ["default", "none", "minimal", "low", "medium", "high", "max"]


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
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-5.4")

    assert model_info.supports_responses_api is True
    assert model_info.supports_tool_search is True
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
                    "executor_labels": {"location": "local"},
                },
                status="active",
            )
        )
        await session.commit()

    class Router:
        async def route_generate(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["model"] == "gpt-4o-mini"
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
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert messages[1] == {"role": "system", "content": "mutable environment"}
    assert messages[2] == {"role": "user", "content": "hi"}
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
        yield {"type": "response.output_text.delta", "delta": "Hello"}
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

    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello from item"
    assert chunks[-1]["usage"]["total_tokens"] == 5
    assert len([chunk for chunk in chunks if chunk.get("choices")]) == 2
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
        yield {"type": "response.output_text.done", "text": "Hello from done"}
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
        yield {"type": "ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA", "delta": "Hello"}
        yield {"type": "ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE", "text": "Hello"}
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
        yield {"type": "response.output_text.delta", "delta": "Hello"}
        yield {"type": "response.output_text.done", "text": "Hello"}
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
            "type": "response.content_part.done",
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
        response_format={"type": "json_object"},
    )

    assert captured["text"] == {"format": {"type": "json_object"}}
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
async def test_litellm_provider_health_reports_degraded_without_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    health = await provider.health()

    assert health.status == "degraded"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_cached_resolution_expires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(ModelRouting(task_type="default", provider_id=None, model="gpt-4o-mini"))
        await session.commit()

    time_points = iter([1.0, 70.0, 70.0])
    monkeypatch.setattr("cognis.providers.llm.litellm.monotonic", lambda: next(time_points))

    provider = LiteLLMProvider(session_factory)
    assert await provider.resolve_model(task_type="default") == "gpt-4o-mini"

    async with session_factory() as session:
        row = await session.get(ModelRouting, "default")
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
        "supports_pdf_input": True,
        "supports_reasoning": False,
        "supports_prompt_caching": True,
        "input_cost_per_token": 0.0000025,
        "output_cost_per_token": 0.00001,
    }
    result = _normalize_proxy_model_info(raw)
    assert result["context_window"] == 1048576
    assert result["max_output_tokens"] == 32768
    assert result["supports_tools"] is True
    assert result["supports_vision"] is True
    assert result["supports_audio_input"] is False
    assert result["supports_pdf_input"] is True
    assert result["supports_reasoning"] is False
    assert result["supports_prompt_caching"] is True
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
    assert model_info.max_output_tokens == 65536
    assert model_info.supports_vision is True
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
