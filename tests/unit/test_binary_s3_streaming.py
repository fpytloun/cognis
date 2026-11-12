"""SDK multipart lifecycle tests and opt-in loopback S3 protocol qualification."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.artifacts.streaming import S3StreamWriter, write_stream


class S3Double:
    """Synchronous SDK double exercising real disk-backed part bodies and workers."""

    def __init__(self, lose_completion: bool = False):
        self.parts = {}
        self.objects = {}
        self.bodies = []
        self.lock = threading.Lock()
        self.second = threading.Event()
        self.active = 0
        self.peak = 0
        self.aborted = False
        self.completed = False
        self.lose_completion = lose_completion

    def create_multipart_upload(self, **kwargs):
        return {"UploadId": "task-owned-multipart"}

    def upload_part(self, **kwargs):
        body, number = kwargs["Body"], kwargs["PartNumber"]
        self.bodies.append(body)
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if number == 2:
                self.second.set()
            if number == 1:
                self.second.wait(2)
            value = body.read()
            self.parts[number] = value
            return {"ETag": hashlib.md5(value, usedforsecurity=False).hexdigest()}
        finally:
            with self.lock:
                self.active -= 1

    def complete_multipart_upload(self, **kwargs):
        self.objects[kwargs["Key"]] = b"".join(
            self.parts[part["PartNumber"]] for part in kwargs["MultipartUpload"]["Parts"]
        )
        self.completed = True
        if self.lose_completion:
            raise OSError("Completion acknowledgement lost")
        return {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def abort_multipart_upload(self, **kwargs):
        self.aborted = True
        if self.completed:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "AbortMultipartUpload")

    def delete_objects(self, **kwargs):
        for item in kwargs["Delete"]["Objects"]:
            self.objects.pop(item["Key"], None)


async def chunks(value: bytes):
    for offset in range(0, len(value), 1024 * 1024):
        yield value[offset : offset + 1024 * 1024]


async def test_disk_backed_parts_are_parallel_bounded_and_closed() -> None:
    value = os.urandom(32 * 1024 * 1024 + 23)
    sdk = S3Double()
    writer = S3StreamWriter(sdk, "binary-test", "payload", "metadata", len(value))
    size, digest = await write_stream(
        writer,
        chunks(value),
        expected_size=len(value),
        content_type="application/octet-stream",
        owner_email="binary-owner@example.com",
    )
    assert size == len(value)
    assert digest == hashlib.sha256(value).hexdigest()
    assert sdk.objects["payload"] == value
    assert 2 <= sdk.peak <= 4
    assert sdk.active == 0
    assert all(body.closed for body in sdk.bodies)
    assert not sdk.aborted


async def test_lost_multipart_completion_ack_cannot_leave_an_orphan() -> None:
    value = os.urandom(16 * 1024 * 1024)
    sdk = S3Double(lose_completion=True)
    writer = S3StreamWriter(sdk, "binary-test", "payload", "metadata", len(value))
    with pytest.raises(OSError, match="acknowledgement"):
        await write_stream(
            writer,
            chunks(value),
            expected_size=len(value),
            content_type="application/octet-stream",
            owner_email="binary-owner@example.com",
        )
    assert sdk.aborted
    assert not sdk.objects
    assert sdk.active == 0
    assert all(body.closed for body in sdk.bodies)


async def test_input_failure_aborts_and_joins_multipart_workers() -> None:
    sdk = S3Double()
    writer = S3StreamWriter(sdk, "binary-test", "payload", "metadata", 20 * 1024 * 1024)

    async def interrupted():
        yield b"a" * (8 * 1024 * 1024)
        raise ConnectionResetError("Source disconnected")

    with pytest.raises(ConnectionResetError):
        await write_stream(
            writer,
            interrupted(),
            expected_size=20 * 1024 * 1024,
            content_type="application/octet-stream",
            owner_email="binary-owner@example.com",
        )
    assert sdk.aborted
    assert not sdk.objects
    assert sdk.active == 0
    assert all(body.closed for body in sdk.bodies)


@pytest.mark.skipif(
    not os.environ.get("COGNIS_BINARY_TEST_S3_ENDPOINT"),
    reason="Requires an explicitly task-owned loopback S3 service",
)
async def test_optional_loopback_s3_protocol() -> None:
    """Run when an explicitly task-owned S3 simulator is supplied by the caller."""
    endpoint = os.environ.get("COGNIS_BINARY_TEST_S3_ENDPOINT")
    assert endpoint is not None
    from urllib.parse import urlsplit

    assert urlsplit(endpoint).hostname in {"127.0.0.1", "localhost"}
    bucket = "binary-transfer-" + uuid.uuid4().hex
    sdk = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )
    await asyncio.to_thread(sdk.create_bucket, Bucket=bucket)
    store = ArtifactStore(
        ArtifactStoreConfig(
            backend="s3",
            s3_endpoint=endpoint,
            s3_bucket=bucket,
            s3_access_key="test",
            s3_secret_key="test",
        )
    )
    value = os.urandom(20 * 1024 * 1024 + 123)
    try:
        size, digest = await store.async_receive_stream(
            "artifacts",
            "native-s3",
            "payload.bin",
            chunks(value),
            "application/octet-stream",
            "binary-owner@example.com",
            expected_size=len(value),
        )
        assert size == len(value)
        assert digest == hashlib.sha256(value).hexdigest()
        assert (await store.async_load("artifacts", "native-s3", "payload.bin"))[0] == value
        ranged = b"".join(
            [
                chunk
                async for chunk in store.async_read_range(
                    "artifacts", "native-s3", "payload.bin", start=1024, end=9 * 1024 * 1024
                )
            ]
        )
        assert ranged == value[1024 : 9 * 1024 * 1024 + 1]
        incomplete = await asyncio.to_thread(sdk.list_multipart_uploads, Bucket=bucket)
        assert not incomplete.get("Uploads")
        for key in (
            "attachments/abandoned/payload.bin",
            "attachments/abandoned-neighbor/payload.bin",
        ):
            await asyncio.to_thread(sdk.create_multipart_upload, Bucket=bucket, Key=key)
        await store.async_discard_staged_object("attachments", "abandoned")
        remaining = await asyncio.to_thread(sdk.list_multipart_uploads, Bucket=bucket)
        assert [upload["Key"] for upload in remaining["Uploads"]] == [
            "attachments/abandoned-neighbor/payload.bin"
        ]
    finally:
        incomplete = await asyncio.to_thread(sdk.list_multipart_uploads, Bucket=bucket)
        for upload in incomplete.get("Uploads", []):
            await asyncio.to_thread(
                sdk.abort_multipart_upload,
                Bucket=bucket,
                Key=upload["Key"],
                UploadId=upload["UploadId"],
            )
        objects = await asyncio.to_thread(sdk.list_objects_v2, Bucket=bucket)
        if objects.get("Contents"):
            await asyncio.to_thread(
                sdk.delete_objects,
                Bucket=bucket,
                Delete={"Objects": [{"Key": item["Key"]} for item in objects["Contents"]]},
            )
        await asyncio.to_thread(sdk.delete_bucket, Bucket=bucket)
