"""One-shot backfill for object-stored rich deliverable chart payloads."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.deliverable import RICH_DELIVERABLE_MAX_BYTES
from cognis.rendering.rich_visuals import (
    rich_payload_has_noncanonical_chart,
    upgrade_legacy_chart_payload,
)
from cognis.store.deliverable_storage import DELIVERABLE_CHART_V1_RICH_KEY_PREFIX
from cognis.store.models import DeliverableRow

logger = get_logger(__name__)

_DEFAULT_BATCH_SIZE = 100
_DEFAULT_STORAGE_RETRY_ATTEMPTS = 3
_DEFAULT_STORAGE_RETRY_DELAY_SECONDS = 0.05
_RICH_MIME_TYPE = "application/json"
_ProcessStatus = Literal[
    "migrated",
    "promoted",
    "missing",
    "load_failed",
    "integrity_mismatch",
    "corrupt",
    "unsupported",
    "upload_failed",
    "cas_conflict",
    "failed",
]


class _ArtifactStore(Protocol):
    async def async_load(
        self, namespace: str, object_id: str, filename: str
    ) -> tuple[bytes, str]: ...

    async def async_save(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        owner_email: str | None = None,
    ) -> None: ...

    async def async_delete(self, namespace: str, object_id: str, filename: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DeliverableChartMigrationStats:
    scanned: int = 0
    batches: int = 0
    migrated: int = 0
    promoted: int = 0
    missing: int = 0
    load_failed: int = 0
    integrity_mismatch: int = 0
    corrupt: int = 0
    unsupported: int = 0
    upload_failed: int = 0
    cas_conflict: int = 0
    failed: int = 0
    old_delete_failed: int = 0

    @property
    def skipped(self) -> int:
        return (
            self.missing
            + self.load_failed
            + self.integrity_mismatch
            + self.corrupt
            + self.unsupported
            + self.upload_failed
            + self.cas_conflict
            + self.failed
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    deliverable_id: str
    namespace: str
    object_id: str
    rich_key: str
    rich_size: int | None
    rich_hash: str | None


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    status: _ProcessStatus
    old_delete_failed: bool = False


class DeliverableChartPayloadMigration:
    """Upgrade current-schema rich payload objects without scanning storage."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_store: _ArtifactStore,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        storage_retry_attempts: int = _DEFAULT_STORAGE_RETRY_ATTEMPTS,
        storage_retry_delay_seconds: float = _DEFAULT_STORAGE_RETRY_DELAY_SECONDS,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if storage_retry_attempts < 1:
            raise ValueError("storage_retry_attempts must be positive")
        if storage_retry_delay_seconds < 0:
            raise ValueError("storage_retry_delay_seconds must be non-negative")
        self._session_factory = session_factory
        self._artifact_store = artifact_store
        self._batch_size = batch_size
        self._storage_retry_attempts = storage_retry_attempts
        self._storage_retry_delay_seconds = storage_retry_delay_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start exactly one failure-isolated background pass."""

        if self._task is None:
            self._task = asyncio.create_task(
                self._run_isolated(),
                name="deliverable-chart-payload-migration",
            )

    async def stop(self) -> None:
        """Cancel and await the background pass."""

        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def run_once(self) -> DeliverableChartMigrationStats:
        """Process all eligible DB rows once in stable bounded keyset batches."""

        counts: dict[str, int] = {
            "scanned": 0,
            "batches": 0,
            "migrated": 0,
            "promoted": 0,
            "missing": 0,
            "load_failed": 0,
            "integrity_mismatch": 0,
            "corrupt": 0,
            "unsupported": 0,
            "upload_failed": 0,
            "cas_conflict": 0,
            "failed": 0,
            "old_delete_failed": 0,
        }
        cursor: str | None = None
        while True:
            candidates = await self._load_batch(cursor)
            if not candidates:
                break
            counts["batches"] += 1
            for candidate in candidates:
                cursor = candidate.deliverable_id
                counts["scanned"] += 1
                try:
                    result = await self._process_cancellation_safe(candidate)
                except Exception:
                    counts["failed"] += 1
                    logger.warning(
                        "deliverable chart payload row migration failed",
                        extra={"extra_data": {"deliverable_id": candidate.deliverable_id}},
                        exc_info=True,
                    )
                    continue
                counts[result.status] += 1
                if result.old_delete_failed:
                    counts["old_delete_failed"] += 1
        return DeliverableChartMigrationStats(**counts)

    async def _run_isolated(self) -> None:
        try:
            stats = await self.run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("deliverable chart payload migration failed")
            return
        logger.info(
            "deliverable chart payload migration completed",
            extra={
                "extra_data": {
                    "scanned": stats.scanned,
                    "batches": stats.batches,
                    "migrated": stats.migrated,
                    "promoted": stats.promoted,
                    "skipped": stats.skipped,
                    "old_delete_failed": stats.old_delete_failed,
                }
            },
        )

    async def _load_batch(self, cursor: str | None) -> list[_Candidate]:
        statement = (
            sa.select(
                DeliverableRow.deliverable_id,
                DeliverableRow.storage_namespace,
                DeliverableRow.storage_object_id,
                DeliverableRow.rich_key,
                DeliverableRow.rich_size,
                DeliverableRow.rich_hash,
            )
            .where(
                DeliverableRow.format == "rich",
                DeliverableRow.rich_key.is_not(None),
                DeliverableRow.rich_key.not_like(f"{DELIVERABLE_CHART_V1_RICH_KEY_PREFIX}.%"),
            )
            .order_by(DeliverableRow.deliverable_id)
            .limit(self._batch_size)
        )
        if cursor is not None:
            statement = statement.where(DeliverableRow.deliverable_id > cursor)
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            _Candidate(
                deliverable_id=row.deliverable_id,
                namespace=row.storage_namespace,
                object_id=row.storage_object_id,
                rich_key=row.rich_key,
                rich_size=row.rich_size,
                rich_hash=row.rich_hash,
            )
            for row in rows
        ]

    async def _process_cancellation_safe(self, candidate: _Candidate) -> _ProcessResult:
        task = asyncio.create_task(self._process(candidate))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await task
            raise

    async def _process(self, candidate: _Candidate) -> _ProcessResult:
        try:
            stored_bytes = await self._load_with_retries(candidate)
        except FileNotFoundError:
            return _ProcessResult("missing")
        except Exception:
            logger.warning(
                "deliverable chart payload load failed",
                extra={"extra_data": {"deliverable_id": candidate.deliverable_id}},
                exc_info=True,
            )
            return _ProcessResult("load_failed")

        if not _matches_row_integrity(stored_bytes, candidate):
            return _ProcessResult("integrity_mismatch")
        if len(stored_bytes) > RICH_DELIVERABLE_MAX_BYTES:
            return _ProcessResult("corrupt")
        try:
            raw_payload = json.loads(stored_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return _ProcessResult("corrupt")
        if not isinstance(raw_payload, dict):
            return _ProcessResult("corrupt")

        upgrade = upgrade_legacy_chart_payload(raw_payload)
        if upgrade.reason is not None:
            return _ProcessResult("unsupported")
        if rich_payload_has_noncanonical_chart(upgrade.payload):
            return _ProcessResult("unsupported")

        migrated_bytes = _json_bytes(upgrade.payload)
        if len(migrated_bytes) > RICH_DELIVERABLE_MAX_BYTES:
            return _ProcessResult("unsupported")
        migrated_hash = hashlib.sha256(migrated_bytes).hexdigest()
        staged_key = f"{DELIVERABLE_CHART_V1_RICH_KEY_PREFIX}.{uuid.uuid4().hex}.json"
        try:
            await self._save_with_retries(candidate, staged_key, migrated_bytes)
            staged_bytes = await self._load_exact_with_retries(
                candidate.namespace,
                candidate.object_id,
                staged_key,
            )
            if len(staged_bytes) != len(migrated_bytes) or not hmac.compare_digest(
                hashlib.sha256(staged_bytes).hexdigest(),
                migrated_hash,
            ):
                raise ValueError("staged payload integrity mismatch")
        except Exception:
            await self._delete_staged_best_effort(candidate, staged_key)
            return _ProcessResult("upload_failed")

        committed = False
        delete_staged_on_failure = True
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sa.update(DeliverableRow)
                    .where(
                        DeliverableRow.deliverable_id == candidate.deliverable_id,
                        DeliverableRow.rich_key == candidate.rich_key,
                        DeliverableRow.rich_hash == candidate.rich_hash,
                        DeliverableRow.status != "superseded",
                    )
                    .values(
                        rich_key=staged_key,
                        rich_size=len(migrated_bytes),
                        rich_hash=migrated_hash,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    await session.rollback()
                    return _ProcessResult("cas_conflict")
                await self._commit(session)
                committed = True
        except Exception:
            pointer_state = await self._staged_pointer_state(
                candidate.deliverable_id,
                staged_key,
                len(migrated_bytes),
                migrated_hash,
            )
            if pointer_state is True:
                committed = True
            else:
                delete_staged_on_failure = pointer_state is False
                raise
        finally:
            if not committed and delete_staged_on_failure:
                await self._delete_staged_best_effort(candidate, staged_key)

        old_delete_failed = False
        try:
            await self._artifact_store.async_delete(
                candidate.namespace,
                candidate.object_id,
                candidate.rich_key,
            )
        except Exception:
            old_delete_failed = True
            logger.warning(
                "deliverable chart payload old key cleanup failed",
                extra={"extra_data": {"deliverable_id": candidate.deliverable_id}},
                exc_info=True,
            )
        status: _ProcessStatus = "migrated" if upgrade.upgraded_blocks else "promoted"
        return _ProcessResult(status, old_delete_failed=old_delete_failed)

    async def _commit(self, session: AsyncSession) -> None:
        await session.commit()

    async def _staged_pointer_state(
        self,
        deliverable_id: str,
        staged_key: str,
        staged_size: int,
        staged_hash: str,
    ) -> bool | None:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        sa.select(
                            DeliverableRow.rich_key,
                            DeliverableRow.rich_size,
                            DeliverableRow.rich_hash,
                        ).where(DeliverableRow.deliverable_id == deliverable_id)
                    )
                ).one_or_none()
        except Exception:
            return None
        if row is None:
            return False
        return (
            row.rich_key == staged_key
            and row.rich_size == staged_size
            and hmac.compare_digest(str(row.rich_hash or ""), staged_hash)
        )

    async def _load_with_retries(self, candidate: _Candidate) -> bytes:
        return await self._load_exact_with_retries(
            candidate.namespace,
            candidate.object_id,
            candidate.rich_key,
        )

    async def _load_exact_with_retries(
        self,
        namespace: str,
        object_id: str,
        key: str,
    ) -> bytes:
        for attempt in range(1, self._storage_retry_attempts + 1):
            try:
                content, _content_type = await self._artifact_store.async_load(
                    namespace,
                    object_id,
                    key,
                )
                return content
            except FileNotFoundError:
                raise
            except Exception:
                if attempt == self._storage_retry_attempts:
                    raise
                await self._storage_retry_delay(attempt)
        raise AssertionError("unreachable")

    async def _save_with_retries(
        self,
        candidate: _Candidate,
        staged_key: str,
        content: bytes,
    ) -> None:
        for attempt in range(1, self._storage_retry_attempts + 1):
            try:
                await self._artifact_store.async_save(
                    candidate.namespace,
                    candidate.object_id,
                    staged_key,
                    content,
                    _RICH_MIME_TYPE,
                )
                return
            except Exception:
                if attempt == self._storage_retry_attempts:
                    raise
                await self._storage_retry_delay(attempt)

    async def _storage_retry_delay(self, attempt: int) -> None:
        delay = self._storage_retry_delay_seconds * (2 ** (attempt - 1))
        if delay:
            await asyncio.sleep(delay)

    async def _delete_staged_best_effort(self, candidate: _Candidate, staged_key: str) -> None:
        with contextlib.suppress(Exception):
            await self._artifact_store.async_delete(
                candidate.namespace,
                candidate.object_id,
                staged_key,
            )


def _matches_row_integrity(content: bytes, candidate: _Candidate) -> bool:
    if (
        not isinstance(candidate.rich_size, int)
        or candidate.rich_size < 0
        or not isinstance(candidate.rich_hash, str)
        or not candidate.rich_hash
    ):
        return False
    if len(content) != candidate.rich_size:
        return False
    actual_hash = hashlib.sha256(content).hexdigest()
    return hmac.compare_digest(actual_hash, candidate.rich_hash)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
