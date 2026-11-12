"""Canonical retained attempt/valuation joins for both physical and logical detail."""

from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import load_only

from cognis.store.usage.models import (
    UsageAttemptRow,
    UsagePriceRevisionRow,
    UsagePricingStateRow,
    UsageProjectionStateRow,
    UsageRequestStatsRow,
    UsageValuationRow,
)


def retained_attempt_statement(basis: str) -> Any:
    """Never expose stale generation, fact-version or current-price valuations."""
    row, state, valuation, stats, price = (
        UsageAttemptRow,
        UsageProjectionStateRow,
        UsageValuationRow,
        UsageRequestStatsRow,
        UsagePriceRevisionRow,
    )
    join_state = and_(state.owner_key == row.owner_key, state.bucket_day == row.bucket_day)
    join_value = and_(
        valuation.owner_key == row.owner_key,
        valuation.bucket_day == row.bucket_day,
        valuation.source_kind == "attempt",
        valuation.source_id == row.attempt_id,
        valuation.fact_version == row.fact_version,
        valuation.generation == state.active_generation,
        state.ready.is_(True),
        state.capture_only.is_(False),
    )
    if basis == "current":
        pricing = UsagePricingStateRow
        join_value = and_(
            join_value,
            select(1)
            .where(
                pricing.provider_id == row.provider_id,
                pricing.model == row.effective_model,
                pricing.generation == valuation.pricing_generation,
                pricing.current_revision_id == valuation.current_revision_id,
            )
            .correlate(row, valuation)
            .exists(),
        )
    return (
        select(row, valuation, stats, price)
        .options(load_only(price.currency, price.basis))
        .outerjoin(state, join_state)
        .outerjoin(valuation, join_value)
        .outerjoin(
            price,
            and_(
                price.revision_id == getattr(valuation, f"{basis}_revision_id"),
                price.provider_id == row.provider_id,
                price.model == row.effective_model,
            ),
        )
        .outerjoin(
            stats,
            and_(
                stats.request_id == row.request_id,
                stats.attempt_id == row.attempt_id,
                stats.usage_subject_key == row.usage_subject_key,
                stats.owner_key == row.owner_key,
            ),
        )
    )
