from __future__ import annotations

import queue
from unittest.mock import MagicMock

import pytest

from cognis.tools.executor.web import extraction_process


@pytest.mark.asyncio
async def test_process_queue_startup_failure_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    context = MagicMock()
    context.Queue.side_effect = OSError("resource exhausted")
    monkeypatch.setattr(extraction_process.multiprocessing, "get_context", lambda _: context)

    with pytest.raises(RuntimeError, match="failed to start"):
        await extraction_process._run_process(lambda: None, (), timeout=0.1)


@pytest.mark.asyncio
async def test_process_start_failure_closes_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    context = MagicMock()
    result_queue = MagicMock()
    process = MagicMock()
    process.start.side_effect = OSError("cannot fork")
    context.Queue.return_value = result_queue
    context.Process.return_value = process
    monkeypatch.setattr(extraction_process.multiprocessing, "get_context", lambda _: context)

    with pytest.raises(RuntimeError, match="failed to start"):
        await extraction_process._run_process(lambda: None, (), timeout=0.1)

    result_queue.close.assert_called_once()


@pytest.mark.asyncio
async def test_daemonic_process_start_assertion_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = MagicMock()
    result_queue = MagicMock()
    process = MagicMock()
    process.start.side_effect = AssertionError("daemonic processes cannot have children")
    context.Queue.return_value = result_queue
    context.Process.return_value = process
    monkeypatch.setattr(extraction_process.multiprocessing, "get_context", lambda _: context)

    with pytest.raises(RuntimeError, match="failed to start"):
        await extraction_process._run_process(lambda: None, (), timeout=0.1)

    result_queue.close.assert_called_once()


@pytest.mark.asyncio
async def test_process_timeout_terminates_then_kills_stuck_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = MagicMock()
    result_queue = MagicMock()
    result_queue.get.side_effect = queue.Empty
    process = MagicMock()
    process.is_alive.side_effect = [True, True]
    context.Queue.return_value = result_queue
    context.Process.return_value = process
    monkeypatch.setattr(extraction_process.multiprocessing, "get_context", lambda _: context)

    with pytest.raises(TimeoutError, match="timed out"):
        await extraction_process._run_process(lambda: None, (), timeout=0.1)

    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.join.call_count == 2
    result_queue.close.assert_called_once()
    result_queue.join_thread.assert_called_once()
