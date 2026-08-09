from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import replace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.bootstrap import (
    _lock_initial_admin_seed,
    maybe_seed_initial_admin,
    seed_builtin_management_skills,
    seed_default_settings,
    seed_system_agents,
)
from cognis.config import load_config
from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.core.system_skills import SYSTEM_SKILL_DEFAULTS
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.settings_schema import DEFAULT_SETTINGS
from cognis.store.database import create_session_factory
from cognis.store.models import Agent, ExecutorRow, Setting, SkillRow, SkillVersionRow, User
from cognis.store.queries import ensure_default_executor, upsert_setting

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


def _url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://")
        else url
    )


def _upgrade(sync_connection: Any) -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", _url())
    config.config_file_name = None
    config.attributes["connection"] = sync_connection
    command.upgrade(config, "head")


async def _seed(factory: Any) -> None:
    async with factory() as session:
        await seed_default_settings(session)
        await seed_system_agents(session)
        await seed_builtin_management_skills(session)
        await session.commit()


class _Hasher:
    def hash(self, password: str) -> str:
        return f"hashed:{password}"


@pytest.mark.asyncio
async def test_concurrent_controller_startup_seeds_are_idempotent() -> None:
    url = _url()
    schema_name = f"cognis_bootstrap_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_upgrade)
        factory = create_session_factory(engine)

        await asyncio.gather(_seed(factory), _seed(factory))

        async with factory() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Setting)) == len(
                DEFAULT_SETTINGS
            )
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(User)
                    .where(User.email == SYSTEM_USER_EMAIL)
                )
                == 1
            )

        async def _seed_admin(email: str) -> None:
            config = replace(
                load_config(),
                initial_admin_email=email,
                initial_admin_password="password",
            )
            async with factory() as session:
                await _lock_initial_admin_seed(session, config)
                await maybe_seed_initial_admin(session, config, _Hasher())
                await session.commit()

        await asyncio.gather(
            _seed_admin("admin-a@example.com"),
            _seed_admin("admin-b@example.com"),
        )
        async with factory() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count()).select_from(User).where(User.role == "admin")
                )
                == 1
            )
            assert await session.scalar(
                sa.select(sa.func.count()).select_from(Agent).where(Agent.is_system.is_(True))
            ) == len(SYSTEM_AGENTS)
            assert await session.scalar(
                sa.select(sa.func.count()).select_from(SkillRow).where(SkillRow.is_system.is_(True))
            ) == len(SYSTEM_SKILL_DEFAULTS)
            assert await session.scalar(
                sa.select(sa.func.count()).select_from(SkillVersionRow)
            ) == len(SYSTEM_SKILL_DEFAULTS)

        async def _upsert(value: int) -> None:
            async with factory() as session:
                await upsert_setting(
                    session,
                    "session.compaction_threshold",
                    value,
                    "session",
                )
                await session.commit()

        await asyncio.gather(_upsert(111), _upsert(222))
        async with factory() as session:
            value = await session.scalar(
                sa.select(Setting.value).where(Setting.key == "session.compaction_threshold")
            )
            assert value in {111, 222}

        async def _ensure_executor() -> str:
            async with factory() as session:
                row = await ensure_default_executor(session)
                await session.commit()
                return row.executor_id

        assert await asyncio.gather(_ensure_executor(), _ensure_executor()) == [
            "default_inprocess",
            "default_inprocess",
        ]
        async with factory() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(ExecutorRow)
                    .where(ExecutorRow.executor_id == "default_inprocess")
                )
                == 1
            )
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()
