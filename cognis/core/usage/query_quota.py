"""Read-only owner/admin provider observation history; no monitor refresh."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select

from cognis.core.usage.access import require_quota_history
from cognis.core.usage.query_metadata import utc
from cognis.core.usage.query_models import ProviderQuotaHistory, ProviderQuotaSample
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.models import LLMProvider
from cognis.store.usage.models import ProviderUsageSampleRow


async def provider_history(
    session: Any,
    scope: Any,
    provider_id: str,
    query: Any,
    *,
    cursors: Any,
) -> ProviderQuotaHistory:
    """Select stored observations only, with owner-only account history."""
    owner = await session.scalar(
        select(LLMProvider.owner_email).where(LLMProvider.provider_id == provider_id)
    )
    if owner is None:
        raise UsageQueryError("usage_provider_unavailable", "Provider history is unavailable", 404)
    require_quota_history(scope, provider_owner_email=owner)
    sample = ProviderUsageSampleRow
    fingerprint = cursors.fingerprint(query, scope, f"quota:{provider_id}")
    cursor = cursors.decode(query.cursor, fingerprint)
    predicates = [
        sample.provider_id == provider_id,
        sample.provider_owner == owner,
        sample.last_observed_at >= utc(query.start),
        sample.last_observed_at < utc(query.end),
    ]
    if query.window:
        predicates.append(sample.window_key == query.window)
    if cursor:
        try:
            at, sample_id = utc(datetime.fromisoformat(cursor[0])), str(cursor[1])
        except (ValueError, TypeError, IndexError) as exc:
            raise UsageQueryError("invalid_usage_cursor", "Invalid quota position") from exc
        predicates.append(
            or_(
                sample.last_observed_at < at,
                and_(sample.last_observed_at == at, sample.sample_id < sample_id),
            )
        )
    values = list(
        (
            await session.scalars(
                select(sample)
                .where(*predicates)
                .order_by(sample.last_observed_at.desc(), sample.sample_id.desc())
                .limit(query.limit + 1)
            )
        ).all()
    )
    page = values[: query.limit]
    return ProviderQuotaHistory(
        provider_id=provider_id,
        samples=[
            ProviderQuotaSample(
                sample_id=row.sample_id,
                observed_at=utc(row.last_observed_at),
                account_generation=hashlib.sha256(
                    f"{row.account_scope_id}:{row.configuration_generation}".encode()
                ).hexdigest(),
                window=row.window_key,
                source=row.source,
                reset_at=utc(row.reset_at) if row.reset_at else None,
                limit=format(row.limit_value, "f") if row.limit_value is not None else None,
                remaining=format(row.latest_remaining, "f")
                if row.latest_remaining is not None
                else None,
                sample_count=row.sample_count,
                first_observed_at=utc(row.first_observed_at),
                maximum_utilization=format(row.maximum_utilization, "f")
                if row.maximum_utilization is not None
                else None,
            )
            for row in page
        ],
        next_cursor=cursors.encode(
            fingerprint, [utc(page[-1].last_observed_at).isoformat(), page[-1].sample_id]
        )
        if len(values) > query.limit
        else None,
        limitations=[
            "Provider-native units are not converted into billed tokens.",
            "Coalesced maxima are not latest utilization; utilization is not inferred.",
            "Sparse gaps and reset windows are not interpolated.",
        ],
    )
