"""Fixed, bounded marginal accounting from one authorized summary statement."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from sqlalchemy import literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from cognis.core.usage.access import AuthorizedUsageScope
from cognis.core.usage.query_labels import group_labels
from cognis.core.usage.query_models import (
    UsageCostBreakdown,
    UsageCostBreakdowns,
    UsageGroup,
    UsageMeta,
    UsageMetrics,
    UsageQuery,
    UsageQueryResponse,
)
from cognis.core.usage.query_populations import (
    UsagePopulationContract,
    aggregate_columns,
    combined_metrics,
)
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.usage_queries import public_group

DIMENSIONS: tuple[Literal["subject", "agent", "provider", "model"], ...] = (
    "subject",
    "agent",
    "provider",
    "model",
)


async def cost_breakdowns(
    session: AsyncSession,
    *,
    scope: AuthorizedUsageScope,
    query: UsageQuery,
    population: UsagePopulationContract,
    meta: UsageMeta,
    base: Select[Any],
) -> UsageQueryResponse:
    """Total plus four marginal arms; never a Cartesian attribution cube."""
    rows = base.cte("usage_breakdown_source")
    kind = population.record_kind(rows.c)
    arms = []
    for dimension in (None, *DIMENSIONS):
        group = public_group(rows.c, dimension, scope=scope) if dimension else literal(None)
        arms.append(
            select(
                literal(dimension or "total").label("dimension"),
                group.label("group_key"),
                rows.c.currency,
                rows.c.basis,
                kind.label("record_kind"),
                *aggregate_columns(rows.c),
            )
            .select_from(rows)
            .group_by(group, rows.c.currency, rows.c.basis, kind)
        )
    statement = union_all(*arms).limit(1001)
    values = list((await session.execute(statement)).mappings())
    if len(values) > 1000:
        raise UsageQueryError(
            "query_budget_exceeded", "Total and four complete cost marginals exceed 1000 strata"
        )
    if any(value["record_kind"] not in {"attempt", "request"} for value in values):
        raise UsageQueryError("usage_population_mismatch", "Unknown summary population", 503)
    grouped: dict[str, dict[str | None, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for value in values:
        grouped[value["dimension"]][value["group_key"]].append(value)

    def measures(items: list[Any]) -> UsageMetrics:
        return combined_metrics(
            [value for value in items if value["record_kind"] == "attempt"],
            [value for value in items if value["record_kind"] == "request"],
            basis=query.pricing_basis,
            contract=population,
        )

    # Agent IDs are internal; show the current authorized agent name.
    labels = {
        dimension: await group_labels(session, scope, dimension, list(grouped[dimension]))
        if dimension == "agent"
        else {key: key for key in grouped[dimension]}
        for dimension in DIMENSIONS
    }
    return UsageQueryResponse(
        meta=meta,
        total=measures(grouped["total"].get(None, [])),
        cost_breakdowns=UsageCostBreakdowns(
            dimensions=[
                UsageCostBreakdown(
                    dimension=dimension,
                    groups=[
                        UsageGroup(
                            key=key,
                            label=labels[dimension].get(key) or key,
                            metrics=measures(items),
                        )
                        for key, items in sorted(
                            grouped[dimension].items(),
                            key=lambda item: (item[0] is not None, item[0] or ""),
                        )
                    ],
                )
                for dimension in DIMENSIONS
            ]
        ),
    )
