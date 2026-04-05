"""In-process executor provider with an in-memory JSON-RPC bridge."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, cast

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.tool import (
    ExecutorCapabilities,
    ExecutorConfig,
    ExecutorHandle,
    ToolCall,
    ToolResult,
)
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.tools.builtin.system import StatusProvider, build_system_tool_handlers
from cognis.tools.executor.definitions import executor_tool_handlers
from cognis.tools.executor.lsp import LSP_MANAGER_KEY, LSPManager
from cognis.tools.mcp import StdioMCPClient, mcp_tools_to_definitions
from cognis.tools.registry import RegisteredTool, ToolExecutionContext, ToolRegistry

_logger = get_logger(__name__)

EXECUTOR_SPAWN_TOTAL = Counter(
    "cognis_executor_spawns_total",
    "Executor spawn attempts",
    labelnames=("outcome",),
)
EXECUTOR_SPAWN_DURATION = Histogram(
    "cognis_executor_spawn_duration_seconds",
    "Executor spawn duration",
    labelnames=("outcome",),
)


@dataclass(slots=True)
class _ExecutorRuntime:
    handle: ExecutorHandle
    connection: InProcessExecutorConnection
    mcp_clients: dict[str, StdioMCPClient] = field(default_factory=dict)
    lsp_manager: LSPManager | None = None


class InProcessExecutorConnection:
    """Direct in-memory implementation of the executor JSON-RPC protocol."""

    def __init__(
        self,
        handle: ExecutorHandle,
        registry: ToolRegistry,
        breaker: CircuitBreaker,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.handle = handle
        self.registry = registry
        self.breaker = breaker
        self.runtime_metadata = runtime_metadata or {}
        self._active_calls: dict[str, asyncio.Task[Any]] = {}

    async def rpc_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle an in-memory JSON-RPC request."""

        if method == "tool.list":
            return {"tools": self.registry.export()}
        if method == "tool.execute":
            tool_call = ToolCall(
                call_id=str(params.get("call_id") or uuid.uuid4().hex),
                name=str(params["tool_name"]),
                arguments=dict(params.get("arguments") or {}),
            )
            timeout_seconds = params.get("timeout_seconds")
            result = await self.tool_execute(
                tool_call,
                timeout_seconds=timeout_seconds if isinstance(timeout_seconds, int) else None,
            )
            return result.model_dump(mode="json")
        if method in {"cancel", "tool.cancel"}:
            await self.cancel_call(str(params["call_id"]))
            return {"status": "cancelled"}
        if method in {"heartbeat", "executor.heartbeat"}:
            return {"status": "ok", "active_calls": len(self._active_calls)}
        raise ValueError(f"Unsupported executor method: {method}")

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return tool metadata available on the executor."""

        return self.registry.export()

    async def tool_execute(
        self, tool_call: ToolCall, timeout_seconds: int | None = None
    ) -> ToolResult:
        """Execute a tool call through the runtime registry."""

        registered_tool = self.registry.get(tool_call.name)
        if registered_tool is None or registered_tool.handler is None:
            return ToolResult(output="Tool is not executable on this executor.", is_error=True)
        handler = registered_tool.handler

        async def invoke_handler() -> ToolResult:
            start = perf_counter()
            context = ToolExecutionContext(
                executor_handle=self.handle,
                runtime_metadata=self.runtime_metadata,
            )
            result = await handler(tool_call.arguments, context)
            duration_ms = int((perf_counter() - start) * 1000)
            return _normalize_tool_result(result, duration_ms)

        task = asyncio.create_task(self.breaker.call(invoke_handler))
        self._active_calls[tool_call.call_id] = task
        try:
            if timeout_seconds is not None:
                return await asyncio.wait_for(task, timeout=timeout_seconds)
            return await task
        except TimeoutError:
            await self.cancel_call(tool_call.call_id)
            return ToolResult(output="Tool execution timed out.", is_error=True)
        except asyncio.CancelledError:
            return ToolResult(output="Tool execution cancelled.", is_error=True)
        except Exception as exc:
            # Include the actual error message so the LLM knows WHY
            # the tool failed and can adjust its approach.
            error_detail = str(exc)[:1000]
            return ToolResult(output=f"Tool execution failed: {error_detail}", is_error=True)
        finally:
            self._active_calls.pop(tool_call.call_id, None)

    async def cancel_call(self, call_id: str) -> None:
        """Cancel a running tool execution task."""

        task = self._active_calls.get(call_id)
        if task is not None and not task.done():
            task.cancel()


class InProcessExecutorProvider:
    """In-process executor provider for MVP tool execution."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        status_provider: StatusProvider | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.status_provider = status_provider
        self._active: dict[str, _ExecutorRuntime] = {}
        self.breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        """Spawn a new in-process executor runtime."""

        start = perf_counter()
        _validate_unique_server_names(config)
        handle = ExecutorHandle(
            executor_id=config.executor_id,
            executor_type="in_process",
            capabilities=ExecutorCapabilities(),
            metadata=dict(config.metadata),
        )
        system_handlers = build_system_tool_handlers(self.session_factory, self.status_provider)
        native_handlers = executor_tool_handlers()
        mcp_clients: dict[str, StdioMCPClient] = {}
        try:
            mcp_clients = await self.breaker.call(lambda: self._start_mcp_clients(config))
            discovered_tools = await self._discover_mcp_tools(config, mcp_clients)
            runtime_tools = [*config.tools, *discovered_tools]
            handle.capabilities = ExecutorCapabilities(tools=[tool.name for tool in runtime_tools])
            registry = ToolRegistry()
            for tool in runtime_tools:
                handler = _build_runtime_handler(
                    tool, system_handlers, mcp_clients, native_handlers
                )
                registry.register(RegisteredTool(definition=tool, handler=cast(Any, handler)))
            # LSP manager (best-effort, non-fatal)
            # Read config from env vars via CognisConfig. The metadata dict
            # may override these (e.g. in tests), but env vars are the
            # primary source — this avoids requiring callers to thread
            # config through ExecutorConfig.metadata.
            lsp_manager: LSPManager | None = None
            runtime_metadata = dict(config.metadata)
            from cognis.config import load_config as _load_config

            _cfg = _load_config()
            lsp_enabled = config.metadata.get("lsp_enabled", _cfg.lsp_enabled)
            if lsp_enabled:
                try:
                    lsp_manager = LSPManager(
                        enabled=True,
                        auto_install=bool(
                            config.metadata.get("lsp_auto_install", _cfg.lsp_auto_install)
                        ),
                        diagnostics_timeout_ms=int(
                            config.metadata.get(
                                "lsp_diagnostics_timeout_ms", _cfg.lsp_diagnostics_timeout_ms
                            )
                        ),
                        idle_timeout_seconds=int(
                            config.metadata.get(
                                "lsp_idle_timeout_seconds", _cfg.lsp_idle_timeout_seconds
                            )
                        ),
                        max_concurrent_servers=int(
                            config.metadata.get(
                                "lsp_max_concurrent_servers", _cfg.lsp_max_concurrent_servers
                            )
                        ),
                    )
                    runtime_metadata[LSP_MANAGER_KEY] = lsp_manager
                    _logger.info(
                        "lsp: manager created for executor",
                        extra={"extra_data": {"executor_id": config.executor_id}},
                    )
                except Exception:
                    _logger.warning(
                        "lsp: failed to create manager, continuing without LSP",
                        extra={"extra_data": {"executor_id": config.executor_id}},
                        exc_info=True,
                    )

            connection = InProcessExecutorConnection(
                handle,
                registry,
                CircuitBreaker(failure_threshold=5, recovery_timeout=30.0),
                runtime_metadata,
            )
        except TimeoutError:
            outcome = "timeout"
            EXECUTOR_SPAWN_TOTAL.labels(outcome=outcome).inc()
            EXECUTOR_SPAWN_DURATION.labels(outcome=outcome).observe(perf_counter() - start)
            await _close_clients(mcp_clients)
            raise
        except Exception:
            outcome = "failure"
            EXECUTOR_SPAWN_TOTAL.labels(outcome=outcome).inc()
            EXECUTOR_SPAWN_DURATION.labels(outcome=outcome).observe(perf_counter() - start)
            await _close_clients(mcp_clients)
            raise
        self._active[handle.executor_id] = _ExecutorRuntime(
            handle=handle,
            connection=connection,
            mcp_clients=mcp_clients,
            lsp_manager=lsp_manager,
        )
        EXECUTOR_SPAWN_TOTAL.labels(outcome="success").inc()
        EXECUTOR_SPAWN_DURATION.labels(outcome="success").observe(perf_counter() - start)
        return handle

    async def get_executor(self, handle: ExecutorHandle) -> InProcessExecutorConnection:
        """Return a live executor connection for the handle."""

        runtime = self._active.get(handle.executor_id)
        if runtime is None:
            raise KeyError(f"Unknown executor: {handle.executor_id}")
        return runtime.connection

    async def cancel(self, handle: ExecutorHandle) -> None:
        """Cancel and remove an active executor runtime."""

        runtime = self._active.pop(handle.executor_id, None)
        if runtime is None:
            return
        await _close_clients(runtime.mcp_clients)
        if runtime.lsp_manager is not None:
            try:
                await runtime.lsp_manager.cleanup()
            except Exception:
                _logger.debug(
                    "lsp: cleanup error during executor cancel",
                    extra={"extra_data": {"executor_id": handle.executor_id}},
                )

    async def list_active(self) -> list[ExecutorHandle]:
        """List active executor handles."""

        return [runtime.handle for runtime in self._active.values()]

    def get_lsp_managers(self) -> list[LSPManager]:
        """Return all active LSP managers across executor runtimes."""
        return [rt.lsp_manager for rt in self._active.values() if rt.lsp_manager is not None]

    async def cleanup(self) -> None:
        """Close all active executor runtimes."""

        for executor_id in list(self._active):
            await self.cancel(self._active[executor_id].handle)

    async def health(self) -> ProviderHealth:
        """Return executor provider health."""

        return ProviderHealth(
            name="executor",
            status="healthy",
            circuit_state=self.breaker.state,
            details={"active_executors": len(self._active)},
        )

    async def _start_mcp_clients(self, config: ExecutorConfig) -> dict[str, StdioMCPClient]:
        clients: dict[str, StdioMCPClient] = {}
        try:
            for server in config.mcp_servers:
                resolved_env = _resolve_secret_refs(server.env, config.secrets)
                client = StdioMCPClient(server, env={**resolved_env, **config.secrets})
                await client.start()
                clients[server.name] = client
        except Exception:
            await _close_clients(clients)
            raise
        return clients

    async def _discover_mcp_tools(
        self,
        config: ExecutorConfig,
        clients: dict[str, StdioMCPClient],
    ) -> list[Any]:
        discovered_tools: list[Any] = []
        for server in config.mcp_servers:
            tools = await clients[server.name].list_tools()
            discovered_tools.extend(
                mcp_tools_to_definitions(server.name, tools, timeout_seconds=server.timeout_seconds)
            )
        return discovered_tools


