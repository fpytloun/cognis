"""In-process executor provider with an in-memory JSON-RPC bridge."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, cast

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis import json_codec as json
from cognis.executor.lsp_runtime import (
    LSPStatusReport,
    build_lsp_manager,
    build_lsp_status_report,
    build_lsp_unavailable_report,
    cleanup_lsp_manager,
)
from cognis.executor.tool_definitions_runtime import executor_tool_handlers
from cognis.logging import get_logger
from cognis.mcp_runtime import (
    MCPClient,
    build_mcp_client,
    mcp_tools_to_definitions,
    runtime_mcp_server_key,
)
from cognis.models.config import ProviderHealth
from cognis.models.program import ProgramRuntimeBinding
from cognis.models.tool import (
    ExecutorCapabilities,
    ExecutorConfig,
    ExecutorHandle,
    ToolCall,
    ToolResult,
    tool_input_schema,
)
from cognis.providers.base import ToolOutputChunkCallback
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.executor.program_rpc import program_rpc_key, program_rpc_key_from_params
from cognis.tools.builtin.conversations import build_conversation_tool_handlers
from cognis.tools.builtin.system import StatusProvider, build_system_tool_handlers
from cognis.tools.builtin.task_continuation import build_task_continuation_tool_handlers
from cognis.tools.executor.browser.handlers import build_manager_from_config
from cognis.tools.executor.browser.manager import (
    BROWSER_CLEANUP_RETAINER_KEY,
    BROWSER_MANAGER_KEY,
    BrowserLifecycleError,
    BrowserManager,
    BrowserManagerCleanupRetainer,
    BrowserSessionOwner,
)
from cognis.tools.executor.file_freshness import get_file_freshness_tracker
from cognis.tools.executor.lsp.manager import LSP_MANAGER_KEY, LSPManager
from cognis.tools.executor.project_context import (
    INTERNAL_PROJECT_CONTEXT_PROBE_TOOL,
    handle_project_context_probe,
)
from cognis.tools.executor.shell import (
    cleanup_shell_manager,
    set_background_shell_completion_callback,
)
from cognis.tools.registry import (
    RegisteredTool,
    ToolBackgroundControl,
    ToolBackgroundRequestOutcome,
    ToolExecutionContext,
    ToolRegistry,
)

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
        program_runtime: Any | None = None,
    ) -> None:
        self.handle = handle
        self.executor_instance_id = uuid.uuid4().hex
        self.registry = registry
        self.breaker = breaker
        self.runtime_metadata = runtime_metadata or {}
        self.internal_handlers = dict(internal_handlers or {})
        self.program_runtime = program_runtime
        self._active_calls: dict[str, asyncio.Task[Any]] = {}
        self._background_controls: dict[str, ToolBackgroundControl] = {}
        self._program_tool_call_handlers: dict[
            tuple[str, str, str, str, str],
            Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        ] = {}
        self._closed = False

    @property
    def executor_id(self) -> str:
        """Return the identity of the current executor handle."""
        return self.handle.executor_id

    def register_program_tool_call_handler(
        self,
        binding: ProgramRuntimeBinding,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        if (
            binding.executor_id != self.executor_id
            or binding.executor_instance_id != self.executor_instance_id
        ):
            raise ValueError("Program callback executor identity does not match this connection.")
        self._program_tool_call_handlers[program_rpc_key(binding)] = handler

    def unregister_program_tool_call_handler(self, binding: ProgramRuntimeBinding) -> None:
        self._program_tool_call_handlers.pop(program_rpc_key(binding), None)

    async def _program_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        key = program_rpc_key_from_params(params)
        handler = self._program_tool_call_handlers.get(key) if key is not None else None
        if handler is None:
            raise RuntimeError("Program tool-call routing is not active.")
        return await handler(params)

    async def send_program_status(self, params: dict[str, Any]) -> bool:
        update_status = getattr(self.program_runtime, "update_status", None)
        return bool(update_status(params)) if callable(update_status) else False

    async def terminate_program_scope(self, scope_id: str) -> dict[str, Any]:
        terminate = getattr(self.program_runtime, "terminate_scope", None)
        if not callable(terminate):
            return {"closed": 0}
        return cast(
            dict[str, Any],
            await terminate(scope_id, reason="conversation_terminal"),
        )

    async def rpc_call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        """Handle an in-memory JSON-RPC request."""

        if method == "tool.list":
            return {"tools": self.registry.export()}
        if method == "skill.assets.prepare":
            from cognis.executor.skill_assets import prepare_skill_assets

            async with asyncio.timeout(timeout):
                return await prepare_skill_assets(params)
        if method == "fs.digest":
            from cognis.executor.file_digest import file_digests

            async with asyncio.timeout(timeout):
                return await file_digests(params)
        if method == "tool.execute":
            tool_call = ToolCall(
                call_id=str(params.get("call_id") or uuid.uuid4().hex),
                name=str(params["tool_name"]),
                arguments=dict(params.get("arguments") or {}),
                runtime_metadata=dict(params.get("runtime_metadata") or {}),
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
        if method == "tool.background":
            call_id = str(params["call_id"])
            outcome = await self.request_background(call_id)
            return {
                "call_id": call_id,
                "accepted": outcome is ToolBackgroundRequestOutcome.ACCEPTED,
                "outcome": outcome.value,
            }
        if method in {"heartbeat", "executor.heartbeat"}:
            return {"status": "ok", "active_calls": len(self._active_calls)}
        if method == "shell.background_status":
            from cognis.tools.executor.shell import list_background_shell_statuses

            include_completed = bool(params.get("include_completed", False))
            return {
                "shells": await list_background_shell_statuses(
                    self.runtime_metadata,
                    include_completed=include_completed,
                )
            }
        if method == "browser.session_terminal":
            manager = self.runtime_metadata.get(BROWSER_MANAGER_KEY)
            retainer = self.runtime_metadata.get(BROWSER_CLEANUP_RETAINER_KEY)
            if not isinstance(manager, BrowserManager) and not isinstance(
                retainer, BrowserManagerCleanupRetainer
            ):
                return {"closed": 0, "complete": True}
            raw_owner = params.get("owner")
            if not isinstance(raw_owner, dict):
                raise ValueError("browser.session_terminal requires owner metadata")
            owner = BrowserSessionOwner.from_dict(raw_owner)
            closed = 0
            if isinstance(manager, BrowserManager):
                closed += await manager.mark_owner_terminal(owner)
            if isinstance(retainer, BrowserManagerCleanupRetainer):
                closed += await retainer.mark_owner_terminal(owner)
            return {
                "closed": closed,
                "complete": True,
            }
        raise ValueError(f"Unsupported executor method: {method}")

    async def file_digests(self, *, paths: list[str]) -> dict[str, Any]:
        """Read-only SHA-256 of regular files on this in-process executor."""
        return await self.rpc_call("fs.digest", {"paths": list(paths)}, timeout=10.0)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return tool metadata available on the executor."""

        return self.registry.export()

    async def tool_execute(
        self,
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: ToolOutputChunkCallback | None = None,
        before_send: Any | None = None,
        on_sent: Any | None = None,
    ) -> ToolResult:
        """Execute a tool call through the runtime registry."""

        del before_send, on_sent
        if self._closed:
            return ToolResult(output="Executor connection is closed.", is_error=True)
        registered_tool = self.registry.get(tool_call.name)
        handler = None if registered_tool is None else registered_tool.handler
        if handler is None:
            handler = self.internal_handlers.get(tool_call.name)
        if handler is None:
            return ToolResult(output="Tool is not executable on this executor.", is_error=True)
        if registered_tool is not None:
            descriptor = registered_tool.definition.descriptor
            local_hash = descriptor.schema_hash if descriptor is not None else None
            expected_hash = tool_call.runtime_metadata.get("tool_contract_hash")
            if local_hash is not None and expected_hash != local_hash:
                return ToolResult(
                    output="Controller and in-process executor tool contracts do not match.",
                    is_error=True,
                    metadata={
                        "code": "tool_contract_mismatch",
                        "expected_hash": expected_hash,
                        "local_hash": local_hash,
                    },
                )
            from cognis.core.tool_arguments import validate_tool_arguments

            validation_error = validate_tool_arguments(
                tool_call.name,
                tool_call.arguments,
                schema=tool_input_schema(registered_tool.definition),
            )
            if validation_error is not None:
                return ToolResult(
                    output=json.dumps(validation_error.as_tool_result()),
                    is_error=True,
                    metadata={"code": "invalid_tool_arguments"},
                )

        background_control = ToolBackgroundControl()

        async def invoke_handler() -> ToolResult:
            start = perf_counter()
            runtime_metadata = {**self.runtime_metadata, **tool_call.runtime_metadata}
            from cognis.core.program_config import PROGRAM_RUNTIME_TOOL_NAMES

            if tool_call.name in PROGRAM_RUNTIME_TOOL_NAMES:
                runtime_metadata["_program_runtime"] = self.program_runtime
                runtime_metadata["_program_rpc_call"] = self._program_tool_call
            context = ToolExecutionContext(
                executor_handle=self.handle,
                runtime_metadata=runtime_metadata,
                shared_runtime_metadata=self.runtime_metadata,
                execution_scope_id=tool_call.execution_scope_id or self.handle.executor_id,
                output_chunk_callback=output_chunk_callback,
                background_control=background_control,
            )
            try:
                result = await handler(tool_call.arguments, context)
            except BrowserLifecycleError as exc:
                return ToolResult(
                    output=str(exc),
                    is_error=True,
                    metadata={"browser_lifecycle_error": exc.code},
                )
            duration_ms = int((perf_counter() - start) * 1000)
            normalized = _normalize_tool_result(result, duration_ms)
            return normalized.model_copy(
                update={
                    "attachments": await context.binary_transfers.prepare_attachments(
                        normalized.attachments
                    )
                }
            )

        task = asyncio.create_task(self.breaker.call(invoke_handler))
        self._active_calls[tool_call.call_id] = task
        self._background_controls[tool_call.call_id] = background_control
        try:
            if timeout_seconds is not None and timeout_seconds != 0:
                return await asyncio.wait_for(task, timeout=timeout_seconds)
            return await task
        except TimeoutError:
            await self.cancel_call(tool_call.call_id)
            return ToolResult(output="Tool execution timed out.", is_error=True)
        except asyncio.CancelledError:
            return ToolResult(output="Tool execution cancelled.", is_error=True)
        except BrowserLifecycleError as exc:
            return ToolResult(
                output=str(exc),
                is_error=True,
                metadata={"browser_lifecycle_error": exc.code},
            )
        except Exception as exc:
            # Include the actual error message so the LLM knows WHY
            # the tool failed and can adjust its approach.
            error_detail = str(exc)[:1000]
            return ToolResult(output=f"Tool execution failed: {error_detail}", is_error=True)
        finally:
            self._active_calls.pop(tool_call.call_id, None)
            self._background_controls.pop(tool_call.call_id, None)

    async def request_background(self, call_id: str) -> ToolBackgroundRequestOutcome:
        """Ask a running tool call to hand its work off to the background."""

        control = self._background_controls.get(call_id)
        if control is None:
            return ToolBackgroundRequestOutcome.UNKNOWN_CALL
        return control.request()

    async def cancel_call(self, call_id: str) -> None:
        """Cancel a running tool execution task."""

        task = self._active_calls.get(call_id)
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        """Cancel calls owned by this disposable connection, not its PTC runtime."""
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._active_calls.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_calls.clear()
        self._background_controls.clear()


