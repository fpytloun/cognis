"""signal-cli JSON-RPC stdio runtime for direct Signal transport.

Manages a per-account ``signal-cli ... jsonRpc`` subprocess, providing
async request/response correlation and inbound notification dispatch.

The command/path for ``signal-cli`` comes from the executor config
(``config.signal.command``), never from per-account metadata.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from typing import Any

from cognis.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STARTUP_TIMEOUT_S = 30.0
_REQUEST_TIMEOUT_S = 30.0
_ATTACHMENT_TIMEOUT_S = 60.0
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB
_STDERR_MAX_LINE = 500  # max chars to log from stderr (redaction safety)


class SignalCliRuntimeError(Exception):
    """Raised when the signal-cli runtime encounters a fatal error."""


class SignalCliRuntime:
    """Manages a single ``signal-cli -a ACCOUNT jsonRpc`` subprocess.

    Provides:
    - Async JSON-RPC request/response correlation
    - ``receive`` notification dispatch via callback
    - Bounded startup/request timeouts
    - Safe shutdown with in-flight request cancellation
    - Redaction-safe stderr draining
    """

    def __init__(
        self,
        *,
        account_number: str,
        command: str = "signal-cli",
        trust_mode: str = "trust-all-known",
        on_notification: Any | None = None,
    ) -> None:
        self._account_number = account_number
        self._command = command
        self._trust_mode = trust_mode
        self._on_notification = on_notification

        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._running = False
        self._version: str | None = None
        self._capabilities: set[str] = set()
        self._stderr_line_count = 0
        self._last_returncode: int | None = None

    @property
    def version(self) -> str | None:
        return self._version

    @property
    def capabilities(self) -> set[str]:
        return self._capabilities

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None and self._process.returncode is None

    @property
    def single_account_mode(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the signal-cli subprocess and perform preflight checks."""
        # Validate command exists
        resolved = shutil.which(self._command)
        if resolved is None:
            raise SignalCliRuntimeError(f"signal-cli command not found: {self._command}")

        args = [
            resolved,
            "--trust-new-identities",
            self._trust_mode,
            "-a",
            self._account_number,
            "jsonRpc",
        ]

        try:
            self._process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=_STARTUP_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise SignalCliRuntimeError(
                "signal-cli subprocess did not start within timeout"
            ) from exc
        except OSError as exc:
            raise SignalCliRuntimeError(f"Failed to start signal-cli: {exc}") from exc

        self._running = True
        self._stderr_line_count = 0
        self._last_returncode = None
        self._reader_task = asyncio.create_task(self._read_stdout(), name="signal-cli-stdout")
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="signal-cli-stderr")

        # Preflight: probe version
        try:
            result = await self.request("version", timeout=10.0)
            self._version = result.get("version")
            logger.info(
                "signal-cli runtime: started",
                extra={
                    "extra_data": {
                        "account": self._account_number,
                        "version": self._version,
                    }
                },
            )
        except Exception as exc:
            logger.debug(
                "signal-cli runtime: version probe failed, continuing",
                extra={"extra_data": {"account": self._account_number}},
                exc_info=True,
            )
            await self._refresh_returncode()
            if not self.is_running:
                message = self._process_exit_message()
                await self.stop()
                raise SignalCliRuntimeError(message) from exc

        # Discover capabilities by probing known methods
        self._capabilities = {"send", "receive"}
        for method in ("sendTyping", "sendReceipt", "updateProfile"):
            # We assume these are available; if they fail at call time
            # we degrade gracefully.
            self._capabilities.add(method)

    async def stop(self) -> None:
        """Stop the subprocess and cancel in-flight requests."""
        self._running = False

        # Cancel all pending requests
        for future in self._pending.values():
            if not future.done():
                future.set_exception(SignalCliRuntimeError("Runtime shutting down"))
        self._pending.clear()

        # Terminate process
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            except ProcessLookupError:
                pass

        # Cancel reader tasks
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        self._process = None
        self._reader_task = None
        self._stderr_task = None

    # ------------------------------------------------------------------
    # JSON-RPC request/response
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = _REQUEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the correlated response."""
        if not self.is_running:
            raise SignalCliRuntimeError("Runtime is not running")

        assert self._process is not None
        assert self._process.stdin is not None

        req_id = self._next_id
        self._next_id += 1

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": req_id,
        }
        if params:
            payload["params"] = params

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            line = json.dumps(payload) + "\n"
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
        except Exception as exc:
            self._pending.pop(req_id, None)
            raise SignalCliRuntimeError(f"Failed to write to signal-cli stdin: {exc}") from exc

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise SignalCliRuntimeError(
                f"signal-cli request '{method}' timed out after {timeout}s"
            ) from exc
        finally:
            self._pending.pop(req_id, None)

        return result

    # ------------------------------------------------------------------
    # Stdout reader (responses + notifications)
    # ------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        """Read line-delimited JSON from signal-cli stdout."""
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            while self._running:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    # EOF — process exited
                    break

                try:
                    msg = json.loads(line_bytes)
                except json.JSONDecodeError:
                    # Redaction-safe: do not log the raw line
                    logger.warning(
                        "signal-cli runtime: invalid JSON on stdout",
                        extra={"extra_data": {"account": self._account_number}},
                    )
                    continue

                msg_id = msg.get("id")
                method = msg.get("method")

                if msg_id is not None and msg_id in self._pending:
                    # Response to a pending request
                    future = self._pending.pop(msg_id)
                    if "error" in msg:
                        error = msg["error"]
                        future.set_exception(
                            SignalCliRuntimeError(
                                f"signal-cli error {error.get('code', -1)}: "
                                f"{error.get('message', 'unknown')}"
                            )
                        )
                    else:
                        future.set_result(msg.get("result", {}))

                elif method == "receive":
                    # Inbound notification
                    if self._on_notification is not None:
                        try:
                            await self._on_notification(msg.get("params", {}))
                        except Exception:
                            logger.warning(
                                "signal-cli runtime: notification handler error",
                                extra={
                                    "extra_data": {
                                        "account": self._account_number,
                                    }
                                },
                                exc_info=True,
                            )

        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning(
                "signal-cli runtime: stdout reader error",
                extra={"extra_data": {"account": self._account_number}},
                exc_info=True,
            )
        finally:
            # Process exited or reader failed — mark as not running
            if self._running:
                self._running = False
                # Fail all pending requests
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(SignalCliRuntimeError(self._process_exit_message()))
                self._pending.clear()

    # ------------------------------------------------------------------
    # Stderr drainer
    # ------------------------------------------------------------------

    async def _drain_stderr(self) -> None:
        """Drain stderr to prevent pipe buffer deadlock.

        Logs warnings but never includes raw message content.
        """
        assert self._process is not None
        assert self._process.stderr is not None

        try:
            while self._running:
                line_bytes = await self._process.stderr.readline()
                if not line_bytes:
                    break
                self._stderr_line_count += 1
                # Never log raw stderr content — it may contain message
                # content, phone numbers, or other sensitive data.
            if self._stderr_line_count > 0:
                logger.debug(
                    "signal-cli stderr: drained lines",
                    extra={
                        "extra_data": {
                            "account": self._account_number,
                            "stderr_line_count": self._stderr_line_count,
                        }
                    },
                )
        except asyncio.CancelledError:
            return
        except Exception:
            pass

    def _process_exit_message(self) -> str:
        returncode = None
        if self._process is not None:
            returncode = self._process.returncode
        self._last_returncode = returncode
        detail = f"returncode={returncode}" if returncode is not None else "returncode=unknown"
        if self._stderr_line_count > 0:
            detail += f", stderr_lines={self._stderr_line_count}"
        return f"signal-cli process exited unexpectedly ({detail})"

    async def _refresh_returncode(self) -> None:
        if self._process is None or self._process.returncode is not None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._process.wait(), timeout=0.1)
