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
    ) -> tuple[str, str | None]:
        # Mirror the real LiteLLMProvider contract: returns
        # ``(model_id, provider_id | None)`` — second element is a string id.
        if task_type == "text_to_speech":
            return "tts-1", "stub-provider"
        if task_type == "speech_to_text":
            return "whisper-1", "stub-provider"
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
        acting_user_email: str | None = None,
    ) -> SpeechToTextResult:
        self.transcribe_calls.append(
            {
                "audio_bytes": audio_bytes,
                "mime_type": mime_type,
                "filename": filename,
                "language": language,
                "acting_user_email": acting_user_email,
            }
        )
        return SpeechToTextResult(
            text="hello world",
            model="whisper-1",
            language="en",
            duration_seconds=1.0,
        )

    async def aclose(self) -> None:
        pass


async def _seed_stub_provider(client: TestClient) -> None:
    """Insert a minimal LLMProvider row that the route can ``session.get`` by id."""
    from cognis.store.models import LLMProvider as LLMProviderRow

    async with client.app.state.session_factory() as session:
        if await session.get(LLMProviderRow, "stub-provider") is not None:
            return
        session.add(
            LLMProviderRow(
                provider_id="stub-provider",
                display_name="Stub",
                location="controller",
                backend="litellm",
                config={"preset": "openai"},
                status="active",
            )
        )
        await session.commit()


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
        asyncio.run(_seed_stub_provider(client))
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


def test_tts_synthesize_recovers_from_stale_artifact_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: when the ``tts_cache`` row was pruned but the matching
    ``artifacts`` row is still present, a re-synthesize for the same
    deterministic ``artifact_id`` must update the existing row instead of
    crashing with a UniqueViolation on ``artifacts_pkey``.
    """
    from sqlalchemy import delete

    from cognis.store.models import TtsCacheRow

    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        asyncio.run(_seed_stub_provider(client))
        asyncio.run(_seed_tts_routing(client))
        _install_stub_llm(client)
        headers = _auth_headers(client.app)

        first = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello stale.", "message_id": "msg_stale", "voice": "nova"},
        )
        assert first.status_code == 200, first.text

        # Simulate a TTL prune that wiped the tts_cache row but left the
        # artifacts row in place.
        async def _wipe_cache_only() -> None:
            async with client.app.state.session_factory() as session:
                await session.execute(delete(TtsCacheRow))
                await session.commit()

        asyncio.run(_wipe_cache_only())

        # Second call — must succeed (no IntegrityError on artifacts_pkey).
        second = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello stale.", "message_id": "msg_stale", "voice": "nova"},
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["cached"] is False
        # The cache row is freshly inserted; a third call should hit the cache.
        third = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello stale.", "message_id": "msg_stale", "voice": "nova"},
        )
        assert third.status_code == 200
        assert third.json()["cached"] is True


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


def test_tts_synthesize_handles_string_provider_id_from_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: ``LLMProvider.resolve_model_target`` returns a provider_id
    *string*, not a row. Earlier the route accessed ``.config`` on the second
    element directly and crashed with ``AttributeError``."""
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client))
        asyncio.run(_seed_stub_provider(client))
        asyncio.run(_seed_tts_routing(client))
        _install_stub_llm(client)

        response = client.post(
            "/api/v1/tts/synthesize",
            headers=_auth_headers(client.app),
            json={"text": "Hi there.", "voice": "alloy"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["voice"] == "alloy"
        # No message_id → no cache row, but the synthesize call must succeed.
        assert body["cached"] is False


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

            async def aclose(self) -> None:
                pass

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
        assert stub.transcribe_calls[0]["acting_user_email"] == "user@example.com"


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
