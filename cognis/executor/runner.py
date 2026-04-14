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
from cognis.models.tool import (
    ExecutorConfig,
    MCPServerConfig,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolSource,
)
from cognis.tools.executor.definitions import executor_tool_definitions, executor_tool_handlers
from cognis.tools.executor.file_freshness import _FILE_FRESHNESS_KEY, get_file_freshness_tracker
from cognis.tools.executor.lsp import (
    LSP_MANAGER_KEY,
    LSP_STATUS_CAPABILITY,
    build_lsp_manager,
    build_lsp_status_report,
    cleanup_lsp_manager,
    resolve_lsp_runtime_config,
)
from cognis.tools.mcp import (
    MCPClient,
    MCPClientError,
    _safe_message,
    build_mcp_client,
    mcp_tools_to_definitions,
    runtime_mcp_server_key,
    validate_unique_server_names,
)

logger = logging.getLogger("cognis.executor.runner")

_HEARTBEAT_INTERVAL = 15
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 60.0
_MCP_PREPARE_TOTAL_TIMEOUT_SECONDS = 90.0
_RUNTIME_METADATA_SCHEMA_VERSION = 1


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
        self._mcp_clients: dict[str, MCPClient] = {}
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
            logger.info("Executor shutting down, cleaning up resources")
            browser_manager = self._runtime_metadata.get("browser_manager")
            if browser_manager is not None:
                with contextlib.suppress(Exception):
                    await browser_manager.cleanup()
            lsp_manager = self._runtime_metadata.get(LSP_MANAGER_KEY)
            if lsp_manager is not None:
                with contextlib.suppress(Exception):
                    await cleanup_lsp_manager(lsp_manager, executor_id=self.config.executor_id)
            await self._close_mcp_clients()
            if self._channel_handler is not None:
                with contextlib.suppress(Exception):
                    await self._channel_handler.stop_all()
            if self._inference_handler is not None:
                with contextlib.suppress(Exception):
                    await self._inference_handler.close()
            logger.info("Executor shutdown complete")

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
        self._config_version = 0
        self._tool_handlers = {}
        self._configured_tool_definitions = []

        logger.info("Connecting to controller at %s", url)
        async with websockets.connect(url, compression="deflate", max_size=10 * 1024 * 1024) as ws:
            logger.info("WebSocket connected, sending executor.ready")
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

            logger.info(
                "Registered with controller as %s",
                response.get("result", {}).get("executor_id", "unknown"),
            )

            # Update channel handler WS reference early so surviving
            # channel adapters can forward inbound messages to the new
            # controller immediately, rather than silently dropping them
            # until executor.configure completes.
            if self._channel_handler is not None:
                self._channel_handler.set_ws(ws)

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                await self._message_loop(ws)
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _message_loop(self, ws: Any) -> None:
        logger.info("Entering message loop, waiting for controller commands")
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
                logger.debug("Received tool.list request")
                await self._handle_tool_list(ws, msg_id)
            elif method == "tool.execute":
                tool_name = params.get("tool_name", params.get("name", "?"))
                logger.debug("Received tool.execute: %s", tool_name)
                task = asyncio.create_task(self._handle_tool_execute(ws, msg_id, params))
                self._active_calls[params.get("call_id", msg_id)] = task
            elif method == "tool.cancel":
                call_id = params.get("call_id")
                logger.debug("Received tool.cancel: %s", call_id)
                if call_id and call_id in self._active_calls:
                    self._active_calls[call_id].cancel()
            elif method == "llm.complete":
                logger.debug("Received llm.complete")
                asyncio.create_task(self._handle_llm_complete(ws, msg_id, params))
            elif method == "llm.transcribe":
                logger.debug("Received llm.transcribe")
                asyncio.create_task(self._handle_llm_transcribe(ws, msg_id, params))
            elif method == "channel.start":
                logger.info("Received channel.start for account %s", params.get("account_id", "?"))
                asyncio.create_task(self._handle_channel_start(ws, msg_id, params))
            elif method == "channel.stop":
                logger.info("Received channel.stop for account %s", params.get("account_id", "?"))
                asyncio.create_task(self._handle_channel_stop(ws, msg_id, params))
            elif method == "channel.send":
                logger.debug("Received channel.send for account %s", params.get("account_id", "?"))
                asyncio.create_task(self._handle_channel_send(ws, msg_id, params))
            elif method == "channel.fetch_media":
                asyncio.create_task(self._handle_channel_fetch_media(ws, msg_id, params))
            elif method == "channel.typing":
                asyncio.create_task(self._handle_channel_typing(ws, msg_id, params))
            elif method == "channel.mark_read":
                asyncio.create_task(self._handle_channel_mark_read(ws, msg_id, params))
            elif method == "channel.sync_profile":
                asyncio.create_task(self._handle_channel_sync_profile(ws, msg_id, params))
            elif method == "lsp.status":
                asyncio.create_task(self._handle_lsp_status(ws, msg_id, params))
            elif method == "executor.cancel":
                logger.info("Received executor.cancel, shutting down")
                self._running = False
                break
            else:
                logger.debug("Received unknown method: %s", method)

    async def _handle_configure(self, ws: Any, msg_id: str | None, params: dict[str, Any]) -> None:
        requested_version = int(params.get("config_version") or (self._config_version + 1))
        mcp_servers_raw = params.get("mcp_servers") or []
        logger.info(
            "Received executor.configure v%d (current v%d, %d MCP server(s))",
            requested_version,
            self._config_version,
            len(mcp_servers_raw),
        )
        if requested_version <= self._config_version:
            logger.warning(
                "Ignoring stale executor.configure v%d (already at v%d)",
                requested_version,
                self._config_version,
            )
            await self._send_rpc_error(ws, msg_id, -32020, "Stale executor.configure version")
            return

        previous_configured = self._configured
        previous_runtime_state = self._runtime_state
        self._runtime_state = "reconfiguring"

        config = params.get("config", {})
        enabled_tools = params.get("enabled_tools", [])
        enabled_tool_groups = params.get("enabled_tool_groups", [])
        secrets = dict(params.get("secrets") or {})

        previous_clients = self._mcp_clients
        previous_tool_handlers = dict(self._tool_handlers)
        previous_tool_definitions = list(self._configured_tool_definitions)
        previous_runtime_metadata = dict(self._runtime_metadata)
        previous_lsp_manager = previous_runtime_metadata.get(LSP_MANAGER_KEY)

        try:
            mcp_servers = [MCPServerConfig.model_validate(item) for item in mcp_servers_raw]
            validate_unique_server_names(mcp_servers)
            (
                staged_mcp_clients,
                discovered_tools,
                mcp_statuses,
                mcp_warnings,
            ) = await asyncio.wait_for(
                self._prepare_mcp_runtime(mcp_servers, secrets),
                timeout=_MCP_PREPARE_TOTAL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "Configure v%d failed during MCP preparation: %s", requested_version, exc
            )
            self._mcp_clients = previous_clients
            self._tool_handlers = previous_tool_handlers
            self._configured_tool_definitions = previous_tool_definitions
            self._runtime_metadata = previous_runtime_metadata
            self._configured = previous_configured
            self._runtime_state = "blocked" if not previous_configured else previous_runtime_state
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
        browser_config = config.get("browser") if isinstance(config, dict) else {}
        from cognis.tools.executor.web.definitions import web_tool_definitions

        web_defs = web_tool_definitions(web_backends)
        # Store web runtime metadata for handler context
        runtime_state = "degraded" if mcp_warnings else "active"
        try:
            effective_lsp_config = resolve_lsp_runtime_config(
                config if isinstance(config, dict) else {}
            )
            self._runtime_metadata = {
                "schema_version": _RUNTIME_METADATA_SCHEMA_VERSION,
                "configure_capabilities": ["mcp_runtime_status_v1", LSP_STATUS_CAPABILITY],
                "legacy_metadata": False,
                "lsp_enabled": effective_lsp_config.enabled,
                "lsp_auto_install": effective_lsp_config.auto_install,
                "lsp_diagnostics_timeout_ms": effective_lsp_config.diagnostics_timeout_ms,
                "lsp_idle_timeout_seconds": effective_lsp_config.idle_timeout_seconds,
                "lsp_max_concurrent_servers": effective_lsp_config.max_concurrent_servers,
                "web_backend": web_config_raw.get("web_backend", "direct"),
                "web_available_backends": web_backends,
                "web_secrets": secrets,
                "browser": browser_config if isinstance(browser_config, dict) else {},
                "environment": _build_environment_payload(),
                "mcp_servers": mcp_statuses,
                "warnings": mcp_warnings,
            }
            get_file_freshness_tracker(self._runtime_metadata)
            try:
                lsp_manager = build_lsp_manager(config if isinstance(config, dict) else {})
            except Exception:
                self._runtime_metadata["lsp_init_failed"] = True
                self._runtime_metadata["lsp_warning"] = "LSP manager initialization failed."
                logger.warning(
                    "Failed to initialize LSP manager for executor runtime", exc_info=True
                )
                lsp_manager = None
            if lsp_manager is not None:
                self._runtime_metadata[LSP_MANAGER_KEY] = lsp_manager

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

            old_clients = self._mcp_clients
            self._mcp_clients = staged_mcp_clients

            # Register skill tool handlers from controller-provided manifests
            skill_manifests_raw = params.get("skill_manifests") or []
            await self._register_skill_handlers(skill_manifests_raw, secrets)

            if self._inference_handler is None:
                from cognis.executor.inference import InferenceHandler

                self._inference_handler = InferenceHandler()
            if self._channel_handler is None:
                from cognis.executor.channel_handler import ChannelHandler

                self._channel_handler = ChannelHandler()
            self._channel_handler.set_ws(ws)
            self._channel_handler.set_executor_config(config)

            if old_clients is not previous_clients:
                await self._close_clients(old_clients)
            elif previous_clients is not staged_mcp_clients:
                await self._close_clients(previous_clients)
            old_browser_manager = previous_runtime_metadata.get("browser_manager")
            if (
                old_browser_manager is not None
                and old_browser_manager is not self._runtime_metadata.get("browser_manager")
            ):
                with contextlib.suppress(Exception):
                    await old_browser_manager.cleanup()
            if previous_lsp_manager is not self._runtime_metadata.get(LSP_MANAGER_KEY):
                await cleanup_lsp_manager(previous_lsp_manager, executor_id=self.config.executor_id)
        except Exception as exc:
            logger.warning(
                "Configure v%d failed during tool/handler setup: %s", requested_version, exc
            )
            current_lsp_manager = self._runtime_metadata.get(LSP_MANAGER_KEY)
            await self._close_clients(staged_mcp_clients)
            self._mcp_clients = previous_clients
            self._tool_handlers = previous_tool_handlers
            self._configured_tool_definitions = previous_tool_definitions
            self._runtime_metadata = previous_runtime_metadata
            self._configured = previous_configured
            self._runtime_state = "blocked" if not previous_configured else previous_runtime_state
            if current_lsp_manager is not previous_lsp_manager:
                await cleanup_lsp_manager(current_lsp_manager, executor_id=self.config.executor_id)
            await self._send_rpc_error(ws, msg_id, -32021, f"Executor configure failed: {exc}")
            return

        self._config_version = requested_version
        self._configured = True
        self._runtime_state = runtime_state
        logger.info(
            "Configure v%d complete: state=%s, %d tool(s), %d MCP client(s)%s",
            requested_version,
            runtime_state,
            len(self._configured_tool_definitions),
            len(self._mcp_clients),
            f", warnings: {mcp_warnings}" if mcp_warnings else "",
        )
        if msg_id is not None:
            await self._send_rpc_result(
                ws,
                msg_id,
                {
                    "status": "configured",
                    "applied_version": self._config_version,
                    "ready": True,
                    "runtime_state": runtime_state,
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
                    "runtime_metadata": self._public_runtime_metadata(),
                },
            )

    async def _register_skill_handlers(
        self, skill_manifests_raw: list[dict[str, Any]], secrets: dict[str, str]
    ) -> None:
        """Register executable skill tool handlers from controller-provided manifests.

        Each manifest contains skill metadata, tool specs with recipes,
        and asset references with signed URLs for staging.
        """
        import hashlib
        import shutil
        import tempfile
        from pathlib import Path

        import httpx

        for manifest in skill_manifests_raw:
            skill_id = manifest.get("skill_id", "")
            skill_tools = manifest.get("tools", [])
            asset_manifest = manifest.get("asset_manifest", [])

            # Stage assets for this skill
            staging_dir = Path(tempfile.mkdtemp(prefix=f"cognis_skill_{skill_id[:8]}_"))
            staged_ok = True
            for asset in asset_manifest:
                filename = asset.get("filename", "")
                signed_url = asset.get("signed_url", "")
                expected_hash = asset.get("content_hash", "")
                if not filename or not signed_url:
                    continue
                # Path traversal protection
                asset_path = (staging_dir / filename).resolve()
                if not str(asset_path).startswith(str(staging_dir.resolve())):
                    logger.warning(
                        "Unsafe skill asset path rejected",
                        extra={"extra_data": {"skill_id": skill_id, "filename": filename}},
                    )
                    staged_ok = False
                    break
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(signed_url)
                        resp.raise_for_status()
                    content = resp.content
                    if expected_hash:
                        actual_hash = hashlib.sha256(content).hexdigest()
                        if actual_hash != expected_hash:
                            logger.warning(
                                "Skill asset hash mismatch",
                                extra={"extra_data": {"skill_id": skill_id, "filename": filename}},
                            )
                            staged_ok = False
                            break
                    asset_path.write_bytes(content)
                except Exception:
                    logger.warning(
                        "Failed to stage skill asset",
                        extra={"extra_data": {"skill_id": skill_id, "filename": filename}},
                        exc_info=True,
                    )
                    staged_ok = False
                    break

            if not staged_ok:
                shutil.rmtree(staging_dir, ignore_errors=True)
                continue

            # Register handlers for each tool in this skill
            for tool_spec in skill_tools:
                tool_name = tool_spec.get("name", "")
                recipe = tool_spec.get("recipe")
                if not tool_name or not recipe:
                    continue

                tool_def = ToolDefinition(
                    name=tool_name,
                    description=tool_spec.get("description", ""),
                    parameters=tool_spec.get("parameters", {"type": "object", "properties": {}}),
                    source=ToolSource(
                        type="skill",
                        skill_id=skill_id,
                        skill_version_id=manifest.get("version_id"),
                    ),
                    category="skill",
                    read_only=bool(tool_spec.get("read_only", False)),
                    non_bypassable=True,
                    timeout_seconds=int(tool_spec.get("timeout_seconds", 60)),
                )
                self._configured_tool_definitions.append(tool_def)
                self._tool_handlers[tool_name] = self._build_skill_recipe_handler(
                    recipe, staging_dir, secrets
                )

            # Track staging dir for cleanup
            if not hasattr(self, "_skill_staging_dirs"):
                self._skill_staging_dirs: list[Path] = []
            self._skill_staging_dirs.append(staging_dir)

    def _build_skill_recipe_handler(
        self,
        recipe: dict[str, Any],
        staging_dir: Any,
        secrets: dict[str, str],
    ) -> Any:
        """Build a handler closure for a skill tool recipe."""
        import asyncio
        from pathlib import Path

        mode = recipe.get("mode", "command")
        entry = recipe.get("entry", "")
        recipe_args = recipe.get("args", [])
        recipe_env = recipe.get("env", {})
        recipe_timeout = recipe.get("timeout_seconds", 60)
        working_dir = recipe.get("working_dir")
        secret_placeholders = recipe.get("secret_placeholders", [])

        async def handler(arguments: dict[str, Any], context: Any) -> str:
            env = dict(os.environ)
            env.update(recipe_env)
            for placeholder in secret_placeholders:
                if placeholder in secrets:
                    env[placeholder] = secrets[placeholder]
            env["SKILL_STAGING_DIR"] = str(staging_dir)

            cwd = Path(staging_dir) / working_dir if working_dir else staging_dir

            if mode == "script":
                script_path = Path(staging_dir) / entry
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
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=recipe_timeout)
            except TimeoutError:
                proc.kill()
                return f"Skill tool execution timed out after {recipe_timeout}s"

            output = stdout.decode(errors="replace")
            if proc.returncode != 0:
                err = stderr.decode(errors="replace")
                return f"Exit code {proc.returncode}\n{output}\n{err}".strip()
            return output

        return handler

    async def _handle_lsp_status(self, ws: Any, msg_id: str | None, _: dict[str, Any]) -> None:
        report = await build_lsp_status_report(
            manager=self._runtime_metadata.get(LSP_MANAGER_KEY),
            executor_id=self.config.executor_id,
            executor_type="websocket",
            source=self._runtime_metadata,
            warnings=list(self._runtime_metadata.get("warnings") or []),
        )
        await self._send_rpc_result(ws, msg_id, report.model_dump(mode="json"))

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
        if not self._configured or self._runtime_state not in {"active", "degraded"}:
            await self._send_rpc_result(
                ws,
                msg_id,
                {
                    "call_id": call_id,
                    "output": (
                        "Executor is not configured or ready yet. "
                        f"Current runtime state: {self._runtime_state}."
                    ),
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
                    execution_scope_id=str(
                        params.get("execution_scope_id")
                        or f"{self.config.executor_id}:{self._runtime_metadata.get('user_email', 'runtime')}"
                    ),
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
                "attachments": result.attachments,
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
                                "response_status": chunk.get("response_status", "completed"),
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
                                "reasoning": chunk.get("reasoning"),
                                "refusal": chunk.get("refusal"),
                                "index": chunk.get("index", 0),
                            },
                        }
                    )
                )

    async def _handle_llm_transcribe(
        self, ws: Any, msg_id: str | None, params: dict[str, Any]
    ) -> None:
        if self._inference_handler is None:
            await self._send_rpc_error(ws, msg_id, -32601, "Inference handler unavailable")
            return

        try:
            encoding = str(params.get("audio_encoding", "hex"))
            audio_payload = str(params.get("audio_base64", ""))
            if encoding != "hex":
                raise ValueError("Unsupported audio encoding")
            audio_bytes = bytes.fromhex(audio_payload)
            request_kwargs = dict(params.get("request_kwargs") or {})
            result = await self._inference_handler.transcribe(
                audio_bytes=audio_bytes,
                mime_type=str(params.get("mime_type", "application/octet-stream")),
                filename=str(params.get("filename", "audio.bin")),
                model=str(params.get("model", "")),
                provider_preset=params.get("provider_preset"),
                request_kwargs=request_kwargs,
                prompt=params.get("prompt"),
                language=params.get("language"),
            )
            await self._send_rpc_result(ws, msg_id, result)
        except Exception as exc:
            await self._send_rpc_error(ws, msg_id, -32000, str(exc)[:500])

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
    ) -> dict[str, MCPClient]:
        clients: dict[str, MCPClient] = {}
        for server in servers:
            client = build_mcp_client(server, secrets)
            await client.connect()
            clients[runtime_mcp_server_key(server)] = client
        return clients

    async def _prepare_mcp_runtime(
        self, servers: list[MCPServerConfig], secrets: dict[str, str]
    ) -> tuple[dict[str, MCPClient], list[ToolDefinition], list[dict[str, Any]], list[str]]:
        clients: dict[str, MCPClient] = {}
        discovered: list[ToolDefinition] = []
        statuses: list[dict[str, Any]] = []
        warnings: list[str] = []
        for server in servers:
            logger.info(
                "MCP: starting server %s (command=%s, transport=%s)",
                server.name,
                server.command,
                server.transport,
            )
            logger.debug(
                "MCP: server %s full config: args=%s, env_keys=%s, header_keys=%s, timeout=%ds",
                server.name,
                server.args,
                sorted(server.env.keys()) if server.env else [],
                sorted(server.headers.keys()) if server.headers else [],
                server.timeout_seconds,
            )
            client = build_mcp_client(server, secrets)
            try:
                await client.connect()
                tools = await client.list_tools()
            except MCPClientError as exc:
                logger.warning(
                    "MCP: server %s failed during %s (%s, timed_out=%s)",
                    server.name,
                    exc.phase,
                    exc.error_class,
                    exc.timed_out,
                )
                if exc.safe_stderr:
                    logger.warning("MCP: server %s stderr: %s", server.name, exc.safe_stderr)
                await client.close()
                statuses.append(
                    {
                        "server_id": server.server_id,
                        "name": server.name,
                        "phase": exc.phase,
                        "status": "failed",
                        "error_class": exc.error_class,
                        "timed_out": exc.timed_out,
                        "message": str(exc),
                        "stderr_summary": exc.safe_stderr,
                    }
                )
                warnings.append(f"MCP server {server.name} failed during {exc.phase}.")
                continue
            except Exception as exc:
                logger.warning(
                    "MCP: server %s failed unexpectedly: %s",
                    server.name,
                    exc,
                )
                await client.close()
                statuses.append(
                    {
                        "server_id": server.server_id,
                        "name": server.name,
                        "phase": "unknown",
                        "status": "failed",
                        "error_class": exc.__class__.__name__.lower(),
                        "timed_out": False,
                        "message": _safe_message(str(exc)),
                    }
                )
                warnings.append(f"MCP server {server.name} failed to initialize.")
                continue
            logger.info(
                "MCP: server %s ready (%d tool(s) discovered)",
                server.name,
                len(tools),
            )
            clients[runtime_mcp_server_key(server)] = client
            statuses.append(
                {
                    "server_id": server.server_id,
                    "name": server.name,
                    "phase": "ready",
                    "status": "ready",
                    "tool_count": len(tools),
                }
            )
            discovered.extend(
                mcp_tools_to_definitions(
                    server.name,
                    tools,
                    timeout_seconds=server.timeout_seconds,
                    server_id=server.server_id,
                )
            )
        return clients, discovered, statuses, warnings

    async def _discover_mcp_tools(self, servers: list[MCPServerConfig]) -> list[ToolDefinition]:
        discovered: list[ToolDefinition] = []
        for server in servers:
            tools = await self._mcp_clients[runtime_mcp_server_key(server)].list_tools()
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
            client = self._mcp_clients[runtime_mcp_server_key(tool.source)]
            raw_tool_name = tool.source.raw_tool_name or tool.name
            return await client.call_tool(raw_tool_name, arguments)

        return _handler

    async def _close_mcp_clients(self) -> None:
        await self._close_clients(self._mcp_clients)
        self._mcp_clients = {}

    async def _close_clients(self, clients: dict[str, MCPClient]) -> None:
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.close()

    def _public_runtime_metadata(self) -> dict[str, Any]:
        metadata = dict(self._runtime_metadata)
        metadata.pop("web_secrets", None)
        metadata.pop("browser_manager", None)
        metadata.pop(LSP_MANAGER_KEY, None)
        metadata.pop(_FILE_FRESHNESS_KEY, None)
        return metadata

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
