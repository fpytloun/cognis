"""Regression tests for active and starting language-server capacity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognis.tools.executor.lsp.manager import LSPManager
from cognis.tools.executor.lsp.servers import LSPServerDefinition


def _project_file(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    target = project / "App.svelte"
    target.write_text("<div />", encoding="utf-8")
    return target


def _client() -> MagicMock:
    client = MagicMock()
    client.is_alive = True
    client.server_id = "svelte"
    client.server_name = "Svelte"
    client.start = AsyncMock()
    client.close = AsyncMock()
    client.did_open = AsyncMock()
    client.did_change = AsyncMock()
    client.wait_for_progress_idle = AsyncMock(return_value=True)
    return client


_SERVER = LSPServerDefinition(
    server_id="svelte",
    extensions=frozenset({".svelte"}),
    command="svelteserver",
    args=("--stdio",),
    root_markers=("package.json",),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("active_count", [0, 1])
async def test_pending_start_reserves_capacity_and_can_be_reused(
    tmp_path: Path,
    active_count: int,
) -> None:
    manager = LSPManager(max_concurrent_servers=active_count + 1)
    first = _project_file(tmp_path, "first")
    second = _project_file(tmp_path, "second")
    started = asyncio.Event()
    release = asyncio.Event()
    client = _client()
    reuse: asyncio.Task | None = None

    async def start() -> None:
        started.set()
        await release.wait()

    client.start.side_effect = start
    if active_count:
        manager._clients[f"svelte:{tmp_path / 'active'}"] = _client()
    try:
        with (
            patch(
                "cognis.tools.executor.lsp.manager.get_servers_for_extension",
                return_value=[_SERVER],
            ),
            patch("cognis.tools.executor.lsp.manager.resolve_command", return_value="svelteserver"),
            patch("cognis.tools.executor.lsp.manager.LSPClient", return_value=client) as clients,
        ):
            await manager.touch_file(str(first), wait=False)
            await asyncio.wait_for(started.wait(), timeout=1)
            reuse = asyncio.create_task(manager.prepare_file(str(first)))
            await asyncio.sleep(0)
            assert not reuse.done()
            await manager.touch_file(str(second), wait=False)

            assert len(manager._spawning) == 1
            assert len(manager._clients) == active_count
            clients.assert_called_once()

            pending = list(manager._spawning.values())
            release.set()
            await asyncio.gather(*pending)
            prepared = await reuse

            assert len(prepared) == 1
            assert prepared[0].client is client
            assert len(manager._clients) == manager.max_concurrent_servers
            assert manager._spawning == {}
    finally:
        if reuse is not None:
            reuse.cancel()
            await asyncio.gather(reuse, return_exceptions=True)
        await manager.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_start", [False, True])
async def test_failed_or_cancelled_start_releases_capacity(
    tmp_path: Path,
    cancel_start: bool,
) -> None:
    manager = LSPManager(max_concurrent_servers=1)
    first = _project_file(tmp_path, "first")
    second = _project_file(tmp_path, "second")
    started = asyncio.Event()
    release = asyncio.Event()
    first_client = _client()
    second_client = _client()

    async def fail_start() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("Language server initialization failed")

    first_client.start.side_effect = fail_start
    try:
        with (
            patch(
                "cognis.tools.executor.lsp.manager.get_servers_for_extension",
                return_value=[_SERVER],
            ),
            patch("cognis.tools.executor.lsp.manager.resolve_command", return_value="svelteserver"),
            patch(
                "cognis.tools.executor.lsp.manager.LSPClient",
                side_effect=[first_client, second_client],
            ) as clients,
        ):
            await manager.touch_file(str(first), wait=False)
            await asyncio.wait_for(started.wait(), timeout=1)
            pending = next(iter(manager._spawning.values()))
            if cancel_start:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                first_client.close.assert_awaited()
            else:
                release.set()
                await pending

            assert manager._clients == {}
            assert manager._spawning == {}

            await manager.touch_file(str(second), wait=False)
            await asyncio.gather(*list(manager._spawning.values()))

            assert clients.call_count == 2
            assert len(manager._clients) == 1
            assert manager._spawning == {}
    finally:
        await manager.cleanup()
