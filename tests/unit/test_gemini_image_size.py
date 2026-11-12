"""Gemini image size mapping through controller and executor LiteLLM boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from cognis.executor.backends.litellm import LiteLLMExecutorBackend
from cognis.models.config import ImageInput
from cognis.providers.llm.image_config import gemini_image_config
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider


@pytest_asyncio.fixture
async def provider(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / (uuid4().hex + '.db')}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        for preset, model in [
            ("gemini", "gemini-nano-banana-2.1"),
            ("openai", "gpt-image-1"),
        ]:
            session.add(
                LLMProvider(
                    provider_id=preset,
                    display_name=preset,
                    location="controller",
                    backend="litellm",
                    status="active",
                    config={"preset": preset, "default_model": model},
                )
            )
        await session.commit()
    try:
        yield LiteLLMProvider(factory)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("1536x1024", {"aspectRatio": "3:2", "imageSize": "1K"}),
        ("1024x1536", {"aspectRatio": "2:3", "imageSize": "1K"}),
        ("1024x1024", {"aspectRatio": "1:1", "imageSize": "1K"}),
        ("2048x2048", {"aspectRatio": "1:1", "imageSize": "2K"}),
        ("4096x4096", {"aspectRatio": "1:1", "imageSize": "4K"}),
        ("1535x1535", {"aspectRatio": "1:1", "imageSize": "1K"}),
        ("1536x1536", {"aspectRatio": "1:1", "imageSize": "2K"}),
        ("3071x3071", {"aspectRatio": "1:1", "imageSize": "2K"}),
        ("3072x3072", {"aspectRatio": "1:1", "imageSize": "4K"}),
        ("2560x1792", {"aspectRatio": "4:3", "imageSize": "2K"}),
        ("1792x2560", {"aspectRatio": "3:4", "imageSize": "2K"}),
        ("1920x1080", {"aspectRatio": "16:9", "imageSize": "1K"}),
        ("1280x896", {"aspectRatio": "4:3", "imageSize": "1K"}),
        ("512x512", {"aspectRatio": "1:1", "imageSize": "1K"}),
        (" 1536X1024 ", {"aspectRatio": "3:2", "imageSize": "1K"}),
        (None, None),
        ("auto", None),
        ("wide", None),
        ("0x1024", None),
        ("-1x1024", None),
        ("1024x", None),
        ("999999999999999999999x1", None),
    ],
)
def test_gemini_size_mapping(size, expected):
    assert gemini_image_config(size, "gemini/gemini-nano-banana-2.1") == expected


def test_gemini_25_flash_does_not_request_unsupported_resolution():
    assert gemini_image_config("4096x4096", "gemini/gemini-2.5-flash-image") == {
        "aspectRatio": "1:1"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["controller", "executor"])
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("size", ["1536x1024", None, "auto", "invalid"])
async def test_gemini_generation_and_edit_use_image_config(
    provider, monkeypatch, execution, edit, size
):
    response = SimpleNamespace(
        model_dump=lambda: {
            "choices": [
                {"message": {"images": [{"image_url": {"url": "data:image/png;base64,YWJj"}}]}}
            ],
            "usage": {},
        }
    )
    complete = AsyncMock(return_value=response)
    monkeypatch.setattr("litellm.acompletion", complete)
    images = [ImageInput(b64_json="YWJj")] if edit else None
    if execution == "controller":
        result = await provider.image_generate(
            prompt="draw",
            model="gemini-nano-banana-2.1",
            size=size,
            images=images,
            max_retries=0,
            timeout=30,
        )
        assert len(result.images) == 1
    else:
        result = await LiteLLMExecutorBackend().image_generate(
            prompt="draw",
            model="gemini/gemini-nano-banana-2.1",
            strategy="acompletion_modalities",
            size=size,
            images=images,
            request_kwargs={"api_base": "https://generativelanguage.googleapis.com/v1beta"},
        )
        assert len(result["images"]) == 1
    kwargs = complete.await_args.kwargs
    assert kwargs["modalities"] == ["image", "text"]
    assert kwargs["stream"] is False
    if execution == "controller":
        assert kwargs["timeout"] == 30
    for key in ("size", "image_size", "extra_body", "generationConfig"):
        assert key not in kwargs
    if size == "1536x1024":
        assert kwargs["imageConfig"] == {"aspectRatio": "3:2", "imageSize": "1K"}
    else:
        assert "imageConfig" not in kwargs
    if edit:
        assert kwargs["messages"][0]["content"][0] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,YWJj"},
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["controller", "executor"])
@pytest.mark.parametrize("edit", [False, True])
async def test_openai_image_size_unchanged(provider, monkeypatch, execution, edit):
    generate = AsyncMock(return_value=SimpleNamespace(data=[{"b64_json": "YWJj"}]))
    complete = AsyncMock()
    monkeypatch.setattr("litellm.aimage_generation", generate)
    monkeypatch.setattr("litellm.aimage_edit", generate)
    monkeypatch.setattr("litellm.acompletion", complete)
    images = [ImageInput(b64_json="YWJj")] if edit else None
    if execution == "controller":
        await provider.image_generate(
            prompt="draw", model="gpt-image-1", size="1536x1024", images=images
        )
    else:
        await LiteLLMExecutorBackend().image_generate(
            prompt="draw",
            model="openai/gpt-image-1",
            size="1536x1024",
            images=images,
            request_kwargs={},
        )
    kwargs = generate.await_args.kwargs
    assert kwargs["size"] == "1536x1024"
    assert "imageConfig" not in kwargs
    assert "extra_body" not in kwargs
    if edit:
        assert kwargs["image"] == [b"abc"]
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_gemini_size_preserves_litellm_serialized_modalities(
    provider, respx_mock, monkeypatch
):
    import json

    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    # LiteLLM's default transport may use aiohttp, which respx cannot intercept.
    # Replace client construction so this test cannot contact a live provider.
    client = httpx.AsyncClient(transport=httpx.MockTransport(respx_mock.handler))
    monkeypatch.setattr(AsyncHTTPHandler, "create_client", lambda *args, **kwargs: client)
    route = respx_mock.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-nano-banana-2.1:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"inlineData": {"mimeType": "image/png", "data": "YWJj"}}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 1,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 2,
                },
            },
        )
    )
    request_kwargs = {
        "api_key": "test-key",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
    }
    original = dict(request_kwargs)
    result = await provider._image_generate_via_completion(
        "gemini/gemini-nano-banana-2.1",
        "draw",
        request_kwargs,
        size="1536x1024",
        client=AsyncHTTPHandler(),
    )
    assert result.images
    assert request_kwargs == original
    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    config = payload["generationConfig"]
    assert {str(item).upper() for item in config["responseModalities"]} == {"IMAGE", "TEXT"}
    assert config["imageConfig"] == {"aspectRatio": "3:2", "imageSize": "1K"}
    for key in ("size", "image_size", "imageConfig", "extra_body"):
        assert key not in payload
    await client.aclose()
