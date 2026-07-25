from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from cognis.core.local_models import sanitize_local_model_error
from cognis.executor.ollama_runtime import (
    OllamaAdapter,
    OllamaRuntimeError,
    OllamaRuntimeHandler,
)
from cognis.models.local_models import (
    OLLAMA_MANAGED_ENDPOINT,
    OllamaRuntimeConfig,
    OllamaRuntimeStartRequest,
)


class _PullStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"status":"pulling manifest"}\n'
        yield b'{"status":"downloading","completed":4,"total":10}\n'
        yield b'{"status":"success","completed":10,"total":10}\n'


@pytest.mark.asyncio
async def test_adapter_streams_bounded_pull_progress_and_rejects_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 11434
        if request.url.path == "/api/pull":
            return httpx.Response(200, stream=_PullStream())
        if request.url.path == "/api/tags":
            return httpx.Response(307, headers={"location": "http://10.0.0.8/api/tags"})
        raise AssertionError(request.url)

    client = httpx.AsyncClient(
        base_url=OLLAMA_MANAGED_ENDPOINT,
        transport=httpx.MockTransport(handler),
    )
    adapter = OllamaAdapter(
        OllamaRuntimeConfig(management_enabled=True, disk_headroom_bytes=0),
        client=client,
    )
    progress: list[dict[str, Any]] = []
    result = await adapter.pull(
        "llama3.2:latest",
        on_progress=lambda frame: _append(progress, frame),
    )
    assert result["status"] == "success"
    assert [frame.get("completed") for frame in progress] == [None, 4, 10]
    with pytest.raises(OllamaRuntimeError, match="redirects are not allowed"):
        await adapter.installed()
    await client.aclose()


@pytest.mark.asyncio
async def test_adapter_uses_custom_derived_loopback_port() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://127.0.0.1:22434/api/version")
        return httpx.Response(200, json={"version": "0.10"})

    config = OllamaRuntimeConfig(port=22434)
    client = httpx.AsyncClient(
        base_url=config.endpoint,
        transport=httpx.MockTransport(handler),
    )
    adapter = OllamaAdapter(config, client=client)

    assert await adapter.version() == "0.10"
    assert config.endpoint == "http://127.0.0.1:22434"
    await client.aclose()


