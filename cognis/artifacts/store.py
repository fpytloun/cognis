"""Artifact storage with filesystem and S3 backends.

Follows the same pattern as Mnemory's ArtifactBackend — a Protocol
interface with pluggable backends selected by configuration.

Storage layout: {namespace}/{object_id}/{filename}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from cognis.logging import get_logger

logger = get_logger(__name__)

# Safe path component pattern — rejects traversal attempts
_SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:@/-]*$")
_MAX_ARTIFACT_SIZE_DEFAULT = 50 * 1024 * 1024  # 50 MB


@dataclass(frozen=True)
class ArtifactStoreConfig:
    """Configuration for the artifact store."""

    backend: str = "filesystem"  # "filesystem" or "s3"
    path: str = ""  # filesystem base path
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "cognis-artifacts"
    s3_region: str = ""
    max_size_bytes: int = _MAX_ARTIFACT_SIZE_DEFAULT
    base_url: str = ""
    signing_secret: str = ""
    signed_url_ttl_seconds: int = 3600


@dataclass(frozen=True)
class ArtifactMetadata:
    """Metadata stored alongside an artifact."""

    content_type: str
    size: int
    owner_email: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_type": self.content_type,
            "size": self.size,
            "owner_email": self.owner_email,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactMetadata:
        return cls(
            content_type=str(data.get("content_type", "application/octet-stream")),
            size=int(data.get("size", 0)),
            owner_email=data.get("owner_email"),
        )


def _validate_path_component(component: str) -> None:
    """Validate a path component to prevent traversal attacks."""
    if not component:
        raise ValueError("Empty path component")
    if ".." in component:
        raise ValueError(f"Path traversal detected: {component}")
    if not _SAFE_PATH_RE.match(component):
        raise ValueError(f"Unsafe path component: {component}")


@runtime_checkable
class ArtifactBackend(Protocol):
    """Protocol for artifact storage backends.

    Storage path: {namespace}/{object_id}/{filename}
    """

    def save(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        owner_email: str | None = None,
    ) -> None: ...

    def load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        """Load artifact content and content_type."""
        ...

    def load_metadata(
        self, namespace: str, object_id: str, filename: str
    ) -> ArtifactMetadata | None:
        """Load artifact metadata without content."""
        ...

    def delete(self, namespace: str, object_id: str, filename: str) -> None: ...

    def delete_object(self, namespace: str, object_id: str) -> None:
        """Delete all files for an object."""
        ...

    def exists(self, namespace: str, object_id: str, filename: str) -> bool: ...

    def get_signed_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int,
    ) -> str | None: ...


class FilesystemArtifactBackend:
    """Store artifacts on the local filesystem.

    Layout: {base_path}/{namespace}/{object_id}/{filename}
    Metadata: {base_path}/{namespace}/{object_id}/.metadata.json
    """

    def __init__(self, base_path: Path) -> None:
        self._base = base_path
        self._base.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, namespace: str, object_id: str, filename: str) -> Path:
        _validate_path_component(namespace)
        _validate_path_component(object_id)
        _validate_path_component(filename)
        path = self._base / namespace / object_id / filename
        resolved = path.resolve()
        if not resolved.is_relative_to(self._base.resolve()):
            raise ValueError("Path traversal detected")
        return resolved

    def _metadata_path(self, namespace: str, object_id: str) -> Path:
        return self._base / namespace / object_id / ".metadata.json"

    def save(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        owner_email: str | None = None,
    ) -> None:
        path = self._resolve_path(namespace, object_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        # Write metadata sidecar
        meta = ArtifactMetadata(
            content_type=content_type,
            size=len(content),
            owner_email=owner_email,
        )
        meta_path = self._metadata_path(namespace, object_id)
        meta_path.write_text(json.dumps(meta.to_dict()), encoding="utf-8")

    def load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        path = self._resolve_path(namespace, object_id, filename)
        if not path.exists():
            raise FileNotFoundError(f"Artifact not found: {namespace}/{object_id}/{filename}")
        content = path.read_bytes()
        meta = self.load_metadata(namespace, object_id, filename)
        content_type = meta.content_type if meta else "application/octet-stream"
        return content, content_type

    def load_metadata(
        self, namespace: str, object_id: str, filename: str
    ) -> ArtifactMetadata | None:
        _validate_path_component(namespace)
        _validate_path_component(object_id)
        meta_path = self._metadata_path(namespace, object_id)
        if not meta_path.exists():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return ArtifactMetadata.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return None

    def delete(self, namespace: str, object_id: str, filename: str) -> None:
        path = self._resolve_path(namespace, object_id, filename)
        if path.exists():
            path.unlink()

    def delete_object(self, namespace: str, object_id: str) -> None:
        _validate_path_component(namespace)
        _validate_path_component(object_id)
        obj_dir = self._base / namespace / object_id
        resolved = obj_dir.resolve()
        if resolved.is_relative_to(self._base.resolve()) and resolved.is_dir():
            shutil.rmtree(resolved)

    def exists(self, namespace: str, object_id: str, filename: str) -> bool:
        try:
            path = self._resolve_path(namespace, object_id, filename)
            return path.exists()
        except ValueError:
            return False

    def get_signed_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int,
    ) -> str | None:
        return None


class S3ArtifactBackend:
    """Store artifacts in S3 (or MinIO-compatible storage).

    Object key: {namespace}/{object_id}/{filename}
    Metadata stored as S3 object metadata.
    """

    def __init__(self, config: ArtifactStoreConfig) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        kwargs: dict[str, Any] = {
            "endpoint_url": config.s3_endpoint,
            "aws_access_key_id": config.s3_access_key,
            "aws_secret_access_key": config.s3_secret_key,
            "config": BotoConfig(signature_version="s3v4"),
        }
        if config.s3_region:
            kwargs["region_name"] = config.s3_region

        self._client = boto3.client("s3", **kwargs)
        self._bucket = config.s3_bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self._bucket)
                logger.info("Created S3 bucket", extra={"extra_data": {"bucket": self._bucket}})
            except Exception:
                logger.warning(
                    "Failed to create S3 bucket",
                    extra={"extra_data": {"bucket": self._bucket}},
                )

    def _key(self, namespace: str, object_id: str, filename: str) -> str:
        _validate_path_component(namespace)
        _validate_path_component(object_id)
        _validate_path_component(filename)
        return f"{namespace}/{object_id}/{filename}"

    def _meta_key(self, namespace: str, object_id: str) -> str:
        return f"{namespace}/{object_id}/.metadata.json"

    def save(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        owner_email: str | None = None,
    ) -> None:
        key = self._key(namespace, object_id, filename)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )
        # Store metadata as separate JSON object
        meta = ArtifactMetadata(
            content_type=content_type,
            size=len(content),
            owner_email=owner_email,
        )
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._meta_key(namespace, object_id),
            Body=json.dumps(meta.to_dict()).encode(),
            ContentType="application/json",
        )

    def load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        key = self._key(namespace, object_id, filename)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            content = response["Body"].read()
            content_type = response.get("ContentType", "application/octet-stream")
            return content, content_type
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(f"Artifact not found: {key}") from None

    def load_metadata(
        self, namespace: str, object_id: str, filename: str
    ) -> ArtifactMetadata | None:
        meta_key = self._meta_key(namespace, object_id)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=meta_key)
            data = json.loads(response["Body"].read())
            return ArtifactMetadata.from_dict(data)
        except Exception:
            return None

    def delete(self, namespace: str, object_id: str, filename: str) -> None:
        key = self._key(namespace, object_id, filename)
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def delete_object(self, namespace: str, object_id: str) -> None:
        _validate_path_component(namespace)
        _validate_path_component(object_id)
        prefix = f"{namespace}/{object_id}/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            objects = page.get("Contents", [])
            if not objects:
                continue
            for obj in objects:
                self._client.delete_object(Bucket=self._bucket, Key=obj["Key"])

    def exists(self, namespace: str, object_id: str, filename: str) -> bool:
        key = self._key(namespace, object_id, filename)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def get_signed_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int,
    ) -> str | None:
        key = self._key(namespace, object_id, filename)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )


class ArtifactStore:
    """High-level artifact store with backend selection and size limits.

    All public methods are synchronous (backends use blocking I/O).
    Callers in async contexts MUST use the ``async_*`` methods which
    wrap operations in ``asyncio.to_thread()``.
    """

    def __init__(self, config: ArtifactStoreConfig) -> None:
        self._config = config
        if config.backend == "s3":
            self._backend: ArtifactBackend = S3ArtifactBackend(config)
        elif config.backend == "filesystem":
            self._backend = FilesystemArtifactBackend(Path(config.path))
        else:
            raise ValueError(f"Unsupported artifact backend: {config.backend}")

    @staticmethod
    def generate_id(prefix: str = "img") -> str:
        """Generate a unique artifact ID."""
        return f"{prefix}_{uuid.uuid4().hex}"

    def save(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        owner_email: str | None = None,
    ) -> None:
        """Save an artifact with size validation (sync)."""
        if len(content) > self._config.max_size_bytes:
            max_mb = self._config.max_size_bytes / (1024 * 1024)
            raise ValueError(f"Artifact exceeds maximum size of {max_mb:.0f}MB")
        self._backend.save(namespace, object_id, filename, content, content_type, owner_email)

    def load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        """Load artifact content and content_type (sync)."""
        return self._backend.load(namespace, object_id, filename)

    def load_metadata(
        self, namespace: str, object_id: str, filename: str
    ) -> ArtifactMetadata | None:
        """Load artifact metadata without content (sync)."""
        return self._backend.load_metadata(namespace, object_id, filename)

    def delete(self, namespace: str, object_id: str, filename: str) -> None:
        """Delete a single artifact file (sync)."""
        self._backend.delete(namespace, object_id, filename)

    def delete_object(self, namespace: str, object_id: str) -> None:
        """Delete all files for an object (sync)."""
        self._backend.delete_object(namespace, object_id)

    def exists(self, namespace: str, object_id: str, filename: str) -> bool:
        """Check if an artifact exists (sync)."""
        return self._backend.exists(namespace, object_id, filename)

    def _filesystem_signature(self, namespace: str, object_id: str, filename: str, exp: int) -> str:
        payload = f"{namespace}:{object_id}:{filename}:{exp}".encode()
        secret = self._config.signing_secret.encode("utf-8")
        return hmac.new(secret, payload, hashlib.sha256).hexdigest()

    def get_signed_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        """Generate a signed URL for an artifact.

        S3 backends use native presigned URLs. Filesystem backends return an
        application URL signed with HMAC.
        """
        ttl = ttl_seconds or self._config.signed_url_ttl_seconds
        backend_url = self._backend.get_signed_url(
            namespace,
            object_id,
            filename,
            ttl_seconds=ttl,
        )
        if backend_url is not None:
            return backend_url
        if not self._config.base_url or not self._config.signing_secret:
            raise ValueError("Artifact signing requires base_url and signing_secret")
        exp = int(time.time()) + ttl
        sig = self._filesystem_signature(namespace, object_id, filename, exp)
        path = f"/api/v1/artifacts/content/{quote(namespace)}/{quote(object_id)}/{quote(filename)}"
        return f"{self._config.base_url}{path}?exp={exp}&sig={sig}"

    def get_public_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        """Generate a Cognis-served signed URL for an artifact.

        Unlike ``get_signed_url()``, this always uses the controller-facing
        Cognis URL instead of backend-native presigned URLs. Use this for any
        user-facing or channel-facing artifact delivery where internal backend
        hostnames may not be reachable.
        """
        if not self._config.base_url or not self._config.signing_secret:
            raise ValueError("Artifact public URLs require base_url and signing_secret")
        ttl = ttl_seconds or self._config.signed_url_ttl_seconds
        exp = int(time.time()) + ttl
        sig = self._filesystem_signature(namespace, object_id, filename, exp)
        path = f"/api/v1/artifacts/content/{quote(namespace)}/{quote(object_id)}/{quote(filename)}"
        return f"{self._config.base_url}{path}?exp={exp}&sig={sig}"

    def verify_signed_request(
        self, namespace: str, object_id: str, filename: str, *, exp: int, sig: str
    ) -> bool:
        if exp < int(time.time()):
            return False
        if not self._config.signing_secret:
            return False
        expected = self._filesystem_signature(namespace, object_id, filename, exp)
        return hmac.compare_digest(expected, sig)

    # ------------------------------------------------------------------
    # Async wrappers — use these from async route handlers and tools
    # ------------------------------------------------------------------

    async def async_save(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        owner_email: str | None = None,
    ) -> None:
        """Save an artifact (async, thread-safe)."""
        import asyncio

        await asyncio.to_thread(
            self.save, namespace, object_id, filename, content, content_type, owner_email
        )

    async def async_load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        """Load artifact content and content_type (async)."""
        import asyncio

        return await asyncio.to_thread(self.load, namespace, object_id, filename)

    async def async_load_metadata(
        self, namespace: str, object_id: str, filename: str
    ) -> ArtifactMetadata | None:
        """Load artifact metadata without content (async)."""
        import asyncio

        return await asyncio.to_thread(self.load_metadata, namespace, object_id, filename)

    async def async_delete(self, namespace: str, object_id: str, filename: str) -> None:
        """Delete a single artifact file (async)."""
        import asyncio

        await asyncio.to_thread(self.delete, namespace, object_id, filename)

    async def async_delete_object(self, namespace: str, object_id: str) -> None:
        """Delete all files for an object (async)."""
        import asyncio

        await asyncio.to_thread(self.delete_object, namespace, object_id)

    async def async_exists(self, namespace: str, object_id: str, filename: str) -> bool:
        """Check if an artifact exists (async)."""
        import asyncio

        return await asyncio.to_thread(self.exists, namespace, object_id, filename)

    async def async_get_signed_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        import asyncio

        return await asyncio.to_thread(
            self.get_signed_url, namespace, object_id, filename, ttl_seconds=ttl_seconds
        )

    async def async_get_public_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        import asyncio

        return await asyncio.to_thread(
            self.get_public_url, namespace, object_id, filename, ttl_seconds=ttl_seconds
        )
