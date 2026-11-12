"""Hosted provider contracts, native typed evaluation, and runtime parity."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from cognis.models.config import ModelInfo
from cognis.providers.llm.bedrock import discover_bedrock_models
from cognis.providers.llm.hosted import discover_hosted_models, is_systemone_model
from cognis.providers.llm.litellm import LiteLLMProvider, _provider_request_kwargs_from_config
from cognis.providers.llm.model_metadata import resolve_model_metadata
from cognis.providers.llm.reasoning import apply_reasoning_config
from cognis.providers.llm.systemone import (
    SystemOneRequest,
    SystemOneResponseError,
    validate_systemone_response,
)
from cognis.store.models import LLMProvider


@pytest.fixture(autouse=True)
def isolated_sdk_metadata_cache():
    """Real SDK tests must not leave metadata from another test's catalog."""
    import litellm

    litellm.get_model_info.cache_clear()
    yield
    litellm.get_model_info.cache_clear()


@pytest.mark.parametrize(
    ("preset", "model", "wire", "base"),
    [
        ("groq", "openai/gpt-oss-20b", "groq/openai/gpt-oss-20b", "https://api.groq.com/openai/v1"),
        (
            "openrouter",
            "anthropic/claude-sonnet-4.5",
            "openrouter/anthropic/claude-sonnet-4.5",
            "https://openrouter.ai/api/v1",
        ),
        (
            "gemini",
            "gemini-2.5-flash",
            "gemini/gemini-2.5-flash",
            "https://generativelanguage.googleapis.com/v1beta",
        ),
        (
            "bedrock",
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude",
            "bedrock/arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude",
            None,
        ),
    ],
)
def test_hosted_routing_preserves_wire_ids(preset, model, wire, base):
    provider = LLMProvider(provider_id="hosted", config={"preset": preset})
    assert LiteLLMProvider._apply_model_prefix(model, provider) == wire
    kwargs = _provider_request_kwargs_from_config(dict(provider.config))
    assert kwargs.get("api_base") == base


@pytest.mark.parametrize(
    ("base_config", "expected"),
    [
        ({}, "https://generativelanguage.googleapis.com/v1beta"),
        (
            {"base_url": "https://generativelanguage.googleapis.com"},
            "https://generativelanguage.googleapis.com/v1beta",
        ),
        (
            {
                "base_url": "https://generativelanguage.googleapis.com",
                "api_base": "https://generativelanguage.googleapis.com",
            },
            "https://generativelanguage.googleapis.com/v1beta",
        ),
        ({"base_url": "https://gateway.test/google/"}, "https://gateway.test/google/v1beta"),
        (
            {"base_url": "https://gateway.test/google/v1beta/"},
            "https://gateway.test/google/v1beta",
        ),
        ({"api_base": "https://gateway.test/google"}, "https://gateway.test/google/v1beta"),
    ],
)
def test_gemini_request_kwargs_use_only_versioned_api_base(base_config, expected):
    config = {"preset": "gemini", **base_config}
    original = dict(config)
    assert _provider_request_kwargs_from_config(config) == {"api_base": expected}
    assert config == original


@pytest.mark.parametrize(
    ("preset", "suffix"),
    [("anthropic", ""), ("groq", "/v1"), ("openrouter", "/v1"), ("ollama", "")],
)
def test_non_gemini_request_kwargs_preserve_base_url(preset, suffix):
    base = "https://gateway.test"
    assert _provider_request_kwargs_from_config({"preset": preset, "base_url": base}) == {
        "base_url": base + suffix,
        "api_base": base + suffix,
    }


