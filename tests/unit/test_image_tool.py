from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

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
    artifact_store.async_get_signed_url = AsyncMock(return_value="https://example.com/signed.png")

    with scoped_runtime_context(user_email="user@example.com"):
        result = await handle_image_tool(
            "image_generate",
            {"prompt": "banner"},
            provider,
            artifact_store=artifact_store,
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
        }
    ]
