"""Configured image routing survives controller and executor API boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.executor.inference import InferenceHandler
from cognis.executor.runner import ExecutorRunner
from cognis.models.config import ImageInput
from cognis.models.tool import ExecutorConfig
from cognis.providers.llm.inference_router import InferenceRouter
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["controller", "executor"])
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize(
    ("preset", "model", "wire_model", "adapter"),
    [
        ("openai", "gpt-image-future-synthetic", "openai/gpt-image-future-synthetic", "openai"),
        ("openai", "gpt-image-1", "openai/gpt-image-1", "openai"),
        ("openai", "dall-e-3", "openai/dall-e-3", "openai"),
        (
            "openai",
            "openai/gpt-image-future-synthetic",
            "openai/gpt-image-future-synthetic",
            "openai",
        ),
        (
            "openai_compatible",
            "studio/image-synthetic",
            "litellm_proxy/studio/image-synthetic",
            "litellm_proxy",
        ),
        (
            "litellm_proxy",
            "openai/image-synthetic",
            "litellm_proxy/openai/image-synthetic",
            "litellm_proxy",
        ),
        ("gemini", "gemini-image-synthetic", "gemini/gemini-image-synthetic", "gemini"),
        (
            "litellm_proxy",
            "gemini-image-synthetic",
            "litellm_proxy/gemini-image-synthetic",
            "litellm_proxy",
        ),
    ],
)
async def test_configured_image_routing(
    tmp_path, monkeypatch, location, edit, preset, model, wire_model, adapter
):
    import litellm

    monkeypatch.setenv("COGNIS_SYNTHETIC_IMAGE_KEY", "synthetic-image-key")
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/image-routing.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(
            LLMProvider(
                provider_id="synthetic-images",
                display_name="Synthetic images",
                location=location,
                backend="litellm",
                status="active",
                config={
                    "preset": preset,
                    "models": [{"model_id": model, "supports_image_generation": True}],
                    "api_base": "https://images.test/v1",
                    "auth_config": {"mode": "env", "env_var": "COGNIS_SYNTHETIC_IMAGE_KEY"},
                },
            )
        )
        await session.commit()

    generate = AsyncMock(return_value=litellm.ImageResponse(data=[{"b64_json": "YWJj"}]))
    edit_call = AsyncMock(return_value=litellm.ImageResponse(data=[{"b64_json": "YWJj"}]))
    complete = AsyncMock(
        return_value=SimpleNamespace(
            model_dump=lambda: {
                "choices": [
                    {"message": {"images": [{"image_url": {"url": "data:image/png;base64,YWJj"}}]}}
                ]
            }
        )
    )
    monkeypatch.setattr(litellm, "aimage_generation", generate)
    monkeypatch.setattr(litellm, "aimage_edit", edit_call)
    monkeypatch.setattr(litellm, "acompletion", complete)
    provider = LiteLLMProvider(factory)
    runner = ExecutorRunner(ExecutorConfig(executor_id="synthetic-images"))
    runner._inference_handler = InferenceHandler()
    runner._send_rpc_result = AsyncMock()
    runner._send_rpc_error = AsyncMock()

    async def rpc_call(*, method, params):
        assert method == "llm.image_generate"
        assert params["model"] == wire_model
        await runner._handle_llm_image_generate(None, params["request_id"], params)
        runner._send_rpc_error.assert_not_awaited()
        return runner._send_rpc_result.call_args.args[2]

    router = InferenceRouter(SimpleNamespace())
    monkeypatch.setattr(
        router, "_find_executor", AsyncMock(return_value=SimpleNamespace(rpc_call=rpc_call))
    )
    provider._inference_router = router
    images = [ImageInput(b64_json="YWJj"), ImageInput(b64_json="ZGVm")] if edit else None
    modalities = "gemini" in model
    mask = ImageInput(b64_json="bWFzaw==") if edit and not modalities else None
    try:
        result = await provider.image_generate(
            "Draw a synthetic square",
            model=f"synthetic-images/{model}",
            size="1024x1024",
            images=images,
            mask=mask,
        )
        assert len(result.images) == 1
        call = complete if modalities else edit_call if edit else generate
        call.assert_awaited_once()
        for other in (generate, edit_call, complete):
            if other is not call:
                other.assert_not_awaited()
        kwargs = call.await_args.kwargs
        assert kwargs["model"] == wire_model
        # Exercise LiteLLM's real local provider resolver without any network call.
        resolved, selected, _, _ = litellm.get_llm_provider(
            model=kwargs["model"], api_base=kwargs["api_base"]
        )
        assert selected == adapter
        assert resolved == wire_model.split("/", 1)[1]
        expected_base = (
            "https://images.test/v1/v1beta" if preset == "gemini" else "https://images.test/v1"
        )
        assert kwargs["api_base"] == expected_base
        assert kwargs["api_key"] == "synthetic-image-key"
        if modalities:
            assert kwargs["modalities"] == ["image", "text"]
            if edit:
                assert [
                    item["image_url"]["url"] for item in kwargs["messages"][0]["content"][:-1]
                ] == ["data:image/png;base64,YWJj", "data:image/png;base64,ZGVm"]
        else:
            assert ("response_format" in kwargs) == (
                not model.rsplit("/", 1)[-1].startswith("gpt-image-")
            )
            if edit:
                assert kwargs["image"] == [b"abc", b"def"]
                assert kwargs["mask"] == b"mask"
        # The shared prefix mapping used by chat is deliberately unchanged.
        async with factory() as session:
            row = await session.get(LLMProvider, "synthetic-images")
            if preset == "openai":
                assert provider._apply_model_prefix(model, row) == model
    finally:
        await runner._inference_handler.close()
        await engine.dispose()
