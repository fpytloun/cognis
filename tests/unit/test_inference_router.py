from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.models.config import GeneratedImage, ImageGenerationResult
from cognis.providers.llm.errors import LLMStreamProviderError
from cognis.providers.llm.inference_router import InferenceRouter


def _handle(executor_id: str, metadata: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        executor_id=executor_id,
        metadata=metadata,
        capabilities=SimpleNamespace(local_inference=True),
    )


class _Connection:
    def __init__(self) -> None:
        self.discover_calls: list[dict[str, object]] = []

    async def llm_complete_stream(self, **_: object):
        yield {"content": "Hello", "tool_calls": None, "reasoning_content": None, "index": 0}
        yield {
            "content": None,
            "tool_calls": None,
            "reasoning_content": '{"decision":"revise"}',
            "reasoning": "Need tests",
            "refusal": None,
            "index": 1,
        }
        yield {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "search_tools", "arguments": '{"query":"docs"}'},
                }
            ],
            "reasoning_content": None,
            "index": 1,
        }
        yield {"done": True, "usage": {"total_tokens": 9}, "finish_reason": "stop"}

    async def rpc_call(self, method: str, params: dict[str, object], timeout: float | None = None):
        del timeout
        if method == "llm.image_generate":
            return ImageGenerationResult(
                images=[GeneratedImage(b64_json="abc", content_type="image/png")],
                model="img",
            )
        if method == "llm.transcribe":
            assert params["model"] == "whisper-1"
            return {"text": "hello from audio", "model": "whisper-1"}
        raise AssertionError(f"unexpected method {method}")

    async def llm_discover_models(self, **kwargs: object):
        self.discover_calls.append(kwargs)
        return [{"model_id": "ollama/llama3.2", "name": "llama3.2"}]


class _Provider:
    def __init__(self) -> None:
        self.connection = _Connection()

    async def list_active(self):
        return [_handle("exec-1", {"labels": {"location": "local"}})]

    async def get_executor(self, handle: SimpleNamespace):
        assert handle.executor_id == "exec-1"
        return self.connection


class _PerformanceConnection:
    async def llm_complete_stream(self, **_: object):
        yield {"content": "Hello", "tool_calls": None, "reasoning_content": None, "index": 0}
        yield {
            "done": True,
            "usage": {"total_tokens": 9},
            "finish_reason": "stop",
            "backend_metadata": {
                "performance": {
                    "is_local": True,
                    "model": "qwen3:8b",
                    "runtime": "Ollama",
                    "executor_id": None,
                    "executor_name": None,
                    "measured_at": "2026-07-13T12:00:00Z",
                }
            },
        }


class _PerformanceProvider:
    def __init__(self) -> None:
        self.connection = _PerformanceConnection()

    async def list_active(self):
        return [
            _handle(
                "exec-1",
                {"labels": {"location": "local"}, "display_name": "Workstation"},
            )
        ]

    async def get_executor(self, handle: SimpleNamespace):
        assert handle.executor_id == "exec-1"
        return self.connection


class _MultiProvider:
    def __init__(self) -> None:
        self.connection = _Connection()
        self.selected_executor_id: str | None = None

    async def list_active(self):
        return [
            _handle("empty-labels", {"labels": {}}),
            _handle("labeled", {"labels": {"location": "local"}}),
        ]

    async def get_executor(self, handle: SimpleNamespace):
        self.selected_executor_id = str(handle.executor_id)
        return self.connection


@pytest.mark.asyncio
async def test_inference_router_route_generate_reconstructs_normalized_response() -> None:
    router = InferenceRouter(_Provider())

    result = await router.route_generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        executor_labels={"location": "local"},
        request_kwargs={"cognis_llm_api": "responses"},
    )

    assert result["choices"][0]["message"]["content"] == "Hello"
    assert result["choices"][0]["message"]["reasoning_content"] == '{"decision":"revise"}'
    assert result["choices"][0]["message"]["reasoning"] == "Need tests"
    assert result["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"
    assert result["usage"]["total_tokens"] == 9


@pytest.mark.asyncio
async def test_inference_router_adds_selected_executor_to_performance_snapshot() -> None:
    router = InferenceRouter(_PerformanceProvider())

    chunks = [
        chunk
        async for chunk in router.route_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="ollama/qwen3:8b",
            executor_labels={"location": "local"},
        )
    ]

    performance = chunks[-1]["performance"]
    assert performance["executor_id"] == "exec-1"
    assert performance["executor_name"] == "Workstation"


