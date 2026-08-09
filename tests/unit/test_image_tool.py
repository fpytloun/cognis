from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognis.models.config import GeneratedImage, ImageGenerationResult, ImageInput
from cognis.runtime_context import scoped_runtime_context
from cognis.tools.builtin.image import handle_image_tool


@pytest.mark.asyncio
async def test_handle_image_tool_strips_empty_optional_args() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
            model="gpt-image-1",
        )
    )

    result = await handle_image_tool(
        "image_generate",
        {
            "prompt": "banner",
            "model": "",
            "size": "",
            "quality": "",
            "n": 1,
        },
        provider,
        artifact_store=None,
    )

    assert not result.is_error
    provider.image_generate.assert_awaited_once_with(
        prompt="banner",
        model=None,
        n=1,
        size=None,
        quality=None,
        images=None,
        mask=None,
    )


@pytest.mark.asyncio
async def test_handle_image_tool_returns_channel_attachments() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
            model="gpt-image-1",
        )
    )
    artifact_store = MagicMock()
    artifact_store.generate_id.return_value = "img_123"
    artifact_store.async_save = AsyncMock()
    artifact_store.async_delete = AsyncMock()
    artifact_store.async_get_public_url = AsyncMock(return_value="https://example.com/signed.png")

    class _Session:
        commit = AsyncMock()

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    with (
        scoped_runtime_context(user_email="user@example.com"),
        patch("cognis.tools.builtin.image.create_artifact_record", AsyncMock()) as create_record,
    ):
        result = await handle_image_tool(
            "image_generate",
            {"prompt": "banner"},
            provider,
            artifact_store=artifact_store,
            session_factory=session_factory,
        )

    payload = json.loads(result.output)
    assert payload["images"][0]["image_id"] == "img_123"
    assert payload["images"][0]["url"] == "https://example.com/signed.png"
    assert result.attachments == [
        {
            "artifact_id": "img_123",
            "url": "https://example.com/signed.png",
            "mime_type": "image/png",
            "filename": "img_123.png",
            "size_bytes": 3,
            "kind": "image",
            "content_b64": "YWJj",
        }
    ]
    create_record.assert_awaited_once()
    artifact_store.async_get_public_url.assert_awaited_once_with("images", "img_123", "image")


@pytest.mark.asyncio
async def test_handle_image_tool_keeps_attachment_without_public_url() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
            model="gpt-image-1",
        )
    )
    artifact_store = MagicMock()
    artifact_store.generate_id.return_value = "img_124"
    artifact_store.async_save = AsyncMock()
    artifact_store.async_delete = AsyncMock()
    artifact_store.async_get_public_url = AsyncMock(return_value=None)

    class _Session:
        commit = AsyncMock()

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    with (
        scoped_runtime_context(user_email="user@example.com"),
        patch("cognis.tools.builtin.image.create_artifact_record", AsyncMock()),
    ):
        result = await handle_image_tool(
            "image_generate",
            {"prompt": "banner"},
            provider,
            artifact_store=artifact_store,
            session_factory=session_factory,
        )

    payload = json.loads(result.output)
    assert payload["images"][0]["url"] == "/api/v1/images/img_124"
    assert result.attachments == [
        {
            "artifact_id": "img_124",
            "mime_type": "image/png",
            "filename": "img_124.png",
            "size_bytes": 3,
            "kind": "image",
            "url": "/api/v1/images/img_124",
            "content_b64": "YWJj",
        }
    ]


@pytest.mark.asyncio
async def test_handle_image_tool_returns_error_when_artifact_registration_fails() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
            model="gpt-image-1",
        )
    )
    artifact_store = MagicMock()
    artifact_store.generate_id.return_value = "img_125"
    artifact_store.async_save = AsyncMock()
    artifact_store.async_delete = AsyncMock()
    artifact_store.async_get_public_url = AsyncMock(return_value="https://example.com/signed.png")

    class _Session:
        commit = AsyncMock()

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    with (
        scoped_runtime_context(user_email="user@example.com"),
        patch(
            "cognis.tools.builtin.image.create_artifact_record",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        result = await handle_image_tool(
            "image_generate",
            {"prompt": "banner"},
            provider,
            artifact_store=artifact_store,
            session_factory=session_factory,
        )

    assert result.is_error
    assert "registration failed" in result.output.lower()
    artifact_store.async_delete.assert_awaited_once_with("images", "img_125", "image")


@pytest.mark.asyncio
async def test_image_edit_resolves_ordered_artifacts_and_mask() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
            model="gpt-image-1",
        )
    )

    artifact_store = MagicMock()
    artifact_store.async_load = AsyncMock(
        side_effect=[
            (b"first-image", "image/png"),
            (b"second-image", "image/png"),
            (b"mask-image", "image/png"),
        ]
    )

    class _ArtifactRow:
        status = "attached"
        owner_email = None
        namespace = "attachments"
        object_id = "art_1"
        filename = "input.png"

    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    with patch("cognis.store.queries.get_artifact_record", AsyncMock(return_value=_ArtifactRow())):
        result = await handle_image_tool(
            "image_edit",
            {
                "prompt": "Place the first image in the second image.",
                "images": ["art_1", "art_2"],
                "mask_artifact_id": "art_mask",
            },
            provider,
            artifact_store=artifact_store,
            session_factory=session_factory,
        )

    assert not result.is_error
    provider.image_generate.assert_awaited_once()
    assert provider.image_generate.await_args.kwargs["images"] == [
        ImageInput(b64_json=base64.b64encode(b"first-image").decode("ascii")),
        ImageInput(b64_json=base64.b64encode(b"second-image").decode("ascii")),
    ]
    assert provider.image_generate.await_args.kwargs["mask"] == ImageInput(
        b64_json=base64.b64encode(b"mask-image").decode("ascii")
    )


