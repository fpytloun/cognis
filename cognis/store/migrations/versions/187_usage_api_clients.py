"""Trusted API-client usage dimensions and retained request headers."""

from alembic import op
from sqlalchemy import Table, inspect, select, update

from cognis.store.usage.schema import ensure_usage_schema

revision = "187_usage_api_clients"
down_revision = "186_merge_usage_read_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply the same idempotent schema path used at startup."""
    ensure_usage_schema(op.get_bind())


def downgrade() -> None:
    """Drop disposable traffic projections; preserve canonical observations."""
    op.drop_table("usage_request_logs")
    from cognis.store.usage.models import UsageArchivedTotalRow

    connection = op.get_bind()
    # Older code rejects v2 JSON even after physical columns have been dropped.
    archive = UsageArchivedTotalRow.__table__
    assert isinstance(archive, Table)
    for row in connection.execute(select(archive.c.archive_id, archive.c.dimensions)):
        dimensions = dict(row.dimensions)
        for field in ("traffic_origin", "traffic_auth_type", "traffic_client_id", "traffic_key_id"):
            dimensions.pop(field, None)
        dimensions["schema_version"] = 1
        connection.execute(
            update(archive)
            .where(archive.c.archive_id == row.archive_id)
            .values(dimensions=dimensions)
        )
    # Receipts/summaries are disposable; a reverse rollout must rebuild from facts.
    for table in (
        "usage_session_totals",
        "usage_hourly_totals",
        "usage_daily_totals",
        "usage_valuations",
    ):
        connection.exec_driver_sql(f"DELETE FROM {table}")
    connection.exec_driver_sql(
        "UPDATE usage_projection_state SET ready = false, retention_paused = true"
    )
    for table in (
        "usage_attempts",
        "usage_session_totals",
        "usage_hourly_totals",
        "usage_daily_totals",
        "usage_archived_totals",
    ):
        with op.batch_alter_table(table) as batch:
            for index in inspect(connection).get_indexes(table):
                name = index["name"]
                if (
                    name
                    and name.startswith(f"ix_{table}_")
                    and name.rsplit(f"ix_{table}_", 1)[-1]
                    in {"origin", "auth_type", "client_id", "key_id"}
                ):
                    batch.drop_index(name)
            for name in (
                "traffic_origin",
                "traffic_auth_type",
                "traffic_client_id",
                "traffic_key_id",
            ):
                batch.drop_column(name)
    with op.batch_alter_table("usage_projection_state") as batch:
        batch.drop_column("traffic_version")
