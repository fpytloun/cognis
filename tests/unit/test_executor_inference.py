"""Unit tests for executor-side LiteLLM inference proxy."""

from __future__ import annotations

import json

import pytest

from cognis.executor.inference import InferenceHandler


@pytest.mark.asyncio
async def test_stream_complete_proxies_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()

    async def fake_stream(**_: object):
        yield {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1}}

    async def fake_acompletion(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.acompletion", fake_acompletion)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"temperature": 0.1},
        )
    ]

    assert chunks[0]["content"] == "Hello"
    assert chunks[-1]["done"] is True


@pytest.mark.asyncio
async def test_stream_complete_serializes_litellm_tool_call_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    class ToolCall:
        def model_dump(self, **_: object) -> dict[str, object]:
            return {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
            }

    async def fake_stream():
        yield {"choices": [{"delta": {"tool_calls": [ToolCall()]}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {}}

    async def fake_acompletion(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.acompletion", fake_acompletion)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="anthropic/claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"tools": []},
        )
    ]

    json.dumps(chunks[0])
    assert chunks[0]["tool_calls"][0]["function"]["name"] == "bash"
    assert chunks[-1]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_stream_complete_returns_error_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()

    async def fake_acompletion(**_: object):
        raise RuntimeError("boom")

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.acompletion", fake_acompletion)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="openai/gpt-4o-mini",
            messages=[],
            request_kwargs={},
        )
    ]

    assert chunks == [
        {
            "done": True,
            "error": "Inference error: boom",
            "response_error": {
                "category": "other",
                "code": "RuntimeError",
                "message": "boom",
                "retry_after_seconds": None,
            },
            "finish_reason": "error",
        }
    ]


@pytest.mark.asyncio
async def test_generate_returns_model_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()

    class Response:
        def model_dump(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "hello"}}]}

    async def fake_acompletion(**_: object):
        return Response()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.acompletion", fake_acompletion)

    result = await handler.generate(
        model="openai/gpt-4o-mini",
        messages=[],
        request_kwargs={},
    )
    assert result["choices"][0]["message"]["content"] == "hello"


