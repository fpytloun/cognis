from __future__ import annotations

import json
from pathlib import Path

import httpx
import litellm
import pytest

from cognis.models.config import ModelInfo
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.providers.llm.model_metadata import (
    catalog_provider_for,
    normalize_model_metadata,
    resolve_model_metadata,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider


@pytest.mark.parametrize("model_id", ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"])
def test_gpt_image_26_documented_catalog_metadata(model_id):
    metadata = resolve_model_metadata(model_id, preset="openai")
    assert metadata["model_mode"] == "image_generation"
    assert metadata["supports_image_generation"] is True
    assert metadata["supports_vision"] is True
    assert metadata["supports_tools"] is False
    assert metadata["supports_streaming"] is False
    assert metadata["supports_structured_output"] is False
    assert "supported_openai_params" not in metadata
    assert metadata["input_cost_per_mtok"] == 5
    assert metadata["cache_read_cost_per_mtok"] == 1.25
    assert metadata["pricing"]["input_cost_per_image_token"] == 0.000008
    assert metadata["pricing"]["output_cost_per_image_token"] == 0.00003
    assert metadata["pricing"]["cache_read_input_image_token_cost"] == 0.000002
    # OpenAI pricing/capabilities must not leak to an unknown compatible host.
    compatible = resolve_model_metadata(
        model_id, preset="openai_compatible", base_url="https://images.test"
    )
    assert "pricing" not in compatible
    assert "supports_image_generation" not in compatible


@pytest.mark.parametrize(
    ("preset", "url", "explicit", "expected"),
    [
        ("openai_compatible", "https://api.groq.com/openai", None, "groq"),
        ("openai_compatible", "https://api.groq.com.attacker.org/openai", None, None),
        ("openai_compatible", "http://localhost:8000/v1", None, None),
        ("openai_compatible", "http://localhost:8000/v1", "groq", "groq"),
        ("openai", "", None, "openai"),
        ("litellm_proxy", "http://localhost:4000", None, "litellm_proxy"),
    ],
)
def test_hosting_identity(preset, url, explicit, expected):
    assert catalog_provider_for(preset, url, explicit) == expected


def test_normalization_preserves_unknown_false_zero_and_allowed_values():
    result = normalize_model_metadata(
        {
            "context_length": 131072,
            "max_completion_tokens": 32768,
            "supports_function_calling": False,
            "supports_vision": None,
            "supported_reasoning_efforts": ["low", "high"],
            "supported_parameters": ["tools", "reasoning_effort"],
            "input_cost_per_token": 0,
            "cache_read_input_token_cost": 0.0000000375,
        }
    )
    assert result["context_window"] == 131072
    assert result["max_output_tokens"] == 32768
    assert result["supports_tools"] is False
    assert "supports_vision" not in result
    assert "supports_streaming" not in result
    assert result["reasoning_efforts"] == ["low", "high"]
    assert result["input_cost_per_mtok"] == 0
    assert result["cache_read_cost_per_mtok"] == 0.0375


def test_invalid_fields_do_not_invent_limits_or_capabilities():
    result = normalize_model_metadata(
        {
            "context_window": True,
            "max_output_tokens": -1,
            "supports_vision": "false",
            "supports_tools": None,
            "input_cost_per_token": "NaN",
            "output_cost_per_token": float("inf"),
            "max_tokens": 1024,
        }
    )
    assert "context_window" not in result
    assert "max_output_tokens" not in result
    assert "supports_tools" not in result
    assert "supports_vision" not in result
    assert "input_cost_per_mtok" not in result
    assert "output_cost_per_mtok" not in result


@pytest.mark.parametrize(
    "metadata",
    [
        {"architecture": {"modality": "text->decisions"}},
        {"output_modalities": ["decisions"]},
        {"model_info": {"mode": "decisions"}},
        {"model_mode": "systemone"},
    ],
)
def test_decision_protocol_metadata_shapes(metadata):
    normalized = normalize_model_metadata(metadata)
    assert normalized["model_mode"] == "systemone"
    assert normalized["supports_tools"] is False
    assert normalized["supports_embedding"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_source", ["provider", "configured"])
async def test_live_metadata_corrects_imported_router_snapshot_without_overwriting_user_restrictions(
    monkeypatch,
    capability_source,
):
    from cognis.providers.llm.hosted import systemone_metadata

    monkeypatch.setattr(
        litellm,
        "get_model_info",
        lambda **kwargs: {
            "mode": "chat",
            "supports_function_calling": True,
            "supports_native_streaming": True,
            "litellm_provider": "openrouter",
        },
    )
    configured = {
        **systemone_metadata(),
        "supports_tools": False,
        "provider_metadata": {
            "field_sources": {
                field: "provider" for field in systemone_metadata() if field != "supports_tools"
            }
        },
    }
    configured["provider_metadata"]["field_sources"]["supports_tools"] = capability_source
    provider = LLMProvider(provider_id="router", config={"preset": "openrouter"})
    info = await LiteLLMProvider(None)._merge_litellm_model_info(
        "typesafe/jev-router", provider, configured
    )
    assert info.model_mode == "chat"
    assert info.supports_streaming is False  # Legacy provenance cannot prove lack of an edit.
    assert info.supports_tools is False  # Explicit restriction, not imported metadata.
    assert configured["model_mode"] == "systemone"  # Metadata reads do not mutate saved config.


def test_namespaced_wire_id_uses_hosting_catalog_and_remote_wins(monkeypatch):
    calls = []

    def lookup(**kwargs):
        calls.append(kwargs)
        return {
            "litellm_provider": "groq",
            "max_input_tokens": 131072,
            "max_output_tokens": 32768,
            "supports_function_calling": True,
            "input_cost_per_token": 0.000000075,
        }

    monkeypatch.setattr(litellm, "get_model_info", lookup)
    result = resolve_model_metadata(
        "openai/gpt-oss-20b",
        preset="openai_compatible",
        base_url="https://api.groq.com/openai",
        remote={"context_window": 65536, "supports_tools": False},
    )
    assert calls == [{"model": "groq/openai/gpt-oss-20b", "custom_llm_provider": "groq"}]
    assert result["context_window"] == 65536
    assert result["max_output_tokens"] == 32768
    assert result["supports_tools"] is False
    assert result["input_cost_per_mtok"] == 0.075
    sources = result["provider_metadata"]["field_sources"]
    assert sources["context_window"] == "provider"
    assert sources["input_cost_per_mtok"] == "catalog:groq"


def test_unknown_endpoint_and_cross_provider_catalog_cannot_inherit_prices(monkeypatch):
    calls = []

    def lookup(**kwargs):
        calls.append(kwargs)
        return {"litellm_provider": "openai", "input_cost_per_token": 0.00001}

    monkeypatch.setattr(litellm, "get_model_info", lookup)
    unknown = resolve_model_metadata(
        "gpt-4o", preset="openai_compatible", base_url="https://private.example.org"
    )
    assert not calls
    assert unknown["provider_metadata"]["catalog_status"] == "unavailable"
    wrong = resolve_model_metadata(
        "gpt-4o", preset="openai_compatible", base_url="https://api.groq.com/openai"
    )
    assert "input_cost_per_mtok" not in wrong
    assert wrong["provider_metadata"]["catalog_status"] == "provider_mismatch"


def test_catalog_failure_returns_partial_remote_metadata(monkeypatch):
    def unavailable(**kwargs):
        raise ValueError("Not in catalog")

    monkeypatch.setattr(litellm, "get_model_info", unavailable)
    result = resolve_model_metadata(
        "private-chat", preset="openai", remote={"max_input_tokens": 65536}
    )
    assert result["context_window"] == 65536
    assert "max_output_tokens" not in result
    assert "input_cost_per_mtok" not in result
    assert result["provider_metadata"]["catalog_status"] == "lookup_failed"


def test_non_token_prices_and_remote_nested_metadata():
    result = normalize_model_metadata(
        {
            "model_info": {"mode": "audio_transcription", "input_cost_per_second": 0.00003},
        }
    )
    assert result["model_mode"] == "audio_transcription"
    assert result["pricing"]["input_cost_per_second"] == 0.00003
    assert "input_cost_per_mtok" not in result
    openrouter = normalize_model_metadata({"pricing": {"prompt": "0", "completion": "0.000002"}})
    assert openrouter["input_cost_per_mtok"] == 0
    assert openrouter["output_cost_per_mtok"] == 2
    ModelInfo.model_validate({"model_id": "whisper-large-v3", **result})


def test_real_bundled_groq_catalog(monkeypatch):
    # Load the packaged file, not LiteLLM's network-refreshed process-global map.
    path = Path(litellm.__file__).parent / "model_prices_and_context_window_backup.json"
    monkeypatch.setattr(litellm, "model_cost", json.loads(path.read_text()))
    result = resolve_model_metadata(
        "openai/gpt-oss-20b", preset="openai_compatible", base_url="https://api.groq.com/openai"
    )
    assert result["provider_metadata"]["catalog_status"] == "matched"
    assert result["context_window"] == 131072
    assert result["max_output_tokens"] == 32768
    assert result["input_cost_per_mtok"] == 0.075
    assert result["output_cost_per_mtok"] == 0.3
    assert result["cache_read_cost_per_mtok"] == 0.0375
    assert result["supports_tools"] is True
    assert result["supports_structured_output"] is True


async def test_manual_ollama_enrichment_has_only_adapter_provenance(monkeypatch):
    async def show(self, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "capabilities": ["completion", "tools"],
                "model_info": {"llama.context_length": 32768},
                "parameters": "num_ctx 8192",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", show)
    provider = LiteLLMProvider(None)
    info = await provider.enrich_model_info(
        "ollama/private-chat", preset="ollama", base_url="http://localhost:11434"
    )
    assert info.context_window == 8192
    assert info.supports_tools is True
    sources = info.provider_metadata["field_sources"]
    assert sources["context_window"] == "provider"
    assert sources["supports_tools"] == "provider"
    assert "max_output_tokens" not in sources
    assert "supports_pdf_input" not in sources


async def test_discovery_preview_saved_runtime_and_overrides(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/metadata.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    config = {
        "preset": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "auth_config": {"mode": "none"},
        "models": [{"model_id": "openai/gpt-oss-20b", "supports_tools": False}],
    }
    async with factory() as db:
        db.add(
            LLMProvider(
                provider_id="groq",
                display_name="Groq",
                location="controller",
                backend="litellm",
                owner_email=SYSTEM_USER_EMAIL,
                config=config,
            )
        )
        await db.commit()
    requested = []

    async def models(self, url, **kwargs):
        requested.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "data": [
                    {"id": "openai/gpt-oss-20b", "context_window": 65536},
                    {"id": "whisper-large-v3"},
                ]
            },
        )

    def catalog(**kwargs):
        if kwargs["model"] == "groq/whisper-large-v3":
            return {"mode": "audio_transcription", "input_cost_per_second": 0.00003}
        return {
            "litellm_provider": "groq",
            "max_input_tokens": 131072,
            "max_output_tokens": 32768,
            "supports_function_calling": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000000075,
            "output_cost_per_token": 0.0000003,
            "cache_read_input_token_cost": 0.0000000375,
            "supported_openai_params": ["tools", "stream", "reasoning_effort"],
        }

    monkeypatch.setattr(httpx.AsyncClient, "get", models)
    monkeypatch.setattr(litellm, "get_model_info", catalog)
    provider = LiteLLMProvider(factory)
    try:
        preview = await provider.discover_models_preview(
            preset=config["preset"], base_url=config["base_url"]
        )
        saved = await provider.discover_models("groq")
        assert preview == saved
        assert requested == ["https://api.groq.com/openai/v1/models"] * 2
        runtime = await provider.get_model_info("openai/gpt-oss-20b", "groq")
        assert runtime.context_window == saved[0]["context_window"] == 65536
        assert runtime.max_output_tokens == saved[0]["max_output_tokens"] == 32768
        assert runtime.input_cost_per_mtok == saved[0]["input_cost_per_mtok"] == 0.075
        assert runtime.cache_read_cost_per_mtok == 0.0375
        assert runtime.supports_tools is False
        assert runtime.provider_metadata["field_sources"]["supports_tools"] == "configured"
        async with factory() as db:
            row = await db.get(LLMProvider, "groq")
            assert row.config == config
            row.config = {
                **config,
                "models": [
                    {
                        "model_id": "openai/gpt-oss-20b",
                        "context_window": 32000,
                        "input_cost_per_mtok": 0,
                    },
                ],
            }
            await db.commit()
        updated = await provider.get_model_info("openai/gpt-oss-20b", "groq")
        assert updated.context_window == 32000
        assert updated.input_cost_per_mtok == 0
    finally:
        await engine.dispose()


async def test_elevenlabs_discovery_saved_models_are_eligible_for_stt_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.core.routing_management import route_model_is_eligible

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/elevenlabs.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    config = {
        "preset": "elevenlabs",
        "base_url": "https://api.elevenlabs.io",
        "auth_config": {"mode": "none"},
        "models": [],
    }
    async with factory() as db:
        db.add(
            LLMProvider(
                provider_id="elevenlabs",
                display_name="ElevenLabs",
                location="controller",
                backend="litellm",
                owner_email=SYSTEM_USER_EMAIL,
                config=config,
            )
        )
        await db.commit()

    async def models(self, url, **kwargs):
        assert url == "https://api.elevenlabs.io/v1/models"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json=[
                {
                    "model_id": "eleven_multilingual_v2",
                    "name": "Multilingual",
                    "can_do_text_to_speech": True,
                },
                {"model_id": "scribe_v2", "name": "Scribe v2", "can_do_text_to_speech": False},
            ],
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", models)
    provider = LiteLLMProvider(factory)
    try:
        preview = await provider.discover_models_preview(
            preset="elevenlabs", base_url=config["base_url"]
        )
        discovered = await provider.discover_models("elevenlabs")
        assert preview == discovered
        expected = {"scribe_v1", "scribe_v2", "scribe_v2_realtime"}
        assert [entry["model_id"] for entry in discovered].count("scribe_v2") == 1
        stt = [entry for entry in discovered if entry.get("supports_transcription")]
        assert {entry["model_id"] for entry in stt} == expected
        for entry in stt:
            assert entry["model_mode"] == "audio_transcription"
            assert entry["supports_speech_synthesis"] is False
            assert entry["supports_tools"] is False
            assert entry["supports_streaming"] is (entry["model_id"] == "scribe_v2_realtime")
        async with factory() as db:
            row = await db.get(LLMProvider, "elevenlabs")
            row.config = {**config, "models": discovered}
            await db.commit()
        async with factory() as db:
            row = await db.get(LLMProvider, "elevenlabs")
            assert {model["model_id"] for model in row.config["models"]} >= expected
        for model_id in expected:
            info = await provider.get_model_info(model_id, "elevenlabs")
            assert info.supports_transcription
            assert info.model_mode == "audio_transcription"
            assert route_model_is_eligible("speech_to_text", model_id=model_id, model_info=info)
            assert not route_model_is_eligible(
                "text_to_speech", model_id=f"elevenlabs/{model_id}", model_info=info
            )
            assert not route_model_is_eligible("default", model_id=model_id, model_info=info)
    finally:
        await engine.dispose()
