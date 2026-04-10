from __future__ import annotations

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig


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