@pytest.mark.asyncio
async def test_gemini_catalog_pagination_uses_header_auth_and_native_limits(respx_mock):
    route = respx_mock.get("https://generativelanguage.googleapis.com/v1beta/models")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.5-flash",
                        "inputTokenLimit": 1000000,
                        "outputTokenLimit": 65536,
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ],
                "nextPageToken": "second",
            },
        ),
        httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-embedding-001",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                    {"name": "models/image-only", "supportedGenerationMethods": ["predict"]},
                    {
                        "name": "models/veo-3.1-generate-preview",
                        "displayName": "Veo 3.1",
                        "supportedGenerationMethods": ["predictLongRunning"],
                    },
                    {
                        "name": "models/veo-3.1-fast-generate-preview",
                        "supportedGenerationMethods": ["predictLongRunning"],
                    },
                    {
                        "name": "models/other-long-running",
                        "supportedGenerationMethods": ["predictLongRunning"],
                    },
                ]
            },
        ),
    ]
    async with httpx.AsyncClient() as client:
        models = await discover_hosted_models(
            client, preset="gemini", base_url="", api_key="test-google-key"
        )
    assert [m["id"] for m in models] == [
        "gemini-2.5-flash",
        "gemini-embedding-001",
        "veo-3.1-generate-preview",
        "veo-3.1-fast-generate-preview",
    ]
    for model in models[2:]:
        assert model["model_mode"] == "video"
        assert model["supports_video_generation"]
        assert not model["supports_tools"]
        assert not model["supports_streaming"]
    assert models[0]["context_window"] == 1000000
    assert models[1]["supports_embedding"]
    assert route.calls[1].request.url.params["pageToken"] == "second"
    assert route.calls[0].request.headers["x-goog-api-key"] == "test-google-key"
    assert "key" not in route.calls[0].request.url.params


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["controller", "executor"])
async def test_gemini_video_discovery_preserves_metadata_on_both_runtimes(respx_mock, runtime):
    from cognis.core.routing_management import route_model_is_eligible
    from cognis.executor.inference import InferenceHandler

    respx_mock.get("https://generativelanguage.googleapis.com/v1beta/models").respond(
        200,
        json={
            "models": [
                {
                    "name": "models/veo-3.1-generate-preview",
                    "supportedGenerationMethods": ["predictLongRunning"],
                }
            ]
        },
    )
    provider = LiteLLMProvider(None)
    if runtime == "controller":
        entries = await provider._discover_models_remote("gemini", "", "test-key")
    else:

        class Router:
            async def discover_models(self, **kwargs):
                return await InferenceHandler().discover_models(
                    preset=kwargs["preset"], base_url=kwargs["base_url"], api_key=kwargs["api_key"]
                )

        provider._inference_router = Router()
        entries = await provider._discover_models_via_executor(
            preset="gemini",
            base_url="",
            api_key="test-key",
            executor_id="catalog-executor",
            executor_labels=None,
        )
    assert len(entries) == 1
    info = ModelInfo(
        **{key: value for key, value in entries[0].items() if key in ModelInfo.model_fields}
    )
    assert info.model_id == "veo-3.1-generate-preview"
    assert info.model_mode == "video" and info.supports_video_generation
    assert not info.supports_tools and not info.supports_streaming
    assert route_model_is_eligible("video_generation", model_id=info.model_id, model_info=info)
    assert not route_model_is_eligible("default", model_id=info.model_id, model_info=info)


def test_jev_metadata_cannot_inherit_chat_defaults():
    for preset, model in (("openrouter", "typesafe/jev-1.13"), ("systemone", "jev-latest")):
        metadata = resolve_model_metadata(model, preset=preset)
        assert metadata["model_mode"] == "systemone"
        assert not metadata["supports_tools"] and not metadata["supports_streaming"]