class InProcessExecutorProvider:
    """Own turn connections and configured-executor-scoped PTC runtimes.

    Turn cancellation closes only the connection and its active calls; retained
    PTC runtimes live until their configured executor scope is terminated or
    this provider shuts down.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        status_provider: StatusProvider | None = None,
        guardrails_provider: Any | None = None,
        compaction_strategy: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.status_provider = status_provider
        self.guardrails_provider = guardrails_provider
        self.compaction_strategy = compaction_strategy
        self._active: dict[str, _ExecutorRuntime] = {}
        self._program_runtimes: dict[str, Any] = {}
        self._browser_cleanup_retainer = BrowserManagerCleanupRetainer()
        self.breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self._background_shell_completed_callback: (
            Callable[[str, dict[str, Any]], Awaitable[None]] | None
        ) = None

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
        system_handlers = dict(config.tool_handlers)
        system_handlers.update(
            build_system_tool_handlers(self.session_factory, self.status_provider)
        )
        if self.guardrails_provider is not None:
            system_handlers.update(
                build_conversation_tool_handlers(
                    self.session_factory,
                    self.guardrails_provider,
                    self.compaction_strategy,
                )
            )
        task_continuation_handlers = build_task_continuation_tool_handlers(self.session_factory)
        system_handlers.update(task_continuation_handlers)
        native_handlers = executor_tool_handlers()
        mcp_clients: dict[str, MCPClient] = {}
        try:
            mcp_clients = await self.breaker.call(lambda: self._start_mcp_clients(config))
            discovered_tools = await self._discover_mcp_tools(config, mcp_clients)
            runtime_tools = [*config.tools, *discovered_tools]
            handle.capabilities = ExecutorCapabilities(
                tools=[tool.name for tool in runtime_tools], binary_transfers=True
            )
            registry = ToolRegistry()
            for tool in runtime_tools:
                handler = _build_runtime_handler(
                    tool, system_handlers, mcp_clients, native_handlers
                )
                registry.register(RegisteredTool(definition=tool, handler=cast(Any, handler)))
            lsp_manager: LSPManager | None = None
            runtime_metadata = dict(config.metadata)
            if self._background_shell_completed_callback is not None:
                completion_callback = self._background_shell_completed_callback

                async def _background_shell_completed(status: dict[str, Any]) -> None:
                    await completion_callback(config.executor_id, status)

                set_background_shell_completion_callback(
                    runtime_metadata,
                    _background_shell_completed,
                )
            get_file_freshness_tracker(runtime_metadata)
            # Stage A: eagerly build the BrowserManager so all tool calls for
            # this executor share a single persistent instance.
            _wire_browser_manager(runtime_metadata)
            runtime_metadata[BROWSER_CLEANUP_RETAINER_KEY] = self._browser_cleanup_retainer
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

            program_runtime = None
            program_runtime_executor_id = config.program_runtime_executor_id
            if program_runtime_executor_id is not None:
                if not program_runtime_executor_id.strip():
                    raise ValueError("Program runtime executor identity must not be empty")
                program_runtime = self._program_runtimes.get(program_runtime_executor_id)
                if program_runtime is None:
                    from cognis.executor.program_runtime import (
                        ProgramRuntime,
                        executor_state_ledger_path,
                    )

                    program_runtime = ProgramRuntime(
                        executor_id=program_runtime_executor_id,
                        ledger_path=executor_state_ledger_path(program_runtime_executor_id),
                    )
                    self._program_runtimes[program_runtime_executor_id] = program_runtime

            connection = InProcessExecutorConnection(
                handle,
                registry,
                CircuitBreaker(failure_threshold=5, recovery_timeout=30.0),
                runtime_metadata,
                internal_handlers={
                    INTERNAL_PROJECT_CONTEXT_PROBE_TOOL: handle_project_context_probe
                },
                program_runtime=program_runtime,
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
        """Cancel one turn connection while preserving provider-owned PTC state."""

        runtime = self._active.pop(handle.executor_id, None)
        if runtime is None:
            return
        browser_manager = runtime.connection.runtime_metadata.get(BROWSER_MANAGER_KEY)
        if isinstance(browser_manager, BrowserManager):
            self._browser_cleanup_retainer.retain(browser_manager)
        try:
            await runtime.connection.close()
            await _close_clients(runtime.mcp_clients)
            await cleanup_lsp_manager(runtime.lsp_manager, executor_id=handle.executor_id)
        finally:
            try:
                await cleanup_shell_manager(runtime.connection.runtime_metadata)
            except Exception:
                _logger.debug(
                    "shell: cleanup error during executor cancel",
                    extra={"extra_data": {"executor_id": handle.executor_id}},
                )

    def has_program_runtime(self, executor_id: str) -> bool:
        """Return whether a configured executor owns retained PTC state."""
        return executor_id in self._program_runtimes

    async def terminate_program_scope(
        self,
        scope_id: str,
        *,
        executor_ids: list[str] | None = None,
    ) -> None:
        """Terminate a scope on every retained runtime, including without live turns.

        A conversation can have used multiple configured executors over its
        lifetime, so terminal cleanup must not be limited to the current pin.
        """
        if not scope_id:
            return
        del executor_ids
        for program_runtime in list(self._program_runtimes.values()):
            terminate = getattr(program_runtime, "terminate_scope", None)
            if callable(terminate):
                await terminate(scope_id, reason="conversation_terminal")

    async def list_active(self) -> list[ExecutorHandle]:
        """List active executor handles."""

        return [runtime.handle for runtime in self._active.values()]

    async def request_background(self, call_id: str) -> ToolBackgroundRequestOutcome:
        """Route a background handoff request to the runtime executing ``call_id``."""

        for runtime in list(self._active.values()):
            outcome = await runtime.connection.request_background(call_id)
            if outcome is not ToolBackgroundRequestOutcome.UNKNOWN_CALL:
                return outcome
        return ToolBackgroundRequestOutcome.UNKNOWN_CALL

    def register_background_shell_completed_callback(
        self,
        callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """Register callback for current and future in-process shell completions."""

        self._background_shell_completed_callback = callback
        for runtime in self._active.values():
            if callback is None:
                set_background_shell_completion_callback(
                    runtime.connection.runtime_metadata,
                    None,
                )
                continue

            executor_id = runtime.handle.executor_id

            async def _background_shell_completed(
                status: dict[str, Any],
                *,
                executor_id: str = executor_id,
            ) -> None:
                await callback(executor_id, status)

            set_background_shell_completion_callback(
                runtime.connection.runtime_metadata,
                _background_shell_completed,
            )

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
        """Close active connections and all provider-owned PTC runtimes."""

        for executor_id in list(self._active):
            await self.cancel(self._active[executor_id].handle)
        program_runtimes = list(self._program_runtimes.values())
        self._program_runtimes.clear()
        for program_runtime in program_runtimes:
            close_program_runtime = getattr(program_runtime, "close", None)
            if callable(close_program_runtime):
                try:
                    await close_program_runtime()
                except Exception:
                    _logger.debug(
                        "program runtime cleanup failed during provider shutdown",
                        exc_info=True,
                    )
        await self._browser_cleanup_retainer.wait_until_empty()

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
                    if (
                        runtime_artifact_store is None
                        and context.shared_runtime_metadata is not None
                    ):
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

                    cwd = (
                        resolve_staged_path(staging_dir, working_dir)
                        if working_dir
                        else staging_dir
                    )

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


def _wire_browser_manager(runtime_metadata: dict[str, Any]) -> None:
    """Build and inject a BrowserManager into the persistent runtime dict.

    Skips silently when browser is disabled or config is absent so the
    caller never has to special-case missing browser support.
    """
    if BROWSER_MANAGER_KEY in runtime_metadata:
        return  # already wired (e.g. from a previous configure cycle)
    browser_cfg = runtime_metadata.get("browser")
    if not isinstance(browser_cfg, dict) or not browser_cfg.get("enabled", True):
        return
    try:
        manager = build_manager_from_config(runtime_metadata)
        runtime_metadata[BROWSER_MANAGER_KEY] = manager
    except Exception as exc:
        _logger.warning(
            "browser: failed to build BrowserManager for in-process executor: %s",
            exc,
        )


def _validate_unique_server_names(config: ExecutorConfig) -> None:
    server_names = [server.name for server in config.mcp_servers]
    if len(server_names) != len(set(server_names)):
        raise ValueError("MCP server names must be unique per executor")
