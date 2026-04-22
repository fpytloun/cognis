"""Background retry queue for tool classification refinement."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from cognis.logging import get_logger
from cognis.models.tool import (
    AUTO_PROFILE_GROUPS,
    ToolDefinition,
    stable_tool_id,
)
from cognis.store.models import ToolClassificationRow
from cognis.store.queries import (
    get_tool_classification_override_rows,
    tool_classification_scope,
    upsert_tool_classification,
)
from cognis.tools.classification import (
    _validate_profile_group,
    llm_classification_outcomes,
    requires_background_classification,
    tool_fingerprint,
)

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _ClaimedClassification:
    classification_id: str
    scope_key: str
    tool_id: str
    attempts: int
    tool_payload: dict[str, Any]


class ToolClassificationQueue:
    """Durable background classifier with infinite exponential backoff."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        llm_provider: Any,
        max_concurrent: int = 4,
        poll_interval_seconds: float = 0.5,
        lease_seconds: int = 300,
        backoff_max_seconds: float = 3600.0,
        max_batch_size: int = 10,
    ) -> None:
        self._session_factory = session_factory
        self._llm_provider = llm_provider
        self._max_concurrent = max_concurrent
        self._poll_interval = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._backoff_max = backoff_max_seconds
        self._max_batch_size = max_batch_size
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()

    async def enqueue_tools(self, tools: list[ToolDefinition], *, owner_email: str | None) -> None:
        dynamic_tools = [tool for tool in tools if requires_background_classification(tool)]
        if not dynamic_tools:
            return
        scope_key = tool_classification_scope(owner_email)
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(ToolClassificationRow).where(
                    ToolClassificationRow.scope_key == scope_key,
                    ToolClassificationRow.tool_id.in_([stable_tool_id(tool) for tool in dynamic_tools]),
                )
            )
            existing = {
                row.tool_id: row
                for row in result.scalars().all()
            }
            override_rows = await get_tool_classification_override_rows(
                session,
                scope_key=scope_key,
                tool_ids=[stable_tool_id(tool) for tool in dynamic_tools],
            )
            overridden_ids = {row.tool_id for row in override_rows}
            for tool in dynamic_tools:
                tool_id = stable_tool_id(tool)
                if tool_id in overridden_ids:
                    continue
                row = existing.get(tool_id)
                if (
                    row is not None
                    and row.status == "ready"
                    and row.category in AUTO_PROFILE_GROUPS
                    and row.capabilities
                    and _validate_profile_group(
                        tool,
                        str(row.category),
                        [str(capability) for capability in row.capabilities],
                    )
                    is None
                ):
                    continue
                if row is not None and row.status in {"pending", "running"}:
                    continue
                attempts = row.attempts if row is not None else 0
                next_retry_at = row.next_retry_at if row is not None else now
                await upsert_tool_classification(
                    session,
                    scope_key=scope_key,
                    owner_email=owner_email,
                    tool_id=tool_id,
                    source_type=tool.source.type,
                    fingerprint=tool_fingerprint(tool),
                    tool_payload=tool.model_dump(mode="json"),
                    status="pending",
                    attempts=attempts,
                    next_retry_at=next_retry_at,
                    last_error=(row.last_error if row is not None else None),
                )
            await session.commit()
        self._wake_event.set()

    async def _drain_loop(self) -> None:
        semaphore = asyncio.Semaphore(self._max_concurrent)
        while True:
            if self._stop_event.is_set() and not await self._has_pending_work():
                break
            claimed = await self._claim_due_items(self._max_batch_size * self._max_concurrent)
            if not claimed:
                self._wake_event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self._poll_interval)
                continue
            batches = self._group_claimed_items(claimed)
            await asyncio.gather(*(self._process_batch(batch, semaphore) for batch in batches))

    async def _has_pending_work(self) -> bool:
        async with self._session_factory() as session:
            now = _utcnow()
            stale_before = now - timedelta(seconds=self._lease_seconds)
            count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(ToolClassificationRow)
                .where(
                    sa.or_(
                        ToolClassificationRow.status == "pending",
                        sa.and_(
                            ToolClassificationRow.status == "running",
                            ToolClassificationRow.last_attempt_at.is_not(None),
                            ToolClassificationRow.last_attempt_at <= stale_before,
                        ),
                    )
                )
            )
            return bool(count)

    async def _claim_due_items(self, limit: int) -> list[_ClaimedClassification]:
        now = _utcnow()
        stale_before = now - timedelta(seconds=self._lease_seconds)
        claimed: list[_ClaimedClassification] = []
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(ToolClassificationRow)
                        .where(
                            sa.or_(
                                sa.and_(
                                    ToolClassificationRow.status == "pending",
                                    sa.or_(
                                        ToolClassificationRow.next_retry_at.is_(None),
                                        ToolClassificationRow.next_retry_at <= now,
                                    ),
                                ),
                                sa.and_(
                                    ToolClassificationRow.status == "running",
                                    ToolClassificationRow.last_attempt_at.is_not(None),
                                    ToolClassificationRow.last_attempt_at <= stale_before,
                                ),
                            )
                        )
                        .order_by(
                            ToolClassificationRow.next_retry_at.asc().nullsfirst(),
                            ToolClassificationRow.updated_at.asc(),
                        )
                        .limit(limit * 2)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                updated = await session.execute(
                    sa.update(ToolClassificationRow)
                    .execution_options(synchronize_session=False)
                    .where(
                        ToolClassificationRow.classification_id == row.classification_id,
                        sa.or_(
                            sa.and_(
                                ToolClassificationRow.status == "pending",
                                sa.or_(
                                    ToolClassificationRow.next_retry_at.is_(None),
                                    ToolClassificationRow.next_retry_at <= now,
                                ),
                            ),
                            sa.and_(
                                ToolClassificationRow.status == "running",
                                ToolClassificationRow.last_attempt_at.is_not(None),
                                ToolClassificationRow.last_attempt_at <= stale_before,
                            ),
                        ),
                    )
                    .values(status="running", last_attempt_at=now, updated_at=now, last_error=None)
                )
                if not updated.rowcount:
                    continue
                claimed.append(
                    _ClaimedClassification(
                        classification_id=row.classification_id,
                        scope_key=row.scope_key,
                        tool_id=row.tool_id,
                        attempts=row.attempts,
                        tool_payload=dict(row.tool_payload or {}),
                    )
                )
                if len(claimed) >= limit:
                    break
            await session.commit()
        return claimed

    def _group_claimed_items(
        self, claimed: list[_ClaimedClassification]
    ) -> list[list[_ClaimedClassification]]:
        grouped: dict[tuple[str, str, str], list[_ClaimedClassification]] = {}
        for item in claimed:
            payload = item.tool_payload
            source = str(payload.get("source", {}).get("type") or payload.get("source_type") or "unknown")
            server = str(
                payload.get("source", {}).get("server_name")
                or payload.get("source", {}).get("server_id")
                or ""
            )
            grouped.setdefault((item.scope_key, source, server), []).append(item)
        batches: list[list[_ClaimedClassification]] = []
        for items in grouped.values():
            for index in range(0, len(items), self._max_batch_size):
                batches.append(items[index : index + self._max_batch_size])
        return batches

    async def _process_batch(
        self, items: list[_ClaimedClassification], semaphore: asyncio.Semaphore
    ) -> None:
        async with semaphore:
            try:
                tools = [ToolDefinition.model_validate(item.tool_payload) for item in items]
                {stable_tool_id(tool): tool for tool in tools}
                updates, rejected = await llm_classification_outcomes(tools, llm=self._llm_provider)
                async with self._session_factory() as session:
                    for item in items:
                        row = await session.get(ToolClassificationRow, item.classification_id)
                        if row is None:
                            continue
                        update = updates.get(item.tool_id)
                        if update is not None:
                            row.status = "ready"
                            row.category = str(update.get("profile_group") or "development")
                            row.capabilities = [
                                str(capability) for capability in update.get("capabilities", [])
                            ]
                            row.classification_source = "llm"
                            row.classification_confidence = float(update.get("confidence") or 0.75)
                            row.last_error = None
                            row.next_retry_at = None
                            row.updated_at = _utcnow()
                            continue
                        attempts = item.attempts + 1
                        backoff_seconds = min(2**attempts, self._backoff_max)
                        row.status = "pending"
                        row.attempts = attempts
                        row.last_error = rejected.get(item.tool_id, "no_classification_result")
                        row.next_retry_at = _utcnow() + timedelta(seconds=backoff_seconds)
                        row.updated_at = _utcnow()
                    await session.commit()
                if rejected:
                    logger.warning(
                        "Tool classification batch completed with retries scheduled",
                        extra={
                            "extra_data": {
                                "batch_size": len(items),
                                "tool_ids": [item.tool_id for item in items],
                                "rejected": rejected,
                            }
                        },
                    )
            except Exception as exc:
                error_text = str(exc)[:1000]
                logger.warning(
                    "Tool classification batch failed",
                    extra={
                        "extra_data": {
                            "batch_size": len(items),
                            "tool_ids": [item.tool_id for item in items],
                            "error": error_text,
                        }
                    },
                    exc_info=True,
                )
                async with self._session_factory() as session:
                    for item in items:
                        row = await session.get(ToolClassificationRow, item.classification_id)
                        if row is None:
                            continue
                        attempts = item.attempts + 1
                        backoff_seconds = min(2**attempts, self._backoff_max)
                        next_retry_at = _utcnow() + timedelta(seconds=backoff_seconds)
                        row.status = "pending"
                        row.attempts = attempts
                        row.last_error = error_text
                        row.next_retry_at = next_retry_at
                        row.updated_at = _utcnow()
                    await session.commit()
