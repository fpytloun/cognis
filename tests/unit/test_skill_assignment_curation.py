"""Skill search, agent auto-attachment, and per-agent usage curation."""

from __future__ import annotations

import importlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_session_factory
from cognis.store.models import Agent, AgentSkillUsageRow
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_skill,
    create_user,
    get_agent,
)
from cognis.store.skill_usage import record_skill_load, skill_usage_payload
from cognis.tools.builtin.skill_management import handle_skill_management_tool
from cognis.tools.skills import agent_skill_usage_report
from tests.schema_parity import (
    _actual_foreign_keys,
    _actual_index_signatures,
    _expected_foreign_keys,
    _expected_index_signatures,
)

OWNER = "owner@example.com"
OTHER = "other@example.com"


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def engine(request, tmp_path):
    schema = f"skill_curation_{uuid.uuid4().hex}"
    admin = None
    if request.param == "postgresql":
        url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(schema))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": f'"{schema}"'}}
        )
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'skills.db'}")
    try:
        await run_schema_bootstrap(engine)
        yield engine
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(sa.schema.DropSchema(schema, cascade=True))
            await admin.dispose()


async def _seed(engine: Any) -> Any:
    factory = create_session_factory(engine)
    async with factory() as session:
        for email in (OWNER, OTHER):
            await create_user(session, email=email, name=email, password_hash="test")
        await create_agent(session, agent_id="agent-a", owner_email=OWNER, name="A")
        await create_agent(
            session,
            agent_id="agent-b",
            owner_email=OWNER,
            name="B",
            skills={"items": [{"skill_id": "kept", "enabled": False}]},
        )
        await create_agent(session, agent_id="foreign", owner_email=OTHER, name="Foreign")
        await create_skill(
            session,
            skill_id="kubernetes-ops",
            name="Kubernetes Ops",
            description="Debug pods and cluster rollouts.",
            instructions="Inspect pods first.",
            tags=["k8s"],
            owner_email=OWNER,
        )
        await create_skill(
            session,
            skill_id="invoice-writer",
            name="Invoice Writer",
            description="Generate monthly invoices.",
            instructions="Use the invoice template.",
            owner_email=OWNER,
        )
        await create_skill(
            session,
            skill_id="kept",
            name="Kept",
            description="Disabled on agent B.",
            instructions="Kept.",
            owner_email=OWNER,
        )
        await create_skill(
            session,
            skill_id="private-other",
            name="Kubernetes Private",
            description="Another user's pods skill.",
            instructions="Private.",
            owner_email=OTHER,
        )
        await session.commit()
    return factory


async def _call(
    factory: Any,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    agent_id: str | None = "agent-a",
    session_id: str | None = None,
    user_email: str = OWNER,
    artifact_store: ArtifactStore | None = None,
) -> Any:
    result = await handle_skill_management_tool(
        tool_name,
        arguments,
        factory,
        user_email,
        artifact_store=artifact_store,
        current_agent_id=agent_id,
        session_id=session_id,
    )
    assert result.is_error is False, result.output
    return json.loads(result.output)


async def _skill_items(factory: Any, agent_id: str) -> list[dict[str, Any]]:
    async with factory() as session:
        agent = await get_agent(session, agent_id)
    assert agent is not None
    return list((agent.skills or {}).get("items") or [])


async def _usage(factory: Any, agent_id: str, skill_id: str) -> tuple[int, int] | None:
    async with factory() as session:
        row = await session.get(AgentSkillUsageRow, (agent_id, skill_id))
    return None if row is None else (row.load_count, row.search_load_count)


@pytest.mark.asyncio
async def test_skill_search_returns_only_selection_fields_for_visible_skills(engine) -> None:
    factory = await _seed(engine)

    by_description = await _call(factory, "skill_search", {"query": "pods"})
    by_tag = await _call(factory, "skill_search", {"query": "k8s"})

    expected = {
        "skills": [
            {
                "skill_id": "kubernetes-ops",
                "name": "Kubernetes Ops",
                "description": "Debug pods and cluster rollouts.",
            }
        ]
    }
    assert by_description == expected
    assert by_tag == expected


@pytest.mark.asyncio
async def test_loads_count_per_agent_and_attribute_search_hits(engine) -> None:
    factory = await _seed(engine)

    await _call(factory, "skill_search", {"query": "invoices"})
    await _call(factory, "skill_load", {"skill_id": "invoice-writer"})
    await _call(factory, "skill_load", {"skill_id": "invoice-writer"})
    await _call(factory, "skill_load", {"skill_id": "kubernetes-ops"}, agent_id="agent-b")

    assert await _usage(factory, "agent-a", "invoice-writer") == (2, 1)
    assert await _usage(factory, "agent-b", "kubernetes-ops") == (1, 0)
    assert await _usage(factory, "agent-a", "kubernetes-ops") is None
    details = await _call(factory, "skill_get", {"skill_id": "invoice-writer"})
    assert details["agent_usage"]["agent_id"] == "agent-a"
    assert details["agent_usage"]["assignment"] == "unattached"
    assert details["agent_usage"]["load_count"] == 2
    assert details["agent_usage"]["search_load_count"] == 1


