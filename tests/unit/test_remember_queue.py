from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.core.events import EventBus, EventType
from cognis.core.remember_queue import RememberRetryQueue
from cognis.store.models import Base, RememberQueueRow, User
from cognis.store.queries import create_conversation, create_session


class _Worker:
    def __init__(self) -> None:
        self.calls = 0
        self.last_kwargs: dict[str, object] | None = None

    async def remember(self, **kwargs: object) -> None:
        self.calls += 1
        self.last_kwargs = dict(kwargs)


class _EventReader:
    def __init__(self) -> None:
        self.recorded_events: list[dict[str, object]] = []

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

    async def record_events(self, **kwargs: object) -> object:
        self.recorded_events.append(dict(kwargs))
        return type("AppendResult", (), {"ok": True, "count": 1, "first_seq": 1, "last_seq": 1})()


class _FailingWorker:
    def __init__(self, message: str = "mnemory unavailable api_key=secret-value") -> None:
        self.message = message
        self.calls = 0

    async def remember(self, **kwargs: object) -> None:
        del kwargs
        self.calls += 1
        raise RuntimeError(self.message)


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


@pytest.mark.asyncio
async def test_remember_queue_logs_and_records_system_notice_on_permanent_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'remember-queue-fail.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(User(email="user@example.com", password_hash="x", role="user"))
        conversation = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="chat",
            conversation_id="conv_test",
        )
        await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            session_id="sess_test",
            intaris_session_id="intaris_test",
            mnemory_session_id="mnemory_test",
        )
        await session.commit()

    event_reader = _EventReader()
    event_bus = EventBus()
    notices: list[dict[str, object]] = []

    async def _capture_notice(event: object) -> None:
        notices.append(event.data)  # type: ignore[attr-defined]

    event_bus.subscribe(EventType.SYSTEM_NOTICE, _capture_notice)
    worker = _FailingWorker()
    queue = RememberRetryQueue(
        worker,
        session_factory=session_factory,
        event_reader=event_reader,
        event_bus=event_bus,
        max_depth=10,
        max_concurrent=1,
    )
    queue.max_retries = 1

    with caplog.at_level("ERROR"):
        await queue.enqueue(
            {
                "session_id": "mnemory_test",
                "cognis_session_id": "sess_test",
                "intaris_session_id": "intaris_test",
                "user_email": "user@example.com",
                "agent_id": "agent-1",
                "messages": [{"role": "assistant", "content": "done"}],
            }
        )
        await queue.start()
        await asyncio.sleep(0.3)
        await queue.stop()

    assert worker.calls >= 1
    async with session_factory() as session:
        row = await session.scalar(sa.select(RememberQueueRow).limit(1))
        assert row is not None
        assert row.status == "failed"
        assert row.last_error is not None
        assert "secret-value" not in row.last_error
        assert "[redacted]" in row.last_error

    assert event_reader.recorded_events
    recorded = event_reader.recorded_events[0]
    assert recorded["session_id"] == "intaris_test"
    events = recorded["events"]
    assert len(events) == 1
    assert events[0].type == "lifecycle"
    assert events[0].data["event"] == "system_notice"
    assert notices
    assert notices[0]["conversation_id"] == "conv_test"
    assert "Background memory save failed after several retries" in str(notices[0]["message"])
    assert any(
        "Remember queue item failed permanently" in record.message for record in caplog.records
    )

    await engine.dispose()
