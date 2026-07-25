"""Compatibility repairs that must run before Alembic loads migration state."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import inspect, text

LEGACY_PROFILE_OVERRIDE_REVISION = "082_system_agent_profile_overrides"
PROFILE_OVERRIDE_REVISION = "082_sys_agent_profile_overrides"
LEGACY_LOCAL_MODEL_REVISIONS = frozenset(
    {
        "083_local_model_foundation",
        "084_local_model_runtime",
        "085_local_model_capacity_bigint",
    }
)


def normalize_legacy_profile_override_revision(sync_conn: object) -> bool:
    """Canonicalize withdrawn revisions before Alembic reads migration state.

    PostgreSQL's default ``alembic_version.version_num`` is ``VARCHAR(32)``,
    so a failed upgrade remains at 081 and follows the canonical migration.
    SQLite/development databases or manually widened PostgreSQL databases can
    contain the former long revision; make its schema idempotently complete
    before replacing its stamp with the short canonical revision. Feature
    branch databases stamped with the former local-model revisions also rewind
    to the canonical 082 point so main's 083-086 migrations run before the
    renumbered local-model chain.
    """

    connection = cast(Any, sync_conn)
    inspector = inspect(connection)
    if "alembic_version" not in inspector.get_table_names():
        return False
    version = connection.execute(
        text("SELECT version_num FROM alembic_version LIMIT 1")
    ).scalar_one_or_none()
    if version not in {LEGACY_PROFILE_OVERRIDE_REVISION, *LEGACY_LOCAL_MODEL_REVISIONS}:
        return False

    if (
        version == LEGACY_PROFILE_OVERRIDE_REVISION
        and "system_agent_overrides" in inspector.get_table_names()
    ):
        columns = {column["name"] for column in inspector.get_columns("system_agent_overrides")}
        if "agent_profiles_override" not in columns:
            connection.execute(
                text("ALTER TABLE system_agent_overrides ADD COLUMN agent_profiles_override JSON")
            )
        if "default_agent_profile_id_override" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE system_agent_overrides "
                    "ADD COLUMN default_agent_profile_id_override VARCHAR"
                )
            )

    connection.execute(
        text("UPDATE alembic_version SET version_num = :canonical WHERE version_num = :legacy"),
        {
            "canonical": PROFILE_OVERRIDE_REVISION,
            "legacy": version,
        },
    )
    return True
