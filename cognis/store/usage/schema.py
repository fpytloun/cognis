"""Idempotent additive accounting schema used by bootstrap and Alembic."""

from __future__ import annotations

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import TIMESTAMP, BigInteger, Boolean, Column, String, inspect, text
from sqlalchemy.engine import Connection

from cognis.store.models import Base
from cognis.store.usage.models import SESSION_CORRELATION_CHECK, USAGE_TABLE_NAMES

QUERY_MODEL_COLUMNS = {
    "usage_attempts": ("effective_model", "requested_model", "provider_reported_model"),
    "usage_price_revisions": ("model",),
    "usage_pricing_state": ("model",),
    "usage_session_totals": ("effective_model",),
    "usage_hourly_totals": ("effective_model",),
    "usage_daily_totals": ("effective_model",),
    "usage_archived_totals": ("model",),
    "provider_usage_samples": ("model_bucket",),
}
QUERY_INDEXES = {
    "usage_attempts": ("ix_usage_attempt_time", "ix_usage_owner_day_row", "ix_usage_next_expiry"),
    "usage_projection_state": ("ix_usage_projection_day",),
    "usage_hourly_totals": ("ix_usage_hourly_day",),
    "usage_daily_totals": ("ix_usage_daily_day",),
}
SUMMARY_TABLES = ("usage_session_totals", "usage_hourly_totals", "usage_daily_totals")


def ensure_usage_schema(connection: Connection) -> None:
    """Create only accounting tables; never replay or import session content."""
    for name in USAGE_TABLE_NAMES:
        table = Base.metadata.tables[name]
        table.create(connection, checkfirst=True)
    ensure_usage_query_contract(connection)
    ensure_usage_proxy_sessions(connection)
    ensure_usage_direct_sessions(connection)
    ensure_usage_maintenance_lifecycle(connection)
    ensure_usage_traffic(connection)
    for name in USAGE_TABLE_NAMES:
        table = Base.metadata.tables[name]
        for index in table.indexes:
            index.create(connection, checkfirst=True)


def ensure_usage_traffic(connection: Connection) -> None:
    """Add traffic projection columns; old generations require bounded rebuilding."""
    operations = Operations(MigrationContext.configure(connection))
    for name in ("usage_attempts", *SUMMARY_TABLES, "usage_archived_totals"):
        actual = {column["name"] for column in inspect(connection).get_columns(name)}
        table = Base.metadata.tables[name]
        for field in ("traffic_origin", "traffic_auth_type", "traffic_client_id", "traffic_key_id"):
            if field not in actual:
                source = table.c[field]
                operations.add_column(
                    name,
                    Column(
                        field,
                        source.type,
                        nullable=source.nullable,
                        server_default="unknown" if field == "traffic_origin" else None,
                    ),
                )
    actual = {
        column["name"] for column in inspect(connection).get_columns("usage_projection_state")
    }
    if "traffic_version" not in actual:
        operations.add_column(
            "usage_projection_state",
            Column("traffic_version", BigInteger, nullable=False, server_default="1"),
        )
        connection.execute(
            text("UPDATE usage_projection_state SET ready = false, retention_paused = true")
        )
    Base.metadata.tables["usage_request_logs"].create(connection, checkfirst=True)


def ensure_usage_proxy_sessions(connection: Connection) -> None:
    """Add only session-specific client correlation, preserving canonical dimensions."""
    operations = Operations(MigrationContext.configure(connection))
    actual_attempts = {
        column["name"] for column in inspect(connection).get_columns("usage_attempts")
    }
    if "client_session_id" not in actual_attempts:
        operations.add_column("usage_attempts", Column("client_session_id", String(160)))
    actual_sessions = {
        column["name"] for column in inspect(connection).get_columns("usage_session_totals")
    }
    constraints = {
        item["name"] for item in inspect(connection).get_check_constraints("usage_session_totals")
    }
    additions = [
        column
        for column in (
            Column("session_kind", String(6), nullable=False, server_default="cognis"),
            Column("client_id", String(160)),
            Column("client_session_id", String(160)),
        )
        if column.name not in actual_sessions
    ]
    if additions or "ck_usage_session_correlation" not in constraints:
        with operations.batch_alter_table("usage_session_totals") as batch:
            for column in additions:
                batch.add_column(column)
            if "ck_usage_session_required" in constraints:
                batch.drop_constraint("ck_usage_session_required", type_="check")
            if "ck_usage_session_correlation" not in constraints:
                batch.create_check_constraint(
                    "ck_usage_session_correlation",
                    "(session_kind = 'cognis' AND session_id IS NOT NULL "
                    "AND client_id IS NULL AND client_session_id IS NULL) OR "
                    "(session_kind = 'proxy' AND session_id IS NULL AND client_session_id IS NOT NULL)",
                )
    _ensure_indexes(
        connection,
        {
            "usage_attempts": ("ix_usage_client_session_time",),
            "usage_session_totals": ("ix_usage_proxy_session",),
        },
    )