@pytest.mark.asyncio
async def test_inference_router_discover_models_routes_to_selected_executor() -> None:
    provider = _Provider()
    router = InferenceRouter(provider)

    result = await router.discover_models(
        preset="ollama",
        base_url="http://localhost:11434",
        api_key="",
        executor_labels={"location": "local"},
        provider_id="ollama",
        owner_email="owner@example.com",
    )

    assert result == [{"model_id": "ollama/llama3.2", "name": "llama3.2"}]
    assert provider.connection.discover_calls == [
        {
            "preset": "ollama",
            "base_url": "http://localhost:11434",
            "api_key": "",
            "provider_id": "ollama",
            "owner_email": "owner@example.com",
        }
    ]


@pytest.mark.asyncio
async def test_inference_router_discover_models_reports_unmatched_executor_id() -> None:
    router = InferenceRouter(_Provider())

    with pytest.raises(RuntimeError, match="executor_id 'missing'"):
        await router.discover_models(
            preset="ollama",
            base_url="http://localhost:11434",
            executor_id="missing",
        )


@pytest.mark.asyncio
async def test_inference_router_discover_models_rejects_missing_selector() -> None:
    router = InferenceRouter(_Provider())

    with pytest.raises(RuntimeError, match="No executor selector"):
        await router.discover_models(
            preset="ollama",
            base_url="http://localhost:11434",
        )


@pytest.mark.asyncio
async def test_inference_router_discover_models_reports_unmatched_executor_labels() -> None:
    router = InferenceRouter(_Provider())

    with pytest.raises(RuntimeError, match="executor_labels"):
        await router.discover_models(
            preset="ollama",
            base_url="http://localhost:11434",
            executor_labels={"location": "other"},
        )


@pytest.mark.asyncio
async def test_inference_router_route_generate_can_target_explicit_executor_without_labels() -> (
    None
):
    provider = _MultiProvider()
    router = InferenceRouter(provider)

    result = await router.route_generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        executor_id="empty-labels",
        request_kwargs={"cognis_llm_api": "responses"},
    )

    assert result["choices"][0]["message"]["content"] == "Hello"
    assert provider.selected_executor_id == "empty-labels"


class _StructuredConnection:
    async def llm_complete_stream(self, **_: object):
        yield {
            "content": None,
            "tool_calls": None,
            "reasoning_content": {"decision": "revise", "feedback": "add tests"},
            "reasoning": ["Need tests"],
            "refusal": None,
            "index": 0,
        }
        yield {
            "done": True,
            "usage": {"total_tokens": 3},
            "finish_reason": "stop",
            "response_status": "completed",
        }


class _StructuredProvider:
    def __init__(self) -> None:
        self.connection = _StructuredConnection()

    async def list_active(self):
        return [_handle("exec-1", {"labels": {"location": "local"}})]

    async def get_executor(self, handle: SimpleNamespace):
        assert handle.executor_id == "exec-1"
        return self.connection


class _ResponseItemMetadataConnection:
    async def llm_complete_stream(self, **_: object):
        yield {
            "content": "Hello",
            "tool_calls": None,
            "reasoning_content": None,
            "response_item_id": "msg_1",
            "content_source": "response.output_text.delta",
            "response_message_phase": "commentary",
            "index": 0,
        }
        yield {"done": True, "usage": {"total_tokens": 3}, "finish_reason": "stop"}


class _ResponseItemMetadataProvider:
    def __init__(self) -> None:
        self.connection = _ResponseItemMetadataConnection()

    async def list_active(self):
        return [_handle("exec-1", {"labels": {"location": "local"}})]

    async def get_executor(self, handle: SimpleNamespace):
        assert handle.executor_id == "exec-1"
        return self.connection


@pytest.mark.asyncio
async def test_inference_router_route_generate_serializes_structured_reasoning_fields() -> None:
    router = InferenceRouter(_StructuredProvider())

    result = await router.route_generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        executor_labels={"location": "local"},
        request_kwargs={"cognis_llm_api": "responses"},
    )

    assert '"decision": "revise"' in result["choices"][0]["message"]["reasoning_content"]
    assert result["choices"][0]["message"]["reasoning"] == '["Need tests"]'
    assert result["response_status"] == "completed"


