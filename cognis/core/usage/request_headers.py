"""Bounded reconstructable request-header projection, owned by the projector."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models.usage import UsageAttemptContext, UsageAttribution
from cognis.models.usage_traffic import traffic_dimensions
from cognis.store.usage.models import UsageAttemptRow, UsageRequestLogRow, UsageRequestStatsRow
from cognis.store.usage.transactions import insert_for


async def project_request_headers(
    session: AsyncSession,
    rows: Sequence[UsageAttemptRow | UsageRequestStatsRow],
    now: datetime,
) -> None:
    """UPSERT only selected canonical headers under the existing projection lease."""
    table = UsageRequestLogRow.__table__
    for row in rows:
        context = (
            UsageAttemptContext.model_validate(row.context_payload)
            if isinstance(row, UsageAttemptRow)
            else None
        )
        if isinstance(row, UsageRequestStatsRow):
            attribution = UsageAttribution.model_validate(row.attribution)
            started_at = row.request_started_at
        else:
            assert context is not None
            attribution = context.attribution
            started_at = context.request_started_at or context.started_at
        identity = attribution.identity
        resources = attribution.resources
        traffic = traffic_dimensions(attribution)
        if isinstance(row, UsageAttemptRow):
            for field, value in traffic.items():
                setattr(row, field, value)
        values: dict[str, Any] = {
            **traffic,
            "owner_key": row.owner_key,
            "usage_subject_key": row.usage_subject_key,
            "request_id": row.request_id,
            "resource_owner": identity.resource_owner,
            "project_id": resources.project_id if resources else None,
            "agent_id": resources.agent_id if resources else None,
            "agent_profile_id": resources.agent_profile_id if resources else None,
            **{
                field: getattr(resources, field) if resources else None
                for field in (
                    "session_id",
                    "conversation_id",
                    "activity_scope_id",
                    "task_id",
                    "step_run_id",
                )
            },
            "bucket_day": started_at.date(),
            "started_at": started_at,
            "registered_at": now,
            "attribution": attribution.model_dump(mode="json", exclude_none=True),
            "logical_outcome": row.logical_outcome
            if isinstance(row, UsageRequestStatsRow)
            else None,
            "http_metadata": (row.server_payload or {}).get("http_metadata")
            if isinstance(row, UsageRequestStatsRow)
            else None,
        }
        statement = insert_for(session, table).values(values)
        if isinstance(row, UsageRequestStatsRow):
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["owner_key", "usage_subject_key", "request_id"],
                    set_={
                        field: getattr(statement.excluded, field)
                        for field in (
                            "attribution",
                            "http_metadata",
                            "logical_outcome",
                            *traffic,
                        )
                    },
                )
            )
        else:
            await session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=["owner_key", "usage_subject_key", "request_id"]
                )
            )