@pytest.mark.asyncio
async def test_stream_complete_normalizes_responses_events(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()

    async def fake_stream():
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
            "response": {"status": "completed", "usage": {"total_tokens": 8}},
        }

    async def fake_aresponses(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    assert chunks[0]["content"] == "Hello"
    assert chunks[1]["tool_calls"][0]["function"]["name"] == "search_tools"
    assert chunks[2]["tool_calls"][0]["function"]["arguments"] == '{"query":"docs"}'
    assert chunks[-1]["usage"]["total_tokens"] == 8


@pytest.mark.asyncio
async def test_stream_complete_responses_projects_messages_to_responses_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executor backend must project chat messages into Responses API input shape."""
    handler = InferenceHandler()
    captured: dict[str, object] = {}

    async def fake_stream():
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"total_tokens": 4},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            },
        }

    async def fake_aresponses(**kwargs: object):
        captured.update(kwargs)
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            ],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    assert chunks[-1]["done"] is True
    assert captured["input"] == [
        {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]
    # cognis_llm_api is a controller-internal marker and must not leak.
    assert "cognis_llm_api" not in captured


@pytest.mark.asyncio
async def test_stream_complete_emits_message_item_text_without_output_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    async def fake_stream():
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
                "usage": {"total_tokens": 6},
            },
        }

    async def fake_aresponses(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    assert chunks[0]["content"] == "Hello from item"
    assert chunks[-1]["usage"]["total_tokens"] == 6


@pytest.mark.asyncio
async def test_stream_complete_emits_output_text_done_without_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    async def fake_stream():
        yield {"type": "response.output_text.done", "text": "Hello from done"}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    async def fake_aresponses(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    assert chunks[0]["content"] == "Hello from done"
    assert chunks[-1]["usage"]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_stream_complete_normalizes_enum_style_event_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    async def fake_stream():
        yield {"type": "ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA", "delta": "Hello"}
        yield {"type": "ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE", "text": "Hello"}
        yield {
            "type": "ResponsesAPIStreamEvents.RESPONSE_COMPLETED",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    async def fake_aresponses(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    text_chunks = [chunk for chunk in chunks if chunk.get("content")]
    assert len(text_chunks) == 1
    assert text_chunks[0]["content"] == "Hello"
    assert chunks[-1]["usage"]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_stream_complete_does_not_duplicate_output_text_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    async def fake_stream():
        yield {"type": "response.output_text.delta", "delta": "Hello"}
        yield {"type": "response.output_text.done", "text": "Hello"}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    async def fake_aresponses(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    text_chunks = [chunk for chunk in chunks if chunk.get("content")]
    assert len(text_chunks) == 1
    assert text_chunks[0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_stream_complete_emits_content_part_done_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    async def fake_stream():
        yield {
            "type": "response.content_part.done",
            "part": {"type": "output_text", "text": "Hello from content part"},
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    async def fake_aresponses(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    assert chunks[0]["content"] == "Hello from content part"


@pytest.mark.asyncio
async def test_stream_complete_emits_reasoning_and_refusal_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    async def fake_stream():
        yield {"type": "response.reasoning_text.delta", "delta": '{"decision":"revise"}'}
        yield {"type": "response.refusal.delta", "delta": "Cannot comply"}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    async def fake_aresponses(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    assert chunks[0]["reasoning_content"] == '{"decision":"revise"}'
    assert chunks[1]["refusal"] == "Cannot comply"


@pytest.mark.asyncio
async def test_generate_normalizes_responses_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()

    class Response:
        def model_dump(self) -> dict[str, object]:
            return {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "search_tools",
                        "arguments": '{"query":"docs"}',
                    },
                ],
            }

    async def fake_aresponses(**_: object):
        return Response()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    result = await handler.generate(
        model="gpt-5.4",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={"cognis_llm_api": "responses"},
    )

    assert result["choices"][0]["message"]["content"] == "hello"
    assert result["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"


@pytest.mark.asyncio
async def test_generate_preserves_reasoning_only_responses_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    class Response:
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

    async def fake_aresponses(**_: object):
        return Response()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    result = await handler.generate(
        model="gpt-5.4",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={"cognis_llm_api": "responses"},
    )

    assert result["choices"][0]["message"]["content"] is None
    assert result["choices"][0]["message"]["reasoning_content"] == '{"decision":"revise"}'


@pytest.mark.asyncio
async def test_generate_translates_responses_tools_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()
    captured: dict[str, object] = {}

    class Response:
        def model_dump(self) -> dict[str, object]:
            return {"status": "completed", "output": []}

    async def fake_aresponses(**kwargs: object):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    await handler.generate(
        model="gpt-5.4",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={
            "cognis_llm_api": "responses",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search_tools",
                        "description": "Search tools",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        },
    )

    assert captured["tools"] == [
        {
            "type": "function",
            "name": "search_tools",
            "description": "Search tools",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


@pytest.mark.asyncio
async def test_stream_complete_returns_error_for_failed_responses_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    async def fake_stream():
        yield {"type": "response.failed", "error": {"message": "bridge failed"}}

    async def fake_aresponses(**_: object):
        return fake_stream()

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.aresponses", fake_aresponses)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"cognis_llm_api": "responses"},
        )
    ]

    assert chunks == [
        {
            "done": True,
            "error": "bridge failed",
            "response_error": {
                "category": "other",
                "message": "bridge failed",
                "provider_event": "response.failed",
                "details": {"event_type": "response.failed", "message": "bridge failed"},
            },
            "finish_reason": "error",
        }
    ]


@pytest.mark.asyncio
async def test_image_generate_method_still_available(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {"data": [{"b64_json": "abc"}]}

    async def fake_aimage_generation(**kwargs: object):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(
        "cognis.executor.inference.litellm.aimage_generation", fake_aimage_generation
    )

    result = await handler.image_generate(
        prompt="draw",
        model="gpt-image-1",
        request_kwargs={},
    )

    assert result["data"][0]["b64_json"] == "abc"
    assert "response_format" not in captured


@pytest.mark.asyncio
async def test_image_generate_omits_response_format_for_gpt_image_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()

    captured: dict[str, object] = {}

    class _Response:
        def model_dump(self) -> dict[str, object]:
            return {"data": [{"b64_json": "abc"}]}

    async def fake_aimage_generation(**kwargs: object):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(
        "cognis.executor.inference.litellm.aimage_generation", fake_aimage_generation
    )

    result = await handler.image_generate(
        prompt="draw",
        model="openai/gpt-image-2",
        request_kwargs={},
    )

    assert result["data"][0]["b64_json"] == "abc"
    assert "response_format" not in captured


@pytest.mark.asyncio
async def test_transcribe_posts_to_openai_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"text": "hello world", "language": "en", "duration": 1.5}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, **kwargs: object) -> _Response:
            captured["url"] = url
            captured.update(kwargs)
            return _Response()

    monkeypatch.setattr("cognis.executor.inference.httpx.AsyncClient", lambda **_: _Client())

    result = await handler.transcribe(
        audio_bytes=b"audio-bytes",
        mime_type="audio/ogg",
        filename="voice.ogg",
        model="openai/gpt-4o-mini-transcribe",
        provider_preset="openai",
        request_kwargs={"api_key": "secret", "api_base": "https://example.test"},
    )

    assert captured["url"] == "https://example.test/v1/audio/transcriptions"
    assert captured["data"] == {"model": "gpt-4o-mini-transcribe"}
    assert result["text"] == "hello world"


@pytest.mark.asyncio
async def test_transcribe_preserves_prefixed_model_for_litellm_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = InferenceHandler()
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"text": "hello world"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, **kwargs: object) -> _Response:
            captured.update(kwargs)
            return _Response()

    monkeypatch.setattr("cognis.executor.inference.httpx.AsyncClient", lambda **_: _Client())

    await handler.transcribe(
        audio_bytes=b"audio-bytes",
        mime_type="audio/ogg",
        filename="voice.ogg",
        model="openai/gpt-4o-transcribe",
        provider_preset="litellm_proxy",
        request_kwargs={"api_key": "secret", "api_base": "https://proxy.example"},
    )

    assert captured["data"] == {"model": "openai/gpt-4o-transcribe"}
