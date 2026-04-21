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
from cognis.tools.executor.browser.manager import BROWSER_MANAGER_KEY, BrowserManager
from cognis.tools.executor.definitions import executor_tool_handlers
from cognis.tools.executor.file_freshness import get_file_freshness_tracker
from cognis.tools.executor.lsp import (
    LSP_MANAGER_KEY,
    LSPManager,
    LSPStatusReport,
    build_lsp_manager,
    build_lsp_status_report,
    build_lsp_unavailable_report,
    cleanup_lsp_manager,
)
from cognis.tools.executor.project_context import (
    INTERNAL_PROJECT_CONTEXT_PROBE_TOOL,
    handle_project_context_probe,
)
from cognis.tools.executor.shell import cleanup_shell_manager
from cognis.tools.mcp import (
    MCPClient,
    build_mcp_client,
    mcp_tools_to_definitions,
    runtime_mcp_server_key,
)
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
    mcp_clients: dict[str, MCPClient] = field(default_factory=dict)
    lsp_manager: LSPManager | None = None


class InProcessExecutorConnection:
    """Direct in-memory implementation of the executor JSON-RPC protocol."""

    def __init__(
        self,
        handle: ExecutorHandle,
        registry: ToolRegistry,
        breaker: CircuitBreaker,
        runtime_metadata: dict[str, Any] | None = None,
        internal_handlers: dict[str, Any] | None = None,
    ) -> None:
        self.handle = handle
        self.registry = registry
        self.breaker = breaker
        self.runtime_metadata = runtime_metadata or {}
        self.internal_handlers = dict(internal_handlers or {})
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
        handler = None if registered_tool is None else registered_tool.handler
        if handler is None:
            handler = self.internal_handlers.get(tool_call.name)
        if handler is None:
            return ToolResult(output="Tool is not executable on this executor.", is_error=True)

        async def invoke_handler() -> ToolResult:
            start = perf_counter()
            context = ToolExecutionContext(
                executor_handle=self.handle,
                runtime_metadata={**self.runtime_metadata, **tool_call.runtime_metadata},
                shared_runtime_metadata=self.runtime_metadata,
                execution_scope_id=tool_call.execution_scope_id or self.handle.executor_id,
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
        mcp_clients: dict[str, MCPClient] = {}
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
            lsp_manager: LSPManager | None = None
            runtime_metadata = dict(config.metadata)
            get_file_freshness_tracker(runtime_metadata)
            try:
                lsp_manager = build_lsp_manager(config.metadata)
                if lsp_manager is not None:
                    runtime_metadata[LSP_MANAGER_KEY] = lsp_manager
                    _logger.info(
                        "lsp: manager created for executor",
                        extra={"extra_data": {"executor_id": config.executor_id}},
                    )
            except Exception:
                runtime_metadata["lsp_init_failed"] = True
                runtime_metadata["lsp_warning"] = "LSP manager initialization failed."
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
                internal_handlers={INTERNAL_PROJECT_CONTEXT_PROBE_TOOL: handle_project_context_probe},
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
        await cleanup_lsp_manager(runtime.lsp_manager, executor_id=handle.executor_id)
        browser_manager = runtime.connection.runtime_metadata.get(BROWSER_MANAGER_KEY)
        if isinstance(browser_manager, BrowserManager):
            try:
                await browser_manager.cleanup()
            except Exception:
                _logger.debug(
                    "browser: cleanup error during executor cancel",
                    extra={"extra_data": {"executor_id": handle.executor_id}},
                )
        try:
            await cleanup_shell_manager(runtime.connection.runtime_metadata)
        except Exception:
            _logger.debug(
                "shell: cleanup error during executor cancel",
                extra={"extra_data": {"executor_id": handle.executor_id}},
            )

    async def list_active(self) -> list[ExecutorHandle]:
        """List active executor handles."""

        return [runtime.handle for runtime in self._active.values()]

    async def get_lsp_statuses(self, *, owner_email: str | None = None) -> list[LSPStatusReport]:
        """Return normalized LSP status for active in-process runtimes."""
        reports: list[LSPStatusReport] = []
        for runtime in self._active.values():
            runtime_owner = runtime.connection.runtime_metadata.get("user_email")
            if owner_email is not None and runtime_owner != owner_email:
                continue
            try:
                report = await build_lsp_status_report(
                    manager=runtime.lsp_manager,
                    executor_id=runtime.handle.executor_id,
                    executor_type=runtime.handle.executor_type,
                    source=runtime.connection.runtime_metadata,
                )
            except Exception:
                _logger.debug(
                    "lsp: failed to build in-process status",
                    extra={"extra_data": {"executor_id": runtime.handle.executor_id}},
                    exc_info=True,
                )
                report = build_lsp_unavailable_report(
                    executor_id=runtime.handle.executor_id,
                    executor_type=runtime.handle.executor_type,
                    source=runtime.connection.runtime_metadata,
                    state="unavailable",
                    warning="Failed to collect in-process LSP status.",
                )
            reports.append(report)
        return reports

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

    async def _start_mcp_clients(self, config: ExecutorConfig) -> dict[str, MCPClient]:
        clients: dict[str, MCPClient] = {}
        try:
            for server in config.mcp_servers:
                client = build_mcp_client(server, config.secrets)
                await client.connect()
                clients[runtime_mcp_server_key(server)] = client
        except Exception:
            await _close_clients(clients)
            raise
        return clients

    async def _discover_mcp_tools(
        self,
        config: ExecutorConfig,
        clients: dict[str, MCPClient],
    ) -> list[Any]:
        discovered_tools: list[Any] = []
        for server in config.mcp_servers:
            tools = await clients[runtime_mcp_server_key(server)].list_tools()
            discovered_tools.extend(
                mcp_tools_to_definitions(
                    server.name,
                    tools,
                    timeout_seconds=server.timeout_seconds,
                    server_id=server.server_id,
                )
            )
        return discovered_tools


def _build_runtime_handler(
    tool: Any,
    system_handlers: dict[str, Callable[[dict[str, Any], ToolExecutionContext], Awaitable[Any]]],
    mcp_clients: dict[str, MCPClient],
    native_handlers: dict[str, Any] | None = None,
) -> Callable[[dict[str, Any], ToolExecutionContext], Awaitable[Any]] | None:
    if tool.source.type == "executor" and native_handlers:
        return native_handlers.get(tool.name)
    if tool.source.type == "builtin":
        return system_handlers.get(tool.name)
    if tool.source.type == "local_mcp" and tool.source.server_name is not None:

        async def local_mcp_handler(
            arguments: dict[str, Any], context: ToolExecutionContext
        ) -> Any:
            del context
            client = mcp_clients[runtime_mcp_server_key(tool.source)]
            raw_tool_name = tool.source.raw_tool_name or tool.name
            return await client.call_tool(raw_tool_name, arguments)

        return local_mcp_handler
    if tool.source.type == "skill" and getattr(tool, "execution_metadata", None):
        return _build_skill_handler(tool)
    return None


def _build_skill_handler(
    tool: Any,
    *,
    artifact_store: Any | None = None,
) -> Callable[[dict[str, Any], ToolExecutionContext], Awaitable[Any]] | None:
    """Build an executor handler for an executable skill tool.

    Supports ``script`` and ``command`` recipe modes.  Assets are staged
    to a temporary directory, the recipe is executed via subprocess, and
    temp files are cleaned up afterward.
    """
    import asyncio
    import hashlib
    import shutil
    import tempfile
    from pathlib import Path

    exec_meta = tool.execution_metadata
    if not exec_meta or "recipe" not in exec_meta:
        return None

    recipe = exec_meta["recipe"]
    mode = recipe.get("mode", "command")
    entry = recipe.get("entry", "")
    recipe_args = recipe.get("args", [])
    recipe_env = recipe.get("env", {})
    recipe_timeout = recipe.get("timeout_seconds", 60)
    working_dir = recipe.get("working_dir")

    def resolve_staged_path(base_dir: Path, relative_path: str) -> Path:
        target = (base_dir / relative_path).resolve()
        if not target.is_relative_to(base_dir.resolve()):
            raise ValueError(f"Unsafe recipe path rejected: {relative_path}")
        return target

    async def skill_handler(arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        staging_dir = Path(tempfile.mkdtemp(prefix="cognis_skill_"))
        try:
            try:
                async def _run() -> str:
                    proc: asyncio.subprocess.Process | None = None
                    runtime_artifact_store = artifact_store
                    if runtime_artifact_store is None and context.shared_runtime_metadata is not None:
                        runtime_artifact_store = context.shared_runtime_metadata.get(
                            "artifact_store"
                        )
                    asset_manifest = exec_meta.get("asset_manifest", [])
                    for asset in asset_manifest:
                        filename = asset.get("filename", "")
                        if not filename:
                            continue
                        asset_path = resolve_staged_path(staging_dir, filename)
                        asset_path.parent.mkdir(parents=True, exist_ok=True)
                        artifact_ns = asset.get("artifact_namespace", "skills")
                        artifact_oid = asset.get("artifact_object_id", "")
                        if artifact_oid:
                            try:
                                if runtime_artifact_store is None:
                                    return "Artifact store is not available for skill asset staging"
                                content, _ct = await runtime_artifact_store.async_load(
                                    artifact_ns, artifact_oid, filename
                                )
                                asset_path.write_bytes(content)
                                expected_hash = asset.get("content_hash", "")
                                if expected_hash:
                                    actual_hash = hashlib.sha256(content).hexdigest()
                                    if actual_hash != expected_hash:
                                        return f"Asset hash mismatch for {filename}"
                            except Exception as exc:
                                return f"Failed to stage asset {filename}: {exc}"

                    env = dict(context.runtime_metadata.get("env", {}))
                    env.update(recipe_env)
                    secret_placeholders = exec_meta.get("secret_placeholders", [])
                    secrets = context.runtime_metadata.get("secrets", {})
                    for placeholder in secret_placeholders:
                        if placeholder in secrets:
                            env[placeholder] = secrets[placeholder]
                    env["SKILL_STAGING_DIR"] = str(staging_dir)

                    cwd = resolve_staged_path(staging_dir, working_dir) if working_dir else staging_dir

                    if mode == "script":
                        script_path = resolve_staged_path(staging_dir, entry)
                        if not script_path.exists():
                            return f"Script not found: {entry}"
                        script_path.chmod(0o755)
                        cmd = [str(script_path), *recipe_args]
                    elif mode == "command":
                        cmd = [entry, *recipe_args]
                    else:
                        return f"Unsupported recipe mode: {mode}"

                    for key, value in arguments.items():
                        cmd = [c.replace(f"{{{key}}}", str(value)) for c in cmd]
                        env[f"SKILL_ARG_{key.upper()}"] = str(value)

                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(cwd),
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = await proc.communicate()
                    except asyncio.CancelledError:
                        if proc.returncode is None:
                            proc.kill()
                            await proc.wait()
                        raise
                    output = stdout.decode(errors="replace")
                    if proc.returncode != 0:
                        err = stderr.decode(errors="replace")
                        return f"Exit code {proc.returncode}\n{output}\n{err}".strip()
                    return output

                return await asyncio.wait_for(_run(), timeout=recipe_timeout)
            except TimeoutError:
                return f"Skill tool execution timed out after {recipe_timeout}s"

        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return skill_handler


async def _close_clients(clients: dict[str, MCPClient]) -> None:
    for client in clients.values():
        await client.close(suppress_cancelled=True)


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


def _validate_unique_server_names(config: ExecutorConfig) -> None:
    server_names = [server.name for server in config.mcp_servers]
    if len(server_names) != len(set(server_names)):
        raise ValueError("MCP server names must be unique per executor")