async def _append(target: list[dict[str, Any]], value: dict[str, Any]) -> None:
    target.append(value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:11434",
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:11434/api",
        "http://127.0.0.1:11434?token=secret",
        "https://127.0.0.1:11434",
    ],
)
def test_managed_endpoint_is_pinned_and_rejects_ssrf_shapes(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        OllamaRuntimeConfig(endpoint=endpoint)  # type: ignore[arg-type]


@pytest.mark.parametrize("port", [0, 65536, "11434", 11434.0, True])
def test_managed_port_requires_an_integer_in_range(port: object) -> None:
    with pytest.raises(ValidationError):
        OllamaRuntimeConfig(port=port)  # type: ignore[arg-type]


class _BlockingAdapter:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.release = asyncio.Event()
        self.pull_calls = 0
        self.active_pulls = 0
        self.max_active_pulls = 0

    async def close(self) -> None:
        return None

    async def version(self) -> str:
        return "0.9"

    async def installed(self) -> list[dict[str, Any]]:
        return []

    async def running(self) -> list[dict[str, Any]]:
        return []

    async def show(self, runtime_name: str) -> dict[str, Any]:
        return {"model": runtime_name}

    async def pull(self, runtime_name: str, *, on_progress: Any) -> dict[str, Any]:
        self.pull_calls += 1
        self.active_pulls += 1
        self.max_active_pulls = max(self.max_active_pulls, self.active_pulls)
        try:
            self.pull_started.set()
            await on_progress({"status": "downloading", "completed": 5})
            await self.release.wait()
            return {"status": "success", "completed": 5}
        finally:
            self.active_pulls -= 1

    async def delete(self, runtime_name: str) -> None:
        return None

    @staticmethod
    async def cancel(task: asyncio.Task[Any]) -> None:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_handler_is_idempotent_and_cancellation_aborts_stream() -> None:
    adapter = _BlockingAdapter()
    runtime = OllamaRuntimeHandler(
        OllamaRuntimeConfig(
            management_enabled=True,
            disk_headroom_bytes=0,
        ),
        adapter=adapter,  # type: ignore[arg-type]
    )
    runtime._disk_preflight = _no_disk_preflight  # type: ignore[method-assign]
    progress: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    request = OllamaRuntimeStartRequest(
        operation_id="lmo_runtime_cancel",
        action="pull",
        runtime_name="llama3.2:latest",
        request_hash="sha256:runtime-cancel",
    )

    first = await runtime.start(
        request,
        on_progress=lambda frame: _append(progress, frame),
        on_complete=lambda frame: _append(completed, frame),
    )
    duplicate = await runtime.start(
        request,
        on_progress=lambda frame: _append(progress, frame),
        on_complete=lambda frame: _append(completed, frame),
    )
    await asyncio.wait_for(adapter.pull_started.wait(), timeout=1)
    assert first.operation_id == duplicate.operation_id
    assert adapter.pull_calls == 1

    assert await runtime.cancel(request.operation_id) is True
    assert runtime.operation_status(request.operation_id).state == "cancelled"  # type: ignore[union-attr]
    assert completed[-1]["state"] == "cancelled"
    assert completed[-1]["message"].startswith("HTTP stream aborted")
    await runtime.close()


async def _no_disk_preflight() -> None:
    return None


@pytest.mark.asyncio
async def test_handler_enforces_executor_wide_pull_concurrency() -> None:
    adapter = _BlockingAdapter()
    runtime = OllamaRuntimeHandler(
        OllamaRuntimeConfig(
            management_enabled=True,
            max_concurrent_pulls=1,
            disk_headroom_bytes=0,
        ),
        adapter=adapter,  # type: ignore[arg-type]
    )
    runtime._disk_preflight = _no_disk_preflight  # type: ignore[method-assign]
    completed: list[dict[str, Any]] = []
    for index, model in enumerate(("llama3.2:latest", "qwen3:8b")):
        await runtime.start(
            OllamaRuntimeStartRequest(
                operation_id=f"lmo_concurrency_{index}",
                action="pull",
                runtime_name=model,
                request_hash=f"sha256:concurrency-{index}",
            ),
            on_progress=lambda frame: _append([], frame),
            on_complete=lambda frame: _append(completed, frame),
        )
    await asyncio.wait_for(adapter.pull_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert adapter.pull_calls == 1
    adapter.release.set()
    for _ in range(100):
        if len(completed) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(completed) == 2
    assert adapter.max_active_pulls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_reconnect_rebinds_callbacks_and_disable_applies_during_pull() -> None:
    adapter = _BlockingAdapter()
    config = OllamaRuntimeConfig(
        management_enabled=True,
        disk_headroom_bytes=0,
    )
    runtime = OllamaRuntimeHandler(config, adapter=adapter)  # type: ignore[arg-type]
    runtime._disk_preflight = _no_disk_preflight  # type: ignore[method-assign]
    request = OllamaRuntimeStartRequest(
        operation_id="lmo_reconnect",
        action="pull",
        runtime_name="llama3.2:latest",
        request_hash="sha256:reconnect",
    )
    old_completed: list[dict[str, Any]] = []
    new_completed: list[dict[str, Any]] = []
    await runtime.start(
        request,
        on_progress=lambda frame: _append([], frame),
        on_complete=lambda frame: _append(old_completed, frame),
    )
    await asyncio.wait_for(adapter.pull_started.wait(), timeout=1)
    await runtime.start(
        request,
        on_progress=lambda frame: _append([], frame),
        on_complete=lambda frame: _append(new_completed, frame),
    )

    await runtime.reconfigure(config.model_copy(update={"management_enabled": False}))
    assert runtime.capability()["management_enabled"] is False
    with pytest.raises(OllamaRuntimeError, match="mutations are disabled"):
        await runtime.start(
            OllamaRuntimeStartRequest(
                operation_id="lmo_disabled",
                action="pull",
                runtime_name="qwen3:8b",
                request_hash="sha256:disabled",
            ),
            on_progress=lambda frame: _append([], frame),
            on_complete=lambda frame: _append([], frame),
        )

    adapter.release.set()
    for _ in range(100):
        if new_completed:
            break
        await asyncio.sleep(0.01)
    assert old_completed == []
    assert new_completed == [{"operation_id": "lmo_reconnect", "state": "succeeded"}]
    await runtime.close()


@pytest.mark.asyncio
async def test_disk_preflight_uses_configured_model_store_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_store = tmp_path / "ollama" / "models"
    observed: list[Path] = []

    def _disk_usage(path: Path) -> Any:
        observed.append(path)
        return type("Usage", (), {"free": 1024})()

    monkeypatch.setattr("cognis.executor.ollama_runtime.shutil.disk_usage", _disk_usage)
    runtime = OllamaRuntimeHandler(
        OllamaRuntimeConfig(
            management_enabled=True,
            disk_headroom_bytes=512,
            model_store_path=str(model_store),
        ),
        adapter=_BlockingAdapter(),  # type: ignore[arg-type]
    )
    await runtime._disk_preflight()  # noqa: SLF001
    assert observed == [tmp_path]
    await runtime.close()


@pytest.mark.asyncio
async def test_disk_preflight_refuses_unknown_model_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    runtime = OllamaRuntimeHandler(
        OllamaRuntimeConfig(management_enabled=True, disk_headroom_bytes=0),
        adapter=_BlockingAdapter(),  # type: ignore[arg-type]
    )
    with pytest.raises(OllamaRuntimeError, match="model-store path is unknown"):
        await runtime._disk_preflight()  # noqa: SLF001
    await runtime.close()


def test_error_sanitization_redacts_credentials_and_bounds_output() -> None:
    sanitized = sanitize_local_model_error(
        "pull failed bearer top-secret api_key=also-secret "
        "http://user:password@127.0.0.1:11434/api?token=query-secret\n" + "x" * 2000
    )
    assert sanitized is not None
    assert "top-secret" not in sanitized
    assert "also-secret" not in sanitized
    assert "password" not in sanitized
    assert "query-secret" not in sanitized
    assert "[redacted]" in sanitized
    assert "\n" not in sanitized
    assert len(sanitized) == 1000


@pytest.mark.asyncio
async def test_terminal_operations_release_transient_registries() -> None:
    adapter = _BlockingAdapter()
    adapter.release.set()
    runtime = OllamaRuntimeHandler(
        OllamaRuntimeConfig(management_enabled=True, disk_headroom_bytes=0),
        adapter=adapter,  # type: ignore[arg-type]
    )
    runtime._disk_preflight = _no_disk_preflight  # type: ignore[method-assign]
    completed = asyncio.Event()
    await runtime.start(
        OllamaRuntimeStartRequest(
            operation_id="lmo_registry_cleanup",
            action="pull",
            runtime_name="llama3.2:latest",
            request_hash="sha256:registry-cleanup",
        ),
        on_progress=lambda frame: _append([], frame),
        on_complete=lambda frame: _set_event(completed),
    )
    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.sleep(0)
    gc.collect()
    assert runtime._tasks == {}  # noqa: SLF001
    assert runtime._callbacks == {}  # noqa: SLF001
    assert len(runtime._model_locks) == 0  # noqa: SLF001
    await runtime.close()


async def _set_event(event: asyncio.Event) -> None:
    event.set()