@pytest.mark.parametrize(
    "preset",
    [
        "openrouter",
        "groq",
        "gemini",
        "bedrock",
        "openai_compatible",
        "litellm_proxy",
        "openai",
        "anthropic",
        "ollama",
        "chatgpt",
        "azure",
    ],
)
@pytest.mark.parametrize(
    ("model", "outputs", "expected"),
    [
        ("typesafe/jev-router", ["text"], "chat"),
        ("typesafe/jev-1.13", ["decisions"], "systemone"),
        ("~typesafe/jev-latest", ["decisions"], "systemone"),
        ("classifier-without-jev-prefix", ["decisions"], "systemone"),
        ("embedding-without-name-hint", ["embeddings"], "embedding"),
        ("jev-named-chat-model", ["text"], "chat"),
        ("typesafe/jev-1.13", ["text"], "chat"),
        ("image-model", ["image"], "image_generation"),
        ("multimodal-chat", ["text", "image"], "chat"),
    ],
)
def test_provider_modalities_classify_models_not_product_names(preset, model, outputs, expected):
    metadata = resolve_model_metadata(
        model,
        preset=preset,
        remote={
            "architecture": {"output_modalities": outputs},
            "supported_parameters": ["tools", "reasoning"],
            "pricing": {"prompt": "0.000000042", "completion": "0"},
        },
    )
    assert metadata["model_mode"] == expected
    assert metadata["supports_embedding"] == (expected == "embedding")
    assert is_systemone_model(model, preset, metadata) == (expected == "systemone")
    assert metadata["input_cost_per_mtok"] == 0.042
    assert metadata["output_cost_per_mtok"] == 0
    if expected == "systemone":
        assert not metadata["supports_tools"] and not metadata["supports_reasoning"]
    elif expected == "chat":
        assert metadata["supports_tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_rejects_decision_protocol_even_without_jev_name(stream):
    provider = LiteLLMProvider(None)
    row = LLMProvider(
        provider_id="typed-router", location="controller", config={"preset": "openrouter"}
    )
    provider._resolve_model_target = AsyncMock(return_value=("classifier-v2", row))
    provider.get_model_info = AsyncMock(
        return_value=ModelInfo(model_id="classifier-v2", model_mode="systemone")
    )
    provider._resolve_provider_kwargs = AsyncMock()
    with pytest.raises(ValueError, match="typed evaluation API"):
        if stream:
            async for _ in provider.stream_generate([], model="classifier-v2"):
                pass
        else:
            await provider.generate([], model="classifier-v2")
    provider._resolve_provider_kwargs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["embedding", "image_generation", "audio_speech", "rerank"])
@pytest.mark.parametrize("stream", [False, True])
async def test_full_catalog_nonchat_models_do_not_enter_chat_generation(mode, stream):
    provider = LiteLLMProvider(None)
    row = LLMProvider(
        provider_id="full-catalog", location="controller", config={"preset": "openrouter"}
    )
    provider._resolve_model_target = AsyncMock(return_value=("nonchat-model", row))
    provider.get_model_info = AsyncMock(
        return_value=ModelInfo(model_id="nonchat-model", model_mode=mode)
    )
    provider._resolve_provider_kwargs = AsyncMock()
    with pytest.raises(ValueError, match="does not support chat generation"):
        if stream:
            async for _ in provider.stream_generate([], model="nonchat-model"):
                pass
        else:
            await provider.generate([], model="nonchat-model")
    provider._resolve_provider_kwargs.assert_not_awaited()


@pytest.mark.parametrize(
    ("mode", "allowed_route"),
    [
        ("embedding", "embedding"),
        ("image_generation", "image_generation"),
        ("audio_speech", "text_to_speech"),
        ("audio_transcription", "speech_to_text"),
        ("systemone", "intaris_evaluation"),
    ],
)
def test_settings_routing_honors_protocol_not_arbitrary_model_name(mode, allowed_route):
    from cognis.core.routing_management import route_model_is_eligible as _route_model_is_eligible

    info = ModelInfo(
        model_id="arbitrary-model-name",
        model_mode=mode,
        supports_embedding=mode == "embedding",
        supports_image_generation=mode == "image_generation",
        supports_audio_input=True,
    )
    assert _route_model_is_eligible(allowed_route, model_id=info.model_id, model_info=info)
    for text_route in ("primary", "compaction", "companion_lite", "attachment_analysis"):
        assert not _route_model_is_eligible(text_route, model_id=info.model_id, model_info=info)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "mode", "allowed"),
    [
        ("classifier-v2", "systemone", True),
        ("typesafe/jev-router", "chat", False),
    ],
)
async def test_systemone_attempt_uses_resolved_protocol(model, mode, allowed):
    from cognis.providers.llm.service import LLMService
    from cognis.providers.llm.systemone import SystemOneRequest

    service = LLMService(None, usage_capture=None)
    row = LLMProvider(provider_id="typed-router", config={"preset": "openrouter"})
    service._resolve_model_target = AsyncMock(return_value=(model, row))
    service.get_model_info = AsyncMock(return_value=ModelInfo(model_id=model, model_mode=mode))
    service._system_one_for_provider = AsyncMock(return_value={"model": model, "answers": {}})
    payload = SystemOneRequest(
        model=model, state="check", questions={"valid": {"type": "noul", "instructions": "Check"}}
    )
    if allowed:
        await service._system_one_once(
            payload, provider_id=row.provider_id, acting_user_email="owner@example.org"
        )
        service._system_one_for_provider.assert_awaited_once()
    else:
        with pytest.raises(ValueError, match="does not support System One"):
            await service._system_one_once(
                payload, provider_id=row.provider_id, acting_user_email="owner@example.org"
            )
        service._system_one_for_provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["openrouter", "openai_compatible", "litellm_proxy"])
