from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from cognis.store.models import Base

CONTROL_TABLES = {"alembic_version"}
LEGACY_EXTRA_COLUMNS = {("deliverables", "outputs")}
LEGACY_EXTRA_INDEXES = {
    ("conversations", "ix_conversations_project_id"),
    ("llm_providers", "ix_llm_providers_owner_email"),
    ("model_routing", "ix_model_routing_owner_email"),
    ("schedules", "ix_schedules_project_id"),
    ("step_runs", "ix_step_runs_superseded_by"),
    ("step_runs", "ix_step_runs_task_id"),
    ("tasks", "ix_tasks_agent_id"),
    ("tasks", "ix_tasks_created_by"),
    ("tasks", "ix_tasks_project_id"),
    ("tasks", "ix_tasks_status_priority"),
}
SEMANTIC_UNIQUE_INDEX_EQUIVALENTS = {("llm_providers", "uq_llm_providers_managed_local_key")}
DIALECT_FK_OVERRIDES = {("browser_sessions", ("user_email",), "users", ("email",), None, "CASCADE")}


def _column_names(value: Any) -> tuple[str, ...]:
    return tuple(str(column) for column in value or ())


def _expected_index_signatures(
    table: sa.Table, dialect_name: str
) -> set[tuple[tuple[str, ...], bool, bool]]:
    return {
        (
            tuple(column.name for column in index.columns),
            bool(index.unique),
            index.dialect_options[dialect_name].get("where") is not None,
        )
        for index in table.indexes
    }


def _actual_index_signatures(
    inspector: sa.Inspector, table_name: str
) -> set[tuple[tuple[str, ...], bool, bool]]:
    dialect_name = inspector.bind.dialect.name
    indexes = {
        (
            _column_names(index["column_names"]),
            bool(index.get("unique")),
            index.get("dialect_options", {}).get(f"{dialect_name}_where") is not None,
        )
        for index in inspector.get_indexes(table_name)
        if index.get("column_names")
    }
    indexes.update(
        (_column_names(constraint["column_names"]), True, False)
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("column_names")
    )
    return indexes


def _expected_foreign_keys(
    table: sa.Table,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str | None]]:
    return {
        (
            tuple(element.parent.name for element in constraint.elements),
            constraint.referred_table.name,
            tuple(element.column.name for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in table.foreign_key_constraints
    }


def _actual_foreign_keys(
    inspector: sa.Inspector, table_name: str
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str | None]]:
    return {
        (
            _column_names(constraint["constrained_columns"]),
            str(constraint["referred_table"]),
            _column_names(constraint["referred_columns"]),
            constraint.get("options", {}).get("ondelete"),
        )
        for constraint in inspector.get_foreign_keys(table_name)
    }


def _type_affinity(column_type: sa.types.TypeEngine[Any]) -> type[Any]:
    return column_type._type_affinity  # noqa: SLF001 - SQLAlchemy comparison API


def assert_schema_matches_metadata(connection: Connection) -> None:
    inspector = sa.inspect(connection)
    expected_tables = set(Base.metadata.tables)
    actual_tables = set(inspector.get_table_names()) - CONTROL_TABLES
    assert actual_tables == expected_tables

    observed_extra_columns: set[tuple[str, str]] = set()
    observed_extra_indexes: set[tuple[str, str]] = set()
    for table_name in sorted(expected_tables):
        table = Base.metadata.tables[table_name]
        actual_columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        expected_columns = {column.name: column for column in table.columns}

        assert set(expected_columns) <= set(actual_columns), table_name
        observed_extra_columns.update(
            (table_name, column_name) for column_name in set(actual_columns) - set(expected_columns)
        )
        for column_name, expected in expected_columns.items():
            actual = actual_columns[column_name]
            assert _type_affinity(actual["type"]) is _type_affinity(expected.type), (
                table_name,
                column_name,
                actual["type"],
                expected.type,
            )
            if not expected.primary_key:
                assert bool(actual["nullable"]) is bool(expected.nullable), (
                    table_name,
                    column_name,
                )

        actual_indexes = _actual_index_signatures(inspector, table_name)
        assert _expected_index_signatures(table, inspector.bind.dialect.name) <= actual_indexes, (
            table_name
        )

        expected_index_names = {index.name for index in table.indexes}
        expected_unique_names = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        observed_extra_indexes.update(
            (table_name, str(index["name"]))
            for index in inspector.get_indexes(table_name)
            if index.get("name")
            and index["name"] not in expected_index_names
            and index["name"] not in expected_unique_names
            and not index.get("duplicates_constraint")
            and (table_name, str(index["name"])) not in SEMANTIC_UNIQUE_INDEX_EQUIVALENTS
        )

        actual_pk = _column_names(inspector.get_pk_constraint(table_name)["constrained_columns"])
        expected_pk = tuple(column.name for column in table.primary_key.columns)
        assert actual_pk == expected_pk, table_name
        expected_foreign_keys = _expected_foreign_keys(table)
        actual_foreign_keys = _actual_foreign_keys(inspector, table_name)
        missing_foreign_keys = expected_foreign_keys - actual_foreign_keys
        assert all(
            (
                table_name,
                constrained_columns,
                referred_table,
                referred_columns,
                expected_ondelete,
                next(
                    (
                        actual_ondelete
                        for (
                            actual_columns,
                            actual_table,
                            actual_referred_columns,
                            actual_ondelete,
                        ) in actual_foreign_keys
                        if actual_columns == constrained_columns
                        and actual_table == referred_table
                        and actual_referred_columns == referred_columns
                    ),
                    None,
                ),
            )
            in DIALECT_FK_OVERRIDES
            for (
                constrained_columns,
                referred_table,
                referred_columns,
                expected_ondelete,
            ) in missing_foreign_keys
        ), (table_name, missing_foreign_keys)

    assert observed_extra_columns == LEGACY_EXTRA_COLUMNS
    assert observed_extra_indexes == LEGACY_EXTRA_INDEXES, (
        observed_extra_indexes,
        LEGACY_EXTRA_INDEXES,
    )
