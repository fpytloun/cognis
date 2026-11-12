"""Transactional video metadata operations. No external I/O inside transactions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.models.video import (
    TERMINAL_VIDEO_STATES,
    VideoJobView,
    VideoMutation,
    VideoRemoteResult,
)
from cognis.store.models import VideoAttemptRow, VideoBudgetRow, VideoJobRow, VideoMutationRow


def microusd(value: Decimal) -> int:
    """Round exposure upward to avoid undershooting a spending boundary."""
    return int((value * 1_000_000).to_integral_value(rounding="ROUND_CEILING"))


def dollars(value: int) -> str:
    return str(Decimal(value) / 1_000_000)


def job_view(row: VideoJobRow) -> VideoJobView:
    """Public projection deliberately excludes remote handles and request content."""
    actions = []
    if row.status == "needs_reconciliation":
        actions = ["reconcile", "abandon"]
    elif row.status == "blocked":
        actions = ["resume", "abandon"]
    elif row.status == "ingesting":
        actions = ["retry_collection", "abandon"]
    return VideoJobView(
        job_id=row.job_id,
        version=row.version,
        operation=row.operation,
        status=row.status,
        provider_id=row.provider_id,
        model=row.model,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        estimated_cost_usd=dollars(row.estimated_microusd) if row.estimated_microusd else None,
        actual_cost_usd=dollars(row.actual_microusd) if row.actual_microusd is not None else None,
        cost_status=row.cost_status,
        cancel_requested=row.cancel_requested,
        output_artifact_ids=[item["artifact_id"] for item in row.outputs],
        error_code=row.error_code,
        recovery_actions=actions,
    )


class VideoJobStore:
    """DB-owned job state and cost reservations; row versions fence user mutations."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def get(self, job_id: str) -> VideoJobRow | None:
        async with self.session_factory() as session:
            return await session.get(VideoJobRow, job_id)

    async def mutation_seen(self, job_id: str, key: str, action: str, version: int) -> bool:
        async with self.session_factory() as session:
            row = await session.get(VideoMutationRow, (job_id, key))
            if row and (row.action != action or row.expected_version != version):
                raise ValueError("Idempotency key already used for a different mutation")
            return row is not None

    async def mutate(
        self,
        row: VideoJobRow,
        mutation: VideoMutation,
        action: str,
        status: str,
        **values: Any,
    ) -> None:
        async with self.session_factory() as session:
            updated = await session.execute(
                update(VideoJobRow)
                .where(
                    VideoJobRow.job_id == row.job_id,
                    VideoJobRow.version == mutation.expected_version,
                )
                .values(
                    status=status, version=row.version + 1, updated_at=datetime.now(UTC), **values
                )
            )
            if cast(CursorResult[Any], updated).rowcount != 1:
                await session.rollback()
                if await self.mutation_seen(
                    row.job_id, mutation.idempotency_key, action, mutation.expected_version
                ):
                    return
                raise ValueError("Video job version changed")
            if action == "cancel":
                # The job CAS serializes against dispatch. State, receipt and unused
                # reservation release share one commit, including crash/replay paths.
                attempt = await session.get(VideoAttemptRow, row.attempt_id)
                current = await session.get(VideoJobRow, row.job_id)
                if current is None:
                    raise ValueError("Video job no longer exists")
                if (
                    attempt is not None
                    and not attempt.dispatched
                    and current.actual_microusd is None
                ):
                    await session.execute(
                        update(VideoJobRow)
                        .where(VideoJobRow.job_id == row.job_id)
                        .values(actual_microusd=0, cost_status="not_submitted")
                    )
                    await session.execute(
                        update(VideoBudgetRow)
                        .where(
                            VideoBudgetRow.owner_email == row.owner_email,
                            VideoBudgetRow.day == row.budget_day,
                        )
                        .values(
                            exposure_microusd=VideoBudgetRow.exposure_microusd
                            - current.estimated_microusd
                        )
                    )
            session.add(
                VideoMutationRow(
                    job_id=row.job_id,
                    key=mutation.idempotency_key,
                    action=action,
                    expected_version=mutation.expected_version,
                )
            )
            await session.commit()

    async def existing(self, owner: str, conversation: str, key: str) -> VideoJobRow | None:
        async with self.session_factory() as session:
            return (
                await session.scalars(
                    select(VideoJobRow).where(
                        VideoJobRow.owner_email == owner,
                        VideoJobRow.conversation_id == conversation,
                        VideoJobRow.idempotency_key == key,
                    )
                )
            ).first()

    async def create(
        self, values: dict[str, Any], *, daily_limit: int | None, active_limit: int
    ) -> VideoJobRow:
        """Reserve exposure and insert job atomically, serializing all owner admissions."""
        async with self.session_factory() as session:
            insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
            # A stable owner lock also serializes active counts across UTC day rollover.
            for day in ("admission", values["budget_day"]):
                await session.execute(
                    insert(VideoBudgetRow)
                    .values(owner_email=values["owner_email"], day=day, exposure_microusd=0)
                    .on_conflict_do_nothing()
                )
            await session.execute(
                update(VideoBudgetRow)
                .where(
                    VideoBudgetRow.owner_email == values["owner_email"],
                    VideoBudgetRow.day == "admission",
                )
                .values(exposure_microusd=VideoBudgetRow.exposure_microusd)
            )
            existing = await session.scalar(
                select(VideoJobRow).where(
                    VideoJobRow.owner_email == values["owner_email"],
                    VideoJobRow.conversation_id == values["conversation_id"],
                    VideoJobRow.idempotency_key == values["idempotency_key"],
                )
            )
            if existing:
                if existing.request_hash != values["request_hash"]:
                    raise ValueError("Idempotency key already used with different video input")
                return existing
            active = await session.scalar(
                select(func.count())
                .select_from(VideoJobRow)
                .where(
                    VideoJobRow.owner_email == values["owner_email"],
                    VideoJobRow.status.not_in(TERMINAL_VIDEO_STATES),
                )
            )
            if (active or 0) >= active_limit:
                raise ValueError("Video concurrency limit reached")
            if daily_limit is not None:
                unknown = await session.scalar(
                    select(VideoJobRow.job_id)
                    .where(
                        VideoJobRow.owner_email == values["owner_email"],
                        VideoJobRow.estimated_microusd == 0,
                        VideoJobRow.actual_microusd.is_(None),
                    )
                    .limit(1)
                )
                if unknown:
                    raise ValueError(
                        "Unresolved video charges must be reconciled before enforcing a budget"
                    )
            reserved = await session.execute(
                update(VideoBudgetRow)
                .where(
                    VideoBudgetRow.owner_email == values["owner_email"],
                    VideoBudgetRow.day == values["budget_day"],
                    (
                        VideoBudgetRow.exposure_microusd + values["estimated_microusd"]
                        <= daily_limit
                        if daily_limit is not None
                        else true()
                    ),
                )
                .values(
                    exposure_microusd=VideoBudgetRow.exposure_microusd
                    + values["estimated_microusd"]
                )
            )
            if cast(CursorResult[Any], reserved).rowcount != 1:
                raise ValueError("Video daily budget exhausted")
            row = VideoJobRow(**values)
            session.add(row)
            await session.flush()
            session.add(VideoAttemptRow(attempt_id=row.attempt_id, job_id=row.job_id))
            await session.commit()
            return row

    async def transition(self, row: VideoJobRow, status: str, **values: Any) -> bool:
        """CAS transition; a concurrent cancel/recovery wins over stale workers."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(VideoJobRow)
                .where(
                    VideoJobRow.job_id == row.job_id,
                    VideoJobRow.version == row.version,
                )
                .values(
                    status=status, version=row.version + 1, updated_at=datetime.now(UTC), **values
                )
            )
            await session.commit()
            return cast(CursorResult[Any], result).rowcount == 1

    async def dispatch(self, row: VideoJobRow) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                update(VideoJobRow)
                .where(
                    VideoJobRow.job_id == row.job_id,
                    VideoJobRow.version == row.version,
                    VideoJobRow.status == "preparing",
                    VideoJobRow.cancel_requested.is_(False),
                )
                .values(status="submitting", version=row.version + 1, cost_status="unknown")
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                return False
            dispatched = await session.execute(
                update(VideoAttemptRow)
                .where(
                    VideoAttemptRow.attempt_id == row.attempt_id,
                    VideoAttemptRow.dispatched.is_(False),
                )
                .values(dispatched=True)
            )
            if cast(CursorResult[Any], dispatched).rowcount != 1:
                await session.rollback()
                return False
            await session.commit()
            return True

    async def record_result(self, attempt_id: str, result: VideoRemoteResult) -> None:
        """Late workers may record evidence, but cannot publish or resubmit."""
        async with self.session_factory() as session:
            job_id = await session.scalar(
                select(VideoAttemptRow.job_id).where(VideoAttemptRow.attempt_id == attempt_id)
            )
            if job_id is None:
                return
            # Match admission's job -> attempt lock order and fence Usage's
            # settled marker against newly arriving provider evidence.
            job = await session.get(VideoJobRow, job_id, with_for_update=True)
            row = await session.scalar(
                select(VideoAttemptRow)
                .where(VideoAttemptRow.attempt_id == attempt_id)
                .with_for_update()
            )
            if row is None:
                return
            previous = row.result
            if previous and (
                previous["handle"] != result.handle
                or (previous["state"] != "running" and result.state == "running")
            ):
                return
            if previous:
                old = VideoRemoteResult.model_validate(previous)
                result = result.model_copy(
                    update={
                        name: getattr(result, name)
                        if getattr(result, name) is not None
                        else getattr(old, name)
                        for name in ("cost_usd", "output_duration_ms")
                    }
                )
            if (
                job is not None
                and "usage_origin" in job.binding
                and (
                    (previous or {}).get("cost_usd")
                    != (str(result.cost_usd) if result.cost_usd is not None else None)
                    or (previous or {}).get("output_duration_ms") != result.output_duration_ms
                )
            ):
                job.binding = {**job.binding, "usage_settled": False}
            await session.execute(
                update(VideoAttemptRow)
                .where(
                    VideoAttemptRow.attempt_id == attempt_id,
                    VideoAttemptRow.version == row.version,
                )
                .values(result=result.model_dump(mode="json"), version=row.version + 1)
            )
            await session.commit()

    async def result(self, attempt_id: str) -> VideoRemoteResult | None:
        async with self.session_factory() as session:
            row = await session.get(VideoAttemptRow, attempt_id)
            return VideoRemoteResult.model_validate(row.result) if row and row.result else None

    async def settle(self, job_id: str, amount: int, *, not_submitted: bool = False) -> None:
        """Charges settle independently of successful artifact publication."""
        async with self.session_factory() as session:
            row = await session.scalar(
                select(VideoJobRow).where(VideoJobRow.job_id == job_id).with_for_update()
            )
            if row is None:
                return
            old = row.actual_microusd
            result = await session.execute(
                update(VideoJobRow)
                .where(
                    VideoJobRow.job_id == job_id,
                    VideoJobRow.actual_microusd.is_(None)
                    if old is None
                    else VideoJobRow.actual_microusd == old,
                )
                .values(
                    actual_microusd=amount,
                    cost_status="not_submitted" if not_submitted else "reported",
                )
            )
            if cast(CursorResult[Any], result).rowcount:
                previous = row.estimated_microusd if old is None else old
                await session.execute(
                    update(VideoBudgetRow)
                    .where(
                        VideoBudgetRow.owner_email == row.owner_email,
                        VideoBudgetRow.day == row.budget_day,
                    )
                    .values(exposure_microusd=VideoBudgetRow.exposure_microusd + amount - previous)
                )
            await session.commit()

    async def due(self, limit: int = 20) -> list[VideoJobRow]:
        async with self.session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(VideoJobRow)
                        .where(
                            VideoJobRow.status.in_(
                                [
                                    "queued",
                                    "preparing",
                                    "submitting",
                                    "running",
                                    "ingesting",
                                    "needs_reconciliation",
                                ]
                            ),
                            VideoJobRow.next_poll_at <= datetime.now(UTC),
                        )
                        .order_by(VideoJobRow.next_poll_at)
                        .limit(limit)
                    )
                ).all()
            )

    async def list(
        self, owner: str, conversation: str, before: str | None = None
    ) -> list[VideoJobRow]:
        async with self.session_factory() as session:
            query = select(VideoJobRow).where(
                VideoJobRow.owner_email == owner, VideoJobRow.conversation_id == conversation
            )
            if before:
                query = query.where(VideoJobRow.job_id < before)
            return list(
                (await session.scalars(query.order_by(VideoJobRow.job_id.desc()).limit(50))).all()
            )

    async def postpone(self, row: VideoJobRow, seconds: int, error: str | None = None) -> None:
        await self.transition(
            row,
            row.status,
            next_poll_at=datetime.now(UTC) + timedelta(seconds=seconds),
            error_code=error,
        )
