"""Standalone executor runner for remote tool and inference proxying."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import logging
import os
import platform
import uuid
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from cognis.core.executor_resolution import filter_tools_by_executor
from cognis.models.tool import ExecutorConfig, MCPServerConfig, ToolCall, ToolDefinition, ToolResult
from cognis.tools.executor.definitions import executor_tool_definitions, executor_tool_handlers
from cognis.tools.mcp import (
    StdioMCPClient,
    mcp_tools_to_definitions,
    resolve_secret_refs,
    validate_unique_server_names,
)

logger = logging.getLogger("cognis.executor.runner")

_HEARTBEAT_INTERVAL = 15
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 60.0


def _build_environment_payload() -> dict[str, str]:
    """Capture executor-local environment metadata for controller guidance."""

    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return {
        "user": user,
        "home": str(Path.home()),
        "cwd": os.getcwd(),
        "hostname": platform.node(),
        "source": "executor_runtime",
        "observed_at": datetime.now(UTC).isoformat(),
    }


class ExecutorRunner:
    """Thin remote hand for tool execution and inference proxying."""

    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config
        self._active_calls: dict[str, asyncio.Task[Any]] = {}
        self._running = True
        self._configured = False
        self._runtime_state = "offline"
        self._config_version = 0
        self._tool_handlers: dict[str, Any] = {}
        self._configured_tool_definitions: list[ToolDefinition] = []
        self._mcp_clients: dict[str, StdioMCPClient] = {}
        self._inference_handler: Any | None = None
        self._channel_handler: Any | None = None
        self._runtime_metadata: dict[str, Any] = {}
        self._started_at = perf_counter()

    async def run(self) -> None:
        reconnect_delay = _RECONNECT_BASE
        try:
            while self._running:
                try:
                    await self._connect_and_serve()
                    reconnect_delay = _RECONNECT_BASE
                    if not self._running:
                        break
                except Exception:
                    logger.warning("Connection lost, reconnecting in %.1fs", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _RECONNECT_MAX)
        finally:
            await self._close_mcp_clients()
            if self._channel_handler is not None:
                with contextlib.suppress(Exception):
                    await self._channel_handler.stop_all()
            if self._inference_handler is not None:
                with contextlib.suppress(Exception):
                    await self._inference_handler.close()

    async def _connect_and_serve(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("websockets package is required for remote executor")
            self._running = False
            return

        url = self.config.controller_url
        if not url or not self.config.controller_token:
            logger.error("controller_url and controller_token are required")
            self._running = False
            return

        self._configured = False
        self._runtime_state = "offline"
        self._tool_handlers = {}
        self._configured_tool_definitions = []

        async with websockets.connect(url, compression="deflate", max_size=10 * 1024 * 1024) as ws:
            ready_id = uuid.uuid4().hex
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "executor.ready",
                        "params": {
                            "token": self.config.controller_token,
                            "environment": _build_environment_payload(),
                            "platform": {
                                "os": platform.system().lower(),
                                "arch": platform.machine().lower(),
                                "python": platform.python_version(),
                            },
                        },
                        "id": ready_id,
                    }
                )
            )
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if "error" in response:
                logger.error("Registration failed: %s", response["error"].get("message", "unknown"))
                self._running = False
                return

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                await self._message_loop(ws)
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _message_loop(self, ws: Any) -> None:
        async for raw_message in ws:
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            method = msg.get("method")
            msg_id = msg.get("id")
            params = msg.get("params", {})

            if method == "executor.configure":
                await self._handle_configure(ws, msg_id, params)
            elif method == "tool.list":
                await self._handle_tool_list(ws, msg_id)
            elif method == "tool.execute":
                task = asyncio.create_task(self._handle_tool_execute(ws, msg_id, params))
                self._active_calls[params.get("call_id", msg_id)] = task
            elif method == "tool.cancel":
                call_id = params.get("call_id")
                if call_id and call_id in self._active_calls:
                    self._active_calls[call_id].cancel()
            elif method == "llm.complete":
                asyncio.create_task(self._handle_llm_complete(ws, msg_id, params))
            elif method == "channel.start":
                asyncio.create_task(self._handle_channel_start(ws, msg_id, params))
            elif method == "channel.stop":
                asyncio.create_task(self._handle_channel_stop(ws, msg_id, params))
            elif method == "channel.send":
                asyncio.create_task(self._handle_channel_send(ws, msg_id, params))
            elif method == "channel.fetch_media":
                asyncio.create_task(self._handle_channel_fetch_media(ws, msg_id, params))
            elif method == "channel.typing":
                asyncio.create_task(self._handle_channel_typing(ws, msg_id, params))
            elif method == "channel.mark_read":
                asyncio.create_task(self._handle_channel_mark_read(ws, msg_id, params))
            elif method == "channel.sync_profile":
                asyncio.create_task(self._handle_channel_sync_profile(ws, msg_id, params))
            elif method == "executor.cancel":
                self._running = False
                break

    async def _handle_configure(self, ws: Any, msg_id: str | None, params: dict[str, Any]) -> None:
        requested_version = int(params.get("config_version") or (self._config_version + 1))
        if requested_version <= self._config_version:
            await self._send_rpc_error(ws, msg_id, -32020, "Stale executor.configure version")
            return

        self._configured = False
        self._runtime_state = "reconfiguring"

        config = params.get("config", {})
        enabled_tools = params.get("enabled_tools", [])
        enabled_tool_groups = params.get("enabled_tool_groups", [])
        mcp_servers_raw = params.get("mcp_servers") or []
        secrets = dict(params.get("secrets") or {})

        try:
            mcp_servers = [MCPServerConfig.model_validate(item) for item in mcp_servers_raw]
            for server in mcp_servers:
                if server.transport != "stdio":
                    raise ValueError(
                        f"Executor-hosted MCP currently supports stdio only (server {server.name})"
                    )
            validate_unique_server_names(mcp_servers)
            await self._close_mcp_clients()
            self._mcp_clients = await self._start_mcp_clients(mcp_servers, secrets)
            discovered_tools = await self._discover_mcp_tools(mcp_servers)
        except Exception as exc:
            self._runtime_state = "blocked"
            await self._send_rpc_error(ws, msg_id, -32021, f"Executor configure failed: {exc}")
            return

        native_defs = filter_tools_by_executor(
            executor_tool_definitions(),
            enabled_tools,
            enabled_tool_groups,
        )

        # Generate dynamic web tool definitions from controller-provided config
        web_config_raw = params.get("web_config") or {}
        web_backends = web_config_raw.get("web_available_backends", ["direct"])
        from cognis.tools.executor.web.definitions import web_tool_definitions

        web_defs = web_tool_definitions(web_backends)
        # Store web runtime metadata for handler context
        self._runtime_metadata = {
            "web_backend": web_config_raw.get("web_backend", "direct"),
            "web_available_backends": web_backends,
            "web_secrets": secrets,
            "environment": _build_environment_payload(),
        }

        self._configured_tool_definitions = [*native_defs, *web_defs, *discovered_tools]
        native_handlers = executor_tool_handlers()
        allowed_native = {t.name for t in native_defs}
        allowed_web = {t.name for t in web_defs}
        self._tool_handlers = {
            name: handler
            for name, handler in native_handlers.items()
            if name in allowed_native or name in allowed_web
        }
        for tool in discovered_tools:
            if tool.source.server_name is not None:
                self._tool_handlers[tool.name] = self._build_mcp_handler(tool)

        if self._inference_handler is None:
            from cognis.executor.inference import InferenceHandler

            self._inference_handler = InferenceHandler()
        if self._channel_handler is None:
            from cognis.executor.channel_handler import ChannelHandler

            self._channel_handler = ChannelHandler()
        self._channel_handler.set_ws(ws)
        self._channel_handler.set_executor_config(config)

        self._config_version = requested_version
        self._configured = True
        self._runtime_state = "active"
        if msg_id is not None:
            await self._send_rpc_result(
                ws,
                msg_id,
                {
                    "status": "configured",
                    "applied_version": self._config_version,
                    "ready": True,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "capabilities": {
                        "tools": [tool.name for tool in self._configured_tool_definitions],
                        "inference": True,
                        "inference_models": [],
                        "inference_type": "litellm_proxy",
                        "channels": True,
                    },
                    "observed_tools": [
                        tool.model_dump(mode="json") for tool in self._configured_tool_definitions
                    ],
                    "config_keys": sorted(config.keys()) if isinstance(config, dict) else [],
                    "environment": self._runtime_metadata.get("environment")
                    or _build_environment_payload(),
                },
            )

    async def _handle_tool_list(self, ws: Any, msg_id: str | None) -> None:
        if msg_id is None:
            return
        await self._send_rpc_result(
            ws,
            msg_id,
            {"tools": [tool.model_dump(mode="json") for tool in self._configured_tool_definitions]},
        )

    async def _handle_tool_execute(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        call_id = params.get("call_id", msg_id or uuid.uuid4().hex)
        if not self._configured or self._runtime_state != "active":
            await self._send_rpc_result(
                ws,
                msg_id,
                {
                    "call_id": call_id,
                    "output": "Executor is not configured or ready yet.",
                    "is_error": True,
                    "duration_ms": 0,
                },
            )
            return

        tool_name = params.get("tool_name", "")
        arguments = params.get("arguments", {})
        timeout_seconds = params.get("timeout_seconds")
        start = perf_counter()
        try:
            handler = self._tool_handlers.get(tool_name)
            if handler is None:
                result = ToolResult(
                    output=f"Tool '{tool_name}' not available on this executor.", is_error=True
                )
            else:
                tool_call = ToolCall(call_id=call_id, name=tool_name, arguments=arguments)
                from cognis.models.tool import ExecutorHandle
                from cognis.tools.registry import ToolExecutionContext

                ctx = ToolExecutionContext(
                    executor_handle=ExecutorHandle(
                        executor_id=self.config.executor_id,
                        executor_type="remote",
                    ),
                    runtime_metadata=self._runtime_metadata,
                )

                async def _invoke() -> Any:
                    return await handler(tool_call.arguments, ctx)

                raw = (
                    await asyncio.wait_for(_invoke(), timeout=timeout_seconds)
                    if timeout_seconds
                    else await _invoke()
                )
                result = _normalize_result(raw, int((perf_counter() - start) * 1000))
        except TimeoutError:
            result = ToolResult(output="Tool execution timed out.", is_error=True)
        except asyncio.CancelledError:
            result = ToolResult(output="Tool execution cancelled.", is_error=True)
        except Exception as exc:
            result = ToolResult(
                output=f"Tool execution failed: {str(exc)[:500]}",
                is_error=True,
                duration_ms=int((perf_counter() - start) * 1000),
            )
        finally:
            self._active_calls.pop(call_id, None)

        await self._send_rpc_result(
            ws,
            msg_id,
            {
                "call_id": call_id,
                "output": result.output,
                "is_error": result.is_error,
                "duration_ms": result.duration_ms,
            },
        )

    async def _handle_llm_complete(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._inference_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Inference handler unavailable")
            return

        request_id = params.get("request_id", msg_id or uuid.uuid4().hex)
        if msg_id is not None:
            await self._send_rpc_result(ws, msg_id, {"status": "streaming"})

        request_kwargs = dict(params.get("request_kwargs") or {})
        request_kwargs.update(
            {
                key: value
                for key, value in params.items()
                if key not in {"request_id", "request_kwargs", "model", "messages"}
            }
        )

        async for chunk in self._inference_handler.stream_complete(
            model=str(params.get("model", "")),
            messages=list(params.get("messages", [])),
            request_kwargs=request_kwargs,
        ):
            if chunk.get("done"):
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "llm.done",
                            "params": {
                                "request_id": request_id,
                                "usage": chunk.get("usage", {}),
                                "finish_reason": chunk.get("finish_reason", "stop"),
                                "error": chunk.get("error"),
                            },
                        }
                    )
                )
            else:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "llm.chunk",
                            "params": {
                                "request_id": request_id,
                                "content": chunk.get("content"),
                                "tool_calls": chunk.get("tool_calls"),
                                "reasoning_content": chunk.get("reasoning_content"),
                                "index": chunk.get("index", 0),
                            },
                        }
                    )
                )

    async def _handle_channel_start(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.start(
                account_id=params.get("account_id", ""),
                channel_type=params.get("channel_type", ""),
                config=params.get("config", {}),
                credentials=params.get("credentials", {}),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_stop(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.stop(account_id=params.get("account_id", ""))
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_send(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.send(
                account_id=params.get("account_id", ""),
                message=params.get("message", {}),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_fetch_media(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.fetch_media(
                account_id=params.get("account_id", ""),
                message=params.get("message", {}),
                attachment=params.get("attachment", {}),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_typing(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.send_typing(
                account_id=params.get("account_id", ""),
                chat_id=params.get("chat_id", ""),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_mark_read(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.mark_read(
                account_id=params.get("account_id", ""),
                chat_id=params.get("chat_id", ""),
                message_id=params.get("message_id", ""),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _handle_channel_sync_profile(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._channel_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Channel handler unavailable")
            return
        try:
            result = await self._channel_handler.sync_profile(
                account_id=params.get("account_id", ""),
                profile_data=params,
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

    async def _heartbeat_loop(self, ws: Any) -> None:
        while self._running:
            try:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "executor.heartbeat",
                            "params": {
                                "uptime_seconds": int(perf_counter() - self._started_at),
                                "active_calls": len(self._active_calls),
                                "configured": self._configured,
                                "runtime_state": self._runtime_state,
                                "config_version": self._config_version,
                            },
                        }
                    )
                )
            except Exception:
                break
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    async def _start_mcp_clients(
        self, servers: list[MCPServerConfig], secrets: dict[str, str]
    ) -> dict[str, StdioMCPClient]:
        clients: dict[str, StdioMCPClient] = {}
        for server in servers:
            client = StdioMCPClient(server, env=resolve_secret_refs(server.env, secrets))
            await client.start()
            clients[server.name] = client
        return clients

    async def _discover_mcp_tools(self, servers: list[MCPServerConfig]) -> list[ToolDefinition]:
        discovered: list[ToolDefinition] = []
        for server in servers:
            tools = await self._mcp_clients[server.name].list_tools()
            discovered.extend(
                mcp_tools_to_definitions(
                    server.name,
                    tools,
                    timeout_seconds=server.timeout_seconds,
                    server_id=server.server_id,
                )
            )
        return discovered

    def _build_mcp_handler(self, tool: ToolDefinition) -> Any:
        async def _handler(arguments: dict[str, Any], _: Any) -> str:
            client = self._mcp_clients[str(tool.source.server_name)]
            raw_tool_name = tool.source.raw_tool_name or tool.name
            return await client.call_tool(raw_tool_name, arguments)

        return _handler

    async def _close_mcp_clients(self) -> None:
        for client in self._mcp_clients.values():
            with contextlib.suppress(Exception):
                await client.close()
        self._mcp_clients = {}

    async def _send_rpc_result(self, ws: Any, msg_id: str | None, result: dict[str, Any]) -> None:
        if msg_id is None:
            return
        await ws.send(json.dumps({"jsonrpc": "2.0", "result": result, "id": msg_id}))

    async def _send_rpc_error(self, ws: Any, msg_id: str | None, code: int, message: str) -> None:
        if msg_id is None:
            return
        await ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": msg_id}
            )
        )


def _normalize_result(raw: Any, duration_ms: int) -> ToolResult:
    if isinstance(raw, ToolResult):
        return raw.model_copy(update={"duration_ms": raw.duration_ms or duration_ms})
    if isinstance(raw, (dict, list)):
        output = json.dumps(raw, sort_keys=True, default=str)
    elif isinstance(raw, str):
        output = raw
    else:
        output = str(raw)
    return ToolResult(output=output, duration_ms=duration_ms)