def ensure_usage_direct_sessions(connection: Connection) -> None:
    """Retain conversation-only money without fabricating physical sessions."""
    checks = inspect(connection).get_check_constraints("usage_session_totals")
    current = next(
        (item["sqltext"] for item in checks if item["name"] == "ck_usage_session_correlation"),
        "",
    )
    if "'direct'" not in current:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("usage_session_totals") as batch:
            if current:
                batch.drop_constraint("ck_usage_session_correlation", type_="check")
            batch.create_check_constraint("ck_usage_session_correlation", SESSION_CORRELATION_CHECK)
    _ensure_indexes(connection, {"usage_session_totals": ("ix_usage_direct_conversation",)})


def rollback_usage_direct_sessions(connection: Connection) -> None:
    """Refuse a downgrade that would lose represented conversation usage."""
    if connection.execute(
        text("SELECT 1 FROM usage_session_totals WHERE session_kind = 'direct' LIMIT 1")
    ).first():
        raise ValueError("Cannot downgrade while direct conversation totals exist")
    operations = Operations(MigrationContext.configure(connection))
    operations.drop_index("ix_usage_direct_conversation", table_name="usage_session_totals")
    with operations.batch_alter_table("usage_session_totals") as batch:
        batch.drop_constraint("ck_usage_session_correlation", type_="check")
        batch.create_check_constraint(
            "ck_usage_session_correlation",
            SESSION_CORRELATION_CHECK.split(" OR (session_kind = 'direct'")[0],
        )


MAINTENANCE_INDEXES = {
    "usage_attempts": ("ix_usage_attempt_scope_expiry",),
    "usage_projection_state": ("ix_usage_scope_expiry",),
    "usage_maintenance_jobs": ("ix_usage_jobs_kind_status", "ix_usage_jobs_completed"),
    "provider_usage_samples": ("ix_usage_sample_expiry",),
    "usage_request_stats": ("ix_usage_request_expiry",),
    "usage_projection_outbox": ("ix_usage_outbox_completed",),
}


def _ensure_indexes(connection: Connection, indexes: dict[str, tuple[str, ...]]) -> None:
    for name, requested in indexes.items():
        for index in Base.metadata.tables[name].indexes:
            if index.name in requested:
                index.create(connection, checkfirst=True)


def ensure_usage_maintenance_lifecycle(connection: Connection) -> None:
    """Maintain indexed scope deadlines, not a recurring scan of canonical detail."""
    operations = Operations(MigrationContext.configure(connection))
    actual = {item["name"] for item in inspect(connection).get_columns("usage_projection_state")}
    if "oldest_detail_at" not in actual:
        operations.add_column(
            "usage_projection_state", Column("oldest_detail_at", TIMESTAMP(timezone=True))
        )
    if "retention_paused" not in actual:
        operations.add_column(
            "usage_projection_state",
            Column("retention_paused", Boolean(), nullable=False, server_default=text("false")),
        )
    constraint = next(
        item
        for item in inspect(connection).get_check_constraints("usage_maintenance_jobs")
        if item["name"] == "ck_usage_job_kind"
    )
    if "'cleanup'" not in constraint["sqltext"]:
        with operations.batch_alter_table("usage_maintenance_jobs") as batch:
            batch.drop_constraint("ck_usage_job_kind", type_="check")
            batch.create_check_constraint(
                "ck_usage_job_kind",
                "kind IN ('backfill', 'reprice', 'rebuild', 'archive', 'cleanup')",
            )
    _ensure_indexes(connection, MAINTENANCE_INDEXES)


def rollback_usage_maintenance_lifecycle(connection: Connection) -> None:
    """Do not discard an active cleanup cursor during rollback."""
    if connection.scalar(
        text(
            "SELECT 1 FROM usage_maintenance_jobs WHERE kind = 'cleanup' "
            "AND status IN ('queued', 'running', 'paused', 'failed') LIMIT 1"
        )
    ):
        raise ValueError("Usage cleanup jobs must finish before downgrade")
    operations = Operations(MigrationContext.configure(connection))
    for table, indexes in MAINTENANCE_INDEXES.items():
        for name in indexes:
            operations.drop_index(name, table_name=table)
    connection.execute(text("DELETE FROM usage_maintenance_jobs WHERE kind = 'cleanup'"))
    with operations.batch_alter_table("usage_maintenance_jobs") as batch:
        batch.drop_constraint("ck_usage_job_kind", type_="check")
        batch.create_check_constraint(
            "ck_usage_job_kind", "kind IN ('backfill', 'reprice', 'rebuild', 'archive')"
        )
    with operations.batch_alter_table("usage_projection_state") as batch:
        batch.drop_column("oldest_detail_at")
        batch.drop_column("retention_paused")


