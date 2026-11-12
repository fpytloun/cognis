"""Exercise real LiteLLM serialization against a network-isolated image API."""

from __future__ import annotations

import base64
import json
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cognis.executor.backends.litellm import LiteLLMExecutorBackend
from cognis.executor.inference import InferenceHandler
from cognis.executor.runner import ExecutorRunner
from cognis.models.config import ImageInput
from cognis.models.tool import ExecutorConfig
from cognis.providers.llm.image_api import image_api_kwargs
from cognis.providers.llm.inference_router import InferenceRouter
from cognis.providers.llm.litellm import LiteLLMProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["controller", "executor", "executor_backend"])
@pytest.mark.parametrize(
    ("preset", "model"),
    [
        ("openai", "gpt-image-future-synthetic"),
        ("openai", "gpt-image-2.6-flare"),
        ("openai", "gpt-image-2.6-sunburst"),
        ("openai_compatible", "studio/gpt-image-synthetic"),
        ("litellm_proxy", "studio/gpt-image-synthetic"),
    ],
)
@pytest.mark.parametrize("operation", ["generate", "references", "masked_edit", "rejected_edit"])
async def test_image_operation_http_boundary(monkeypatch, runtime, preset, model, operation):
    import litellm
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    from litellm.llms.openai.openai import OpenAIChatCompletion

    # Synthetic bytes stay byte-identical in multipart uploads; nothing is read
    # from the filesystem, downloaded, or sent to a live provider.
    source_bytes = [b"\x89PNG\r\n\x1a\n" + bytes([index]) * 16 for index in range(4)]
    mask_bytes = b"\x89PNG\r\n\x1a\nsynthetic-mask"
    images = (
        [ImageInput(b64_json=base64.b64encode(data).decode()) for data in source_bytes]
        if operation != "generate"
        else None
    )
    mask = (
        ImageInput(b64_json=base64.b64encode(mask_bytes).decode())
        if operation == "masked_edit"
        else None
    )
    # Publisher namespaces must remain literal for compatible/proxy endpoints.
    base = "https://images.test/custom/v1"
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-image-key"
        if request.url.path.endswith("/generations"):
            payload = json.loads(request.content)
            # Reproduce the upstream's rejection rather than accepting arbitrary
            # kwargs as the previous provider mocks did.
            if "image" in payload or "mask" in payload:
                return httpx.Response(400, json={"error": {"message": "Unknown parameter: image"}})
            assert operation == "generate"
            assert request.url == f"{base}/images/generations"
            fields = payload
        else:
            assert request.url == f"{base}/images/edits"
            assert operation != "generate"
            assert request.headers["content-type"].startswith("multipart/form-data;")
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\nMIME-Version: 1.0\r\n\r\n"
                + request.content
            )
            parts = list(message.iter_parts())
            uploads = [
                part.get_payload(decode=True)
                for part in parts
                if part.get_param("name", header="content-disposition") == "image[]"
            ]
            assert uploads == source_bytes
            masks = [
                part.get_payload(decode=True)
                for part in parts
                if part.get_param("name", header="content-disposition") == "mask"
            ]
            assert masks == ([mask_bytes] if mask is not None else [])
            fields = {
                part.get_param("name", header="content-disposition"): part.get_payload(
                    decode=True
                ).decode()
                for part in parts
                if part.get_filename() is None
            }
            if operation == "rejected_edit":
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Synthetic edit rejection",
                            "type": "invalid_request_error",
                        }
                    },
                )
        assert fields["model"] == model
        assert fields["prompt"] == "Compose a synthetic scene"
        assert str(fields["n"]) == "1"
        assert fields["size"] == "1536x1024"
        assert fields["quality"] == "high"
        assert "response_format" not in fields
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "YWJj"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(AsyncHTTPHandler, "create_client", lambda *args, **kwargs: client)
    handler = AsyncHTTPHandler()
    monkeypatch.setattr(
        "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
        lambda *args, **kwargs: handler,
    )
    monkeypatch.setattr(
        OpenAIChatCompletion, "_get_async_http_client", lambda *args, **kwargs: client
    )
    monkeypatch.setattr(
        OpenAIChatCompletion, "get_cached_openai_client", lambda *args, **kwargs: None
    )
    provider = LiteLLMProvider(None)
    target = SimpleNamespace(location=runtime, config={"preset": preset})
    provider._resolve_model_target = AsyncMock(return_value=(model, target))
    connection_kwargs = {"api_key": "synthetic-image-key", "api_base": base}
    provider._resolve_provider_kwargs = AsyncMock(return_value=connection_kwargs)
    runner = ExecutorRunner(ExecutorConfig(executor_id="synthetic-images"))
    runner._inference_handler = InferenceHandler()
    runner._send_rpc_result = AsyncMock()
    runner._send_rpc_error = AsyncMock()

    async def rpc_call(*, method, params):
        assert method == "llm.image_generate"
        await runner._handle_llm_image_generate(None, params["request_id"], params)
        if runner._send_rpc_error.await_count:
            raise RuntimeError(runner._send_rpc_error.call_args.args[3])
        return runner._send_rpc_result.call_args.args[2]

    router = InferenceRouter(SimpleNamespace())
    monkeypatch.setattr(
        router, "_find_executor", AsyncMock(return_value=SimpleNamespace(rpc_call=rpc_call))
    )
    provider._inference_router = router

    async def execute():
        arguments = dict(
            prompt="Compose a synthetic scene",
            model=model,
            images=images,
            mask=mask,
            size="1536x1024",
            quality="high",
            n=1,
        )
        if runtime == "executor_backend":
            arguments["model"] = provider._apply_model_prefix(model, target)
            if preset == "openai":
                arguments["model"] = f"openai/{model}"
            return await LiteLLMExecutorBackend().image_generate(
                **arguments, request_kwargs=connection_kwargs
            )
        return await provider.image_generate(**arguments)

    try:
        if operation == "rejected_edit":
            with pytest.raises(
                (litellm.BadRequestError, RuntimeError), match="Synthetic edit rejection"
            ):
                await execute()
        else:
            result = await execute()
            assert len(result["images"] if isinstance(result, dict) else result.images) == 1
        assert len(calls) == 1  # No retry or fallback dropping the references.
    finally:
        await runner._inference_handler.close()
        await client.aclose()


@pytest.mark.parametrize(
    ("images", "mask", "error"),
    [
        ([], None, "at least one"),
        (None, ImageInput(b64_json="YWJj"), "requires input images"),
        ([ImageInput(b64_json="not base64")], None, "valid base64"),
        ([ImageInput(b64_json="")], None, "must not be empty"),
    ],
)
def test_image_api_rejects_invalid_inputs(images, mask, error):
    with pytest.raises(ValueError, match=error):
        image_api_kwargs(
            "openai/gpt-image-synthetic", response_format="b64_json", images=images, mask=mask
        )