@pytest.mark.parametrize("runtime", ["controller", "executor"])
async def test_openrouter_discovery_fetches_decision_models_on_both_runtimes(
    respx_mock, preset, runtime
):
    from cognis.executor.inference import InferenceHandler

    models = [
        {
            "id": "typesafe/jev-router",
            "architecture": {"output_modalities": ["text"]},
            "supported_parameters": ["tools", "reasoning"],
        },
        {
            "id": "typesafe/jev-1.13",
            "architecture": {"output_modalities": ["decisions"]},
            "pricing": {"prompt": "0.000000042", "completion": "0"},
        },
        {"id": "~typesafe/jev-latest", "architecture": {"output_modalities": ["decisions"]}},
        {
            "id": "vector-model",
            "architecture": {"output_modalities": ["embeddings"]},
            "pricing": {"prompt": "0.0000001", "completion": "0"},
        },
    ]

    def catalog(request):
        visible = models if request.url.params.get("output_modalities") == "all" else models[:1]
        return httpx.Response(200, json={"data": visible})

    route = respx_mock.get("https://openrouter.ai/api/v1/models").mock(side_effect=catalog)
    base = "https://openrouter.ai/api/v1"
    provider = LiteLLMProvider(None)
    if runtime == "controller":
        entries = await provider._discover_models_remote(preset, base, "test-key")
    else:

        class Router:
            async def discover_models(self, **kwargs):
                return await InferenceHandler().discover_models(
                    preset=kwargs["preset"], base_url=kwargs["base_url"], api_key=kwargs["api_key"]
                )

        provider._inference_router = Router()
        entries = await provider._discover_models_via_executor(
            preset=preset,
            base_url=base,
            api_key="test-key",
            executor_id="catalog-executor",
            executor_labels=None,
        )
    assert [m["model_id"] for m in entries] == [m["id"] for m in models]
    assert [m["model_mode"] for m in entries] == ["chat", "systemone", "systemone", "embedding"]
    assert entries[1]["input_cost_per_mtok"] == 0.042
    assert entries[3]["input_cost_per_mtok"] == 0.1
    assert entries[3]["supports_embedding"] is True
    from cognis.core.routing_management import route_model_is_eligible as _route_model_is_eligible

    embedding_info = ModelInfo(
        **{key: value for key, value in entries[3].items() if key in ModelInfo.model_fields}
    )
    assert _route_model_is_eligible(
        "embedding", model_id=embedding_info.model_id, model_info=embedding_info
    )
    assert not _route_model_is_eligible(
        "primary", model_id=embedding_info.model_id, model_info=embedding_info
    )
    assert route.calls.last.request.url.params["output_modalities"] == "all"


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["openai_compatible", "litellm_proxy"])
@pytest.mark.parametrize("runtime", ["controller", "executor"])
async def test_compatible_openrouter_typed_dispatch_on_both_runtimes(respx_mock, preset, runtime):
    from cognis.executor.backends.litellm import LiteLLMExecutorBackend
    from cognis.executor.inference_types import CognisInferenceRequest
    from cognis.providers.llm.service import LLMService

    base_url = "https://openrouter.ai/api/v1"
    upstream = {
        "model": "typesafe/jev-1.13",
        "answers": {"valid": {"type": "noul", "noul": 0.9}},
    }
    route = respx_mock.post(f"{base_url}/systemone").respond(200, json=upstream)
    service = LLMService(None, usage_capture=None)
    row = LLMProvider(
        provider_id="compatible-router",
        location=runtime,
        owner_email="owner@example.org",
        config={"preset": preset, "base_url": base_url},
    )
    service._resolve_model_target = AsyncMock(return_value=("typesafe/jev-1.13", row))
    service.get_model_info = AsyncMock(
        return_value=ModelInfo(model_id="typesafe/jev-1.13", model_mode="systemone")
    )
    service._resolve_provider_kwargs = AsyncMock(
        return_value={"api_base": base_url, "api_key": "test-router-key"}
    )
    if runtime == "executor":

        async def executor(model, messages, provider, *, request_kwargs, acting_user_email):
            from cognis.providers.llm.usage_binding import take_binding

            request_kwargs = dict(request_kwargs)
            take_binding(request_kwargs, model=model)
            assert request_kwargs["preset"] == "openrouter"
            return await LiteLLMExecutorBackend().generate(
                CognisInferenceRequest(
                    model=model, messages=messages, request_kwargs=request_kwargs, stream=False
                )
            )

        service._executor_generate = executor
    payload = SystemOneRequest(
        model="typesafe/jev-1.13",
        state="check",
        questions={"valid": {"type": "noul", "instructions": "Check"}},
    )
    assert (
        await service._system_one_once(
            payload, provider_id=row.provider_id, acting_user_email=row.owner_email
        )
        == upstream
    )
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-router-key"