def _build_runtime_handler(
    tool: Any,
    system_handlers: dict[str, Callable[[dict[str, Any], ToolExecutionContext], Awaitable[Any]]],
    mcp_clients: dict[str, StdioMCPClient],
    native_handlers: dict[str, Any] | None = None,
) -> Callable[[dict[str, Any], ToolExecutionContext], Awaitable[Any]] | None:
    if tool.source.type == "executor" and native_handlers:
        return native_handlers.get(tool.name)
    if tool.source.type == "builtin":
        return system_handlers.get(tool.name)
    if tool.source.type == "local_mcp" and tool.source.server_name is not None:

        async def local_mcp_handler(
            arguments: dict[str, Any], context: ToolExecutionContext
        ) -> str:
            del context
            client = mcp_clients[tool.source.server_name]
            raw_tool_name = tool.source.raw_tool_name or tool.name
            return await client.call_tool(raw_tool_name, arguments)

        return local_mcp_handler
    return None


async def _close_clients(clients: dict[str, StdioMCPClient]) -> None:
    for client in clients.values():
        await client.close()


def _normalize_tool_result(result: Any, duration_ms: int) -> ToolResult:
    if isinstance(result, ToolResult):
        return result.model_copy(update={"duration_ms": result.duration_ms or duration_ms})
    if isinstance(result, (dict, list)):
        output = json.dumps(result, sort_keys=True, default=str)
    elif isinstance(result, str):
        output = result
    else:
        output = str(result)
    return ToolResult(output=output, duration_ms=duration_ms)


def _resolve_secret_refs(env: dict[str, str], secrets: dict[str, str]) -> dict[str, str]:
    """Resolve ``$secret:NAME`` references in MCP server environment variables.

    Values starting with ``$secret:`` are replaced with the corresponding
    secret from the resolved secrets dict.  All other values pass through
    unchanged.
    """
    resolved: dict[str, str] = {}
    for key, value in env.items():
        if value.startswith("$secret:"):
            secret_name = value[len("$secret:") :]
            resolved[key] = secrets.get(secret_name, "")
        else:
            resolved[key] = value
    return resolved


def _validate_unique_server_names(config: ExecutorConfig) -> None:
    server_names = [server.name for server in config.mcp_servers]
    if len(server_names) != len(set(server_names)):
        raise ValueError("MCP server names must be unique per executor")