def rollback_usage_proxy_sessions(connection: Connection) -> None:
    """Do not silently discard retained correlation on an explicitly requested downgrade."""
    if "ix_usage_direct_conversation" in {
        item["name"] for item in inspect(connection).get_indexes("usage_session_totals")
    }:
        rollback_usage_direct_sessions(connection)
    if connection.scalar(
        text("SELECT 1 FROM usage_session_totals WHERE session_kind = 'proxy' LIMIT 1")
    ) or connection.scalar(
        text("SELECT 1 FROM usage_attempts WHERE client_session_id IS NOT NULL LIMIT 1")
    ):
        raise ValueError("Retained proxy correlation prevents a lossy downgrade")
    operations = Operations(MigrationContext.configure(connection))
    operations.drop_index("ix_usage_client_session_time", table_name="usage_attempts")
    operations.drop_index("ix_usage_proxy_session", table_name="usage_session_totals")
    with operations.batch_alter_table("usage_session_totals") as batch:
        batch.drop_constraint("ck_usage_session_correlation", type_="check")
        batch.create_check_constraint("ck_usage_session_required", "session_id IS NOT NULL")
        batch.drop_column("session_kind")
        batch.drop_column("client_id")
        batch.drop_column("client_session_id")
    with operations.batch_alter_table("usage_attempts") as batch:
        batch.drop_column("client_session_id")


def ensure_usage_query_contract(connection: Connection) -> None:
    """Upgrade previous-180 owned columns without replacing canonical data."""
    operations = Operations(MigrationContext.configure(connection))
    for name in USAGE_TABLE_NAMES:
        expected = Base.metadata.tables[name]
        actual = {column["name"]: column for column in inspect(connection).get_columns(name)}
        changes = [
            column
            for column in expected.columns
            if column.name in actual
            and (
                getattr(actual[column.name]["type"], "length", None)
                != getattr(column.type, "length", None)
                or actual[column.name]["nullable"] != column.nullable
            )
        ]
        missing_reduction = (
            name == "usage_coverage_epochs" and "metadata_reduced_events" not in actual
        )
        if not changes and not missing_reduction:
            continue
        with operations.batch_alter_table(name) as batch:
            for column in changes:
                batch.alter_column(
                    column.name,
                    existing_type=actual[column.name]["type"],
                    type_=column.type,
                    nullable=column.nullable,
                )
            if missing_reduction:
                batch.add_column(
                    Column(
                        "metadata_reduced_events", BigInteger(), nullable=False, server_default="0"
                    )
                )


def rollback_usage_query_contract(connection: Connection) -> None:
    """Refuse lossy rollback of canonical names/bindings or unallocated requests."""
    operations = Operations(MigrationContext.configure(connection))
    inspector = inspect(connection)
    names = set(inspector.get_table_names())
    for table, columns in QUERY_MODEL_COLUMNS.items():
        for column in columns:
            if connection.scalar(
                text(f'SELECT 1 FROM "{table}" WHERE length("{column}") > 160 LIMIT 1')
            ):
                raise ValueError("Usage model names cannot be truncated by rollback")
    for table in SUMMARY_TABLES:
        if connection.scalar(
            text(
                f'SELECT 1 FROM "{table}" WHERE provider_id IS NULL OR effective_model IS NULL '
                "OR source IS NULL OR streaming IS NULL LIMIT 1"
            )
        ):
            raise ValueError("Unallocated request summaries cannot be discarded by rollback")
    if "usage_historical_price_bindings" in names and connection.scalar(
        text("SELECT 1 FROM usage_historical_price_bindings LIMIT 1")
    ):
        raise ValueError("Canonical historical price bindings cannot be discarded by rollback")
    for table, indexes in QUERY_INDEXES.items():
        existing = {index["name"] for index in inspect(connection).get_indexes(table)}
        for index in indexes:
            if index in existing:
                operations.drop_index(index, table_name=table)
    for table, columns in QUERY_MODEL_COLUMNS.items():
        with operations.batch_alter_table(table) as batch:
            for column in columns:
                batch.alter_column(column, existing_type=String(512), type_=String(160))
            if table in SUMMARY_TABLES:
                batch.alter_column("provider_id", existing_type=String(160), nullable=False)
                batch.alter_column("effective_model", existing_type=String(160), nullable=False)
                batch.alter_column("source", existing_type=String(20), nullable=False)
                batch.alter_column("streaming", existing_type=Boolean(), nullable=False)
                batch.alter_column("resource_owner", existing_type=String(320), type_=String(160))
    with operations.batch_alter_table("usage_coverage_epochs") as batch:
        batch.drop_column("metadata_reduced_events")
    if "usage_historical_price_bindings" in names:
        operations.drop_table("usage_historical_price_bindings")