@pytest.mark.parametrize("preset", ["groq", "systemone"])
@pytest.mark.asyncio
async def test_other_hosted_catalogs_do_not_receive_openrouter_filter(respx_mock, preset):
    from cognis.providers.llm.hosted import PROVIDER_BASES

    route = respx_mock.get(f"{PROVIDER_BASES[preset]}/models").respond(
        200, json={"data": [{"id": "native-model"}]}
    )
    async with httpx.AsyncClient() as client:
        await discover_hosted_models(client, preset=preset, base_url="", api_key="test-key")
    assert "output_modalities" not in route.calls.last.request.url.params


def test_native_reasoning_uses_hosting_contract_not_publisher_contract():
    info = ModelInfo(model_id="anthropic/claude-sonnet-4.5", supports_reasoning=True)
    prepared = apply_reasoning_config(
        {"reasoning_effort": "medium"},
        model_id=info.model_id,
        model_info=info,
        provider_preset="openrouter",
    )
    assert prepared.request_kwargs["reasoning"] == {"effort": "medium"}
    assert "thinking" not in prepared.request_kwargs
    info = ModelInfo(model_id="gemini-3-pro-preview", supports_reasoning=True)
    prepared = apply_reasoning_config(
        {"reasoning_effort": "high"},
        model_id=info.model_id,
        model_info=info,
        provider_preset="gemini",
    )
    assert prepared.request_kwargs["reasoning_effort"] == "high"
    with pytest.raises(ValueError, match="disabled"):
        apply_reasoning_config(
            {"reasoning_effort": "none"},
            model_id=info.model_id,
            model_info=info,
            provider_preset="gemini",
        )


def test_typed_contract_preserves_answers_and_rejects_invalid_values():
    payload = SystemOneRequest.model_validate(
        {
            "model": "jev-1.13",
            "state": ["state"],
            "questions": {
                "choice": {
                    "type": "choice",
                    "instructions": "Pick",
                    "criteria": {"a": "A", "b": "B"},
                },
                "score": {"type": "score", "instructions": "Score", "criteria": ["Low", "High"]},
                "noul": {"type": "noul", "instructions": {"statement": "Is it safe?"}},
            },
        }
    )
    data = {
        "model": "jev-1.13",
        "answers": {
            "choice": {
                "type": "choice",
                "choice": "a",
                "confidence": 0.8,
                "probabilities": {"a": 0.9, "b": 0.1},
            },
            "score": {
                "type": "score",
                "score": 0.7,
                "confidence": 0.9,
                "probabilities": {"0": 0.3, "1": 0.7},
                "legend": {"0": "Low", "1": "High"},
            },
            "noul": {"type": "noul", "noul": 0.98},
        },
        "usage": {"input_tokens": 12},
    }
    assert validate_systemone_response(data, payload) == data
    data["answers"]["noul"]["noul"] = float("nan")
    with pytest.raises(SystemOneResponseError, match="^systemone_response_schema$"):
        validate_systemone_response(data, payload)
    data["answers"]["noul"]["noul"] = 1
    del data["answers"]["choice"]
    with pytest.raises(SystemOneResponseError, match="^systemone_answer_set$"):
        validate_systemone_response(data, payload)


