"""Exact-job HTTP transfers for executor inference, never binary WebSocket payloads."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, select

from cognis.artifacts.store import ArtifactMetadata
from cognis.models.video import (
    TERMINAL_VIDEO_STATES,
    VideoCapabilities,
    VideoOutput,
    VideoRemoteResult,
    VideoRequest,
)
from cognis.providers.video import VideoConnection, VideoInput
from cognis.store.models import VideoJobRow, VideoTransferRow

if TYPE_CHECKING:
    from cognis.core.video import VideoService


class VideoTransfers:
    def __init__(self, service: VideoService):
        self.service = service

    def artifact_id(self, row: VideoJobRow, output_key: str = "output") -> str:
        return "art_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{row.attempt_id}/{output_key}").hex

    async def issue(self, row: VideoJobRow, output_key: str = "output") -> dict[str, Any]:
        policy = await self.service.policy(row.owner_email)
        token = secrets.token_urlsafe(32)
        async with self.service.sessions() as session:
            session.add(
                VideoTransferRow(
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    job_id=row.job_id,
                    attempt_id=row.attempt_id,
                    direction="output",
                    max_bytes=policy.max_output_bytes,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    artifact_id=self.artifact_id(row, output_key),
                )
            )
            await session.commit()
        return {
            "url": self.service.artifacts.base_url.rstrip("/") + "/api/v1/videos/transfer",
            "token": token,
            "max_bytes": policy.max_output_bytes,
        }

    async def authorize(
        self, token: str | None, direction: str
    ) -> tuple[VideoTransferRow, VideoJobRow]:
        if not token or len(token) > 100:
            raise PermissionError("Transfer not found")
        async with self.service.sessions() as session:
            transfer = await session.get(
                VideoTransferRow, hashlib.sha256(token.encode()).hexdigest()
            )
        if transfer is None or transfer.direction != direction:
            raise PermissionError("Transfer not found")
        expiry = (
            transfer.expires_at.replace(tzinfo=UTC)
            if transfer.expires_at.tzinfo is None
            else transfer.expires_at
        )
        row = await self.service.store.get(transfer.job_id)
        if (
            expiry <= datetime.now(UTC)
            or row is None
            or row.attempt_id != transfer.attempt_id
            or row.status in TERMINAL_VIDEO_STATES
            or row.cancel_requested
        ):
            raise PermissionError("Transfer unavailable")
        await self.service.context(row.owner_email, row.conversation_id)
        return transfer, row

    async def receive(self, token: str | None, chunks: AsyncIterator[bytes]) -> dict[str, str]:
        from cognis.providers.video_media import mp4_stream

        transfer, row = await self.authorize(token, "output")
        artifact_id = transfer.artifact_id
        if artifact_id is None:
            raise PermissionError("Transfer has no artifact binding")
        if await self.service.artifacts.async_load_metadata(
            "video_staging", artifact_id, "video-1.mp4"
        ):
            return {"artifact_id": artifact_id}
        await self.service.artifacts.async_save_stream(
            "video_staging",
            artifact_id,
            "video-1.mp4",
            mp4_stream(chunks),
            content_type="video/mp4",
            owner_email=row.owner_email,
            max_bytes=transfer.max_bytes,
        )
        try:
            # Recheck cancellation/revocation after external I/O.
            await self.authorize(token, "output")
        except BaseException:
            await self.service.artifacts.async_delete_object("video_staging", artifact_id)
            raise
        return {"artifact_id": artifact_id}

    async def cleanup(self) -> None:
        """Reclaim completed/cancelled/expired transfers, retaining any live transfer."""
        now = datetime.now(UTC)
        async with self.service.sessions() as session:
            rows = list(
                await session.scalars(
                    select(VideoTransferRow)
                    .join(VideoJobRow)
                    .where(
                        VideoJobRow.status.in_(TERMINAL_VIDEO_STATES)
                        | (VideoTransferRow.expires_at <= now)
                    )
                    .limit(100)
                )
            )
        for transfer in rows:
            async with self.service.sessions() as session:
                live = await session.scalar(
                    select(VideoTransferRow.token_hash)
                    .join(VideoJobRow)
                    .where(
                        VideoTransferRow.artifact_id == transfer.artifact_id,
                        VideoTransferRow.expires_at > now,
                        VideoJobRow.status.not_in(TERMINAL_VIDEO_STATES),
                    )
                    .limit(1)
                )
                if not live:
                    await self.service.artifacts.async_delete_object(
                        "video_staging", transfer.artifact_id
                    )
                await session.execute(
                    delete(VideoTransferRow).where(
                        VideoTransferRow.token_hash == transfer.token_hash
                    )
                )
                await session.commit()

    async def staged(self, row: VideoJobRow, artifact_id: str) -> ArtifactMetadata:
        async with self.service.sessions() as session:
            transfer = await session.scalar(
                select(VideoTransferRow).where(
                    VideoTransferRow.job_id == row.job_id,
                    VideoTransferRow.attempt_id == row.attempt_id,
                    VideoTransferRow.artifact_id == artifact_id,
                    VideoTransferRow.direction == "output",
                )
            )
        if transfer is None:
            raise PermissionError("Output does not belong to this attempt")
        metadata = await self.service.artifacts.async_load_metadata(
            "video_staging", artifact_id, "video-1.mp4"
        )
        if metadata is None:
            raise ValueError("Staged video expired")
        return cast(ArtifactMetadata, metadata)


class ExecutorVideoAdapter:
    """Same portable provider adapter on the configured executor; no runtime fallback."""

    def __init__(
        self,
        service: VideoService,
        connection: VideoConnection,
        binding: dict[str, Any],
        row: VideoJobRow | None = None,
    ):
        self.service, self.connection, self.binding, self.row = service, connection, binding, row
        self.transfers = VideoTransfers(service)

    async def call(self, operation: str, **values: Any) -> dict[str, Any]:
        from cognis.providers.llm.usage_executor import executor_metadata, ingest_executor

        usage = (
            self.service.capture_binding(self.connection, self.binding, self.row)
            if operation == "submit"
            else None
        )
        metadata, context = executor_metadata(
            usage,
            None,
            executor_id=self.binding.get("executor_id"),
            parameters={},
            streaming=False,
        )
        if metadata:
            values["backend_metadata"] = metadata
        try:
            result = await self.service.llm.video_rpc(
                self.binding,
                {
                    "operation": operation,
                    "connection": self.connection.model_dump(),
                    **values,
                },
            )
        except BaseException:
            if usage is not None:
                usage.request.capture.failed_events += 1
            raise
        result = ingest_executor(result, usage, context)
        if result.get("error"):
            raise ValueError("Video operation failed; submission may be uncertain")
        return result

    async def capabilities(self) -> VideoCapabilities:
        from cognis.models.video import VideoCapabilities

        return VideoCapabilities.model_validate(await self.call("capabilities"))

    async def submit(self, request: VideoRequest, inputs: list[VideoInput]) -> VideoRemoteResult:
        from cognis.models.video import VideoRemoteResult

        result = await self.call(
            "submit",
            request=request.model_dump(mode="json"),
            inputs=[source.model_dump() for source in inputs],
        )
        return VideoRemoteResult.model_validate(result)

    async def poll(self, handle: str | None) -> VideoRemoteResult:
        from cognis.models.video import VideoRemoteResult

        return VideoRemoteResult.model_validate(
            await self.call(
                "poll",
                handle=handle,
            )
        )

    async def stage_output(self, output: VideoOutput, *, output_index: int | None = None) -> str:
        from cognis.providers.video import VideoProviderError, omni_interaction_handle

        inline = omni_interaction_handle(self.connection, output.locator) is not None
        if inline and (type(output_index) is not int or output_index < 0):
            raise VideoProviderError("invalid_inline_output_index")
        if self.row is None:
            raise ValueError("Video job is required to collect output")
        key = hashlib.sha256(output.locator.encode()).hexdigest()
        if inline:
            key = hashlib.sha256(f"{output.locator}\0{output_index}".encode()).hexdigest()
        artifact_id = self.transfers.artifact_id(self.row, key)
        try:
            await self.transfers.staged(self.row, artifact_id)
            return artifact_id
        except (PermissionError, ValueError):
            pass
        result = await self.call(
            "collect",
            output=output.model_dump(),
            output_ticket=await self.transfers.issue(self.row, key),
            **({"output_index": output_index} if inline else {}),
        )
        staged_artifact_id = result.get("artifact_id")
        if not isinstance(staged_artifact_id, str):
            raise ValueError("Executor returned no staged artifact")
        return staged_artifact_id
