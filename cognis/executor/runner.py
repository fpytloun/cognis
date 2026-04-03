"""Standalone executor runner for remote tool and inference proxying."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import uuid
from time import perf_counter
from typing import Any

from cognis.core.executor_resolution import filter_tools_by_executor
from cognis.models.tool import ExecutorConfig, ToolCall, ToolDefinition, ToolResult
from cognis.tools.executor.definitions import executor_tool_definitions, executor_tool_handlers

logger = logging.getLogger("cognis.executor.runner")

_HEARTBEAT_INTERVAL = 15
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 60.0


class ExecutorRunner:
    """Thin remote hand for tool execution and inference proxying."""

    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config
        self._active_calls: dict[str, asyncio.Task[Any]] = {}
        self._running = True
        self._configured = False
        self._tool_handlers: dict[str, Any] = {}
        self._configured_tool_definitions: list[ToolDefinition] = []
        self._inference_handler: Any | None = None
        self._channel_handler: Any | None = None
        self._started_at = perf_counter()

    async def run(self) -> None:
        """Main entry point — connect and serve until cancelled."""
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
        self._tool_handlers = {}
        self._configured_tool_definitions = []

        async with websockets.connect(
            url,
            compression="deflate",
            max_size=10 * 1024 * 1024,
        ) as ws:
            ready_id = uuid.uuid4().hex
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "executor.ready",
                        "params": {
                            "token": self.config.controller_token,
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
            elif method == "executor.cancel":
                self._running = False
                break

    async def _handle_configure(self, ws: Any, msg_id: str | None, params: dict[str, Any]) -> None:
        config = params.get("config", {})
        enabled_tools = params.get("enabled_tools", [])
        enabled_tool_groups = params.get("enabled_tool_groups", [])

        all_defs = executor_tool_definitions()
        self._configured_tool_definitions = filter_tools_by_executor(
            all_defs,
            enabled_tools,
            enabled_tool_groups,
        )
        allowed = {tool.name for tool in self._configured_tool_definitions}
        all_handlers = executor_tool_handlers()
        self._tool_handlers = {
            name: handler for name, handler in all_handlers.items() if name in allowed
        }

        if self._inference_handler is None:
            from cognis.executor.inference import InferenceHandler

            self._inference_handler = InferenceHandler()

        if self._channel_handler is None:
            from cognis.executor.channel_handler import ChannelHandler

            self._channel_handler = ChannelHandler()
        self._channel_handler.set_ws(ws)

        self._configured = True
        if msg_id:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "result": {
                            "status": "configured",
                            "capabilities": {
                                "tools": [tool.name for tool in self._configured_tool_definitions],
                                "inference": True,
                                "inference_models": [],
                                "inference_type": "litellm_proxy",
                                "channels": True,
                            },
                            "config_keys": sorted(config.keys())
                            if isinstance(config, dict)
                            else [],
                        },
                        "id": msg_id,
                    }
                )
            )

    async def _handle_tool_list(self, ws: Any, msg_id: str | None) -> None:
        if not msg_id:
            return
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "result": {
                        "tools": [
                            tool.model_dump(mode="json")
                            for tool in self._configured_tool_definitions
                        ],
                    },
                    "id": msg_id,
                }
            )
        )

    async def _handle_tool_execute(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        call_id = params.get("call_id", msg_id or uuid.uuid4().hex)
        if not self._configured:
            await self._send_rpc_result(
                ws,
                msg_id,
                {
                    "call_id": call_id,
                    "output": "Executor is not configured yet.",
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

                async def _invoke() -> Any:
                    return await handler(tool_call.arguments, None)

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
        if msg_id:
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

    # ------------------------------------------------------------------
    # Channel adapter methods
    # ------------------------------------------------------------------

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
                            },
                        }
                    )
                )
            except Exception:
                break
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    async def _send_rpc_result(self, ws: Any, msg_id: str | None, result: dict[str, Any]) -> None:
        if msg_id is None:
            return
        await ws.send(json.dumps({"jsonrpc": "2.0", "result": result, "id": msg_id}))

    async def _send_rpc_error(self, ws: Any, msg_id: str | None, code: int, message: str) -> None:
        if msg_id is None:
            return
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": code, "message": message},
                    "id": msg_id,
                }
            )
        )


def _normalize_result(raw: Any, duration_ms: int) -> ToolResult:
    """Normalize a handler return value to ToolResult."""
    if isinstance(raw, ToolResult):
        return raw.model_copy(update={"duration_ms": raw.duration_ms or duration_ms})
    if isinstance(raw, (dict, list)):
        output = json.dumps(raw, sort_keys=True, default=str)
    elif isinstance(raw, str):
        output = raw
    else:
        output = str(raw)
    return ToolResult(output=output, duration_ms=duration_ms)
