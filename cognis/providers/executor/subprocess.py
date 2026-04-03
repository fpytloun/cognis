"""Subprocess executor provider — spawns executor as a local process.

The subprocess communicates with the controller via WebSocket, reusing
the ``WebSocketExecutorProvider`` for connection management.  Secrets
are delivered via stdin (never CLI arguments) to avoid exposure in
process listings.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from typing import Any

from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.tool import ExecutorConfig, ExecutorHandle
from cognis.providers.executor.websocket import (
    WebSocketExecutorConnection,
    WebSocketExecutorProvider,
)

_logger = get_logger(__name__)

_SUBPROCESS_CONNECT_TIMEOUT = 30  # seconds to wait for subprocess to connect
_SUBPROCESS_KILL_TIMEOUT = 10  # seconds to wait after SIGTERM before SIGKILL


class SubprocessExecutorProvider:
    """Spawns executor processes on the local machine.

    Each subprocess runs ``python -m cognis.executor`` and connects back
    to the controller via WebSocket.  The ``WebSocketExecutorProvider``
    handles the actual connection lifecycle.
    """

    def __init__(
        self,
        ws_provider: WebSocketExecutorProvider,
        auth_provider: Any,
        controller_port: int = 8080,
    ) -> None:
        self._ws_provider = ws_provider
        self._auth_provider = auth_provider
        self._controller_port = controller_port
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        """Spawn a subprocess executor and wait for it to connect.

        1. Generate a short-lived JWT (5 min) for the subprocess.
        2. Start the subprocess with config (minus secrets) as CLI arg.
        3. Pipe secrets via stdin.
        4. Wait for the subprocess to connect via WebSocket.
        """
        executor_id = config.executor_id

        # Generate short-lived token
        token = self._auth_provider.sign_executor_token(executor_id, ttl_seconds=300)

        # Build config for the subprocess (without secrets or token — those go via stdin)
        controller_url = f"ws://localhost:{self._controller_port}/api/executor/ws"
        subprocess_config = config.model_copy(
            update={
                "controller_url": controller_url,
                "controller_token": None,  # token delivered via stdin
                "secrets": {},  # secrets delivered via stdin
            }
        )

        # Start subprocess
        _logger.info(
            "executor_subprocess: spawning",
            extra={"extra_data": {"executor_id": executor_id}},
        )

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cognis.executor",
            "--controller-url",
            controller_url,
            "--executor-id",
            executor_id,
            "--config-json",
            subprocess_config.model_dump_json(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Pipe token + secrets via stdin (never CLI args for sensitive data).
        # Format: first line is the JWT token, remaining is secrets JSON.
        if process.stdin is not None:
            stdin_payload = token + "\n" + json.dumps(config.secrets)
            process.stdin.write(stdin_payload.encode())
            process.stdin.close()
        elif process.stdin is not None:
            process.stdin.close()

        self._processes[executor_id] = process

        # Wait for the subprocess to connect via WebSocket
        # The WebSocketExecutorProvider handles the connection lifecycle
        spawn_config = config.model_copy(update={"executor_id": executor_id})
        try:
            handle = await asyncio.wait_for(
                self._ws_provider.spawn(spawn_config),
                timeout=_SUBPROCESS_CONNECT_TIMEOUT,
            )
        except TimeoutError:
            _logger.error(
                "executor_subprocess: subprocess did not connect in time",
                extra={"extra_data": {"executor_id": executor_id}},
            )
            await self._kill_process(executor_id)
            raise

        handle.executor_type = "subprocess"
        return handle

    async def get_executor(self, handle: ExecutorHandle) -> WebSocketExecutorConnection:
        """Return the live connection (delegates to WS provider)."""
        return await self._ws_provider.get_executor(handle)

    async def cancel(self, handle: ExecutorHandle) -> None:
        """Cancel the subprocess executor."""
        # First try graceful shutdown via WebSocket
        with contextlib.suppress(Exception):
            await self._ws_provider.cancel(handle)

        # Then terminate the process
        await self._kill_process(handle.executor_id)

    async def list_active(self) -> list[ExecutorHandle]:
        """List active subprocess executors."""
        active = await self._ws_provider.list_active()
        return [h for h in active if h.executor_type == "subprocess"]

    async def cleanup(self) -> None:
        """Terminate all subprocess executors."""
        for executor_id in list(self._processes):
            await self._kill_process(executor_id)

    async def health(self) -> ProviderHealth:
        """Report subprocess executor health."""
        alive = sum(1 for p in self._processes.values() if p.returncode is None)
        return ProviderHealth(
            name="executor_subprocess",
            status="healthy",
            details={"active_processes": alive},
        )

    async def _kill_process(self, executor_id: str) -> None:
        """Terminate a subprocess, escalating from SIGTERM to SIGKILL."""
        process = self._processes.pop(executor_id, None)
        if process is None or process.returncode is not None:
            return

        _logger.info(
            "executor_subprocess: terminating",
            extra={"extra_data": {"executor_id": executor_id}},
        )

        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_SUBPROCESS_KILL_TIMEOUT)
        except TimeoutError:
            _logger.warning(
                "executor_subprocess: SIGTERM timeout, sending SIGKILL",
                extra={"extra_data": {"executor_id": executor_id}},
            )
            process.kill()
            await process.wait()
