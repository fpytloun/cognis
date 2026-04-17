from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.core.remember_queue import RememberRetryQueue
from cognis.store.models import Base, RememberQueueRow, User


class _Worker:
    def __init__(self) -> None:
        self.calls = 0
        self.last_kwargs: dict[str, object] | None = None

    async def remember(self, **kwargs: object) -> None:
        self.calls += 1
        self.last_kwargs = dict(kwargs)


class _EventReader:
    async def read_events(self, **kwargs: object) -> object:
        del kwargs
        return type(
            "EventRead",
            (),
            {
                "events": [
                    type(
                        "Event",
                        (),
                        {
                            "seq": 1,
                            "type": "user_message",
                            "data": {"content": "hi", "attachments": []},
                        },
                    )(),
                    type(
                        "Event",
                        (),
                        {
                            "seq": 2,
                            "type": "assistant_message",
                            "data": {"content": "done", "attachments": []},
                        },
                    )(),
                    type(
                        "Event",
                        (),
                        {
                            "seq": 3,
                            "type": "user_message",
                            "data": {"content": "newer user", "attachments": []},
                        },
                    )(),
                    type(
                        "Event",
                        (),
                        {
                            "seq": 4,
                            "type": "assistant_message",
                            "data": {"content": "newer assistant", "attachments": []},
                        },
                    )(),
                ]
            },
        )()


@pytest.mark.asyncio
async def test_remember_queue_processes_items() -> None:
    worker = _Worker()
    queue = RememberRetryQueue(worker, max_depth=10, max_concurrent=1)
    await queue.start()
    await queue.enqueue({"session_id": "s1", "messages": []})
    await asyncio.sleep(0.3)
    await queue.stop()

    assert worker.calls >= 1


@pytest.mark.asyncio
async def test_remember_queue_replays_persisted_items_after_restart(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'remember-queue.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(User(email="user@example.com", password_hash="x", role="user"))
        await session.commit()

    worker = _Worker()
    queue = RememberRetryQueue(
        worker,
        session_factory=session_factory,
        event_reader=_EventReader(),
        max_depth=10,
        max_concurrent=1,
    )
    await queue.enqueue(
        {
            "session_id": "s1",
            "intaris_session_id": "intaris-s1",
            "user_email": "user@example.com",
            "agent_id": "agent-1",
            "user_event_seq": 1,
            "assistant_event_seq": 2,
        }
    )

    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(RememberQueueRow))
        assert count == 1

    replay_queue = RememberRetryQueue(
        worker,
        session_factory=session_factory,
        event_reader=_EventReader(),
        max_depth=10,
        max_concurrent=1,
    )
    await replay_queue.start()
    await asyncio.sleep(0.4)
    await replay_queue.stop()

    assert worker.calls >= 1
    assert worker.last_kwargs is not None
    assert worker.last_kwargs["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ]
    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(RememberQueueRow))
        assert count == 0

    await engine.dispose()
