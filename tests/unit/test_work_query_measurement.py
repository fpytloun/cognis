from __future__ import annotations

import asyncio
import importlib.util
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine


def test_query_samples_include_request_children_but_exclude_lifespan_workers(tmp_path) -> None:
    source = Path(__file__).parents[1] / "integration/test_work_live_performance_postgres.py"
    spec = importlib.util.spec_from_file_location("work_query_measurement_fixture", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'query-scope.db'}")
    background_queries: list[str] = []

    def observed(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if statement == "SELECT 99":
            background_queries.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", observed)

    async def query(value: int) -> None:
        async with engine.connect() as connection:
            await connection.execute(sa.text(f"SELECT {value}"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        trigger = asyncio.Event()
        finished = asyncio.Event()
        app.state.trigger = trigger
        app.state.finished = finished

        async def background() -> None:
            while True:
                await trigger.wait()
                trigger.clear()
                for _ in range(5):
                    await query(99)
                finished.set()

        worker = asyncio.create_task(background())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.engine = engine
    app.state.auth_provider = SimpleNamespace(
        sign_access_token=lambda *_args: "synthetic-query-scope-token"
    )

    @app.middleware("http")
    async def authentication_queries(request: Request, call_next):
        await query(1)
        return await call_next(request)

    @app.get("/measured")
    async def measured(request: Request) -> dict[str, bool]:
        await query(2)
        await asyncio.create_task(query(3))
        request.app.state.trigger.set()
        await request.app.state.finished.wait()
        request.app.state.finished.clear()
        return {"ok": True}

    with TestClient(app) as client:
        _timings, counts, _slowest, _plans = module._samples(client, "/measured")

    # Authentication, handler and request-owned child SQL are all required.
    assert counts == [3] * 7
    assert len(background_queries) == 5 * (2 + 7)
