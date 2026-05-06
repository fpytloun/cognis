from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognis.models.config import GeneratedImage, ImageGenerationResult
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
        image=None,
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

    with scoped_runtime_context(user_email="user@example.com"), patch(
        "cognis.tools.builtin.image.create_artifact_record", AsyncMock()
    ) as create_record:
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

    with scoped_runtime_context(user_email="user@example.com"):
        with patch("cognis.tools.builtin.image.create_artifact_record", AsyncMock()):
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

    with scoped_runtime_context(user_email="user@example.com"), patch(
        "cognis.tools.builtin.image.create_artifact_record",
        AsyncMock(side_effect=RuntimeError("db down")),
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
async def test_image_edit_accepts_inline_base64_source() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(images=[], model="gpt-image-1")
    )

    result = await handle_image_tool(
        "image_edit",
        {"prompt": "brighten", "image_b64": "YWJj"},
        provider,
        artifact_store=None,
    )

    assert not result.is_error
    provider.image_generate.assert_awaited_once_with(
        prompt="brighten",
        model=None,
        n=1,
        size=None,
        quality=None,
        image="YWJj",
    )


@pytest.mark.asyncio
async def test_image_edit_rejects_source_path_with_helpful_error(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(images=[], model="gpt-image-1")
    )
    image_path = tmp_path / "input.png"
    image_path.write_bytes(b"abc")

    result = await handle_image_tool(
        "image_edit",
        {"prompt": "brighten", "source_path": str(image_path)},
        provider,
        artifact_store=None,
    )

    assert result.is_error
    assert "artifact_publish" in result.output
    provider.image_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_edit_accepts_source_artifact_id() -> None:
    provider = MagicMock()
    provider.image_generate = AsyncMock(
        return_value=ImageGenerationResult(images=[], model="gpt-image-1")
    )
    artifact_store = MagicMock()
    artifact_store.async_load = AsyncMock(return_value=(b"artifact-bytes", "image/png"))

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
            {"prompt": "brighten", "source_artifact_id": "art_1"},
            provider,
            artifact_store=artifact_store,
            session_factory=session_factory,
        )

    assert not result.is_error
    provider.image_generate.assert_awaited_once()
    assert provider.image_generate.await_args.kwargs["image"] == base64.b64encode(
        b"artifact-bytes"
    ).decode("ascii")