@pytest.mark.asyncio
async def test_image_generate_resolves_ordered_references() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
            model="gemini-image",
        )
    )
    artifact_store = MagicMock()
    artifact_store.async_load = AsyncMock(
        side_effect=[(b"first-reference", "image/png"), (b"second-reference", "image/png")]
    )

    result = await handle_image_tool(
        "image_generate",
        {"prompt": "Combine the references.", "references": ["img_first", "img_second"]},
        provider,
        artifact_store=artifact_store,
    )

    assert not result.is_error
    assert provider.image_generate.await_args.kwargs["images"] == [
        ImageInput(b64_json=base64.b64encode(b"first-reference").decode("ascii")),
        ImageInput(b64_json=base64.b64encode(b"second-reference").decode("ascii")),
    ]
    assert provider.image_generate.await_args.kwargs["mask"] is None


@pytest.mark.asyncio
async def test_image_generate_materializes_lazy_reference_before_resolution() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
            model="gemini-image",
        )
    )
    artifact_store = MagicMock()
    artifact_store.async_load = AsyncMock(return_value=(b"lazy-reference", "image/png"))

    class _ArtifactRow:
        status = "attached"
        namespace = "attachments"
        object_id = "att_materialized"
        filename = "reference.png"

    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    materialize = AsyncMock(
        return_value=MagicMock(
            is_error=False,
            metadata={"artifact_id": "att_materialized"},
        )
    )
    with (
        patch(
            "cognis.tools.builtin.artifact_tools.materialize_tool_artifact_ref",
            materialize,
        ),
        patch("cognis.store.queries.get_artifact_record", AsyncMock(return_value=_ArtifactRow())),
    ):
        result = await handle_image_tool(
            "image_generate",
            {
                "prompt": "Use the reference.",
                "references": ["tool_artifact:call_web:media:1"],
            },
            provider,
            artifact_store=artifact_store,
            session_factory=session_factory,
            user_email="user@example.com",
            runtime_metadata={
                "authorized_lazy_artifact_refs": ["tool_artifact:call_web:media:1"],
            },
        )

    assert not result.is_error
    materialize.assert_awaited_once_with(
        "tool_artifact:call_web:media:1",
        artifact_store=artifact_store,
        session_factory=session_factory,
        user_email="user@example.com",
        runtime_metadata={
            "authorized_lazy_artifact_refs": ["tool_artifact:call_web:media:1"],
        },
    )
    assert provider.image_generate.await_args.kwargs["images"] == [
        ImageInput(b64_json=base64.b64encode(b"lazy-reference").decode("ascii"))
    ]


@pytest.mark.asyncio
async def test_image_generate_rejects_non_image_reference() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock()
    artifact_store = MagicMock()
    artifact_store.async_load = AsyncMock(return_value=(b"not an image", "application/pdf"))

    result = await handle_image_tool(
        "image_generate",
        {"prompt": "Use the reference.", "references": ["img_not_an_image"]},
        provider,
        artifact_store=artifact_store,
    )

    assert result.is_error
    assert result.output == "Error: references[0] must be an image artifact ID."
    provider.image_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_generate_omits_blank_references() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(
            images=[GeneratedImage(b64_json="YWJj", content_type="image/png")],
            model="gemini-image",
        )
    )

    result = await handle_image_tool(
        "image_generate",
        {"prompt": "Draw a dinosaur.", "references": [""]},
        provider,
    )

    assert not result.is_error
    assert provider.image_generate.await_args.kwargs["images"] is None


@pytest.mark.asyncio
async def test_image_edit_rejects_non_array_images() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock()

    result = await handle_image_tool(
        "image_edit",
        {"prompt": "brighten", "images": "art_1"},
        provider,
        artifact_store=MagicMock(),
    )

    assert result.is_error
    assert "images must be an array" in result.output
    provider.image_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_generate_rejects_empty_references() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock()

    result = await handle_image_tool(
        "image_generate",
        {"prompt": "Combine the images.", "references": []},
        provider,
        artifact_store=MagicMock(),
    )

    assert result.is_error
    assert "references must contain at least one" in result.output
    provider.image_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_generate_returns_error_when_provider_returns_no_images() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(images=[], model="gemini-image")
    )

    result = await handle_image_tool(
        "image_generate",
        {"prompt": "banner"},
        provider,
        artifact_store=None,
    )

    assert result.is_error
    assert "no image data" in result.output
