"""Atomic source-detail compaction; no inference, scanner or independent worker."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from cognis.core.usage.contributions import MEASURES, decode_measures, digest_json
from cognis.core.usage.dimensions import UsageDimensions, encode_usage_dimensions
from cognis.core.usage.pricing import UsagePriceResult, billable_units
from cognis.core.usage.projector import UsageProjector, as_utc
from cognis.store.usage.codec import terminal_event
from cognis.store.usage.models import (
    UsageArchivedTotalRow,
    UsageArchiveManifestRow,
    UsageAttemptRow,
    UsageCorrectionRow,
    UsageDailyTotalRow,
    UsageHistoricalPriceBindingRow,
    UsageHourlyTotalRow,
    UsageMaintenanceJobRow,
    UsageProjectionStateRow,
    UsageRequestLogRow,
    UsageRequestStatsRow,
    UsageSessionTotalRow,
    UsageValuationRow,
)
from cognis.store.usage.transactions import insert_for


async def refresh_detail_anchor(session: AsyncSession, state: UsageProjectionStateRow) -> None:
    """Two scope-indexed first-row reads; pending/unprojected facts never become eligible."""
    attempt = await session.scalar(
        select(UsageAttemptRow.finalized_at)
        .where(
            UsageAttemptRow.owner_key == state.owner_key,
            UsageAttemptRow.bucket_day == state.bucket_day,
            UsageAttemptRow.fact_version > 0,
            UsageAttemptRow.projected_version == UsageAttemptRow.fact_version,
            UsageAttemptRow.finalized_at.is_not(None),
        )
        .order_by(UsageAttemptRow.finalized_at, UsageAttemptRow.row_id)
        .limit(1)
    )
    request = await session.scalar(
        select(UsageRequestStatsRow.updated_at)
        .where(
            UsageRequestStatsRow.owner_key == state.owner_key,
            UsageRequestStatsRow.bucket_day == state.bucket_day,
            UsageRequestStatsRow.logical_outcome.is_not(None),
            UsageRequestStatsRow.projected_version == UsageRequestStatsRow.fact_version,
        )
        .order_by(UsageRequestStatsRow.updated_at, UsageRequestStatsRow.request_id)
        .limit(1)
    )
    values = [as_utc(value) for value in (attempt, request) if value is not None]
    state.oldest_detail_at = min(values) if values else None


class UsageDetailRetention:
    """Own archive job state and all compaction effects inside the caller's transaction lease."""

    def __init__(self, projector: UsageProjector) -> None:
        self.projector = projector

    @staticmethod
    def pause(
        job: UsageMaintenanceJobRow,
        state: UsageProjectionStateRow,
        now: datetime,
        reason: str,
    ) -> None:
        """A safety exception blocks retention, never its own rebuild or normal projection."""
        state.exclusive_job_id = None
        state.exclusive_job_kind = None
        state.retention_paused = True
        job.status = "paused"
        job.error_code = reason
        job.completed_at = now

    async def process_page(
        self,
        session: AsyncSession,
        job: UsageMaintenanceJobRow,
        state: UsageProjectionStateRow,
        *,
        now: datetime,
        detail_days: int,
        page_rows: int,
        page_bytes: int,
    ) -> None:
        """Archive selected terminal, projected facts; retain every unsupported source."""
        owner, day = state.owner_key, state.bucket_day
        cutoff = now - timedelta(days=detail_days)
        cursor = job.cursor or {"kind": "attempt"}
        kind = cursor["kind"]
        # Candidates follow the expiry index; physical locks follow capture's ID order.
        if kind == "attempt":
            query = (
                select(UsageAttemptRow)
                .options(
                    load_only(
                        UsageAttemptRow.attempt_id,
                        UsageAttemptRow.row_id,
                        UsageAttemptRow.finalized_at,
                    )
                )
                .where(
                    UsageAttemptRow.owner_key == owner,
                    UsageAttemptRow.bucket_day == day,
                    UsageAttemptRow.fact_version > 0,
                    UsageAttemptRow.projected_version == UsageAttemptRow.fact_version,
                    UsageAttemptRow.finalized_at <= cutoff,
                )
            )
            if cursor.get("at"):
                query = query.where(
                    tuple_(UsageAttemptRow.finalized_at, UsageAttemptRow.row_id)
                    > (datetime.fromisoformat(cursor["at"]), cursor["key"])
                )
            candidates = list(
                (
                    await session.scalars(
                        query.order_by(UsageAttemptRow.finalized_at, UsageAttemptRow.row_id).limit(
                            page_rows
                        )
                    )
                ).all()
            )
            if not candidates:
                job.cursor = {"kind": "request"}
                return
            last_at = candidates[-1].finalized_at
            assert last_at is not None
            next_cursor = {
                "kind": kind,
                "at": as_utc(last_at).isoformat(),
                "key": candidates[-1].row_id,
            }
            attempts = list(
                (
                    await session.scalars(
                        select(UsageAttemptRow)
                        .where(
                            UsageAttemptRow.attempt_id.in_([row.attempt_id for row in candidates]),
                        )
                        .order_by(UsageAttemptRow.attempt_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).all()
            )
            rows: list[UsageAttemptRow | UsageRequestStatsRow] = [
                row
                for row in attempts
                if row.finalized_at is not None
                and as_utc(row.finalized_at) <= cutoff
                and row.fact_version > 0
                and row.projected_version == row.fact_version
            ]
        else:
            request_query = (
                select(UsageRequestStatsRow)
                .options(
                    load_only(
                        UsageRequestStatsRow.request_id,
                        UsageRequestStatsRow.updated_at,
                    )
                )
                .where(
                    UsageRequestStatsRow.owner_key == owner,
                    UsageRequestStatsRow.bucket_day == day,
                    UsageRequestStatsRow.logical_outcome.is_not(None),
                    UsageRequestStatsRow.projected_version == UsageRequestStatsRow.fact_version,
                    UsageRequestStatsRow.updated_at <= cutoff,
                )
            )
            if cursor.get("at"):
                request_query = request_query.where(
                    tuple_(UsageRequestStatsRow.updated_at, UsageRequestStatsRow.request_id)
                    > (datetime.fromisoformat(cursor["at"]), cursor["key"])
                )
            request_candidates = list(
                (
                    await session.scalars(
                        request_query.order_by(
                            UsageRequestStatsRow.updated_at, UsageRequestStatsRow.request_id
                        ).limit(page_rows)
                    )
                ).all()
            )
            if not request_candidates:
                state.exclusive_job_id = None
                state.exclusive_job_kind = None
                state.retention_paused = False
                await refresh_detail_anchor(session, state)
                job.status, job.completed_at = "complete", now
                return
            next_cursor = {
                "kind": kind,
                "at": as_utc(request_candidates[-1].updated_at).isoformat(),
                "key": request_candidates[-1].request_id,
            }
            requests = list(
                (
                    await session.scalars(
                        select(UsageRequestStatsRow)
                        .where(
                            UsageRequestStatsRow.request_id.in_(
                                [row.request_id for row in request_candidates]
                            ),
                        )
                        .order_by(UsageRequestStatsRow.request_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).all()
            )
            rows = [
                row
                for row in requests
                if row.logical_outcome is not None
                and as_utc(row.updated_at) <= cutoff
                and row.projected_version == row.fact_version
            ]
        if not rows:
            job.cursor = next_cursor
            return
        source_ids = [
            row.attempt_id if isinstance(row, UsageAttemptRow) else row.request_id for row in rows
        ]
        keys = [(owner, day, state.active_generation, kind, source_id) for source_id in source_ids]
        receipts = await self.projector.receipts(session, keys)
        if any(
            key not in receipts or receipts[key].fact_version != row.fact_version
            for row, key in zip(rows, keys, strict=True)
        ):
            self.pause(job, state, now, "archive_requires_rebuild")
            return
        # A disposable receipt is not canonical authority. Recompute and compare
        # before allowing its raw source to disappear.
        states = {(owner, day): state}
        if kind == "attempt":
            await self.projector.project_attempts(
                session,
                [row for row in rows if isinstance(row, UsageAttemptRow)],
                states,
                now,
                verify_receipts=True,
            )
        else:
            await self.projector.project_requests(
                session,
                [row for row in rows if isinstance(row, UsageRequestStatsRow)],
                states,
                now,
                verify_receipts=True,
            )
        source_bytes = sum(
            len(
                json.dumps(
                    row.context_payload | {"terminal": row.terminal_payload},
                    ensure_ascii=False,
                    default=str,
                ).encode()
            )
            if isinstance(row, UsageAttemptRow)
            else len(
                json.dumps(
                    {
                        "attribution": row.attribution,
                        "server": row.server_payload,
                        "client": row.client_payload,
                    },
                    ensure_ascii=False,
                    default=str,
                ).encode()
            )
            for row in rows
        )
        receipt_bytes = sum(
            len(json.dumps(receipts[key].contribution, ensure_ascii=False).encode()) for key in keys
        )
        if source_bytes + receipt_bytes * 4 + len(rows) * 1024 > page_bytes:
            self.pause(job, state, now, "archive_page_budget")
            return
        manifest_id = f"uam_{uuid.uuid4().hex}"
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        prices: dict[tuple[str, str], UsageValuationRow] = {}
        evidence = []
        for row, key, source_id in zip(rows, keys, source_ids, strict=True):
            receipt = receipts[key]
            payload = receipt.contribution
            encoding = encode_usage_dimensions(
                UsageDimensions.model_validate(payload["dimensions"]),
                grain="hourly",
            )
            dimensions = encoding.dimensions.model_dump(mode="json")
            dimensions.update(traffic_client_id=None, traffic_key_id=None, schema_version=2)
            encoding = encode_usage_dimensions(
                UsageDimensions.model_validate(dimensions), grain="hourly"
            )
            units_complete = (
                receipt.current_status == "complete"
                or billable_units(terminal_event(row).usage, terminal_event(row).context.selection)[
                    1
                ]
                if isinstance(row, UsageAttemptRow)
                else True
            )
            billing = {
                "record_kind": kind,
                "historical_revision_id": receipt.historical_revision_id,
                "current_revision_id": receipt.current_revision_id,
                "service_tier": row.service_tier if isinstance(row, UsageAttemptRow) else None,
                "cache_ttl": row.cache_ttl if isinstance(row, UsageAttemptRow) else None,
                "semantic_profile": row.semantic_profile
                if isinstance(row, UsageAttemptRow)
                else None,
                "semantic_version": row.semantic_version
                if isinstance(row, UsageAttemptRow)
                else None,
                "unit_completeness": units_complete,
                "unit_dimensions": sorted(payload["units"]),
                "quality": row.usage_completeness
                if isinstance(row, UsageAttemptRow)
                else "not_applicable",
                "outcome": row.outcome if isinstance(row, UsageAttemptRow) else row.logical_outcome,
            }
            if isinstance(row, UsageAttemptRow):
                selection = terminal_event(row).context.selection
                billing["operation"] = selection.operation
                billing["media_parameters"] = {
                    "image_size": selection.effective_parameters.image_size,
                    "image_quality": selection.effective_parameters.image_quality,
                }
                tier_evidence = selection.service_tier_applicability
                if tier_evidence is not None:
                    billing["service_tier_applicability"] = tier_evidence.model_dump(mode="json")
                    billing["operation"] = selection.operation
                    billing["effective_service_tier"] = selection.effective_parameters.service_tier
                    billing["billing_rule_version"] = (
                        2 if tier_evidence.state == "not_applicable" else 1
                    )
                if selection.operation in {
                    "image_generate",
                    "image_edit",
                    "transcribe",
                    "synthesize",
                    "video_generate",
                    "video_extend",
                }:
                    billing["billing_rule_version"] = 3
            group_key = encoding.dimension_key, digest_json(billing)
            prices[group_key] = receipt
            group = groups.get(group_key)
            if group is None:
                group = {
                    **dimensions,
                    **decode_measures(payload),
                    "archive_id": f"uar_{uuid.uuid4().hex}",
                    "manifest_id": manifest_id,
                    "owner_key": owner,
                    "bucket_day": day,
                    "bucket_hour": 0,
                    "dimension_key": encoding.dimension_key,
                    "dimensions": dimensions,
                    "model": dimensions["effective_model"],
                    "effort": dimensions["effective_effort"],
                    "record_kind": kind,
                    "historical_revision_id": receipt.historical_revision_id,
                    "current_revision_id": receipt.current_revision_id,
                    "semantic_profile": billing["semantic_profile"],
                    "semantic_version": billing["semantic_version"],
                    "billing_stratum_key": group_key[1],
                    "billing_units": {"units": dict(payload["units"]), **billing},
                    "exact_reprice_supported": True,
                    "source_detail_count": 1,
                    "evidence_digest": "",
                    "created_at": now,
                }
                groups[group_key] = group
            else:
                for name, value in decode_measures(payload).items():
                    group[name] += value
                for name, value in payload["units"].items():
                    group["billing_units"]["units"][name] += value
                group["source_detail_count"] += 1
            evidence.append(
                (
                    source_id,
                    row.fact_version,
                    row.terminal_digest
                    if isinstance(row, UsageAttemptRow)
                    else (row.server_digest, row.client_digest),
                )
            )
        evidence_digest = digest_json(evidence)
        table = UsageArchivedTotalRow.__table__
        await session.execute(
            insert_for(session, table).values(
                [
                    {
                        **{key: value for key, value in group.items() if key in table.c},
                        "evidence_digest": evidence_digest,
                    }
                    for group in groups.values()
                ]
            )
        )
        seeds = []
        for group_key, group in groups.items():
            price = prices[group_key]
            payload = {
                "owner_key": owner,
                "day": day.isoformat(),
                "hour": 0,
                "grain": "daily",
                "dimensions": group["dimensions"],
                "units": group["billing_units"]["units"],
                "measures": {
                    name: str(group[name]) if isinstance(group[name], Decimal) else group[name]
                    for name in MEASURES
                },
            }
            seeds.append(
                self.projector.receipt_values(
                    (owner, day, state.active_generation, "archive", group["archive_id"]),
                    1,
                    price.pricing_generation,
                    price.historical_revision_id,
                    price.current_revision_id,
                    UsagePriceResult(
                        group["historical_cost"],
                        price.historical_status,
                        group["currency"],
                        group["basis"],
                        (),
                    ),
                    UsagePriceResult(
                        group["current_cost"],
                        price.current_status,
                        group["currency"],
                        group["basis"],
                        (),
                    ),
                    payload,
                    now,
                )
            )
        old_parts = [(receipts[key].contribution, -1) for key in keys]
        await self.projector.apply_changes(
            session,
            {state.active_generation: old_parts},
            seeds,
            now,
            grains=("hourly",),
        )
        # Daily totals survive detail expiry, but private client/key attribution does not.
        await self.projector.apply_changes(
            session,
            {state.active_generation: [*old_parts, *((seed["contribution"], 1) for seed in seeds)]},
            [],
            now,
            grains=("daily",),
        )
        proxy_parts = [
            part
            for part in old_parts
            if (part[0].get("session") or {}).get("session_kind") == "proxy"
        ]
        if proxy_parts:
            await self.projector.apply_changes(
                session,
                {state.active_generation: proxy_parts},
                [],
                now,
                grains=("session",),
            )
        # Remove tombstone dimensions only after all grains have been compensated.
        for summary in (UsageDailyTotalRow, UsageHourlyTotalRow, UsageSessionTotalRow):
            await session.execute(
                delete(summary)
                .where(
                    summary.owner_key == owner,
                    summary.bucket_day == day,
                    or_(
                        summary.generation != state.active_generation,
                        and_(*(getattr(summary, name) == 0 for name in MEASURES)),
                    ),
                )
                .execution_options(synchronize_session=False)
            )
        state.archive_revision += 1
        session.add(
            UsageArchiveManifestRow(
                manifest_id=manifest_id,
                job_id=job.job_id,
                owner_key=owner,
                bucket_day=day,
                archive_revision=state.archive_revision,
                detail_cutoff=cutoff,
                source_cut_row_id=max(
                    row.row_id for row in rows if isinstance(row, UsageAttemptRow)
                )
                if kind == "attempt"
                else 0,
                source_cut_outbox_id=0,
                source_cursor=next_cursor["key"] if kind == "attempt" else 0,
                detail_count=len(rows),
                evidence_digest=evidence_digest,
                protocol_version=1,
                completed_at=now,
            )
        )
        await session.execute(
            delete(UsageValuationRow).where(
                UsageValuationRow.source_kind == kind,
                UsageValuationRow.source_id.in_(source_ids),
            )
        )
        if kind == "attempt":
            await session.execute(
                delete(UsageCorrectionRow).where(UsageCorrectionRow.attempt_id.in_(source_ids))
            )
            await session.execute(
                delete(UsageHistoricalPriceBindingRow).where(
                    UsageHistoricalPriceBindingRow.attempt_id.in_(source_ids)
                )
            )
            await session.execute(
                delete(UsageAttemptRow).where(UsageAttemptRow.attempt_id.in_(source_ids))
            )
        else:
            await session.execute(
                delete(UsageRequestStatsRow).where(UsageRequestStatsRow.request_id.in_(source_ids))
            )
        header = UsageRequestLogRow
        await session.execute(
            delete(header)
            .where(
                header.owner_key == owner,
                header.request_id.in_([row.request_id for row in rows]),
                ~select(1)
                .where(
                    UsageAttemptRow.owner_key == header.owner_key,
                    UsageAttemptRow.request_id == header.request_id,
                )
                .exists(),
                ~select(1)
                .where(
                    UsageRequestStatsRow.owner_key == header.owner_key,
                    UsageRequestStatsRow.request_id == header.request_id,
                )
                .exists(),
            )
            .execution_options(synchronize_session=False)
        )
        state.detail_expired_before = cutoff
        job.cursor = next_cursor
        job.processed_count += len(rows)
