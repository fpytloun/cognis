from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.tools.builtin.artifact_tools import analyze_attachment_ref


@pytest.mark.asyncio
async def test_analyze_audio_attachment_uses_speech_to_text() -> None:
    llm = SimpleNamespace(
        resolve_model_target=AsyncMock(return_value=("whisper-1", "provider-1")),
        get_model_info=AsyncMock(
            return_value=SimpleNamespace(supported_audio_mime_types=["audio/mp4"])
        ),
        transcribe=AsyncMock(return_value=SimpleNamespace(text="transcribed audio")),
    )
    attachment = AttachmentRef(
        artifact_id="art-audio",
        kind=ArtifactKind.AUDIO,
        mime_type="audio/x-m4a",
        filename="recording.m4a",
        size_bytes=5,
    )

    result = await analyze_attachment_ref(
        attachment=attachment,
        content=b"audio",
        prompt=None,
        llm=llm,
        artifact_store=SimpleNamespace(),
        session_factory=SimpleNamespace(),
        current_model="chat-model",
        current_provider_id="chat-provider",
        owner_email="user@example.com",
    )

    assert result.is_error is False
    assert result.output == "transcribed audio"
    assert result.metadata["analysis_task_type"] == "speech_to_text"
    assert result.metadata["fallback"] == "audio_transcription"
    llm.transcribe.assert_awaited_once_with(
        b"audio",
        mime_type="audio/mp4",
        filename="recording.m4a",
        acting_user_email="user@example.com",
    )
    llm.resolve_model_target.assert_awaited_once_with(
        task_type="speech_to_text",
        acting_user_email="user@example.com",
    )


@pytest.mark.asyncio
async def test_analyze_audio_attachment_reports_transcription_failure() -> None:
    llm = SimpleNamespace(
        resolve_model_target=AsyncMock(return_value=("whisper-1", None)),
        get_model_info=AsyncMock(return_value=SimpleNamespace()),
        transcribe=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    )

    result = await analyze_attachment_ref(
        attachment=AttachmentRef(
            artifact_id="art-audio",
            kind=ArtifactKind.AUDIO,
            mime_type="audio/ogg",
            filename="recording.ogg",
            size_bytes=5,
        ),
        content=b"audio",
        prompt=None,
        llm=llm,
        artifact_store=SimpleNamespace(),
        session_factory=SimpleNamespace(),
        current_model="chat-model",
        current_provider_id=None,
    )

    assert result.is_error is True
    assert "provider unavailable" in result.output
    assert result.metadata["analysis_task_type"] == "speech_to_text"


@pytest.mark.asyncio
async def test_analyze_audio_attachment_bounds_large_transcript() -> None:
    llm = SimpleNamespace(
        resolve_model_target=AsyncMock(return_value=("whisper-1", None)),
        get_model_info=AsyncMock(
            return_value=SimpleNamespace(supported_audio_mime_types=["audio/ogg"])
        ),
        transcribe=AsyncMock(return_value=SimpleNamespace(text="x" * 100_001)),
    )

    result = await analyze_attachment_ref(
        attachment=AttachmentRef(
            artifact_id="art-audio",
            kind=ArtifactKind.AUDIO,
            mime_type="audio/ogg",
            filename="recording.ogg",
            size_bytes=5,
        ),
        content=b"audio",
        prompt=None,
        llm=llm,
        artifact_store=SimpleNamespace(),
        session_factory=SimpleNamespace(),
        current_model="chat-model",
        current_provider_id=None,
    )

    assert result.is_error is False
    assert result.metadata["truncated"] is True
    assert result.output.endswith("[Transcript truncated at 100,000 characters.]")
