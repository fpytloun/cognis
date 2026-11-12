"""Image tool options must resolve through the same authorized provider catalog."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from cognis.api.runtime_support import enrich_image_tool_model_descriptions
from cognis.models.config import GeneratedImage, ImageGenerationResult, ImageInput
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.providers.llm.service import LLMService
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider, ModelRouting
from cognis.tools.builtin.image import IMAGE_EDIT_TOOL, IMAGE_GENERATE_TOOL, handle_image_tool


@pytest_asyncio.fixture
async def catalog(tmp_path: Path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/models.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        for provider_id, preset, owner, status, models in [
            ("gemini", "gemini", SYSTEM_USER_EMAIL, "active", ["gemini-nano-banana-2.1"]),
            ("openai", "openai", SYSTEM_USER_EMAIL, "active", ["gpt-image-2.5-sunburst"]),
            ("litellm", "litellm_proxy", SYSTEM_USER_EMAIL, "disabled", ["gpt-image-2"]),
            ("private", "openai", "bob@example.com", "active", ["gpt-image-private"]),
            ("personal", "openai", "alice@example.com", "active", ["gpt-image-personal"]),
            ("groq", "groq", SYSTEM_USER_EMAIL, "active", ["openai/gpt-oss-120b"]),
        ]:
            session.add(
                LLMProvider(
                    provider_id=provider_id,
                    display_name=provider_id,
                    location="controller",
                    backend="litellm",
                    owner_email=owner,
                    status=status,
                    config={
                        "preset": preset,
                        "models": [
                            {"model_id": model, "supports_image_generation": preset != "groq"}
                            for model in models
                        ],
                    },
                )
            )
        session.add(
            ModelRouting(
                task_type="image_generation",
                provider_id="gemini",
                model="gemini-nano-banana-2.1",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"])
async def test_gpt_image_26_catalog_picker_and_routing(catalog, model_id, usage_capture):
    async with catalog() as session:
        row = await session.get(LLMProvider, "openai")
        row.config = {"preset": "openai", "models": [{"model_id": model_id}]}
        await session.commit()
    provider = LLMService(catalog, usage_capture=usage_capture)
    info = await provider.get_model_info(model_id, provider_id="openai")
    assert info.model_mode == "image_generation"
    assert info.supports_image_generation is True
    assert info.supports_tools is False
    tools = await enrich_image_tool_model_descriptions(
        [IMAGE_GENERATE_TOOL, IMAGE_EDIT_TOOL], provider, acting_user_email="alice@example.com"
    )
    for tool in tools:
        assert tool.parameters["properties"]["model"]["enum"] == [
            "gemini/gemini-nano-banana-2.1",
            f"openai/{model_id}",
            "personal/gpt-image-personal",
        ]
    plan = await provider.inference_plan(
        model=f"openai/{model_id}",
        task_type="image_generation",
        acting_user_email="alice@example.com",
    )
    assert plan.candidates[0].target.reference == f"openai/{model_id}"
    assert await provider.resolve_model_target(model_id, task_type="image_generation") == (
        model_id,
        "openai",
    )


@pytest.mark.asyncio
async def test_model_list_filters_inactive_and_invisible_providers(catalog):
    provider = LiteLLMProvider(catalog)
    assert {model["provider_id"] for model in await provider.list_models()} == {
        "gemini",
        "openai",
        "groq",
    }
    assert {
        model["provider_id"]
        for model in await provider.list_models(acting_user_email="alice@example.com")
    } == {"gemini", "openai", "groq", "personal"}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class", [LiteLLMProvider, LLMService])
@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("gemini/gemini-nano-banana-2.1", ("gemini-nano-banana-2.1", "gemini")),
        ("openai/gpt-image-2.5-sunburst", ("gpt-image-2.5-sunburst", "openai")),
        ("gpt-image-2.5-sunburst", ("gpt-image-2.5-sunburst", "openai")),
        ("groq/openai/gpt-oss-120b", ("openai/gpt-oss-120b", "groq")),
        ("openai/gpt-oss-120b", ("openai/gpt-oss-120b", "groq")),
        (None, ("gemini-nano-banana-2.1", "gemini")),
    ],
)
async def test_image_model_target_resolves_references(
    catalog, provider_class, reference, expected, usage_capture
):
    provider = provider_class(catalog, usage_capture=usage_capture)
    assert await provider.resolve_model_target(reference, task_type="image_generation") == expected
    if reference:
        assert await provider.resolve_model_reference(reference) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference", ["litellm/gpt-image-2", "private/gpt-image-private", "gpt-image-private"]
)
async def test_image_model_target_rejects_unavailable_references(catalog, reference, usage_capture):
    provider = LLMService(catalog, usage_capture=usage_capture)
    with pytest.raises(ValueError, match="No inference provider configured"):
        await provider.inference_plan(model=reference, task_type="image_generation")


@pytest.mark.asyncio
async def test_image_model_target_reports_ambiguous_bare_id(catalog, usage_capture):
    async with catalog() as session:
        session.add(
            LLMProvider(
                provider_id="other-openai",
                display_name="Other OpenAI",
                location="controller",
                backend="litellm",
                config={"models": [{"model_id": "gpt-image-2.5-sunburst"}]},
                status="active",
            )
        )
        await session.commit()
    provider = LLMService(catalog, usage_capture=usage_capture)
    with pytest.raises(ValueError, match="ambiguous.*openai/gpt-image-2.5-sunburst"):
        await provider.resolve_model_target("gpt-image-2.5-sunburst", task_type="image_generation")
    assert await provider.resolve_model_target(
        "openai/gpt-image-2.5-sunburst", task_type="image_generation"
    ) == ("gpt-image-2.5-sunburst", "openai")


@pytest.mark.asyncio
async def test_image_tool_enum_contains_only_usable_qualified_references(catalog, usage_capture):
    provider = LLMService(catalog, usage_capture=usage_capture)
    tools = await enrich_image_tool_model_descriptions(
        [IMAGE_GENERATE_TOOL, IMAGE_EDIT_TOOL], provider, acting_user_email="alice@example.com"
    )
    expected = [
        "gemini/gemini-nano-banana-2.1",
        "openai/gpt-image-2.5-sunburst",
        "personal/gpt-image-personal",
    ]
    for tool in tools:
        model = tool.parameters["properties"]["model"]
        assert model["enum"] == expected
        assert "Default: gemini/gemini-nano-banana-2.1." in model["description"]
        for reference in model["enum"]:
            plan = await provider.inference_plan(
                model=reference, task_type="image_generation", acting_user_email="alice@example.com"
            )
            assert len(plan.candidates) == 1
            assert plan.candidates[0].target.reference == reference
    # Enrichment must not mutate the canonical static tool definitions.
    assert "enum" not in IMAGE_GENERATE_TOOL.parameters["properties"]["model"]


@pytest.mark.asyncio
async def test_missing_image_route_does_not_hide_available_models(catalog, usage_capture):
    provider = LLMService(catalog, usage_capture=usage_capture)
    provider.resolve_model_target = AsyncMock(side_effect=ValueError("No image route"))
    tools = await enrich_image_tool_model_descriptions([IMAGE_GENERATE_TOOL], provider)
    model = tools[0].parameters["properties"]["model"]
    assert model["enum"] == ["gemini/gemini-nano-banana-2.1", "openai/gpt-image-2.5-sunburst"]
    assert "Default:" not in model["description"]


@pytest.mark.asyncio
async def test_model_target_preserves_unknown_metadata_and_explicit_provider_pin(catalog):
    provider = LiteLLMProvider(catalog)
    assert await provider.resolve_model_target("unconfigured-model") == ("unconfigured-model", None)
    # An explicit provider pin means the whole model string is a literal wire ID.
    assert await provider.resolve_model_target(
        "openai/gpt-oss-120b", explicit_provider_id="groq"
    ) == ("openai/gpt-oss-120b", "groq")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["image_generate", "image_edit"])
@pytest.mark.parametrize("explicit_model", [None, "personal/gpt-image-personal"])
async def test_image_handler_executes_in_callers_model_scope(
    catalog, monkeypatch, tool_name, explicit_model, usage_capture
):
    async with catalog() as session:
        session.add(
            ModelRouting(
                owner_email="alice@example.com",
                task_type="image_generation",
                provider_id="personal",
                model="gpt-image-personal",
            )
        )
        await session.commit()
    transport = AsyncMock(
        return_value=ImageGenerationResult(
            model="gpt-image-personal",
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
        )
    )
    monkeypatch.setattr(LiteLLMProvider, "image_generate", transport)
    arguments = {"prompt": "banner", "model": explicit_model}
    if tool_name == "image_edit":
        arguments["images"] = ["img_reference"]
        # Artifact authorization is covered separately; retain real model routing.
        monkeypatch.setattr(
            "cognis.tools.builtin.image._resolve_image_artifacts_to_b64",
            AsyncMock(return_value=[ImageInput(b64_json="YWJj", content_type="image/png")]),
        )
    result = await handle_image_tool(
        tool_name,
        arguments,
        LLMService(catalog, usage_capture=usage_capture),
        user_email="alice@example.com",
    )
    assert not result.is_error, result.output
    assert transport.await_count == 1
    kwargs = transport.await_args.kwargs
    assert kwargs["model"] == "gpt-image-personal"
    assert kwargs["provider_id"] == "personal"
    assert kwargs["acting_user_email"] == "alice@example.com"
