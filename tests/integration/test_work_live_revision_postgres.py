from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.store.database import create_session_factory
from cognis.store.models import Base
from cognis.store.queries import create_user
from cognis.store.work_live_invalidation import (
    bump_live_work_revision,
    read_live_work_revision,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


def _asyncpg_url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("COGNIS_TEST_POSTGRES_URL must use PostgreSQL with asyncpg")
    return url


@pytest.mark.asyncio
async def test_postgres_first_revision_is_atomic_and_transactional() -> None:
    url = _asyncpg_url()
    schema_name = f"cognis_work_revision_{uuid.uuid4().hex}"
    owner_email = "revision-owner@example.com"
    admin_engine = create_async_engine(url)
    async with admin_engine.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await create_user(
                session,
                email=owner_email,
                name="Revision Owner",
                password_hash="hash",
            )
            await session.commit()

        async with session_factory() as session:
            assert await bump_live_work_revision(session, owner_email) == 1
            await session.rollback()
        async with session_factory() as session:
            assert await read_live_work_revision(session, owner_email) == 0

        async def bump() -> int:
            async with session_factory() as session:
                revision = await bump_live_work_revision(session, owner_email)
                await session.commit()
                return revision

        assert set(await asyncio.gather(bump(), bump())) == {1, 2}
        async with session_factory() as session:
            assert await read_live_work_revision(session, owner_email) == 2
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()
