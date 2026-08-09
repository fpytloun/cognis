from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.core.controller_directory import ControllerInstanceDirectory
from cognis.core.controller_runtime import ControllerRuntime
from cognis.core.executor_connection_ownership import ExecutorConnectionOwnership
from cognis.providers.executor.websocket import WebSocketExecutorProvider
from cognis.store.database import create_session_factory
from cognis.store.models import Base, ControllerInstanceRow
from cognis.store.queries import create_executor

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
async def test_two_controller_postgres_remote_owner_discovery_and_epoch_change() -> None:
    url = _url()
    schema_name = f"cognis_executor_bridge_{uuid.uuid4().hex}"
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
            now = datetime.now(UTC)
            session.add(
                ControllerInstanceRow(
                    owner_id="controller-b:boot-b",
                    controller_id="controller-b",
                    incarnation_id="boot-b",
                    internal_url="http://controller-b.internal:8000",
                    lifecycle_state="ready",
                    heartbeat_at=now,
                    expires_at=now + timedelta(minutes=1),
                )
            )
            await session.commit()
        remote = ExecutorConnectionOwnership(factory, "controller-b:boot-b")
        first = await remote.takeover_validated("executor-1", token_version=0)
        assert first is not None

        runtime = ControllerRuntime("controller-a", incarnation_id="boot-a")
        provider = WebSocketExecutorProvider()
        directory = ControllerInstanceDirectory(factory, runtime, internal_url=None)
        await provider.configure_cluster(
            enabled=True,
            session_factory=factory,
            controller_directory=directory,
            controller_runtime=runtime,
            auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
        )
        first_proxy = provider.get_connection("executor-1")
        assert first_proxy is not None
        assert first_proxy.owner_id == "controller-b:boot-b"
        assert first_proxy.epoch == first.epoch

        second = await remote.takeover("executor-1")
        await provider.refresh_cluster_directory()
        second_proxy = provider.get_connection("executor-1")
        assert second_proxy is not None
        assert second_proxy is not first_proxy
        assert second_proxy.epoch == second.epoch
        await provider.cleanup()
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()
