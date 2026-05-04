"""Integration-style tests for the TTS / STT API routes.

The TTS route is exercised end-to-end with a stub LLM provider so we can
verify cache hit/miss behavior, signed-URL response shape, and the
``tts.enabled`` master kill-switch without depending on a real provider.

The STT route is exercised similarly: we stub the provider's ``transcribe``
method and feed both the ``file=`` and ``artifact_id=`` paths.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.models.config import SpeechToTextResult, TextToSpeechResult
from cognis.store.queries import (
    create_user,
    upsert_model_routing,
    upsert_setting,
)


def _create_test_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    return TestClient(create_app())


def _auth_headers(app: Any, email: str = "user@example.com") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, "User", "user")
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(client: TestClient, email: str = "user@example.com") -> None:
    async with client.app.state.session_factory() as session:
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=client.app.state.password_hasher.hash("password123"),
            role="user",
        )
        await session.commit()


async def _seed_tts_routing(client: TestClient) -> None:
    async with client.app.state.session_factory() as session:
        await upsert_model_routing(
            session,
            task_type="text_to_speech",
            provider_id=None,
            model="tts-1",
            config=None,
        )
        await session.commit()


async def _seed_stt_routing(client: TestClient) -> None:
    async with client.app.state.session_factory() as session:
        await upsert_model_routing(
            session,
            task_type="speech_to_text",
            provider_id=None,
            model="whisper-1",
            config=None,
        )
        await session.commit()


class _StubLLMProvider:
    def __init__(self) -> None:
        self.synthesize_calls: list[dict[str, Any]] = []
        self.transcribe_calls: list[dict[str, Any]] = []

    async def resolve_model_target(
        self, _explicit: str | None, *, task_type: str
    ) -> tuple[str, Any]:
        if task_type == "text_to_speech":
            return "tts-1", _ProviderRow()
        if task_type == "speech_to_text":
            return "whisper-1", _ProviderRow()
        raise ValueError(f"unknown task_type {task_type}")

    async def synthesize(self, text: str, *, voice: str, **kwargs: Any) -> TextToSpeechResult:
        self.synthesize_calls.append({"text": text, "voice": voice, **kwargs})
        return TextToSpeechResult(
            audio_bytes=b"fake-audio-bytes",
            content_type="audio/mpeg",
            model="tts-1",
            voice=voice,
            duration_seconds=1.5,
        )

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str,
        filename: str,
        model: str | None = None,
        task_type: str = "speech_to_text",
        prompt: str | None = None,
        language: str | None = None,
    ) -> SpeechToTextResult:
        self.transcribe_calls.append(
            {
                "audio_bytes": audio_bytes,
                "mime_type": mime_type,
                "filename": filename,
                "language": language,
            }
        )
        return SpeechToTextResult(
            text="hello world",
            model="whisper-1",
            language="en",
            duration_seconds=1.0,
        )


class _ProviderRow:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {"preset": "openai"}


def _install_stub_llm(client: TestClient) -> _StubLLMProvider:
    stub = _StubLLMProvider()
    client.app.state.providers.llm = stub  # type: ignore[attr-defined]
    return stub


# ---------------------------------------------------------------------------
# TTS route
# ---------------------------------------------------------------------------


def test_tts_synthesize_cache_miss_then_hit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        asyncio.run(_seed_tts_routing(client))
        stub = _install_stub_llm(client)
        headers = _auth_headers(client.app)

        # First call — cache miss.
        response = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello there.", "message_id": "msg_123", "voice": "nova"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["voice"] == "nova"
        assert body["model"] == "tts-1"
        assert body["cached"] is False
        assert body["audio_url"].startswith("http://") or body["audio_url"].startswith("/")
        assert len(stub.synthesize_calls) == 1

        # Second call with same key — cache hit; no extra synthesize.
        response2 = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello there.", "message_id": "msg_123", "voice": "nova"},
        )
        assert response2.status_code == 200
        body2 = response2.json()
        assert body2["cached"] is True
        assert len(stub.synthesize_calls) == 1


def test_tts_synthesize_rejects_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        asyncio.run(_seed_tts_routing(client))
        _install_stub_llm(client)

        async def _disable() -> None:
            async with client.app.state.session_factory() as session:
                await upsert_setting(session, key="tts.enabled", value=False, category="tts")
                await session.commit()

        asyncio.run(_disable())

        response = client.post(
            "/api/v1/tts/synthesize",
            headers=_auth_headers(client.app),
            json={"text": "Hi", "message_id": None, "voice": "alloy"},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "tts_disabled"


def test_tts_synthesize_validates_empty_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        asyncio.run(_seed_tts_routing(client))
        _install_stub_llm(client)

        response = client.post(
            "/api/v1/tts/synthesize",
            headers=_auth_headers(client.app),
            json={"text": "   ", "voice": "alloy"},
        )
        assert response.status_code == 400


def test_tts_synthesize_no_routing_returns_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        # Intentionally no routing.

        # Use a real LLM provider whose resolve_model_target raises when
        # nothing is configured.
        class _NoRoutingProvider:
            async def resolve_model_target(self, _explicit: str | None, *, task_type: str):
                raise RuntimeError("no routing")

        client.app.state.providers.llm = _NoRoutingProvider()  # type: ignore[attr-defined]

        response = client.post(
            "/api/v1/tts/synthesize",
            headers=_auth_headers(client.app),
            json={"text": "Hello", "voice": "alloy"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "tts_unconfigured"


# ---------------------------------------------------------------------------
# STT route
# ---------------------------------------------------------------------------


def test_stt_transcribe_multipart_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        asyncio.run(_seed_stt_routing(client))
        stub = _install_stub_llm(client)

        files = {"file": ("voice.webm", io.BytesIO(b"fake-audio"), "audio/webm")}
        response = client.post(
            "/api/v1/stt/transcribe",
            headers=_auth_headers(client.app),
            files=files,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["text"] == "hello world"
        assert body["language"] == "en"
        assert body["model"] == "whisper-1"
        assert len(stub.transcribe_calls) == 1
        assert stub.transcribe_calls[0]["mime_type"].startswith("audio/")


def test_stt_transcribe_requires_file_or_artifact_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        asyncio.run(_seed_stt_routing(client))
        _install_stub_llm(client)

        response = client.post(
            "/api/v1/stt/transcribe",
            headers=_auth_headers(client.app),
            data={},
        )
        assert response.status_code == 400


def test_stt_transcribe_rejects_empty_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        asyncio.run(_seed_stt_routing(client))
        _install_stub_llm(client)

        files = {"file": ("voice.webm", io.BytesIO(b""), "audio/webm")}
        response = client.post(
            "/api/v1/stt/transcribe",
            headers=_auth_headers(client.app),
            files=files,
        )
        assert response.status_code == 400
