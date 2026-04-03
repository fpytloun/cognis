"""Unit tests for executor-side inference handler."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cognis.executor.inference import InferenceHandler
from cognis.models.tool import InferenceConfig


@pytest.mark.asyncio
async def test_stream_complete_no_model() -> None:
    """stream_complete yields error when no model is specified."""
    config = InferenceConfig(endpoint="http://localhost:11434/v1")
    handler = InferenceHandler(config)

    chunks = []
    async for chunk in handler.stream_complete(messages=[{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0]["done"] is True
    assert "No model" in chunks[0]["error"]
    await handler.close()


@pytest.mark.asyncio
async def test_stream_complete_with_default_model() -> None:
    """stream_complete uses default_model when model is not specified."""
    config = InferenceConfig(
        endpoint="http://localhost:11434/v1",
        default_model="llama3.2",
    )
    handler = InferenceHandler(config)

    # Mock the HTTP client to return a streaming response
    mock_response = AsyncMock()
    mock_response.status_code = 200

    async def mock_aiter_lines() -> Any:
        yield 'data: {"choices": [{"delta": {"content": "Hello"}, "finish_reason": null}]}'
        yield 'data: {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]}'
        yield "data: [DONE]"

    mock_response.aiter_lines = mock_aiter_lines

    # Create a mock context manager
    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    with patch.object(handler._client, "stream", return_value=mock_stream_cm):
        chunks = []
        async for chunk in handler.stream_complete(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    # Should have content chunks + done
    assert len(chunks) >= 2
    # Last chunk should be done
    assert chunks[-1]["done"] is True
    await handler.close()


@pytest.mark.asyncio
async def test_stream_complete_connection_error() -> None:
    """stream_complete yields error on connection failure."""
    import httpx

    config = InferenceConfig(
        endpoint="http://localhost:99999/v1",
        default_model="test-model",
    )
    handler = InferenceHandler(config)

    # Mock the HTTP client to raise ConnectError
    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    with patch.object(handler._client, "stream", return_value=mock_stream_cm):
        chunks = []
        async for chunk in handler.stream_complete(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0]["done"] is True
    assert "Cannot connect" in chunks[0]["error"]
    await handler.close()
