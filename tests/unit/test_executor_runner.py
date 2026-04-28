"""Unit tests for the executor runner module."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cognis.cli import executor as cli_executor
from cognis.executor import __main__ as executor_main
from cognis.executor.runner import ExecutorRunner, _normalize_result
from cognis.models.tool import (
    ExecutorConfig,
    MCPServerConfig,
    ToolDefinition,
    ToolResult,
    ToolSource,
)
from cognis.tools.executor.lsp import LSP_MANAGER_KEY, LSP_STATUS_CAPABILITY
from cognis.tools.mcp import MCPClientError


class DummyWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class DummyMessageWebSocket(DummyWebSocket):
    def __init__(self, messages: list[dict]) -> None:
        super().__init__()
        self._messages = [json.dumps(message) for message in messages]

    def __aiter__(self) -> DummyMessageWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def test_normalize_result_from_string() -> None:
    result = _normalize_result("hello world", 42)
    assert isinstance(result, ToolResult)
    assert result.output == "hello world"
    assert result.duration_ms == 42


def test_normalize_result_from_dict() -> None:
    result = _normalize_result({"key": "value"}, 10)
    assert result.output == '{"key": "value"}'


def test_normalize_result_from_tool_result() -> None:
    result = _normalize_result(ToolResult(output="x", is_error=True), 7)
    assert result.is_error is True
    assert result.duration_ms == 7


@pytest.mark.asyncio
async def test_handle_configure_filters_tools() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": ["read", "glob"], "enabled_tool_groups": [], "config": {}},
    )

    assert runner._configured is True
    # Native tools filtered to read + glob; web tools (web_fetch, web_search)
    # are always added from the default "direct" backend.
    assert {"read", "glob"}.issubset(set(runner._tool_handlers))
    assert "bash" not in runner._tool_handlers  # not in enabled_tools
    caps_tools = ws.sent[-1]["result"]["capabilities"]["tools"]
    assert "read" in caps_tools
    assert "glob" in caps_tools
    assert "environment" in ws.sent[-1]["result"]


@pytest.mark.asyncio
async def test_handle_tool_list_returns_configured_definitions() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})
    ws.sent.clear()

    await runner._handle_tool_list(ws, "list-1")

    assert ws.sent[-1]["result"]["tools"][0]["name"] == "read"


@pytest.mark.asyncio
async def test_handle_tool_execute_requires_configuration() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    await runner._handle_tool_execute(
        ws,
        "call-1",
        {"call_id": "call-1", "tool_name": "read", "arguments": {}},
    )

    assert ws.sent[-1]["result"]["is_error"] is True
    assert "not configured" in ws.sent[-1]["result"]["output"].lower()


@pytest.mark.asyncio
async def test_handle_llm_complete_streams_chunks() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": [], "config": {}})

    runner._inference_handler = AsyncMock()

    async def _stream_complete(**_: object):
        yield {"content": "Hello", "index": 0}
        yield {"done": True, "usage": {"prompt_tokens": 1}, "finish_reason": "stop"}

    runner._inference_handler.stream_complete = _stream_complete
    ws.sent.clear()

    await runner._handle_llm_complete(
        ws,
        "rpc-1",
        {"request_id": "req-1", "model": "openai/gpt-4o-mini", "messages": []},
    )

    assert ws.sent[0]["result"]["status"] == "streaming"
    assert ws.sent[1]["method"] == "llm.chunk"
    assert ws.sent[2]["method"] == "llm.done"


@pytest.mark.asyncio
async def test_handle_llm_transcribe_returns_result() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": [], "config": {}})

    runner._inference_handler = AsyncMock()
    runner._inference_handler.transcribe = AsyncMock(
        return_value={"text": "hello", "model": "whisper-1"}
    )
    ws.sent.clear()

    await runner._handle_llm_transcribe(
        ws,
        "rpc-1",
        {
            "audio_base64": b"abc".hex(),
            "audio_encoding": "hex",
            "mime_type": "audio/ogg",
            "filename": "voice.ogg",
            "model": "whisper-1",
            "request_kwargs": {},
        },
    )

    assert ws.sent[-1]["result"]["text"] == "hello"


@pytest.mark.asyncio
async def test_heartbeat_includes_configuration_state() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    task = asyncio.create_task(runner._heartbeat_loop(ws))
    await asyncio.sleep(0)
    runner._running = False
    await task
    assert ws.sent[0]["params"]["configured"] is False


@pytest.mark.asyncio
async def test_executor_cancel_acknowledges_before_shutdown() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyMessageWebSocket(
        [
            {
                "jsonrpc": "2.0",
                "method": "executor.cancel",
                "id": "cancel-1",
                "params": {"reason": "cancelled"},
            }
        ]
    )

    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": [], "config": {}})
    ws.sent.clear()

    await runner._message_loop(ws)

    assert runner._running is False
    assert ws.sent[-1]["id"] == "cancel-1"
    assert ws.sent[-1]["result"]["status"] == "shutting_down"


@pytest.mark.asyncio
async def test_handle_configure_reports_degraded_when_some_mcp_servers_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    async def _prepare_mcp_runtime(
        servers: list[object], secrets: dict[str, str]
    ) -> tuple[dict[str, object], list[ToolDefinition], list[dict[str, object]], list[str]]:
        del servers, secrets
        return (
            {},
            [
                ToolDefinition(
                    name="mcp_todoist__list_tasks",
                    description="List tasks",
                    parameters={"type": "object", "properties": {}},
                    source=ToolSource(
                        type="local_mcp", server_name="todoist", raw_tool_name="list_tasks"
                    ),
                    category="mcp",
                )
            ],
            [
                {"name": "todoist", "status": "ready", "phase": "ready", "tool_count": 1},
                {
                    "name": "github",
                    "status": "failed",
                    "phase": "initialize",
                    "error_class": "timeout",
                    "timed_out": True,
                    "message": "github startup timed out",
                    "stderr_summary": "npm error: missing token",
                },
            ],
            ["MCP server github failed during initialize."],
        )

    monkeypatch.setattr(runner, "_prepare_mcp_runtime", _prepare_mcp_runtime)

    await runner._handle_configure(
        ws,
        "cfg-1",
        {
            "enabled_tools": ["read"],
            "mcp_servers": [
                {
                    "name": "todoist",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "todoist"],
                }
            ],
            "config": {},
        },
    )

    assert runner._configured is True
    assert runner._runtime_state == "degraded"
    assert ws.sent[-1]["result"]["runtime_state"] == "degraded"
    assert ws.sent[-1]["result"]["runtime_metadata"]["warnings"]
    degraded_server = ws.sent[-1]["result"]["runtime_metadata"]["mcp_servers"][1]
    assert degraded_server["message"] == "github startup timed out"
    assert degraded_server["stderr_summary"] == "npm error: missing token"


@pytest.mark.asyncio
async def test_prepare_mcp_runtime_suppresses_failed_client_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _BrokenClient:
        async def connect(self) -> None:
            raise MCPClientError(
                "googleworkspace",
                "initialize",
                "redirect failed",
                error_class="httpstatuserror",
            )

        async def list_tools(self) -> list[dict[str, object]]:
            return []

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            del tool_name, arguments
            return {}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled
            raise BaseExceptionGroup("cleanup failed", [RuntimeError("cleanup")])

    monkeypatch.setattr("cognis.executor.runner.build_mcp_client", lambda *_: _BrokenClient())

    clients, discovered, statuses, warnings = await runner._prepare_mcp_runtime(
        [
            MCPServerConfig(
                name="googleworkspace",
                transport="streamable_http",
                url="http://mcp-gws.openwebui.svc.cluster.local/mcp/",
            )
        ],
        {},
    )

    assert clients == {}
    assert discovered == []
    assert warnings == ["MCP server googleworkspace failed during initialize."]
    assert statuses[0]["status"] == "failed"
    assert statuses[0]["message"] == "redirect failed"


@pytest.mark.asyncio
async def test_handle_tool_execute_allows_degraded_runtime() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})
    runner._runtime_state = "degraded"

    async def _handler(_: dict[str, object], context: object) -> ToolResult:
        del context
        return ToolResult(output="ok", is_error=False, metadata={"analysis": "ok"})

    runner._tool_handlers["read"] = _handler
    ws.sent.clear()

    await runner._handle_tool_execute(
        ws,
        "tool-1",
        {"call_id": "call-1", "tool_name": "read", "arguments": {}},
    )

    assert ws.sent[-1]["result"]["is_error"] is False
    assert ws.sent[-1]["result"]["output"] == "ok"
    assert ws.sent[-1]["result"]["metadata"] == {"analysis": "ok"}


@pytest.mark.asyncio
async def test_failed_reconfigure_preserves_previous_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})

    async def _broken_prepare(
        servers: list[object], secrets: dict[str, str]
    ) -> tuple[dict[str, object], list[ToolDefinition], list[dict[str, object]], list[str]]:
        del servers, secrets
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_prepare_mcp_runtime", _broken_prepare)
    ws.sent.clear()

    await runner._handle_configure(ws, "cfg-2", {"enabled_tools": ["glob"], "config": {}})

    assert runner._configured is True
    assert "read" in runner._tool_handlers
    assert "glob" not in runner._tool_handlers
    assert ws.sent[-1]["error"]["message"].startswith("Executor configure failed")


@pytest.mark.asyncio
async def test_runner_shutdown_cleans_browser_manager() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    cleaned: list[str] = []

    class _BrowserManager:
        async def cleanup(self) -> None:
            cleaned.append("cleanup")

    async def _fake_connect_and_serve() -> None:
        runner._runtime_metadata["browser_manager"] = _BrowserManager()
        runner._running = False

    runner._connect_and_serve = _fake_connect_and_serve  # type: ignore[method-assign]

    await runner.run()

    assert cleaned == ["cleanup"]


@pytest.mark.asyncio
async def test_runner_shutdown_suppresses_cancelled_cleanup() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _BrowserManager:
        async def cleanup(self) -> None:
            raise asyncio.CancelledError()

    class _ChannelHandler:
        async def stop_all(self) -> None:
            raise asyncio.CancelledError()

    class _InferenceHandler:
        async def close(self) -> None:
            raise asyncio.CancelledError()

    async def _fake_connect_and_serve() -> None:
        runner._runtime_metadata["browser_manager"] = _BrowserManager()
        runner._channel_handler = _ChannelHandler()
        runner._inference_handler = _InferenceHandler()
        runner._running = False

    async def _fake_close_mcp_clients() -> None:
        raise asyncio.CancelledError()

    runner._connect_and_serve = _fake_connect_and_serve  # type: ignore[method-assign]
    runner._close_mcp_clients = _fake_close_mcp_clients  # type: ignore[method-assign]

    await runner.run()


@pytest.mark.asyncio
async def test_runner_propagates_external_cancellation() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    async def _fake_connect_and_serve() -> None:
        await asyncio.Future()

    runner._connect_and_serve = _fake_connect_and_serve  # type: ignore[method-assign]

    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_handle_configure_creates_lsp_manager() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": ["read"], "config": {"lsp_enabled": True}},
    )

    assert runner._runtime_metadata.get(LSP_MANAGER_KEY) is not None
    caps = ws.sent[-1]["result"]["runtime_metadata"]["configure_capabilities"]
    assert LSP_STATUS_CAPABILITY in caps


@pytest.mark.asyncio
async def test_handle_lsp_status_returns_disabled_when_manager_missing() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": [], "config": {"lsp_enabled": False}},
    )
    ws.sent.clear()

    await runner._handle_lsp_status(ws, "lsp-1", {})

    assert ws.sent[-1]["result"]["state"] == "disabled"
    assert ws.sent[-1]["result"]["enabled"] is False


@pytest.mark.asyncio
async def test_tool_execute_passes_execution_scope_id() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})

    seen_scope: dict[str, str | None] = {}

    async def _handler(_: dict[str, object], context: object) -> dict[str, object]:
        seen_scope["value"] = getattr(context, "execution_scope_id", None)
        return {"output": "ok", "is_error": False}

    runner._tool_handlers["read"] = _handler
    ws.sent.clear()

    await runner._handle_tool_execute(
        ws,
        "tool-1",
        {
            "call_id": "call-1",
            "tool_name": "read",
            "arguments": {},
            "execution_scope_id": "session-123",
        },
    )

    assert seen_scope["value"] == "session-123"


@pytest.mark.asyncio
async def test_handle_configure_degrades_when_lsp_manager_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    def _broken_build_lsp_manager(_: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr("cognis.executor.runner.build_lsp_manager", _broken_build_lsp_manager)

    await runner._handle_configure(
        ws,
        "cfg-1",
        {"enabled_tools": ["read"], "config": {"lsp_enabled": True}},
    )

    assert runner._configured is True
    assert runner._runtime_metadata.get(LSP_MANAGER_KEY) is None
    assert runner._runtime_metadata["lsp_init_failed"] is True

    ws.sent.clear()
    await runner._handle_lsp_status(ws, "lsp-2", {})
    assert ws.sent[-1]["result"]["state"] == "unavailable"


def test_executor_main_suppresses_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "workdir"
    launch = tmp_path / "launch"
    workdir.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)

    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setenv("COGNIS_EXECUTOR_WORKDIR", str(workdir))
    monkeypatch.setattr(sys, "argv", ["cognis-executor"])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()


def test_executor_main_defaults_workdir_to_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    launch = tmp_path / "launch"
    home.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.delenv("COGNIS_EXECUTOR_WORKDIR", raising=False)
    monkeypatch.setattr(executor_main.Path, "home", staticmethod(lambda: home))

    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            assert Path.cwd() == home
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(sys, "argv", ["cognis-executor"])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()


def test_executor_main_uses_env_workdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_workdir = tmp_path / "env-workdir"
    launch = tmp_path / "launch"
    env_workdir.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.setenv("COGNIS_EXECUTOR_WORKDIR", str(env_workdir))

    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            assert Path.cwd() == env_workdir
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(sys, "argv", ["cognis-executor"])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()


def test_executor_main_cli_workdir_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_workdir = tmp_path / "env-workdir"
    cli_workdir = tmp_path / "cli-workdir"
    launch = tmp_path / "launch"
    env_workdir.mkdir()
    cli_workdir.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.setenv("COGNIS_EXECUTOR_WORKDIR", str(env_workdir))

    class _Runner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def run(self) -> None:
            assert Path.cwd() == cli_workdir
            raise asyncio.CancelledError()

    monkeypatch.setattr("cognis.executor.runner.ExecutorRunner", _Runner)
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(
        sys,
        "argv",
        ["cognis-executor", "--workdir", str(cli_workdir)],
    )
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        executor_main.main()


def test_executor_main_rejects_invalid_workdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "missing"
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "ws://localhost:8080/api/executor/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "token")
    monkeypatch.setattr(sys, "argv", ["cognis-executor", "--workdir", str(invalid)])
    with open(os.devnull) as devnull:
        monkeypatch.setattr(sys, "stdin", devnull)
        with pytest.raises(SystemExit) as exc_info:
            executor_main.main()

    assert exc_info.value.code == 1
    assert "Executor working directory does not exist" in capsys.readouterr().err


def test_cli_executor_workdir_defaults_to_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("COGNIS_EXECUTOR_WORKDIR", raising=False)
    monkeypatch.setattr(cli_executor.Path, "home", staticmethod(lambda: home))

    assert cli_executor._resolve_workdir(None) == str(home)


# ---------------------------------------------------------------------------
# Regression tests: reconfigure shutdown caused by anyio cross-task teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_clients_swallows_base_exception_group() -> None:
    """_close_clients must not let BaseExceptionGroup escape to the caller.

    When anyio detects that exit stacks entered in one task are being
    aclose()d from a different task, it raises BaseExceptionGroup.  The
    runner's except Exception: guard does NOT catch BaseExceptionGroup
    (it is a BaseException subclass, not Exception), so we must handle it
    explicitly to prevent the executor from shutting down on reconfigure.
    """
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    closed: list[str] = []

    class _FakeClient:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            if self.name == "bomb":
                raise BaseExceptionGroup("anyio-cancel", [RuntimeError("cross-task scope")])
            closed.append(self.name)

    clients = {"ok": _FakeClient("ok"), "bomb": _FakeClient("bomb"), "ok2": _FakeClient("ok2")}

    # Must not raise and must continue iterating past the "bomb" client.
    await runner._close_clients(clients, suppress_cancelled=True)  # type: ignore[arg-type]

    assert "ok" in closed
    assert "ok2" in closed
    assert "bomb" not in closed


@pytest.mark.asyncio
async def test_close_clients_reraises_cancelled_when_not_suppressed() -> None:
    """_close_clients propagates CancelledError when suppress_cancelled=False."""
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))

    class _CancellingClient:
        async def close(self, *, suppress_cancelled: bool = False) -> None:
            raise asyncio.CancelledError()

    clients = {"c": _CancellingClient()}

    with pytest.raises(asyncio.CancelledError):
        await runner._close_clients(clients, suppress_cancelled=False)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_spawn_background_close_does_not_block_caller() -> None:
    """_spawn_background_close returns immediately; the close runs in a task."""
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    close_started = asyncio.Event()
    close_done = asyncio.Event()

    class _SlowClient:
        async def close(self, *, suppress_cancelled: bool = False) -> None:
            close_started.set()
            await asyncio.sleep(0.05)
            close_done.set()

    runner._spawn_background_close({"slow": _SlowClient()})  # type: ignore[arg-type]

    # The method returns before the close finishes.
    assert not close_done.is_set()
    # Allow the background task to run to completion.
    await asyncio.wait_for(close_done.wait(), timeout=2)
    assert close_done.is_set()


@pytest.mark.asyncio
async def test_run_finally_drains_pending_closes() -> None:
    """run() cancels + awaits _pending_closes before exiting.

    Stale subprocess transports must be cleaned up before the event loop
    closes so that BaseSubprocessTransport.__del__ doesn't fire with
    'Event loop is closed' at interpreter shutdown.
    """
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    task_was_cancelled: list[bool] = []

    async def _long_close() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            task_was_cancelled.append(True)

    async def _fake_connect_and_serve() -> None:
        # Yield once so _long_close gets a chance to start and reach its
        # first await (asyncio.sleep) before we set _running=False.
        await asyncio.sleep(0)
        runner._running = False

    task: asyncio.Task[None] = asyncio.create_task(_long_close(), name="test-close")
    runner._pending_closes.add(task)
    runner._connect_and_serve = _fake_connect_and_serve  # type: ignore[method-assign]

    await runner.run()

    assert task.done(), "background close task must be done after run() returns"
    assert task_was_cancelled == [True], "CancelledError must have been raised inside the task"
    assert len(runner._pending_closes) == 0


@pytest.mark.asyncio
async def test_reconfigure_uses_background_close_for_stale_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfiguring must not call _close_clients inline for stale MCP clients.

    Previously, _handle_configure awaited _close_clients(previous_clients) on
    the configure task.  If anyio cross-task scope teardown raised
    BaseExceptionGroup this escaped run()'s except Exception: guard and caused
    the executor to exit.  The fix spawns teardown on a background task.
    """
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="t"))
    ws = DummyWebSocket()

    # First configure – establishes v1 state.
    await runner._handle_configure(ws, "cfg-1", {"enabled_tools": ["read"], "config": {}})
    assert runner._config_version == 1
    ws.sent.clear()

    # Inject a fake stale MCP client that raises BaseExceptionGroup on close.
    bomb_close_called = asyncio.Event()

    class _BombClient:
        async def close(self, *, suppress_cancelled: bool = False) -> None:
            bomb_close_called.set()
            raise BaseExceptionGroup("anyio-cancel", [RuntimeError("cross-task scope")])

    runner._mcp_clients = {"bomb": _BombClient()}  # type: ignore[assignment]

    # Second configure (reconfigure) – must succeed and NOT shut the runner down.
    await runner._handle_configure(ws, "cfg-2", {"enabled_tools": ["glob"], "config": {}})

    assert runner._config_version == 2
    assert runner._configured is True
    assert runner._running is True  # critical: runner must not exit
    assert ws.sent[-1]["result"]["applied_version"] == 2

    # The background close task should eventually call close() on the bomb client.
    await asyncio.wait_for(bomb_close_called.wait(), timeout=2)
