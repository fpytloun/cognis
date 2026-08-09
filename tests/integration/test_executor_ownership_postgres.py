from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.core.executor_connection_ownership import ExecutorConnectionOwnership
from cognis.store.database import create_session_factory
from cognis.store.models import Base, ExecutorRow
from cognis.store.queries import create_executor, get_executor_row

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


@pytest.mark.asyncio
async def test_postgres_executor_takeover_fences_old_controller() -> None:
    url = _url()
    schema_name = f"cognis_executor_ownership_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        async with factory() as session:
            await create_executor(
                session,
                executor_id="executor-1",
                name="Executor",
                executor_type="websocket",
            )
            await session.commit()
        controller_a = ExecutorConnectionOwnership(factory, "controller-a:boot-a")
        controller_b = ExecutorConnectionOwnership(factory, "controller-b:boot-b")
        old = await controller_a.takeover("executor-1")
        new = await controller_b.takeover("executor-1")
        assert new.epoch > old.epoch

        async with factory() as session:
            stale = await controller_a.update_runtime_state(
                session,
                old,
                runtime_state="offline",
            )
            current = await controller_b.update_runtime_state(
                session,
                new,
                runtime_state="active",
            )
            await session.commit()
        assert stale is None
        assert current is not None
        async with factory() as session:
            row = await get_executor_row(session, "executor-1")
            assert row is not None and row.runtime_state == "active"

        async with factory() as admin_session:
            row = await admin_session.scalar(
                select(ExecutorRow).where(ExecutorRow.executor_id == "executor-1").with_for_update()
            )
            assert row is not None
            row.status = "inactive"
            await admin_session.flush()
            racing_takeover = asyncio.create_task(
                controller_a.takeover_validated("executor-1", token_version=0)
            )
            await asyncio.sleep(0.05)
            assert not racing_takeover.done()
            await admin_session.commit()
        assert await racing_takeover is None
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()