@pytest.mark.asyncio
async def test_agent_created_and_edited_skills_attach_to_the_persistent_agent(
    engine, tmp_path
) -> None:
    factory = await _seed(engine)
    artifacts = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    async with factory() as session:
        await create_conversation(
            session,
            conversation_id="conv-b",
            agent_id="agent-b",
            user_email=OWNER,
            context_type="web",
        )
        parent = await create_session(session, "conv-b", OWNER, "agent-b")
        child = await create_session(
            session,
            "conv-b",
            OWNER,
            "system:implement",
            parent_session_id=parent.session_id,
            delegation_mode="delegate",
        )
        await session.commit()

    created = await _call(
        factory,
        "skill_write",
        {"name": "Release Notes", "instructions": "Write release notes."},
        artifact_store=artifacts,
    )
    skill_id = created["skill_id"]
    await _call(
        factory,
        "skill_patch",
        {"skill_id": skill_id, "description": "Draft release notes."},
        artifact_store=artifacts,
    )
    await _call(
        factory,
        "skill_patch",
        {"skill_id": skill_id, "instructions": "Write concise release notes."},
        agent_id="system:implement",
        session_id=child.session_id,
        artifact_store=artifacts,
    )
    await _call(
        factory,
        "skill_patch",
        {"skill_id": skill_id, "instructions": "Write short release notes."},
        agent_id="foreign",
        artifact_store=artifacts,
    )

    assert await _skill_items(factory, "agent-a") == [{"skill_id": skill_id, "enabled": True}]
    assert await _skill_items(factory, "agent-b") == [
        {"skill_id": "kept", "enabled": False},
        {"skill_id": skill_id, "enabled": True},
    ]
    assert await _skill_items(factory, "foreign") == []


@pytest.mark.asyncio
async def test_usage_report_flags_never_loaded_manual_attachment(engine) -> None:
    factory = await _seed(engine)
    old = datetime.now(UTC) - timedelta(days=45)
    async with factory() as session:
        await session.execute(
            sa.update(Agent)
            .where(Agent.agent_id == "agent-a")
            .values(
                skills={"items": [{"skill_id": "invoice-writer", "enabled": True}]}, updated_at=old
            )
        )
        await record_skill_load(session, "agent-b", "kubernetes-ops", at=old)
        await session.commit()
        agent = await get_agent(session, "agent-a")
        assert agent is not None
        report = await agent_skill_usage_report(session, agent=agent, owner_email=OWNER)

    assert report == [
        {
            "skill_id": "invoice-writer",
            "name": "Invoice Writer",
            "assignment": "attached",
            "load_count": 0,
            "search_load_count": 0,
            "last_loaded_at": None,
            "suggestion": "consider_removing",
        }
    ]


def test_usage_suggestions_follow_assignment_and_age() -> None:
    now = datetime(2026, 11, 4, tzinfo=UTC)
    stale = AgentSkillUsageRow(
        load_count=4,
        search_load_count=0,
        last_loaded_at=now - timedelta(days=31),
        created_at=now - timedelta(days=90),
    )
    recent = AgentSkillUsageRow(
        load_count=1, search_load_count=0, last_loaded_at=now, created_at=now
    )
    found = AgentSkillUsageRow(
        load_count=3,
        search_load_count=3,
        last_loaded_at=now,
        created_at=now - timedelta(days=2),
    )

    old = now - timedelta(days=60)

    def suggestion(
        row: AgentSkillUsageRow | None,
        assignment: str,
        *,
        agent_updated_at: datetime = old,
        tracking_started: datetime | None = old,
    ) -> str | None:
        return skill_usage_payload(
            row,
            assignment=assignment,
            agent_updated_at=agent_updated_at,
            tracking_started=tracking_started,
            now=now,
        )["suggestion"]

    assert suggestion(stale, "attached") == "consider_removing"
    assert suggestion(recent, "attached") is None
    assert suggestion(stale, "global") is None
    assert suggestion(stale, "auto_loaded") is None
    assert suggestion(found, "unattached") == "consider_attaching"
    assert suggestion(recent, "unattached") is None
    # A manually attached skill that was never loaded or searched.
    assert suggestion(None, "attached") == "consider_removing"
    assert suggestion(None, "attached", tracking_started=None) is None
    # The agent changed recently, so the attachment may be new.
    assert suggestion(None, "attached", agent_updated_at=now - timedelta(days=3)) is None
    assert suggestion(stale, "attached", agent_updated_at=now - timedelta(days=3)) is None


def _assert_usage_schema_matches_metadata(connection: Any) -> None:
    table = AgentSkillUsageRow.__table__
    inspector = sa.inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns(table.name)}
    assert set(columns) == {column.name for column in table.columns}
    for expected in table.columns:
        assert columns[expected.name]["nullable"] == expected.nullable
        assert columns[expected.name]["type"]._type_affinity is expected.type._type_affinity
    assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
        "agent_id",
        "skill_id",
    ]
    assert _actual_foreign_keys(inspector, table.name) == _expected_foreign_keys(table)
    assert _actual_index_signatures(inspector, table.name) == _expected_index_signatures(
        table, connection.dialect.name
    )


@pytest.mark.asyncio
async def test_usage_bootstrap_and_migration_are_idempotent_and_match(engine, monkeypatch) -> None:
    factory = await _seed(engine)
    await _call(factory, "skill_load", {"skill_id": "invoice-writer"})
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    migration = importlib.import_module("cognis.store.migrations.versions.188_agent_skill_usage")

    def migrate(connection: Any, *steps: str) -> None:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        for step in steps:
            getattr(migration, step)()

    def check(connection: Any, rows: int) -> None:
        _assert_usage_schema_matches_metadata(connection)
        assert connection.scalar(sa.select(sa.func.count()).select_from(AgentSkillUsageRow)) == rows

    async with engine.begin() as connection:
        # Upgrading a bootstrapped database keeps recorded usage.
        await connection.run_sync(migrate, "upgrade")
        await connection.run_sync(check, 1)
        await connection.run_sync(migrate, "downgrade")
    # Bootstrap recreates the table on the previous schema.
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    async with engine.begin() as connection:
        await connection.run_sync(check, 0)
        await connection.run_sync(migrate, "downgrade", "upgrade", "upgrade")
        await connection.run_sync(check, 0)
