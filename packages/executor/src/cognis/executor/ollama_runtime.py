"""Executor-local managed Ollama adapter and operation handler."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

import httpx

from cognis.core.local_models import parse_local_model_reference, sanitize_local_model_error
from cognis.models.local_models import (
    LOCAL_MODEL_BYTE_COUNT_MAX,
    LocalModelOperationAction,
    OllamaRuntimeConfig,
    OllamaRuntimeOperationStatus,
    OllamaRuntimeStartRequest,
    OllamaRuntimeStatus,
)

OllamaProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
OllamaCompletionCallback = Callable[[dict[str, Any]], Awaitable[None]]

_MAX_MODELS = 1000
_MAX_OPERATIONS = 256
_MAX_STREAM_LINE_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class OllamaRuntimeError(RuntimeError):
    """Sanitized managed Ollama runtime failure."""


def _safe_error(exc: BaseException | str) -> str:
    return sanitize_local_model_error(str(exc)) or "managed Ollama operation failed"


def _bounded_mapping(value: Any, *, max_bytes: int = 64 * 1024) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        encoded = json.dumps(value, ensure_ascii=True, default=str)
    except Exception:
        return {}
    if len(encoded.encode("utf-8")) > max_bytes:
        return {}
    decoded = json.loads(encoded)
    return decoded if isinstance(decoded, dict) else {}


class OllamaAdapter:
    """Typed HTTP adapter pinned to the executor loopback Ollama API."""

    def __init__(
        self,
        config: OllamaRuntimeConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
        if str(self._client.base_url).rstrip("/") != config.endpoint:
            raise ValueError("managed Ollama client must use the pinned loopback endpoint")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not path.startswith("/api/") or "?" in path or "#" in path:
            raise ValueError("invalid managed Ollama API path")
        response = await self._client.request(method, path, **kwargs)
        if response.is_redirect:
            raise OllamaRuntimeError("managed Ollama API redirects are not allowed")
        response.raise_for_status()
        return response

    async def version(self) -> str | None:
        response = await self._request("GET", "/api/version")
        data = _bounded_mapping(response.json())
        value = data.get("version")
        return str(value)[:120] if isinstance(value, str) and value else None

    async def probe(self) -> dict[str, Any]:
        return {"reachable": True, "version": await self.version()}

    async def installed(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/tags")
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise OllamaRuntimeError("managed Ollama model list exceeded the response limit")
        data = _bounded_mapping(response.json(), max_bytes=_MAX_RESPONSE_BYTES)
        models = data.get("models")
        if not isinstance(models, list):
            return []
        return [
            bounded
            for item in models[:_MAX_MODELS]
            if (bounded := _bounded_mapping(item)) and isinstance(bounded.get("name"), str)
        ]

    async def show(self, runtime_name: str) -> dict[str, Any]:
        canonical = parse_local_model_reference(runtime_name)
        if canonical.runtime_name != runtime_name:
            raise ValueError("runtime_name must be canonical")
        response = await self._request(
            "POST",
            "/api/show",
            json={"model": canonical.runtime_name},
        )
        return _bounded_mapping(response.json(), max_bytes=_MAX_RESPONSE_BYTES)

    async def running(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/ps")
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise OllamaRuntimeError("managed Ollama process list exceeded the response limit")
        data = _bounded_mapping(response.json(), max_bytes=_MAX_RESPONSE_BYTES)
        models = data.get("models")
        if not isinstance(models, list):
            return []
        return [
            bounded
            for item in models[:_MAX_MODELS]
            if (bounded := _bounded_mapping(item)) and isinstance(bounded.get("name"), str)
        ]

    async def pull(
        self,
        runtime_name: str,
        *,
        on_progress: OllamaProgressCallback,
    ) -> dict[str, Any]:
        canonical = parse_local_model_reference(runtime_name)
        if canonical.runtime_name != runtime_name:
            raise ValueError("runtime_name must be canonical")
        async with self._client.stream(
            "POST",
            "/api/pull",
            json={"model": canonical.runtime_name, "stream": True},
        ) as response:
            if response.is_redirect:
                raise OllamaRuntimeError("managed Ollama API redirects are not allowed")
            response.raise_for_status()
            final: dict[str, Any] = {}
            async for line in response.aiter_lines():
                if not line:
                    continue
                if len(line.encode("utf-8")) > _MAX_STREAM_LINE_BYTES:
                    raise OllamaRuntimeError("managed Ollama progress frame exceeded the limit")
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise OllamaRuntimeError(
                        "managed Ollama returned invalid pull progress"
                    ) from exc
                if not isinstance(frame, dict):
                    continue
                if isinstance(frame.get("error"), str):
                    raise OllamaRuntimeError(_safe_error(frame["error"]))
                final = _bounded_mapping(frame)
                await on_progress(final)
            return final

    async def delete(self, runtime_name: str) -> None:
        canonical = parse_local_model_reference(runtime_name)
        if canonical.runtime_name != runtime_name:
            raise ValueError("runtime_name must be canonical")
        await self._request(
            "DELETE",
            "/api/delete",
            json={"model": canonical.runtime_name},
        )

    @staticmethod
    async def cancel(task: asyncio.Task[Any]) -> None:
        """Abort an active HTTP stream without claiming remote rollback."""

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class OllamaRuntimeHandler:
    """Executor-local registry for idempotent managed Ollama operations."""

    def __init__(
        self,
        config: OllamaRuntimeConfig | None = None,
        *,
        adapter: OllamaAdapter | None = None,
    ) -> None:
        self.config = config or OllamaRuntimeConfig()
        self.adapter = adapter or OllamaAdapter(self.config)
        self._pull_semaphore = asyncio.Semaphore(self.config.max_concurrent_pulls)
        self._model_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._callbacks: dict[
            str,
            tuple[OllamaProgressCallback, OllamaCompletionCallback],
        ] = {}
        self._operations: OrderedDict[str, OllamaRuntimeOperationStatus] = OrderedDict()
        self._model_store_path = self._resolve_model_store_path(self.config)
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    async def reconfigure(self, config: OllamaRuntimeConfig) -> None:
        """Apply config only when no managed operation is active."""

        async with self._lifecycle_lock:
            await self._reconfigure_locked(config)

    async def _reconfigure_locked(self, config: OllamaRuntimeConfig) -> None:
        if any(not task.done() for task in self._tasks.values()):
            if (
                config.model_copy(update={"management_enabled": self.config.management_enabled})
                == self.config
            ):
                self.config = config
                return
            if config != self.config:
                raise OllamaRuntimeError(
                    "managed Ollama runtime cannot be reconfigured while operations are active"
                )
            return
        if config == self.config:
            return
        await self.adapter.close()
        self.config = config
        self.adapter = OllamaAdapter(config)
        self._pull_semaphore = asyncio.Semaphore(config.max_concurrent_pulls)
        self._model_store_path = self._resolve_model_store_path(config)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.adapter.close()

    def capability(self) -> dict[str, Any]:
        return {
            "runtime_type": "ollama",
            "port": self.config.port,
            "endpoint": self.config.endpoint,
            "management_enabled": self.config.management_enabled,
            "max_concurrent_pulls": self.config.max_concurrent_pulls,
            "disk_headroom_bytes": self.config.disk_headroom_bytes,
        }

    async def inspect(self) -> OllamaRuntimeStatus:
        try:
            version, installed, running = await asyncio.gather(
                self.adapter.version(),
                self.adapter.installed(),
                self.adapter.running(),
            )
            return OllamaRuntimeStatus(
                port=self.config.port,
                endpoint=self.config.endpoint,
                management_enabled=self.config.management_enabled,
                reachable=True,
                version=version,
                installed=installed,
                running=running,
                operations=list(self._operations.values())[-_MAX_OPERATIONS:],
            )
        except Exception as exc:
            return OllamaRuntimeStatus(
                port=self.config.port,
                endpoint=self.config.endpoint,
                management_enabled=self.config.management_enabled,
                reachable=False,
                operations=list(self._operations.values())[-_MAX_OPERATIONS:],
                error=_safe_error(exc),
            )

    async def inspect_model(self, runtime_name: str) -> dict[str, Any]:
        return await self.adapter.show(runtime_name)

    def operation_status(self, operation_id: str) -> OllamaRuntimeOperationStatus | None:
        return self._operations.get(operation_id)

    async def start(
        self,
        request: OllamaRuntimeStartRequest,
        *,
        on_progress: OllamaProgressCallback,
        on_complete: OllamaCompletionCallback,
    ) -> OllamaRuntimeOperationStatus:
        async with self._lifecycle_lock:
            return await self._start_locked(
                request,
                on_progress=on_progress,
                on_complete=on_complete,
            )

    async def _start_locked(
        self,
        request: OllamaRuntimeStartRequest,
        *,
        on_progress: OllamaProgressCallback,
        on_complete: OllamaCompletionCallback,
    ) -> OllamaRuntimeOperationStatus:
        if self._closed:
            raise OllamaRuntimeError("managed Ollama runtime is closed")
        if not self.config.management_enabled:
            raise OllamaRuntimeError("managed Ollama mutations are disabled on this executor")
        existing = self._operations.get(request.operation_id)
        if existing is not None:
            if (
                existing.request_hash != request.request_hash
                or existing.action != request.action
                or existing.runtime_name != request.runtime_name
            ):
                raise OllamaRuntimeError(
                    "operation_id was already used with a different managed request"
                )
            if existing.state == "running":
                self._callbacks[request.operation_id] = (on_progress, on_complete)
            return existing
        if sum(status.state == "running" for status in self._operations.values()) >= (
            _MAX_OPERATIONS
        ):
            raise OllamaRuntimeError("managed Ollama operation capacity is exhausted")

        status = OllamaRuntimeOperationStatus(
            operation_id=request.operation_id,
            action=request.action,
            runtime_name=request.runtime_name,
            request_hash=request.request_hash,
            state="running",
        )
        self._remember(status)
        self._callbacks[request.operation_id] = (on_progress, on_complete)
        task = asyncio.create_task(
            self._run_operation(request),
            name=f"ollama-{request.action.value}-{request.operation_id}",
        )
        self._tasks[request.operation_id] = task
        task.add_done_callback(lambda _task: self._operation_done(request.operation_id))
        return status

    async def cancel(self, operation_id: str) -> bool:
        task = self._tasks.get(operation_id)
        status = self._operations.get(operation_id)
        if task is None or status is None or task.done():
            return False
        await self.adapter.cancel(task)
        return True

    async def _run_operation(
        self,
        request: OllamaRuntimeStartRequest,
    ) -> None:
        sequence = 0
        progress_bytes = 0

        async def _progress(frame: dict[str, Any]) -> None:
            nonlocal sequence, progress_bytes
            sequence += 1
            completed = frame.get("completed")
            if (
                isinstance(completed, int)
                and not isinstance(completed, bool)
                and 0 <= completed <= LOCAL_MODEL_BYTE_COUNT_MAX
            ):
                progress_bytes = max(progress_bytes, completed)
            phase = str(frame.get("status") or "downloading")[:120]
            status = self._operations[request.operation_id].model_copy(
                update={
                    "progress_seq": sequence,
                    "progress_bytes": progress_bytes,
                    "phase": phase,
                }
            )
            self._remember(status)
            callbacks = self._callbacks.get(request.operation_id)
            if callbacks is not None:
                with contextlib.suppress(Exception):
                    await callbacks[0](
                        {
                            "operation_id": request.operation_id,
                            "progress_seq": sequence,
                            "progress_bytes": progress_bytes,
                            "phase": phase,
                        }
                    )

        async def _complete(payload: dict[str, Any]) -> None:
            callbacks = self._callbacks.get(request.operation_id)
            if callbacks is not None:
                with contextlib.suppress(Exception):
                    await callbacks[1](payload)

        try:
            model_lock = self._model_locks.setdefault(
                request.runtime_name,
                asyncio.Lock(),
            )
            async with model_lock:
                if request.action == LocalModelOperationAction.PULL:
                    async with self._pull_semaphore:
                        await self._disk_preflight()
                        installed = await self.adapter.installed()
                        if (
                            self._is_installed(installed, request.runtime_name)
                            and not request.force
                        ):
                            await _progress({"status": "already present", "completed": 0})
                        else:
                            await self.adapter.pull(
                                request.runtime_name,
                                on_progress=_progress,
                            )
                else:
                    installed = await self.adapter.installed()
                    if self._is_installed(installed, request.runtime_name):
                        await self.adapter.delete(request.runtime_name)
            terminal = self._operations[request.operation_id].model_copy(
                update={"state": "succeeded", "phase": "complete"}
            )
            self._remember(terminal)
            await _complete(
                {
                    "operation_id": request.operation_id,
                    "state": "succeeded",
                }
            )
        except asyncio.CancelledError:
            terminal = self._operations[request.operation_id].model_copy(
                update={"state": "cancelled", "phase": "cancelled"}
            )
            self._remember(terminal)
            await _complete(
                {
                    "operation_id": request.operation_id,
                    "state": "cancelled",
                    "message": (
                        "HTTP stream aborted; already-downloaded data may remain in Ollama"
                    ),
                }
            )
            raise
        except Exception as exc:
            error = _safe_error(exc)
            terminal = self._operations[request.operation_id].model_copy(
                update={"state": "failed", "phase": "failed", "error": error}
            )
            self._remember(terminal)
            await _complete(
                {
                    "operation_id": request.operation_id,
                    "state": "failed",
                    "error": error,
                }
            )

    async def _disk_preflight(self) -> None:
        if self._model_store_path is None:
            raise OllamaRuntimeError("Ollama model-store path is unknown; refusing managed pull")
        usage = await asyncio.to_thread(
            self._disk_usage_for_model_store,
            self._model_store_path,
        )
        if usage.free < self.config.disk_headroom_bytes:
            raise OllamaRuntimeError("insufficient disk headroom for managed Ollama pull")

    @staticmethod
    def _resolve_model_store_path(config: OllamaRuntimeConfig) -> Path | None:
        configured = config.model_store_path or os.environ.get("OLLAMA_MODELS")
        if not configured:
            return None
        try:
            return Path(configured).expanduser()
        except (OSError, RuntimeError):
            return None

    @staticmethod
    def _disk_usage_for_model_store(model_store_path: Path) -> Any:
        candidate = model_store_path
        while not candidate.exists():
            parent = candidate.parent
            if parent == candidate:
                raise OllamaRuntimeError("Ollama model-store filesystem cannot be determined")
            candidate = parent
        return shutil.disk_usage(candidate)

    @staticmethod
    def _is_installed(models: list[dict[str, Any]], runtime_name: str) -> bool:
        return any(
            str(item.get("name") or item.get("model") or "") == runtime_name for item in models
        )

    def _remember(self, status: OllamaRuntimeOperationStatus) -> None:
        self._operations[status.operation_id] = status
        self._operations.move_to_end(status.operation_id)
        while len(self._operations) > _MAX_OPERATIONS:
            operation_id = next(
                (key for key, value in self._operations.items() if value.state != "running"),
                None,
            )
            if operation_id is None:
                break
            self._operations.pop(operation_id, None)
            self._tasks.pop(operation_id, None)

    def _operation_done(self, operation_id: str) -> None:
        self._tasks.pop(operation_id, None)
        self._callbacks.pop(operation_id, None)
