"""Unit tests for executor-side LiteLLM inference proxy."""

from __future__ import annotations

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

    monkeypatch.setattr("cognis.executor.inference.litellm.acompletion", fake_acompletion)

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
async def test_stream_complete_returns_error_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()

    async def fake_acompletion(**_: object):
        raise RuntimeError("boom")

    monkeypatch.setattr("cognis.executor.inference.litellm.acompletion", fake_acompletion)

    chunks = [
        chunk
        async for chunk in handler.stream_complete(
            model="openai/gpt-4o-mini",
            messages=[],
            request_kwargs={},
        )
    ]

    assert chunks == [{"done": True, "error": "Inference error: boom", "finish_reason": "error"}]


@pytest.mark.asyncio
async def test_generate_returns_model_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = InferenceHandler()

    class Response:
        def model_dump(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "hello"}}]}

    async def fake_acompletion(**_: object):
        return Response()

    monkeypatch.setattr("cognis.executor.inference.litellm.acompletion", fake_acompletion)

    result = await handler.generate(
        model="openai/gpt-4o-mini",
        messages=[],
        request_kwargs={},
    )
    assert result["choices"][0]["message"]["content"] == "hello"
