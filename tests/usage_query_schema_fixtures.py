"""Frozen previous-180 column expectations and isolated synthetic index workloads."""

import importlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, String, inspect, select, text
from sqlalchemy.engine import Connection

from cognis.core.usage.dimensions import UsageDimensions, encode_usage_dimensions
from cognis.store.models import Base
from cognis.store.usage.codec import context_values, terminal_values
from cognis.store.usage.models import UsageAttemptRow, UsageSummaryMeasures
from cognis.store.usage.schema import ensure_usage_schema
from tests.usage_fixtures import finalized_attempt

EXPECTED_QUERY_INDEXES = {
    "usage_attempts": ("ix_usage_attempt_time", "ix_usage_owner_day_row", "ix_usage_next_expiry"),
    "usage_projection_state": ("ix_usage_projection_day",),
    "usage_daily_totals": ("ix_usage_daily_day",),
    "usage_hourly_totals": ("ix_usage_hourly_day",),
}
PREVIOUS_MODEL_COLUMNS = {
    "usage_attempts": ("effective_model", "requested_model", "provider_reported_model"),
    "usage_price_revisions": ("model",),
    "usage_pricing_state": ("model",),
    "usage_session_totals": ("effective_model",),
    "usage_hourly_totals": ("effective_model",),
    "usage_daily_totals": ("effective_model",),
    "usage_archived_totals": ("model",),
    "provider_usage_samples": ("model_bucket",),
}
SUMMARY_TABLES_180 = ("usage_session_totals", "usage_hourly_totals", "usage_daily_totals")


def previous_180_schema(connection: Connection) -> None:
    """Construct precisely the old widths/nullability, before invoking new upgrade."""
    ensure_usage_schema(connection)
    op = Operations(MigrationContext.configure(connection))
    for table, names in EXPECTED_QUERY_INDEXES.items():
        for name in names:
            op.drop_index(name, table_name=table)
    op.drop_table("usage_historical_price_bindings")
    with op.batch_alter_table("usage_coverage_epochs") as batch:
        batch.drop_column("metadata_reduced_events")
    for table, names in PREVIOUS_MODEL_COLUMNS.items():
        with op.batch_alter_table(table) as batch:
            for name in names:
                batch.alter_column(name, existing_type=String(512), type_=String(160))
            if table in SUMMARY_TABLES_180:
                for name, existing in (
                    ("provider_id", String(160)),
                    ("effective_model", String(160)),
                    ("source", String(20)),
                    ("streaming", Boolean()),
                ):
                    batch.alter_column(name, existing_type=existing, nullable=False)
                batch.alter_column("resource_owner", existing_type=String(320), type_=String(160))
    assert inspect(connection).get_columns("usage_attempts")[0]["name"] == "row_id"
    model = next(
        c
        for c in inspect(connection).get_columns("usage_attempts")
        if c["name"] == "effective_model"
    )
    assert model["type"].length == 160
    assert "metadata_reduced_events" not in {
        column["name"] for column in inspect(connection).get_columns("usage_coverage_epochs")
    }
    for table, names in EXPECTED_QUERY_INDEXES.items():
        assert not set(names) & {index["name"] for index in inspect(connection).get_indexes(table)}


def exercise_query_upgrade(connection: Connection) -> None:
    previous_180_schema(connection)
    now = datetime(2026, 10, 29, tzinfo=UTC)
    event = finalized_attempt()
    values = context_values(event.context, now) | terminal_values(event, now) | {"fact_version": 1}
    connection.execute(UsageAttemptRow.__table__.insert().values(values))
    migration = importlib.import_module("cognis.store.migrations.versions.181_usage_query_contract")
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    ensure_usage_schema(connection)
    ensure_usage_schema(connection)
    for table, names in EXPECTED_QUERY_INDEXES.items():
        assert set(names) <= {index["name"] for index in inspect(connection).get_indexes(table)}
    for table, names in PREVIOUS_MODEL_COLUMNS.items():
        columns = {column["name"]: column for column in inspect(connection).get_columns(table)}
        assert all(columns[name]["type"].length == 512 for name in names)
    assert (
        connection.scalar(select(UsageAttemptRow.effective_model))
        == event.context.selection.effective_model
    )
    # No fake provider/model is needed to persist request-only metric populations.
    dims = UsageDimensions(
        record_kind="request",
        usage_subject_key=event.context.attribution.identity.usage_subject_key,
        purpose="chat",
        operation="request",
    )
    encoded = encode_usage_dimensions(dims, grain="daily")
    table = Base.metadata.tables["usage_daily_totals"]
    request = {
        **encoded.dimensions.model_dump(mode="json"),
        "dimensions": encoded.dimensions.model_dump(mode="json"),
        "dimension_key": encoded.dimension_key,
        "owner_key": "opaque-owner-not-a-subject",
        "bucket_day": date(2026, 10, 29),
        "generation": 1,
        "updated_at": now,
        **{name: 0 for name in UsageSummaryMeasures.__annotations__},
    }
    connection.execute(
        table.insert().values({key: value for key, value in request.items() if key in table.c})
    )
    assert connection.scalar(text("SELECT provider_id IS NULL FROM usage_daily_totals"))
    long_values = values | {
        "attempt_id": "attempt_long_model",
        "effective_model": "m" * 180,
        "requested_model": "r" * 180,
        "provider_reported_model": "p" * 180,
    }
    connection.execute(UsageAttemptRow.__table__.insert().values(long_values))
    assert connection.scalar(text("SELECT max(length(effective_model)) FROM usage_attempts")) == 180
    # A lossy downgrade must refuse before truncating canonical names or requests.
    with Operations.context(MigrationContext.configure(connection)):
        try:
            migration.downgrade()
        except ValueError as exc:
            assert "cannot" in str(exc)
        else:
            raise AssertionError("Lossy rollback must be refused")
    assert connection.scalar(text("SELECT count(*) FROM usage_attempts")) == 2


