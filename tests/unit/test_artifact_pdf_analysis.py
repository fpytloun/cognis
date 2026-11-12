from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.tools.builtin import artifact_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_route", [False, True])
@pytest.mark.parametrize("content", [b"", b"Not a PDF document"])
async def test_compatible_pdf_without_url_or_extractable_text_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    configured_route: bool,
    content: bytes,
) -> None:
    model_id, provider_id = uuid4().hex, uuid4().hex
    monkeypatch.setattr(
        artifact_tools,
        "_get_attachment_analysis_route",
        AsyncMock(return_value=(model_id, provider_id) if configured_route else (None, None)),
    )
    monkeypatch.setattr(
        artifact_tools,
        "_get_model_info",
        AsyncMock(return_value=SimpleNamespace(supports_pdf_input=True)),
    )
    generate = AsyncMock()
    monkeypatch.setattr(artifact_tools, "_generate_analysis", generate)
    result = await artifact_tools.analyze_attachment_ref(
        attachment=AttachmentRef(
            artifact_id=f"art_{uuid4().hex}",
            kind=ArtifactKind.PDF,
            mime_type="application/pdf",
            filename="source.pdf",
            size_bytes=len(content),
        ),
        content=content,
        prompt=None,
        llm=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        session_factory=SimpleNamespace(),
        current_model=None if configured_route else model_id,
        current_provider_id=None if configured_route else provider_id,
    )
    assert result.is_error
    assert "signed URL" in result.output
    assert result.metadata is None or result.metadata.get("fallback") != "pdf_text_extraction"
    assert "Not a" not in caplog.text
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_compatible_pdf_with_url_keeps_native_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id, provider_id = uuid4().hex, uuid4().hex
    artifact_id = f"art_{uuid4().hex}"
    monkeypatch.setattr(
        artifact_tools, "_get_attachment_analysis_route", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        artifact_tools,
        "_get_model_info",
        AsyncMock(return_value=SimpleNamespace(supports_pdf_input=True)),
    )
    extract = AsyncMock()
    monkeypatch.setattr(artifact_tools, "_extract_pdf_text", extract)
    generate = AsyncMock(
        return_value=({"choices": [{"message": {"content": "Native PDF analysis"}}]}, "url")
    )
    monkeypatch.setattr(artifact_tools, "_generate_analysis", generate)
    result = await artifact_tools.analyze_attachment_ref(
        attachment=AttachmentRef(
            artifact_id=artifact_id,
            kind=ArtifactKind.PDF,
            mime_type="application/pdf",
            filename="source.pdf",
            size_bytes=len(b"Authorized PDF bytes"),
            url=f"https://feedback.test/read/{artifact_id}",
        ),
        content=b"Authorized PDF bytes",
        prompt=None,
        llm=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        session_factory=SimpleNamespace(),
        current_model=model_id,
        current_provider_id=provider_id,
    )
    assert not result.is_error and result.output == "Native PDF analysis"
    assert result.metadata.get("fallback") != "pdf_text_extraction"
    extract.assert_not_called()
    generate.assert_awaited_once()