@pytest.mark.asyncio
async def test_inference_router_forwards_response_item_metadata() -> None:
    router = InferenceRouter(_ResponseItemMetadataProvider())

    chunks = [
        chunk
        async for chunk in router.route_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
            executor_labels={"location": "local"},
        )
    ]

    assert chunks[0]["response_item_id"] == "msg_1"
    assert chunks[0]["content_source"] == "response.output_text.delta"
    assert chunks[0]["response_message_phase"] == "commentary"


class _ErrorConnection:
    async def llm_complete_stream(self, **_: object):
        yield {
            "error": "provider rate limit",
            "response_error": {
                "category": "rate_limit",
                "message": "provider rate limit",
                "retry_after_seconds": 23,
            },
        }


class _ErrorProvider:
    def __init__(self) -> None:
        self.connection = _ErrorConnection()

    async def list_active(self):
        return [_handle("exec-1", {"labels": {"location": "local"}})]

    async def get_executor(self, handle: SimpleNamespace):
        assert handle.executor_id == "exec-1"
        return self.connection


@pytest.mark.asyncio
async def test_inference_router_preserves_structured_stream_errors() -> None:
    router = InferenceRouter(_ErrorProvider())

    chunks = [
        chunk
        async for chunk in router.route_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
            executor_labels={"location": "local"},
        )
    ]

    assert chunks == [
        {
            "error": "provider rate limit",
            "mid_stream_failure": True,
            "response_error": {
                "category": "rate_limit",
                "message": "provider rate limit",
                "retry_after_seconds": 23,
            },
        }
    ]


@pytest.mark.asyncio
async def test_inference_router_generate_raises_structured_provider_error() -> None:
    router = InferenceRouter(_ErrorProvider())

    with pytest.raises(LLMStreamProviderError) as exc_info:
        await router.route_generate(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-5.4",
            executor_labels={"location": "local"},
        )

    assert exc_info.value.to_payload()["category"] == "rate_limit"
    assert exc_info.value.to_payload()["retry_after_seconds"] == 23


class _FragmentedToolConnection:
    async def llm_complete_stream(self, **_: object):
        # A streamed tool call arrives as a name-only fragment plus N
        # argument fragments, all sharing the same index.
        yield {
            "content": None,
            "tool_calls": [{"index": 0, "id": "call_frag", "function": {"name": "write_file"}}],
            "reasoning_content": None,
            "index": 0,
        }
        yield {
            "content": None,
            "tool_calls": [{"index": 0, "function": {"arguments": '{"path": "/tmp'}}],
            "reasoning_content": None,
            "index": 1,
        }
        yield {
            "content": None,
            "tool_calls": [{"index": 0, "function": {"arguments": '/foo.txt"}'}}],
            "reasoning_content": None,
            "index": 2,
        }
        yield {
            "content": None,
            "tool_calls": [
                {
                    "index": 1,
                    "id": "call_second",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                }
            ],
            "reasoning_content": None,
            "index": 3,
        }
        yield {"done": True, "usage": {"total_tokens": 5}, "finish_reason": "tool_calls"}


class _FragmentedToolProvider:
    def __init__(self) -> None:
        self.connection = _FragmentedToolConnection()

    async def list_active(self):
        return [_handle("exec-1", {"labels": {"location": "local"}})]

    async def get_executor(self, handle: SimpleNamespace):
        return self.connection


@pytest.mark.asyncio
async def test_inference_router_route_generate_merges_fragmented_tool_calls() -> None:
    router = InferenceRouter(_FragmentedToolProvider())

    result = await router.route_generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        executor_labels={"location": "local"},
    )

    tool_calls = result["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 2
    assert tool_calls[0]["id"] == "call_frag"
    assert tool_calls[0]["function"]["name"] == "write_file"
    assert tool_calls[0]["function"]["arguments"] == '{"path": "/tmp/foo.txt"}'
    assert tool_calls[1]["id"] == "call_second"
    assert tool_calls[1]["function"]["arguments"] == '{"command": "ls"}'
    assert result["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_inference_router_route_transcribe_returns_result() -> None:
    router = InferenceRouter(_Provider())

    result = await router.route_transcribe(
        audio_bytes=b"abc",
        mime_type="audio/ogg",
        filename="voice.ogg",
        model="whisper-1",
        executor_labels={"location": "local"},
    )

    assert result.text == "hello from audio"
