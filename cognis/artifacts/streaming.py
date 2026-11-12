"""Cancellation-safe, bounded-memory object writers for native binary transfers."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from botocore.exceptions import ClientError

from cognis import json_codec as json
from cognis.artifacts.json_validation import JSONStreamValidator
from cognis.transfers.models import TRANSFER_CHUNK_BYTES


async def storage_io[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Join worker I/O before allowing cancellation to reclaim its resources."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


class StreamWriter(Protocol):
    """An uncommitted immutable object, owned by exactly one receiver."""

    async def write(self, chunk: bytes) -> None: ...
    async def complete(self, metadata: dict[str, Any]) -> None: ...
    async def abort(self) -> None: ...


class FilesystemStreamWriter:
    """Write once on the destination filesystem; commit by atomic replacement."""

    def __init__(self, path: Path, metadata_path: Path) -> None:
        self.path = path
        self.metadata_path = metadata_path
        self.source: BinaryIO | None = None
        self.staging_path: Path | None = None
        self.published = False

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The receiving task owns this file across asynchronous write calls.
        descriptor, name = tempfile.mkstemp(dir=self.path.parent)
        self.staging_path = Path(name)
        self.source = os.fdopen(descriptor, "w+b")

    async def write(self, chunk: bytes) -> None:
        if self.source is None:
            await storage_io(self._open)
        assert self.source is not None
        await storage_io(self.source.write, chunk)

    def _complete(self, metadata: dict[str, Any]) -> None:
        if self.source is None:
            self._open()
        assert self.source is not None and self.staging_path is not None
        self.source.flush()
        os.fsync(self.source.fileno())
        self.source.close()
        self.staging_path.replace(self.path)
        self.published = True
        self.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    async def complete(self, metadata: dict[str, Any]) -> None:
        """Publish bytes only after the receiver validates the complete stream."""
        await storage_io(self._complete, metadata)

    def _abort(self) -> None:
        if self.source is not None:
            self.source.close()
        if self.staging_path is not None:
            self.staging_path.unlink(missing_ok=True)
        if self.published:
            self.path.unlink(missing_ok=True)
            self.metadata_path.unlink(missing_ok=True)

    async def abort(self) -> None:
        """Discard only the object owned by this immutable upload slot."""
        await storage_io(self._abort)


class S3StreamWriter:
    """Bounded parallel multipart upload; large parts spool to disk, not RAM."""

    def __init__(
        self,
        client: Any,
        bucket: str,
        key: str,
        metadata_key: str,
        expected_size: int,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.client, self.bucket, self.key, self.metadata_key = client, bucket, key, metadata_key
        self.content_type = content_type
        # S3 allows 10,000 parts. Larger files increase disk-backed part sizes,
        # never process-memory buffers.
        self.part_size = max(8 * 1024 * 1024, (expected_size + 9998) // 9999)
        self.upload_id: str | None = None
        self.part: BinaryIO | None = None
        self.part_bytes = 0
        self.next_part = 1
        self.pending: set[asyncio.Task[dict[str, Any]]] = set()
        self.parts: list[dict[str, Any]] = []
        self.published = False
        self.started = False

    async def _start(self) -> None:
        if self.upload_id is None:

            def begin() -> None:
                self.started = True
                result = self.client.create_multipart_upload(
                    Bucket=self.bucket, Key=self.key, ContentType=self.content_type
                )
                self.upload_id = result["UploadId"]

            await storage_io(begin)

    async def _send_part(self, source: BinaryIO, number: int) -> dict[str, Any]:
        try:

            def upload() -> dict[str, Any]:
                source.seek(0)
                result = self.client.upload_part(
                    Bucket=self.bucket,
                    Key=self.key,
                    UploadId=self.upload_id,
                    PartNumber=number,
                    Body=source,
                )
                return {"PartNumber": number, "ETag": result["ETag"]}

            return await storage_io(upload)
        finally:
            await storage_io(source.close)

    async def _flush_part(self) -> None:
        assert self.part is not None
        self.pending.add(asyncio.create_task(self._send_part(self.part, self.next_part)))
        self.part, self.part_bytes = None, 0
        self.next_part += 1
        if len(self.pending) >= 4:
            done, pending = await asyncio.wait(self.pending, return_when=asyncio.FIRST_COMPLETED)
            self.pending = pending
            self.parts.extend(await asyncio.gather(*done))

    async def write(self, chunk: bytes) -> None:
        await self._start()
        view = memoryview(chunk)
        while view:
            if self.part is None:

                def open_part() -> None:
                    self.part = tempfile.TemporaryFile()  # noqa: SIM115

                await storage_io(open_part)
            assert self.part is not None
            length = min(len(view), self.part_size - self.part_bytes)
            await storage_io(self.part.write, view[:length])
            self.part_bytes += length
            view = view[length:]
            if self.part_bytes == self.part_size:
                await self._flush_part()

    async def complete(self, metadata: dict[str, Any]) -> None:
        """Join all part uploads before completing the immutable object."""
        if self.upload_id is None:

            def publish_empty() -> None:
                self.started = True
                self.client.put_object(
                    Bucket=self.bucket, Key=self.key, Body=b"", ContentType=self.content_type
                )
                self.published = True

            await storage_io(publish_empty)
        else:
            if self.part is not None:
                await self._flush_part()
            self.parts.extend(await asyncio.gather(*self.pending))
            self.pending.clear()

            def publish_parts() -> None:
                self.client.complete_multipart_upload(
                    Bucket=self.bucket,
                    Key=self.key,
                    UploadId=self.upload_id,
                    MultipartUpload={
                        "Parts": sorted(self.parts, key=lambda part: part["PartNumber"])
                    },
                )
                self.published = True

            await storage_io(publish_parts)
        await storage_io(
            self.client.put_object,
            Bucket=self.bucket,
            Key=self.metadata_key,
            Body=json.dumps(metadata).encode(),
            ContentType="application/json",
        )

    async def abort(self) -> None:
        """Join workers before aborting multipart state and reclaiming resources."""
        await asyncio.gather(*self.pending, return_exceptions=True)
        self.pending.clear()
        if self.part is not None:
            await storage_io(self.part.close)
            self.part = None
        if self.upload_id is not None and not self.published:
            try:
                await storage_io(
                    self.client.abort_multipart_upload,
                    Bucket=self.bucket,
                    Key=self.key,
                    UploadId=self.upload_id,
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "NoSuchUpload":
                    raise
        if self.started or self.published:
            await storage_io(
                self.client.delete_objects,
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": self.key}, {"Key": self.metadata_key}]},
            )


async def write_stream(
    writer: StreamWriter,
    content: AsyncIterator[bytes],
    *,
    expected_size: int,
    content_type: str,
    owner_email: str,
    check_current: Callable[[], Any] | None = None,
    validator: JSONStreamValidator | None = None,
) -> tuple[int, str]:
    """Receive exact bytes with bounded writes and receiver-computed integrity."""
    total = 0
    digest = hashlib.sha256()
    try:
        async for incoming in content:
            for offset in range(0, len(incoming), TRANSFER_CHUNK_BYTES):
                chunk = incoming[offset : offset + TRANSFER_CHUNK_BYTES]
                total += len(chunk)
                if total > expected_size:
                    raise ValueError("Transfer exceeds its declared length")
                digest.update(chunk)
                if validator is not None:
                    validator.feed(chunk)
                await writer.write(chunk)
            if check_current is not None:
                await check_current()
        if total != expected_size:
            raise ValueError("Transfer is truncated")
        if check_current is not None:
            await check_current()
        if validator is not None:
            validator.finish()
        await writer.complete(
            {"content_type": content_type, "size": total, "owner_email": owner_email}
        )
        return total, digest.hexdigest()
    except BaseException:
        await writer.abort()
        raise
