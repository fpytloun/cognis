"""Add the reconstructable persistent Work cache foundation."""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "130_work_reconstructable_cache"
down_revision: str | Sequence[str] | None = "129_work_record_file_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE_NONNEGATIVE = (
    "retry_count >= 0 AND lease_fence >= 0 AND reconciliation_count >= 0 "
    "AND expected_generation_count >= 0 AND expected_stream_count >= 0 "
    "AND expected_projection_count >= 0 AND expected_record_count >= 0 "
    "AND expected_current_file_count >= 0"
)
PROJECTION_NONNEGATIVE = (
    "target_seq >= 0 AND covered_through_seq >= 0 AND retry_count >= 0 "
    "AND lease_fence >= 0 AND record_count >= 0 AND evidence_record_count >= 0 "
    "AND mutation_count >= 0 AND command_count >= 0 AND file_count >= 0 "
    "AND artifact_count >= 0 AND deliverable_count >= 0 AND additions >= 0 "
    "AND deletions >= 0 AND omitted_file_count >= 0 "
    "AND expected_record_count >= 0 AND expected_record_file_count >= 0"
)
SCOPE_LIFECYCLE = (
    "lifecycle_state IN ('missing','recovering','ready','stale','failed','source_unavailable')"
)


def _column_names(table_name: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _check_constraints(table_name: str) -> dict[str, str]:
    return {
        str(constraint["name"]): str(constraint.get("sqltext") or "")
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }


def _index_names(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _create_index_if_missing(table_name: str, name: str, columns: list[str]) -> None:
    if name not in _index_names(table_name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    scope_columns = _column_names("work_scope_states")
    scope_additions: tuple[sa.Column[Any], ...] = (
        sa.Column("lifecycle_state", sa.String(), server_default="missing", nullable=False),
        sa.Column("active_cache_epoch", sa.String(), nullable=True),
        sa.Column("recovery_cache_epoch", sa.String(), nullable=True),
        sa.Column("topology_projector_version", sa.String(), nullable=True),
        sa.Column("topology_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("durable_cursor", sa.JSON(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reconciliation_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reconciliation_requested_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expected_generation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expected_stream_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expected_projection_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expected_record_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "expected_current_file_count", sa.BigInteger(), server_default="0", nullable=False
        ),
    )
    scope_checks = _check_constraints("work_scope_states")
    with op.batch_alter_table("work_scope_states") as batch:
        for column in scope_additions:
            if column.name not in scope_columns:
                batch.add_column(column)
        if "ck_work_scope_states_lifecycle" not in scope_checks:
            batch.create_check_constraint("ck_work_scope_states_lifecycle", SCOPE_LIFECYCLE)
        if "ck_work_scope_states_cache_nonnegative" not in scope_checks:
            batch.create_check_constraint(
                "ck_work_scope_states_cache_nonnegative", SCOPE_NONNEGATIVE
            )
    _create_index_if_missing(
        "work_scope_states",
        "ix_work_scope_states_recovery",
        ["lifecycle_state", "next_retry_at"],
    )
    _create_index_if_missing(
        "work_scope_states",
        "ix_work_scope_states_lease",
        ["lease_expires_at", "lease_fence"],
    )

    projection_columns = _column_names("work_session_projections")
    projection_additions: tuple[sa.Column[Any], ...] = (
        sa.Column("next_head_check_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *(
            sa.Column(name, type_, server_default="0", nullable=False)
            for name, type_ in (
                ("record_count", sa.BigInteger()),
                ("evidence_record_count", sa.BigInteger()),
                ("mutation_count", sa.Integer()),
                ("command_count", sa.Integer()),
                ("file_count", sa.Integer()),
                ("artifact_count", sa.Integer()),
                ("deliverable_count", sa.Integer()),
                ("additions", sa.BigInteger()),
                ("deletions", sa.BigInteger()),
                ("omitted_file_count", sa.Integer()),
                ("expected_record_count", sa.BigInteger()),
                ("expected_record_file_count", sa.BigInteger()),
            )
        ),
    )
    projection_checks = _check_constraints("work_session_projections")
    projection_check_is_extended = "expected_record_file_count" in projection_checks.get(
        "ck_work_session_projections_nonnegative", ""
    )
    with op.batch_alter_table("work_session_projections") as batch:
        for column in projection_additions:
            if column.name not in projection_columns:
                batch.add_column(column)
        if not projection_check_is_extended:
            if "ck_work_session_projections_nonnegative" in projection_checks:
                batch.drop_constraint("ck_work_session_projections_nonnegative", type_="check")
            batch.create_check_constraint(
                "ck_work_session_projections_nonnegative", PROJECTION_NONNEGATIVE
            )

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "work_scope_generations" not in tables:
        op.create_table(
            "work_scope_generations",
            sa.Column("scope_key", sa.String(), nullable=False),
            sa.Column("cache_epoch", sa.String(), nullable=False),
            sa.Column("state", sa.String(), server_default="missing", nullable=False),
            sa.Column("topology_projector_version", sa.String(), nullable=False),
            sa.Column("core_projector_version", sa.String(), nullable=False),
            sa.Column("files_projector_version", sa.String(), nullable=False),
            sa.Column("topology_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("graph_fingerprint", sa.String(), nullable=True),
            sa.Column("durable_cursor", sa.JSON(), nullable=True),
            sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("lease_owner", sa.String(), nullable=True),
            sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("lease_fence", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column("expected_stream_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "expected_projection_count", sa.Integer(), server_default="0", nullable=False
            ),
            sa.Column("expected_record_count", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column(
                "expected_current_file_count", sa.BigInteger(), server_default="0", nullable=False
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.CheckConstraint(
                "state IN ('missing','recovering','ready','stale','failed','source_unavailable')",
                name="ck_work_scope_generations_state",
            ),
            sa.CheckConstraint(
                "retry_count >= 0 AND lease_fence >= 0 AND expected_stream_count >= 0 "
                "AND expected_projection_count >= 0 AND expected_record_count >= 0 "
                "AND expected_current_file_count >= 0",
                name="ck_work_scope_generations_nonnegative",
            ),
            sa.ForeignKeyConstraint(
                ["scope_key"], ["work_scope_states.scope_key"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("scope_key", "cache_epoch"),
        )
    _create_index_if_missing(
        "work_scope_generations",
        "ix_work_scope_generations_state_retry",
        ["state", "next_retry_at"],
    )
    _create_index_if_missing(
        "work_scope_generations",
        "ix_work_scope_generations_lease",
        ["lease_expires_at", "lease_fence"],
    )

    if "work_scope_generation_streams" not in tables:
        op.create_table(
            "work_scope_generation_streams",
            sa.Column("scope_key", sa.String(), nullable=False),
            sa.Column("cache_epoch", sa.String(), nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("event_store_id", sa.String(), server_default="intaris", nullable=False),
            sa.Column("event_store_session_id", sa.String(), nullable=False),
            sa.Column("workstream_key", sa.String(), nullable=False),
            sa.Column("workstream_kind", sa.String(), nullable=False),
            sa.Column("parent_key", sa.String(), nullable=True),
            sa.Column("root_key", sa.String(), nullable=False),
            sa.Column("edge_kind", sa.String(), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("activity_scope_id", sa.String(), nullable=True),
            sa.Column("presentation", sa.JSON(), server_default="{}", nullable=False),
            sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "ordinal >= 0 AND last_seq >= 0",
                name="ck_work_scope_generation_streams_nonnegative",
            ),
            sa.ForeignKeyConstraint(
                ["scope_key", "cache_epoch"],
                ["work_scope_generations.scope_key", "work_scope_generations.cache_epoch"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("scope_key", "cache_epoch", "session_id"),
        )
    _create_index_if_missing(
        "work_scope_generation_streams",
        "ix_work_scope_generation_streams_root",
        ["scope_key", "cache_epoch", "root_key", "ordinal"],
    )
    _create_index_if_missing(
        "work_scope_generation_streams",
        "ix_work_scope_generation_streams_event",
        ["event_store_id", "event_store_session_id", "scope_key", "cache_epoch"],
    )
    _create_index_if_missing(
        "work_scope_generation_streams",
        "ix_work_scope_generation_streams_session",
        ["session_id", "scope_key"],
    )
    _create_index_if_missing(
        "work_scope_generation_streams",
        "ix_work_scope_generation_streams_activity",
        ["activity_scope_id", "scope_key", "cache_epoch"],
    )

    if "work_current_files" not in tables:
        op.create_table(
            "work_current_files",
            sa.Column("current_file_id", sa.String(), nullable=False),
            sa.Column("owner_email", sa.String(), nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("materializer_version", sa.String(), nullable=False),
            sa.Column("file_projector_version", sa.String(), nullable=False),
            sa.Column("path_generation_id", sa.String(), nullable=False),
            sa.Column("source_store", sa.String(), server_default="intaris", nullable=False),
            sa.Column("source_session_id", sa.String(), nullable=False),
            sa.Column("source_seq", sa.BigInteger(), nullable=False),
            sa.Column("source_event_id", sa.String(), nullable=True),
            sa.Column("source_item_id", sa.String(), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("path_id", sa.String(), nullable=False),
            sa.Column("relative_path", sa.Text(), nullable=True),
            sa.Column("root_id", sa.String(), nullable=True),
            sa.Column("root_label", sa.String(), nullable=True),
            sa.Column("previous_path", sa.Text(), nullable=True),
            sa.Column("previous_path_id", sa.String(), nullable=True),
            sa.Column("rename_ordinal", sa.Integer(), server_default="0", nullable=False),
            sa.Column("recreate_ordinal", sa.Integer(), server_default="0", nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("binary", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("generated", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("preview", sa.Text(), nullable=True),
            sa.Column("preview_size", sa.Integer(), server_default="0", nullable=False),
            sa.Column("preview_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("preview_omitted", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("content_hash", sa.String(), nullable=True),
            sa.Column("content_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("content_scrubbed_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "source_seq >= 0 AND rename_ordinal >= 0 AND recreate_ordinal >= 0 "
                "AND preview_size >= 0",
                name="ck_work_current_files_nonnegative",
            ),
            sa.ForeignKeyConstraint(["owner_email"], ["users.email"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("current_file_id"),
            sa.UniqueConstraint(
                "owner_email",
                "session_id",
                "materializer_version",
                "file_projector_version",
                "path_generation_id",
                name="uq_work_current_files_upsert_identity",
            ),
        )
    _create_index_if_missing(
        "work_current_files",
        "ix_work_current_files_session_root_path",
        [
            "owner_email",
            "session_id",
            "materializer_version",
            "file_projector_version",
            "root_id",
            "path_id",
        ],
    )
    _create_index_if_missing(
        "work_current_files",
        "ix_work_current_files_source",
        ["source_store", "source_session_id", "source_seq"],
    )
    _create_index_if_missing(
        "work_current_files",
        "ix_work_current_files_previous_path",
        ["previous_path_id", "session_id"],
    )
    _create_index_if_missing(
        "work_current_files",
        "ix_work_current_files_content_expiry",
        ["content_expires_at", "content_scrubbed_at"],
    )


def downgrade() -> None:
    op.drop_table("work_current_files")
    op.drop_table("work_scope_generation_streams")
    op.drop_table("work_scope_generations")

    with op.batch_alter_table("work_session_projections") as batch:
        batch.drop_constraint("ck_work_session_projections_nonnegative", type_="check")
        batch.create_check_constraint(
            "ck_work_session_projections_nonnegative",
            "target_seq >= 0 AND covered_through_seq >= 0 AND retry_count >= 0 "
            "AND lease_fence >= 0",
        )
        for name in (
            "expected_record_file_count",
            "expected_record_count",
            "omitted_file_count",
            "deletions",
            "additions",
            "deliverable_count",
            "artifact_count",
            "file_count",
            "command_count",
            "mutation_count",
            "evidence_record_count",
            "record_count",
            "next_head_check_at",
        ):
            batch.drop_column(name)

    op.drop_index("ix_work_scope_states_lease", table_name="work_scope_states")
    op.drop_index("ix_work_scope_states_recovery", table_name="work_scope_states")
    with op.batch_alter_table("work_scope_states") as batch:
        batch.drop_constraint("ck_work_scope_states_cache_nonnegative", type_="check")
        batch.drop_constraint("ck_work_scope_states_lifecycle", type_="check")
        for name in (
            "expected_current_file_count",
            "expected_record_count",
            "expected_projection_count",
            "expected_stream_count",
            "expected_generation_count",
            "reconciled_at",
            "reconciliation_requested_at",
            "reconciliation_count",
            "lease_fence",
            "lease_expires_at",
            "lease_owner",
            "next_retry_at",
            "last_error",
            "retry_count",
            "durable_cursor",
            "topology_complete",
            "topology_projector_version",
            "recovery_cache_epoch",
            "active_cache_epoch",
            "lifecycle_state",
        ):
            batch.drop_column(name)
