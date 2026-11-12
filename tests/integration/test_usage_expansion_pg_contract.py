"""No-service guards and real-metadata compilation; not PostgreSQL qualification."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from cognis.core.usage.access import AuthorizedUsageScope, UsageInvocation
from cognis.core.usage.query_models import UsageSummaryRequest
from cognis.store.models import Base
from cognis.store.usage.models import USAGE_TABLE_NAMES
from cognis.store.usage.schema import ensure_usage_schema
from cognis.store.usage_membership import membership
from tests.integration import test_usage_expansion_postgres as pg_cases


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+asyncpg://usage_test@/usage_expansion?host=/run/cognis-usage-pg",
        "postgresql://usage_test@127.0.0.1:5432/usage_expansion",
        "postgresql+asyncpg://usage_test@[::1]:5432/usage_expansion",
    ],
)
def test_private_runtime_url_contract_without_connecting(value: str) -> None:
    url = pg_cases.isolated_postgres_url(value, admitted=True)
    assert url.drivername == "postgresql+asyncpg"
    assert url.password is None and url.database == "usage_expansion"


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+asyncpg://usage_test@127.0.0.1:32823/usage_expansion",
        "postgresql+asyncpg://usage_test@127.0.0.1:5433/usage_expansion",
        "postgresql+asyncpg://usage_test@host.docker.internal/usage_expansion",
        "postgresql+asyncpg://usage_test@localhost/usage_expansion",
        "postgresql+asyncpg://usage_test@192.0.2.1/usage_expansion",
        "postgresql+asyncpg://other_user@127.0.0.1/usage_expansion",
        "postgresql+asyncpg://usage_test@127.0.0.1/other_database",
        "postgresql+asyncpg://usage_test:unadmitted-password@127.0.0.1/usage_expansion",
        "postgresql+asyncpg://usage_test@/usage_expansion?host=/run/postgresql",
        "postgresql+asyncpg://usage_test@127.0.0.1/usage_expansion?host=/run/cognis-usage-pg",
        "postgresql+asyncpg://usage_test@127.0.0.1/usage_expansion?service=ambient",
        "sqlite+aiosqlite:///usage_expansion",
    ],
)
def test_published_remote_or_inherited_fixture_is_refused_without_connecting(value: str) -> None:
    with pytest.raises(ValueError):
        pg_cases.isolated_postgres_url(value, admitted=True)


def test_explicit_namespace_admission_marker_is_required_without_connecting() -> None:
    with pytest.raises(ValueError, match="admission"):
        pg_cases.isolated_postgres_url(
            "postgresql+asyncpg://usage_test@127.0.0.1/usage_expansion",
            admitted=False,
        )


def test_actual_core_and_usage_schema_metadata_compile_for_postgres() -> None:
    core = pg_cases.core_tables()
    assert core and {table.name for table in core}.isdisjoint(USAGE_TABLE_NAMES)
    assert all(foreign.column.table in core for table in core for foreign in table.foreign_keys)
    assert {
        "users",
        "agents",
        "agent_grants",
        "user_ui_state",
        pg_cases.WorkLiveRevisionRow.__table__.name,
        "projects",
        "project_grants",
        "conversations",
        "sessions",
        "managed_conversation_links",
        "tasks",
        "step_runs",
    } <= {table.name for table in core}
    tables = [*core, *(Base.metadata.tables[name] for name in USAGE_TABLE_NAMES)]
    for table in tables:
        assert str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect()))
        for index in table.indexes:
            assert str(sa.schema.CreateIndex(index).compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("kind", ["conversation", "execution"])
@pytest.mark.parametrize("inclusion", ["direct", "inclusive"])
def test_actual_membership_statement_compiles_without_connecting(
    kind: str,
    inclusion: str,
) -> None:
    query = UsageSummaryRequest.model_validate(
        {
            "items": [{"kind": kind, "id": "focused_resource"}],
            "inclusion": inclusion,
        }
    )
    scope = AuthorizedUsageScope(
        actor=pg_cases.ACTOR,
        mode="personal",
        project_id=None,
        consumer=pg_cases.ACTOR,
        invocation=None,
    )
    statement = sa.select(membership(query.items, query.inclusion, scope))
    assert str(statement.compile(dialect=postgresql.dialect()))


def test_fixture_uses_actual_schema_owner_and_public_constructor_arguments() -> None:
    source = Path(pg_cases.__file__).read_text()
    tree = ast.parse(source)
    fixture = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "pg_accounting"
    )
    assert pg_cases.ensure_usage_schema is ensure_usage_schema
    # This is the actual setup await, not a dead marker added to satisfy a source guard.
    assert any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "run_sync"
        and any(
            isinstance(argument, ast.Name) and argument.id == "ensure_usage_schema"
            for argument in node.value.args
        )
        for node in ast.walk(fixture)
    )
    owners = {
        name: getattr(pg_cases, name)
        for name in (
            "create_user",
            "create_agent",
            "create_agent_grant",
            "create_project",
            "create_project_grant",
            "create_conversation",
            "create_session",
        )
    }
    owners["UsageInvocation"] = UsageInvocation
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            owner = owners.get(node.func.id)
            if owner is not None:
                signature = inspect.signature(owner)
                assert {
                    argument.arg for argument in node.keywords if argument.arg is not None
                } <= signature.parameters.keys(), (node.func.id, node.lineno)