def explain_query_indexes(connection: Connection) -> dict[str, Any]:
    """Real PostgreSQL planner selection with defaults, not enable_seqscan tricks."""
    ensure_usage_schema(connection)
    event = finalized_attempt()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    base = (
        context_values(event.context, start)
        | terminal_values(event, start)
        | {
            "fact_version": 1,
            "projected_version": 1,
            "context_payload": {},
            "terminal_payload": {},
        }
    )
    for offset in range(0, 2560, 128):
        attempts = []
        daily = []
        hourly = []
        states = []
        for index in range(offset, offset + 128):
            at = start + timedelta(hours=index * 2)
            owner = f"opaque-owner-{index}"
            attempts.append(
                base
                | {
                    "attempt_id": f"attempt_{index}",
                    "request_id": f"request_{index}",
                    "owner_key": owner,
                    "started_at_observed": at,
                    "bucket_day": at.date(),
                    "finalized_at": at,
                }
            )
            dimensions = UsageDimensions(
                record_kind="request",
                usage_subject_key="service:index-fixture",
                purpose="chat",
                operation="request",
            )
            encoding = encode_usage_dimensions(dimensions, grain="daily")
            summary = {
                **encoding.dimensions.model_dump(mode="json"),
                **{name: 0 for name in UsageSummaryMeasures.__annotations__},
                "owner_key": owner,
                "bucket_day": at.date(),
                "generation": 1,
                "dimension_key": encoding.dimension_key,
                "dimensions": encoding.dimensions.model_dump(mode="json"),
                "updated_at": at,
            }
            table = Base.metadata.tables["usage_daily_totals"]
            summary = {key: value for key, value in summary.items() if key in table.c}
            daily.append(summary)
            hourly.append(summary | {"bucket_hour": at.hour})
            states.append(
                {
                    "owner_key": owner,
                    "bucket_day": at.date(),
                    "active_generation": 1,
                    "ready": True,
                    "capture_only": False,
                    "archive_revision": 0,
                    "updated_at": at,
                }
            )
        connection.execute(UsageAttemptRow.__table__.insert(), attempts)
        connection.execute(Base.metadata.tables["usage_daily_totals"].insert(), daily)
        connection.execute(Base.metadata.tables["usage_hourly_totals"].insert(), hourly)
        connection.execute(Base.metadata.tables["usage_projection_state"].insert(), states)
    for table in (
        "usage_attempts",
        "usage_daily_totals",
        "usage_hourly_totals",
        "usage_projection_state",
    ):
        connection.execute(text(f'ANALYZE "{table}"'))
    since = start + timedelta(hours=2550 * 2)
    cases = {
        "ix_usage_attempt_time": (
            "SELECT row_id FROM usage_attempts WHERE started_at_observed >= :at "
            "ORDER BY started_at_observed, row_id LIMIT 20",
            {"at": since},
        ),
        "ix_usage_next_expiry": (
            "SELECT row_id FROM usage_attempts WHERE finalized_at IS NOT NULL "
            "AND projected_version = fact_version AND finalized_at <= :at "
            "ORDER BY finalized_at, row_id LIMIT 128",
            {"at": since},
        ),
        "ix_usage_daily_day": (
            "SELECT sum(finalized_count) FROM usage_daily_totals WHERE bucket_day >= :day",
            {"day": since.date()},
        ),
        "ix_usage_hourly_day": (
            "SELECT sum(finalized_count) FROM usage_hourly_totals WHERE bucket_day >= :day",
            {"day": since.date()},
        ),
        "ix_usage_projection_day": (
            "SELECT owner_key FROM usage_projection_state WHERE bucket_day >= :day "
            "ORDER BY bucket_day, owner_key LIMIT 128",
            {"day": since.date()},
        ),
    }
    evidence = {}
    for expected, (query, bindings) in cases.items():
        result = connection.scalar(
            text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}"), bindings
        )
        plan = json.loads(result) if isinstance(result, str) else result
        assert expected in json.dumps(plan), plan
        evidence[expected] = plan
    return evidence
