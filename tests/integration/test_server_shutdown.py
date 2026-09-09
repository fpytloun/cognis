from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def test_sigterm_drains_before_uvicorn_closes_listener(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    port = _free_port()
    log_path = tmp_path / "shutdown.jsonl"
    release_path = tmp_path / "release"
    script = tmp_path / "server.py"
    script.write_text(
        """
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from cognis.api.server import run_server
from cognis.core.shutdown import ShutdownCallbacks, ShutdownCoordinator
from cognis.models.tool import ExecutorCapabilities, ToolCall
from cognis.providers.executor.websocket import WebSocketExecutorProvider

port = int(sys.argv[1])
log_path = Path(sys.argv[2])
release_path = Path(sys.argv[3])
coordinator = ShutdownCoordinator()
provider = WebSocketExecutorProvider()
accepted_tool = None
complete_tool = None

class ExecutorSocket:
    def __init__(self):
        self.sent = []
        self.received = asyncio.Queue()

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_json(self):
        return await self.received.get()

    async def close(self, code=1000, reason=None):
        del code, reason

def record(stage):
    with log_path.open("a") as stream:
        stream.write(json.dumps({"stage": stage}) + "\\n")

async def begin():
    record("begin")

async def turns(drain_timeout, cancel_timeout):
    record("turns")
    return {"timed_out": 0}

async def tools(timeout):
    record("tools")
    return await provider.drain_tool_calls(timeout_seconds=timeout)

@asynccontextmanager
async def lifespan(app):
    global accepted_tool, complete_tool
    executor_socket = ExecutorSocket()
    connection = provider.register_connection(
        "exec-1",
        executor_socket,
        ExecutorCapabilities(tools=["bash"]),
    )
    accepted_tool = asyncio.create_task(
        connection.tool_execute(
            ToolCall(
                call_id="tc-sigterm",
                name="bash",
                arguments={"command": "sleep 1"},
            ),
            timeout_seconds=5,
        )
    )
    while not executor_socket.sent:
        await asyncio.sleep(0)

    async def finish_tool():
        while not release_path.exists():
            await asyncio.sleep(0.01)
        request = executor_socket.sent[0]
        executor_socket.received.put_nowait(
            {
                "jsonrpc": "2.0",
                "result": {"output": "done", "is_error": False},
                "id": request["id"],
            }
        )
        result = await accepted_tool
        record("tool_result:" + result.output)

    complete_tool = asyncio.create_task(finish_tool())
    coordinator.configure(
        ShutdownCallbacks(begin, turns, tools),
        drain_timeout_seconds=5,
        cancel_timeout_seconds=1,
    )
    yield
    await coordinator.drain()
    await complete_tool
    await provider.cleanup()
    record("lifespan")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"ready": True}

run_server(
    uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None),
    coordinator,
)
"""
    )
    process = subprocess.Popen(
        [sys.executable, str(script), str(port), str(log_path), str(release_path)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    try:
        _wait_for(
            lambda: (
                json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2).read())
                == {"ready": True}
            )
        )
        os.kill(process.pid, signal.SIGTERM)
        _wait_for(lambda: log_path.exists() and '"stage": "tools"' in log_path.read_text())

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            assert response.status == 200

        release_path.touch()
        assert process.wait(timeout=10) in {0, -signal.SIGTERM}
        stages = [json.loads(line)["stage"] for line in log_path.read_text().splitlines()]
        assert stages == ["begin", "turns", "tools", "tool_result:done", "lifespan"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