@pytest.mark.asyncio
async def test_bedrock_catalog_uses_runtime_chain_and_paginates(monkeypatch):
    import boto3

    client = MagicMock()
    client.list_foundation_models.return_value = {
        "modelSummaries": [
            {
                "modelId": "amazon.nova-lite-v1:0",
                "outputModalities": ["TEXT"],
                "inferenceTypesSupported": ["ON_DEMAND"],
                "responseStreamingSupported": True,
            },
            {
                "modelId": "amazon.titan-embed-text-v2:0",
                "outputModalities": ["EMBEDDING"],
                "inferenceTypesSupported": ["ON_DEMAND"],
            },
            {
                "modelId": "provisioned",
                "outputModalities": ["TEXT"],
                "inferenceTypesSupported": ["PROVISIONED"],
            },
        ]
    }
    client.list_inference_profiles.side_effect = [
        {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "us.amazon.nova-lite-v1:0", "status": "ACTIVE"}
            ],
            "nextToken": "next",
        },
        {"inferenceProfileSummaries": []},
    ]
    session = MagicMock()
    session.client.return_value = client
    factory = MagicMock(return_value=session)
    monkeypatch.setattr(boto3, "Session", factory)
    models = await discover_bedrock_models(region="us-east-1", profile="development")
    factory.assert_called_once_with(profile_name="development", region_name="us-east-1")
    assert [m["model_id"] for m in models] == [
        "amazon.nova-lite-v1:0",
        "amazon.titan-embed-text-v2:0",
        "us.amazon.nova-lite-v1:0",
    ]
    assert models[1]["supports_embedding"]
    assert client.list_inference_profiles.call_args.kwargs["nextToken"] == "next"
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_executor_native_decision_transport(respx_mock):
    from cognis.executor.backends.litellm import LiteLLMExecutorBackend
    from cognis.executor.inference_types import CognisInferenceRequest

    payload = {
        "model": "jev-1.13",
        "state": "state",
        "questions": {"ok": {"type": "noul", "instructions": "Is it safe?"}},
    }
    output = {"model": "jev-1.13", "answers": {"ok": {"type": "noul", "noul": 1}}}
    respx_mock.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=output)
    )
    result = await LiteLLMExecutorBackend().generate(
        CognisInferenceRequest(
            model="jev-1.13",
            messages=[],
            request_kwargs={
                "cognis_llm_api": "systemone",
                "payload": payload,
                "preset": "systemone",
                "api_key": "test-typesafe-key",
                "timeout": 3.5,
            },
        )
    )
    assert result == output


@pytest.mark.parametrize(
    "probabilities,choice",
    [
        ({"a": 0, "b": 0}, "a"),
        ({"a": 1, "b": 1}, "a"),
        ({"a": 0.1, "b": 0.9}, "a"),
    ],
)
def test_invalid_decision_distribution_is_rejected(probabilities, choice):
    payload = SystemOneRequest.model_validate(
        {
            "model": "jev-1.13",
            "state": "state",
            "questions": {
                "q": {"type": "choice", "instructions": "Pick", "criteria": {"a": "A", "b": "B"}}
            },
        }
    )
    with pytest.raises(ValueError):
        validate_systemone_response(
            {
                "model": "jev-1.13",
                "answers": {
                    "q": {
                        "type": "choice",
                        "choice": choice,
                        "probabilities": probabilities,
                        "confidence": 0.5,
                    },
                },
            },
            payload,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["controller", "executor"])
