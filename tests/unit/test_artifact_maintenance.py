"""Unit tests for the artifact maintenance service TTS pruning path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.core.artifact_maintenance import ArtifactMaintenanceService
from cognis.store.queries import (
    create_artifact_record,
    get_artifact_record,
    get_tts_cache_entry,
    insert_tts_cache_entry,
    upsert_setting,
)


def _create_test_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    return TestClient(create_app())


async def _seed_tts_artifact(
    client: TestClient,
    *,
    message_id: str,
    voice: str,
    model: str,
    artifact_id: str,
    age_days: int,
) -> None:
    """Seed both a tts_cache row and an artifacts row + storage bytes."""
    artifact_store = client.app.state.artifact_store  # type: ignore[attr-defined]
    await artifact_store.async_save(
        "tts",
        artifact_id,
        "speech.mp3",
        b"\x00\x01\x02fake-audio",
        "audio/mpeg",
        owner_email="user@example.com",
    )
    async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
        await insert_tts_cache_entry(
            session,
            message_id=message_id,
            voice=voice,
            model=model,
            artifact_id=artifact_id,
            artifact_filename="speech.mp3",
            content_type="audio/mpeg",
            owner_email="user@example.com",
            duration_seconds=1.0,
            size_bytes=11,
        )
        await create_artifact_record(
            session,
            artifact_id=artifact_id,
            namespace="tts",
            object_id=artifact_id,
            filename="speech.mp3",
            owner_email="user@example.com",
            purpose="tts",
            kind="audio",
            mime_type="audio/mpeg",
            size_bytes=11,
            status="active",
        )
        # Backdate the cache row so it falls past the TTL cutoff.
        cache_row = await get_tts_cache_entry(
            session, message_id=message_id, voice=voice, model=model
        )
        assert cache_row is not None
        cache_row.created_at = datetime.now(UTC) - timedelta(days=age_days)
        await session.commit()


def _build_service(client: TestClient) -> ArtifactMaintenanceService:
    return ArtifactMaintenanceService(
        session_factory=client.app.state.session_factory,  # type: ignore[attr-defined]
        artifact_store=client.app.state.artifact_store,  # type: ignore[attr-defined]
    )


def test_run_once_prunes_expired_tts_cache_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        artifact_id = "tts_expired_unit_test_id"
        asyncio.run(
            _seed_tts_artifact(
                client,
                message_id="msg_expired",
                voice="nova",
                model="tts-1",
                artifact_id=artifact_id,
                age_days=60,  # well past default TTL of 30 days
            )
        )

        service = _build_service(client)
        asyncio.run(service.run_once())

        async def _verify_gone() -> tuple[Any, Any, bool]:
            async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
                cache_row = await get_tts_cache_entry(
                    session, message_id="msg_expired", voice="nova", model="tts-1"
                )
                artifact_row = await get_artifact_record(session, artifact_id)
            exists = await client.app.state.artifact_store.async_exists(  # type: ignore[attr-defined]
                "tts", artifact_id, "speech.mp3"
            )
            return cache_row, artifact_row, exists

        cache_row, artifact_row, blob_exists = asyncio.run(_verify_gone())
        assert cache_row is None, "tts_cache row should be pruned"
        assert artifact_row is None, "artifacts row should be pruned"
        assert blob_exists is False, "artifact bytes should be gone"


def test_run_once_keeps_recent_tts_cache_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        artifact_id = "tts_fresh_unit_test_id"
        asyncio.run(
            _seed_tts_artifact(
                client,
                message_id="msg_fresh",
                voice="alloy",
                model="tts-1",
                artifact_id=artifact_id,
                age_days=1,
            )
        )

        service = _build_service(client)
        asyncio.run(service.run_once())

        async def _verify_kept() -> Any:
            async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
                return await get_tts_cache_entry(
                    session, message_id="msg_fresh", voice="alloy", model="tts-1"
                )

        cache_row = asyncio.run(_verify_kept())
        assert cache_row is not None, "fresh tts_cache row must not be pruned"


def test_run_once_uses_setting_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        artifact_id = "tts_override_unit_test_id"
        asyncio.run(
            _seed_tts_artifact(
                client,
                message_id="msg_override",
                voice="echo",
                model="tts-1",
                artifact_id=artifact_id,
                age_days=2,
            )
        )

        # Tighten the TTL to 1 day so the 2-day-old row is now expired.
        async def _set_override() -> None:
            async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
                await upsert_setting(session, key="tts.cache_ttl_days", value=1, category="tts")
                await session.commit()

        asyncio.run(_set_override())

        service = _build_service(client)
        asyncio.run(service.run_once())

        async def _verify_pruned() -> Any:
            async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
                return await get_tts_cache_entry(
                    session, message_id="msg_override", voice="echo", model="tts-1"
                )

        assert asyncio.run(_verify_pruned()) is None


def test_run_once_handles_missing_storage_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the underlying artifact bytes are already gone, the prune still
    proceeds and removes the metadata rows without raising."""
    with _create_test_client(monkeypatch, tmp_path) as client:
        artifact_id = "tts_missing_blob_unit_test_id"
        asyncio.run(
            _seed_tts_artifact(
                client,
                message_id="msg_missing_blob",
                voice="alloy",
                model="tts-1",
                artifact_id=artifact_id,
                age_days=60,
            )
        )

        # Wipe the blob ahead of time.
        asyncio.run(
            client.app.state.artifact_store.async_delete_object(  # type: ignore[attr-defined]
                "tts", artifact_id
            )
        )

        service = _build_service(client)
        # Must not raise.
        asyncio.run(service.run_once())

        async def _verify() -> Any:
            async with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
                return await get_tts_cache_entry(
                    session,
                    message_id="msg_missing_blob",
                    voice="alloy",
                    model="tts-1",
                )

        assert asyncio.run(_verify()) is None
