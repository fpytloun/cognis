"""Executor runner — connects to the controller and executes tools.

This module is the core of the standalone executor process.  It:
1. Connects to the controller via WebSocket.
2. Sends ``executor.ready`` with capabilities and auth token.
3. Listens for ``tool.execute``, ``tool.cancel``, ``llm.complete``,
   ``executor.cancel``, and ``executor.configure`` messages.
4. Executes tools using the same native handlers from
   ``cognis.tools.executor``.
5. Sends ``executor.heartbeat`` every 15 seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from time import perf_counter
from typing import Any

from cognis.models.tool import ExecutorConfig, ToolCall, ToolResult

logger = logging.getLogger("cognis.executor.runner")

_HEARTBEAT_INTERVAL = 15  # seconds
_RECONNECT_BASE = 1.0  # initial reconnect delay
_RECONNECT_MAX = 60.0  # max reconnect delay


class ExecutorRunner:
    """Standalone executor process that connects to a Cognis controller."""

    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config
        self._active_calls: dict[str, asyncio.Task[Any]] = {}
        self._running = True
        self._secrets: dict[str, str] = dict(config.secrets)
        self._tool_handlers: dict[str, Any] = {}
        self._inference_handler: Any | None = None

    async def run(self) -> None:
        """Main entry point — connect and run until cancelled."""
        self._init_tools()
        self._init_inference()

        reconnect_delay = _RECONNECT_BASE
        try:
            while self._running:
                try:
                    await self._connect_and_serve()
                    # Reset delay after a successful connection lifecycle
                    reconnect_delay = _RECONNECT_BASE
                    # Clean disconnect — don't reconnect
                    if not self._running:
                        break
                except Exception:
                    logger.warning("Connection lost, reconnecting in %.1fs", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _RECONNECT_MAX)
        finally:
            # Clean up inference handler
            if self._inference_handler is not None:
                with contextlib.suppress(Exception):
                    await self._inference_handler.close()

    async def _connect_and_serve(self) -> None:
        """Single connection lifecycle."""
        try:
            import websockets
        except ImportError:
            logger.error(
                "websockets package is required for remote executor. "
                "Install with: pip install websockets"
            )
            self._running = False
            return

        url = self.config.controller_url
        if not url:
            logger.error("No controller_url configured")
            self._running = False
            return

        logger.info("Connecting to controller: %s", url)

        try:
            async with websockets.connect(
                url,
                compression="deflate",
                max_size=10 * 1024 * 1024,  # 10 MB max message
            ) as ws:
                # Send executor.ready
                ready_id = uuid.uuid4().hex
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "executor.ready",
                            "params": {
                                "executor_id": self.config.executor_id,
                                "token": self.config.controller_token,
                                "capabilities": {
                                    "tools": list(self._tool_handlers.keys()),
                                    "inference": self._inference_handler is not None,
                                    "inference_models": self._get_inference_models(),
                                    "inference_type": self._get_inference_type(),
                                },
                            },
                            "id": ready_id,
                        }
                    )
                )

                # Wait for registration confirmation
                response = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "error" in response:
                    err = response["error"]
                    logger.error("Registration failed: %s", err.get("message", "unknown"))
                    self._running = False
                    return

                logger.info("Registered with controller as %s", self.config.executor_id)

                # Start heartbeat and message loop
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                try:
                    await self._message_loop(ws)
                finally:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
        except asyncio.CancelledError:
            self._running = False
        except Exception:
            logger.warning("WebSocket connection error", exc_info=True)
            raise

    async def _message_loop(self, ws: Any) -> None:
        """Process incoming messages from the controller."""
        async for raw_message in ws:
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            method = msg.get("method")
            msg_id = msg.get("id")
            params = msg.get("params", {})

            if method == "tool.execute":
                task = asyncio.create_task(self._handle_tool_execute(ws, msg_id, params))
                call_id = params.get("call_id", msg_id)
                self._active_calls[call_id] = task
            elif method == "tool.cancel":
                call_id = params.get("call_id")
                if call_id and call_id in self._active_calls:
                    self._active_calls[call_id].cancel()
            elif method == "llm.complete":
                asyncio.create_task(self._handle_llm_complete(ws, msg_id, params))
            elif method == "executor.configure":
                self._handle_configure(params)
                if msg_id:
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "result": {"status": "configured"},
                                "id": msg_id,
                            }
                        )
                    )
            elif method == "executor.cancel":
                logger.info("Received executor.cancel, shutting down")
                self._running = False
                break

    async def _handle_tool_execute(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        """Execute a tool and send the result back."""
        call_id = params.get("call_id", msg_id or uuid.uuid4().hex)
        tool_name = params.get("tool_name", "")
        arguments = params.get("arguments", {})
        timeout_seconds = params.get("timeout_seconds")

        start = perf_counter()
        try:
            handler = self._tool_handlers.get(tool_name)
            if handler is None:
                result = ToolResult(
                    output=f"Tool '{tool_name}' not available on this executor.",
                    is_error=True,
                )
            else:
                tool_call = ToolCall(call_id=call_id, name=tool_name, arguments=arguments)

                async def _invoke() -> Any:
                    return await handler(tool_call.arguments, None)

                if timeout_seconds:
                    raw = await asyncio.wait_for(_invoke(), timeout=timeout_seconds)
                else:
                    raw = await _invoke()

                duration_ms = int((perf_counter() - start) * 1000)
                result = _normalize_result(raw, duration_ms)
        except TimeoutError:
            result = ToolResult(output="Tool execution timed out.", is_error=True)
        except asyncio.CancelledError:
            result = ToolResult(output="Tool execution cancelled.", is_error=True)
        except Exception as exc:
            duration_ms = int((perf_counter() - start) * 1000)
            result = ToolResult(
                output=f"Tool execution failed: {str(exc)[:500]}",
                is_error=True,
                duration_ms=duration_ms,
            )
        finally:
            self._active_calls.pop(call_id, None)

        if msg_id:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "result": {
                            "call_id": call_id,
                            "output": result.output,
                            "is_error": result.is_error,
                            "duration_ms": result.duration_ms,
                        },
                        "id": msg_id,
                    }
                )
            )

    async def _handle_llm_complete(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        """Handle an LLM completion request via local inference."""
        request_id = params.get("request_id", msg_id or uuid.uuid4().hex)

        if self._inference_handler is None:
            if msg_id:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "error": {"code": -32601, "message": "Inference not available"},
                            "id": msg_id,
                        }
                    )
                )
            return

        # Acknowledge the request
        if msg_id:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "result": {"status": "streaming"},
                        "id": msg_id,
                    }
                )
            )

        # Stream chunks
        try:
            async for chunk in self._inference_handler.stream_complete(
                messages=params.get("messages", []),
                model=params.get("model"),
                tools=params.get("tools"),
                temperature=params.get("temperature"),
                max_tokens=params.get("max_tokens"),
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
                                    "index": chunk.get("index", 0),
                                },
                            }
                        )
                    )
        except Exception as exc:
            logger.warning("Inference error: %s", exc)
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "llm.done",
                        "params": {
                            "request_id": request_id,
                            "error": str(exc)[:500],
                            "finish_reason": "error",
                        },
                    }
                )
            )

    def _handle_configure(self, params: dict[str, Any]) -> None:
        """Process executor.configure — receive secrets and extra config."""
        secrets = params.get("secrets", {})
        if secrets:
            self._secrets.update(secrets)
            logger.info("Received %d secrets via executor.configure", len(secrets))

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Send periodic heartbeats to the controller."""
        while self._running:
            try:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "executor.heartbeat",
                            "params": {
                                "uptime_seconds": 0,  # TODO: track actual uptime
                                "active_calls": len(self._active_calls),
                            },
                        }
                    )
                )
            except Exception:
                break
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    def _init_tools(self) -> None:
        """Initialize native tool handlers."""
        try:
            from cognis.tools.executor.definitions import executor_tool_handlers

            enabled = self.config.metadata.get("enabled_tools", "*")
            handlers = executor_tool_handlers()
            if enabled == "*":
                self._tool_handlers = handlers
            else:
                tool_list = enabled.split(",") if isinstance(enabled, str) else enabled
                self._tool_handlers = {
                    name: handler for name, handler in handlers.items() if name in tool_list
                }
            logger.info("Initialized %d tool handlers", len(self._tool_handlers))
        except ImportError:
            logger.warning("Could not import tool handlers — running without tools")

    def _init_inference(self) -> None:
        """Initialize inference handler if configured."""
        if self.config.inference is None:
            return
        try:
            from cognis.executor.inference import InferenceHandler

            self._inference_handler = InferenceHandler(self.config.inference)
            logger.info(
                "Inference handler initialized: %s (%s)",
                self.config.inference.endpoint,
                self.config.inference.default_model,
            )
        except Exception:
            logger.warning("Failed to initialize inference handler", exc_info=True)

    def _get_inference_models(self) -> list[str]:
        if self.config.inference and self.config.inference.models:
            return self.config.inference.models
        return []

    def _get_inference_type(self) -> str | None:
        if self.config.inference:
            return self.config.inference.type
        return None


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