@pytest.mark.parametrize(
    "preset,model,url",
    [
        (
            "openrouter",
            "anthropic/claude-sonnet-4.5",
            "https://openrouter.ai/api/v1/chat/completions",
        ),
        ("openrouter", "openrouter/auto", "https://openrouter.ai/api/v1/chat/completions"),
        ("groq", "openai/gpt-oss-20b", "https://api.groq.com/openai/v1/chat/completions"),
    ],
)
async def test_locked_sdk_preserves_upstream_publisher_model(sdk_http, runtime, preset, model, url):
    from cognis.executor.backends.litellm import LiteLLMExecutorBackend
    from cognis.executor.inference_types import CognisInferenceRequest
    from cognis.providers.llm.transport import LiteLLMTransport

    client, requests, responses = sdk_http
    responses.append(
        httpx.Response(
            200,
            json={
                "id": "chat-test",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "service_tier": "on_demand",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
    )
    provider = LLMProvider(provider_id="wire", config={"preset": preset})
    wire_model = LiteLLMProvider._apply_model_prefix(model, provider)
    kwargs = {
        **_provider_request_kwargs_from_config(provider.config),
        "api_key": "test-hosted-key",
        "num_retries": 0,
        "client": client,
    }
    messages = [{"role": "user", "content": "Check"}]
    if runtime == "controller":
        await LiteLLMTransport().completion(model=wire_model, messages=messages, **kwargs)
    else:
        await LiteLLMExecutorBackend().generate(
            CognisInferenceRequest(
                model=wire_model, messages=messages, request_kwargs=kwargs, stream=False
            )
        )
    assert str(requests[-1].url) == url
    assert json.loads(requests[-1].content)["model"] == model


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["controller", "executor"])
@pytest.mark.parametrize("effort", ["low", "high"])
async def test_locked_gemini_sdk_version_and_thinking_wire(sdk_http, runtime, effort):
    from cognis.executor.backends.litellm import LiteLLMExecutorBackend
    from cognis.executor.inference_types import CognisInferenceRequest
    from cognis.providers.llm.transport import LiteLLMTransport

    model = "gemini-3-pro-preview"
    client, requests, responses = sdk_http
    responses.append(
        httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "index": 0,
                        "content": {"role": "model", "parts": [{"text": "OK"}]},
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
    provider = LLMProvider(provider_id="gemini", config={"preset": "gemini"})
    kwargs = apply_reasoning_config(
        {
            **_provider_request_kwargs_from_config(provider.config),
            "reasoning_effort": effort,
            "api_key": "test-google-key",
            "num_retries": 0,
            "client": client,
        },
        model_id=model,
        model_info=ModelInfo(model_id=model, supports_reasoning=True),
        provider_preset="gemini",
    ).request_kwargs
    wire_model = LiteLLMProvider._apply_model_prefix(model, provider)
    messages = [{"role": "user", "content": "Check"}]
    if runtime == "controller":
        await LiteLLMTransport().completion(model=wire_model, messages=messages, **kwargs)
    else:
        await LiteLLMExecutorBackend().generate(
            CognisInferenceRequest(
                model=wire_model, messages=messages, request_kwargs=kwargs, stream=False
            )
        )
    assert requests[-1].url.path == f"/v1beta/models/{model}:generateContent"
    config = json.loads(requests[-1].content)["generationConfig"]
    assert config["thinkingConfig"]["thinkingLevel"] == effort


@pytest_asyncio.fixture
async def sdk_http(monkeypatch):
    """Intercept the SDK's actual HTTP client even when it uses an aiohttp backend."""
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    requests = []
    responses = []

    def handle(request):
        requests.append(request)
        if not responses:
            raise AssertionError("Unexpected SDK HTTP request")
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        monkeypatch.setattr(AsyncHTTPHandler, "create_client", lambda self, **kwargs: http)
        yield AsyncHTTPHandler(), requests, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["controller", "executor"])
async def test_gemini_streaming_uses_versioned_sdk_url(sdk_http, runtime):
    from cognis.executor.backends.litellm import LiteLLMExecutorBackend
    from cognis.executor.inference_types import CognisInferenceRequest
    from cognis.providers.llm.transport import LiteLLMTransport

    client, requests, responses = sdk_http
    model = "gemini-2.5-flash"
    chunk = {
        "candidates": [
            {
                "index": 0,
                "content": {"role": "model", "parts": [{"text": "OK"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    }
    responses.append(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\n",
        )
    )
    provider = LLMProvider(provider_id="gemini", config={"preset": "gemini"})
    kwargs = {
        **_provider_request_kwargs_from_config(provider.config),
        "api_key": "test-google-key",
        "client": client,
        "num_retries": 0,
    }
    wire_model = LiteLLMProvider._apply_model_prefix(model, provider)
    messages = [{"role": "user", "content": "Check"}]
    if runtime == "controller":
        stream = await LiteLLMTransport().completion(
            model=wire_model, messages=messages, stream=True, **kwargs
        )
        chunks = [c async for c in stream]
        assert any(c.choices and c.choices[0].delta.content == "OK" for c in chunks)
    else:
        chunks = [
            c
            async for c in LiteLLMExecutorBackend().stream_complete(
                CognisInferenceRequest(model=wire_model, messages=messages, request_kwargs=kwargs)
            )
        ]
        assert any(c.get("content") == "OK" for c in chunks)
    assert requests[-1].url.path == f"/v1beta/models/{model}:streamGenerateContent"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["controller", "executor"])
async def test_gemini_embeddings_native_url_and_response(sdk_http, runtime):
    import litellm

    from cognis.executor.backends.litellm import LiteLLMExecutorBackend
    from cognis.executor.inference_types import CognisInferenceRequest

    client, requests, responses = sdk_http
    responses.append(httpx.Response(200, json={"embeddings": [{"values": [0.1, 0.2]}]}))
    model = "gemini-embedding-001"
    provider = LLMProvider(provider_id="gemini", config={"preset": "gemini"})
    kwargs = {
        **_provider_request_kwargs_from_config(provider.config),
        "api_key": "test-google-key",
        "client": client,
    }
    wire_model = LiteLLMProvider._apply_model_prefix(model, provider)
    if runtime == "controller":
        result = (await litellm.aembedding(model=wire_model, input=["text"], **kwargs)).model_dump()
    else:
        result = await LiteLLMExecutorBackend().generate(
            CognisInferenceRequest(
                model=wire_model,
                messages=[],
                request_kwargs={**kwargs, "cognis_llm_api": "embeddings", "input": ["text"]},
                stream=False,
            )
        )
    assert result["data"][0]["embedding"] == [0.1, 0.2]
    assert requests[-1].url.path == f"/v1beta/models/{model}:batchEmbedContents"


@pytest.mark.asyncio
async def test_bedrock_embedding_location_and_aws_config_are_resolved_before_dispatch(monkeypatch):
    import litellm

    provider = LLMProvider(
        provider_id="bedrock",
        location="executor",
        owner_email="owner@test.local",
        config={
            "preset": "bedrock",
            "aws_region_name": "us-east-1",
            "aws_profile_name": "executor-only",
        },
    )
    service = LiteLLMProvider(MagicMock())
    monkeypatch.setattr(
        service,
        "_resolve_model_target",
        AsyncMock(return_value=("amazon.titan-embed-text-v2:0", provider)),
    )
    remote = AsyncMock(
        return_value={
            "data": [{"index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 3},
        }
    )
    monkeypatch.setattr(service, "_executor_generate", remote)
    local = AsyncMock(side_effect=AssertionError("Controller must not run executor inference"))
    monkeypatch.setattr(litellm, "aembedding", local)
    result = await service.embed_response(["text"], acting_user_email="owner@test.local")
    local.assert_not_called()
    assert result["usage"] == {"prompt_tokens": 3}
    wire = remote.call_args.kwargs["request_kwargs"]
    assert remote.call_args.args[0] == "bedrock/amazon.titan-embed-text-v2:0"
    assert wire["aws_region_name"] == "us-east-1"
    assert wire["aws_profile_name"] == "executor-only"
    assert wire["cognis_llm_api"] == "embeddings" and wire["input"] == ["text"]
