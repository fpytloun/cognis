from __future__ import annotations

from typing import Any

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
