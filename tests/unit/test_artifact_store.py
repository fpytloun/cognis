from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from cognis.api.routes.artifacts import _clamp_ttl_to_artifact_expiry, _is_expired
from cognis.artifacts import store as artifact_store_module
from cognis.artifacts.store import (
    ArtifactStore,
    ArtifactStoreConfig,
    S3ArtifactBackend,
    sanitize_artifact_filename,
)


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._pages


class _FakeS3Client:
    def __init__(self) -> None:
        self.deleted_objects: list[tuple[str, str]] = []
        self.delete_objects_calls: list[dict[str, Any]] = []
        self.paginate_kwargs: dict[str, Any] | None = None

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == "list_objects_v2"
        client = self

        class _RecordingPaginator(_FakePaginator):
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                client.paginate_kwargs = kwargs
                return super().paginate(**kwargs)

        return _RecordingPaginator(
            [
                {
                    "Contents": [
                        {"Key": "images/img_123/image.png"},
                        {"Key": "images/img_123/.metadata.json"},
                    ]
                }
            ]
        )

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deleted_objects.append((Bucket, Key))

    def delete_objects(self, **kwargs: Any) -> None:
        self.delete_objects_calls.append(kwargs)


def test_public_url_uses_cognis_base_url(tmp_path) -> None:
    store = ArtifactStore(
        ArtifactStoreConfig(
            backend="filesystem",
            path=str(tmp_path),
            base_url="https://cognis.example.com",
            signing_secret="test-secret",
        )
    )

    url = store.get_public_url("images", "img_123", "image")

    assert url.startswith(
        "https://cognis.example.com/api/v1/artifacts/content/images/img_123/image?"
    )
    assert "minio.minio.svc.cluster.local" not in url


def test_public_view_url_uses_distinct_route_and_signature(tmp_path) -> None:
    store = ArtifactStore(
        ArtifactStoreConfig(
            backend="filesystem",
            path=str(tmp_path),
            base_url="https://cognis.example.com",
            signing_secret="test-secret",
        )
    )

    download_url = store.get_public_url("reports", "html_123", "report.html")
    view_url = store.get_public_url("reports", "html_123", "report.html", mode="view")

    assert "/api/v1/artifacts/content/reports/html_123/report.html?" in download_url
    assert "/api/v1/artifacts/view/reports/html_123/report.html?" in view_url
    assert download_url.split("sig=", 1)[1] != view_url.split("sig=", 1)[1]


def test_public_url_clamps_at_signing_time_to_absolute_artifact_expiry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ArtifactStore(
        ArtifactStoreConfig(
            backend="filesystem",
            path=str(tmp_path),
            base_url="https://cognis.example.com",
            signing_secret="test-secret",
            signed_url_ttl_seconds=3600,
        )
    )
    expires_at = datetime.fromtimestamp(1010, tz=UTC)
    monkeypatch.setattr(artifact_store_module.time, "time", lambda: 1005)

    url = store.get_public_url(
        "attachments",
        "art_123",
        "input.pdf",
        expires_at=expires_at,
    )

    assert parse_qs(urlparse(url).query)["exp"] == ["1010"]
    monkeypatch.setattr(artifact_store_module.time, "time", lambda: 1010)
    with pytest.raises(ValueError, match="Artifact has expired"):
        store.get_public_url(
            "attachments",
            "art_123",
            "input.pdf",
            expires_at=expires_at,
        )


def test_sanitize_artifact_filename_preserves_json_extension() -> None:
    assert sanitize_artifact_filename("export (1).json") == "export_1_.json"


def test_sanitize_artifact_filename_drops_path_components() -> None:
    assert sanitize_artifact_filename("../chat export.json") == "chat_export.json"
    assert sanitize_artifact_filename(r"C:\\tmp\\data.json") == "data.json"


def test_filesystem_store_accepts_sanitized_json_upload_name(tmp_path) -> None:
    store = ArtifactStore(
        ArtifactStoreConfig(
            backend="filesystem",
            path=str(tmp_path),
            base_url="https://cognis.example.com",
            signing_secret="test-secret",
        )
    )
    filename = sanitize_artifact_filename("export (1).json")

    store.save("attachments", "att_123", filename, b"{}", "application/json")

    content, content_type = store.load("attachments", "att_123", filename)
    assert content == b"{}"
    assert content_type == "application/json"


def test_s3_delete_object_deletes_prefix_keys_individually() -> None:
    backend = S3ArtifactBackend.__new__(S3ArtifactBackend)
    client = _FakeS3Client()
    backend._client = client
    backend._bucket = "artifacts"

    backend.delete_object("images", "img_123")

    assert client.paginate_kwargs == {"Bucket": "artifacts", "Prefix": "images/img_123/"}
    assert client.deleted_objects == [
        ("artifacts", "images/img_123/image.png"),
        ("artifacts", "images/img_123/.metadata.json"),
    ]
    assert client.delete_objects_calls == []


def test_artifact_route_expiry_helper_rejects_expired_rows() -> None:
    class _Row:
        expires_at = None

    row = _Row()
    now = datetime.now(UTC)

    row.expires_at = now - timedelta(seconds=1)
    assert _is_expired(row, now=now) is True

    row.expires_at = now + timedelta(seconds=1)
    assert _is_expired(row, now=now) is False


def test_artifact_route_expiry_helper_handles_naive_datetimes() -> None:
    class _Row:
        expires_at = None

    row = _Row()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row.expires_at = datetime(2025, 12, 31, 23, 59, 59)

    assert _is_expired(row, now=now) is True


def test_artifact_signed_url_ttl_clamps_to_artifact_expiry() -> None:
    class _Row:
        expires_at = datetime.now(UTC) + timedelta(seconds=300)

    assert _clamp_ttl_to_artifact_expiry(_Row(), 3600) <= 300
