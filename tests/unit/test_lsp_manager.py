"""Unit tests for the LSP manager."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognis.tools.executor.lsp.manager import (
    LSPManager,
    PreparedClient,
    _find_project_root,
    _should_skip_server_for_path,
)
from cognis.tools.executor.lsp.query import QueryOutcome, QueryStatus, aggregate_status
from cognis.tools.executor.lsp.servers import GOPLS, PYRIGHT, RUST_ANALYZER
from cognis.tools.executor.lsp.types import (
    DiagnosticFreshness,
    DiagnosticSnapshot,
    DiagnosticWaitResult,
)


class TestFindProjectRoot:
    """Test project root detection."""

    def test_finds_root_marker(self, tmp_path: Path) -> None:
        """Should find directory containing a root marker."""
        (tmp_path / "pyproject.toml").touch()
        sub = tmp_path / "src" / "pkg"
        sub.mkdir(parents=True)
        (sub / "module.py").touch()

        root = _find_project_root(str(sub / "module.py"), ("pyproject.toml",))
        assert root == str(tmp_path)

    def test_finds_nearest_root(self, tmp_path: Path) -> None:
        """Should find the nearest root marker, not a parent one."""
        (tmp_path / "go.mod").touch()
        sub = tmp_path / "subproject"
        sub.mkdir()
        (sub / "go.mod").touch()
        (sub / "main.go").touch()

        root = _find_project_root(str(sub / "main.go"), ("go.mod",))
        assert root == str(sub)

    def test_falls_back_to_parent_dir(self, tmp_path: Path) -> None:
        """When no marker found, fall back to file's parent directory."""
        (tmp_path / "random.py").touch()
        root = _find_project_root(str(tmp_path / "random.py"), ("nonexistent_marker.toml",))
        assert root == str(tmp_path)

    def test_empty_markers(self, tmp_path: Path) -> None:
        """Empty root markers should fall back to parent directory."""
        (tmp_path / "script.sh").touch()
        root = _find_project_root(str(tmp_path / "script.sh"), ())
        assert root == str(tmp_path)

    def test_skips_pyright_for_scratch_roots(self) -> None:
        """Scratch copies should not spawn heavyweight Pyright roots."""
        assert _should_skip_server_for_path("pyright", "/tmp/cognis/foo.py", "/tmp/cognis")
        assert _should_skip_server_for_path("pyright", "/var/tmp/cognis/foo.py", "/var/tmp")
        assert not _should_skip_server_for_path("ruff", "/tmp/cognis/foo.py", "/tmp/cognis")
        assert not _should_skip_server_for_path(
            "pyright", "/home/alice/src/sample/foo.py", "/home/alice/src/sample"
        )


class TestLSPManagerBasic:
    """Test basic LSP manager behavior."""

    def test_disabled_manager(self) -> None:
        manager = LSPManager(enabled=False)
        assert not manager.enabled

    @pytest.mark.asyncio()
    async def test_touch_file_disabled(self) -> None:
        """Disabled manager should no-op on touch_file."""
        manager = LSPManager(enabled=False)
        # Should not raise
        await manager.touch_file("/src/foo.py", wait=True)
        assert manager.get_diagnostics() == {}

    @pytest.mark.asyncio()
    async def test_touch_file_unknown_extension(self) -> None:
        """Unknown extension should not spawn any server."""
        manager = LSPManager(enabled=True)
        await manager.touch_file("/src/data.xyz", wait=True)
        assert len(manager._clients) == 0

    @pytest.mark.asyncio()
    async def test_touch_file_skips_pyright_for_scratch_files(self) -> None:
        """Temp file edits should avoid spawning expensive Pyright clients."""
        # The scratch roots are /tmp and /var/tmp, whatever TMPDIR points to.
        scratch = tempfile.TemporaryDirectory(dir="/tmp")
        target = Path(scratch.name) / "module.py"
        target.write_text("x = 1\n")
        manager = LSPManager(enabled=True)

        with (
            scratch,
            patch(
                "cognis.tools.executor.lsp.manager.resolve_command",
                new=AsyncMock(return_value="cmd"),
            ),
            patch("cognis.tools.executor.lsp.manager.LSPClient") as mock_client_class,
        ):
            mock_client = AsyncMock()
            mock_client.is_alive = True
            mock_client_class.return_value = mock_client

            await manager.prepare_file(str(target))

        spawned_servers = [call.kwargs["server_id"] for call in mock_client_class.call_args_list]
        assert "pyright" not in spawned_servers
        assert "ruff" in spawned_servers

    @pytest.mark.asyncio()
    async def test_spawn_cancelled_during_start_closes_unregistered_client(
        self, tmp_path: Path
    ) -> None:
        """A caller deadline during the handshake must not leak the server."""
        project = tmp_path / "cancel"
        project.mkdir()
        target = project / "module.py"
        target.write_text("x = 1\n")
        manager = LSPManager(enabled=True)

        started = asyncio.Event()

        async def slow_start() -> None:
            started.set()
            await asyncio.sleep(60)

        with (
            patch(
                "cognis.tools.executor.lsp.manager.resolve_command",
                new=AsyncMock(return_value="cmd"),
            ),
            patch("cognis.tools.executor.lsp.manager.LSPClient") as mock_client_class,
            patch(
                "cognis.tools.executor.lsp.manager._should_skip_server_for_path",
                return_value=False,
            ),
        ):
            mock_client = AsyncMock()
            mock_client.is_alive = True
            mock_client.start = AsyncMock(side_effect=slow_start)
            mock_client.close = AsyncMock()
            mock_client_class.return_value = mock_client

            task = asyncio.create_task(manager.prepare_file(str(target)))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert mock_client.close.await_count >= 1
        assert manager._clients == {}
        assert manager._spawning == {}

    @pytest.mark.asyncio()
    async def test_spawn_passes_workspace_configuration(self, tmp_path: Path) -> None:
        """Server definitions should pass default workspace config to clients."""
        project = tmp_path / "workspace-config"
        project.mkdir()
        target = project / "module.py"
        target.write_text("x = 1\n")
        manager = LSPManager(enabled=True)

        with (
            patch(
                "cognis.tools.executor.lsp.manager.resolve_command",
                new=AsyncMock(return_value="cmd"),
            ),
            patch("cognis.tools.executor.lsp.manager.LSPClient") as mock_client_class,
            patch(
                "cognis.tools.executor.lsp.manager._should_skip_server_for_path",
                return_value=False,
            ),
        ):
            mock_client = AsyncMock()
            mock_client.is_alive = True
            mock_client_class.return_value = mock_client

            await manager.prepare_file(str(target))

        kwargs = next(
            call.kwargs
            for call in mock_client_class.call_args_list
            if call.kwargs["workspace_configuration"] is not None
        )
        assert kwargs["workspace_configuration"]["python"]["analysis"]["diagnosticMode"] == (
            "openFilesOnly"
        )
        assert kwargs["init_options"]["settings"]["python"]["analysis"]["diagnosticMode"] == (
            "openFilesOnly"
        )
        assert "**/.worktrees" in kwargs["workspace_configuration"]["python"]["analysis"]["exclude"]

    @pytest.mark.asyncio()
    async def test_python_edit_diagnostics_use_ruff_only(self, tmp_path: Path) -> None:
        """Edit-time Python diagnostics should avoid spawning Pyright."""
        project = tmp_path / "diagnostics"
        project.mkdir()
        target = project / "module.py"
        target.write_text("x = 1\n")
        manager = LSPManager(enabled=True)

        with (
            patch(
                "cognis.tools.executor.lsp.manager.resolve_command",
                new=AsyncMock(return_value="cmd"),
            ),
            patch("cognis.tools.executor.lsp.manager.LSPClient") as mock_client_class,
            patch(
                "cognis.tools.executor.lsp.manager._should_skip_server_for_path",
                return_value=False,
            ),
        ):
            mock_client = MagicMock()
            mock_client.is_alive = True
            mock_client.start = AsyncMock()
            mock_client.did_open = AsyncMock()
            mock_client.did_save = AsyncMock()
            mock_client.wait_for_diagnostics.return_value = MagicMock(
                server_id="ruff",
                uri="file:///tmp/module.py",
                status=MagicMock(value="fresh_unversioned"),
                snapshot=None,
            )
            mock_client.get_diagnostic_snapshots.return_value = {}
            mock_client_class.return_value = mock_client

            await manager.touch_file(str(target), wait=True, purpose="diagnostics")

        spawned_servers = [call.kwargs["server_id"] for call in mock_client_class.call_args_list]
        assert spawned_servers == ["ruff"]

    @pytest.mark.asyncio()
    async def test_edit_diagnostic_collection_excludes_unselected_active_clients(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "module.py"
        target.write_text("x = 1\n")
        uri = f"file://{target}"
        manager = LSPManager(enabled=True)
        manager._opened_files["ruff:/project"] = {uri}
        manager._opened_files["pyright:/project"] = {uri}
        manager._file_versions[("ruff:/project", uri)] = 0

        ruff_client = MagicMock()
        ruff_client.server_id = "ruff"
        ruff_client.is_alive = True
        ruff_client.did_change = AsyncMock()
        ruff_client.did_save = AsyncMock()
        ruff_snapshot = DiagnosticSnapshot(
            server_id="ruff",
            uri=uri,
            document_version=1,
            diagnostic_version=1,
            received_sequence=1,
            received_at_monotonic=0.0,
            diagnostics=[],
            freshness=DiagnosticFreshness.FRESH,
        )
        ruff_client.wait_for_diagnostics = AsyncMock(
            return_value=DiagnosticWaitResult(
                server_id="ruff",
                uri=uri,
                target_version=1,
                status=DiagnosticFreshness.FRESH,
                duration_ms=1,
                snapshot=ruff_snapshot,
            )
        )
        pyright_client = MagicMock()
        pyright_client.server_id = "pyright"
        pyright_client.is_alive = True
        pyright_snapshot = DiagnosticSnapshot(
            server_id="pyright",
            uri=uri,
            document_version=99,
            diagnostic_version=99,
            received_sequence=99,
            received_at_monotonic=0.0,
            diagnostics=[],
            freshness=DiagnosticFreshness.FRESH,
        )
        pyright_client.get_diagnostic_snapshots.return_value = {uri: pyright_snapshot}
        manager._clients["ruff:/project"] = ruff_client
        manager._clients["pyright:/project"] = pyright_client

        server_def = MagicMock()
        server_def.server_id = "ruff"
        server_def.root_markers = ()
        server_def.language_id.return_value = "python"
        with (
            patch(
                "cognis.tools.executor.lsp.manager.get_servers_for_extension",
                return_value=[server_def],
            ),
            patch(
                "cognis.tools.executor.lsp.manager._find_project_root",
                return_value="/project",
            ),
        ):
            collection = await manager.touch_file(str(target), wait=True, purpose="diagnostics")

        snapshots = collection.snapshots_by_path[str(target)]
        assert [snapshot.server_id for snapshot in snapshots] == ["ruff"]

    def test_pyright_project_config_takes_precedence(self, tmp_path: Path) -> None:
        """Native Pyright project config should suppress Cognis defaults."""
        (tmp_path / "pyrightconfig.json").write_text("{}\n")

        assert PYRIGHT.workspace_configuration_for(str(tmp_path)) is None
        assert PYRIGHT.initialization_options_for(str(tmp_path)) is None

    def test_pyright_pyproject_config_takes_precedence(self, tmp_path: Path) -> None:
        """[tool.pyright] should suppress Cognis Pyright defaults."""
        (tmp_path / "pyproject.toml").write_text("[tool.pyright]\ntypeCheckingMode = 'strict'\n")

        assert PYRIGHT.workspace_configuration_for(str(tmp_path)) is None
        assert PYRIGHT.initialization_options_for(str(tmp_path)) is None

    def test_pyright_python_path_from_nearest_venv(self, tmp_path: Path) -> None:
        """The interpreter is discovered up to the git boundary and always passed."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        interpreter = repo / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        package_root = repo / "packages" / "executor"
        package_root.mkdir(parents=True)

        assert PYRIGHT.project_python_path(str(package_root)) == str(interpreter)
        config = PYRIGHT.workspace_configuration_for(str(package_root))
        assert config is not None
        assert config["python"]["pythonPath"] == str(interpreter)
        assert config["python"]["analysis"]["diagnosticMode"] == "openFilesOnly"
        options = PYRIGHT.initialization_options_for(str(package_root))
        assert options is not None
        assert options["settings"]["python"]["pythonPath"] == str(interpreter)

        # Native project config drops Cognis analysis defaults but keeps the interpreter.
        (package_root / "pyrightconfig.json").write_text("{}\n")
        native = PYRIGHT.workspace_configuration_for(str(package_root))
        assert native == {"python": {"pythonPath": str(interpreter)}}

    def test_pyright_python_path_stops_at_git_boundary(self, tmp_path: Path) -> None:
        outer = tmp_path / ".venv" / "bin" / "python"
        outer.parent.mkdir(parents=True)
        outer.touch()
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)

        assert PYRIGHT.project_python_path(str(repo)) is None
        assert PYRIGHT.workspace_configuration_for(str(repo)) == PYRIGHT.workspace_configuration

    def test_non_python_servers_have_safe_default_excludes(self, tmp_path: Path) -> None:
        """Broad workspace servers should receive safe default directory filters."""
        gopls_config = GOPLS.workspace_configuration_for(str(tmp_path))
        rust_config = RUST_ANALYZER.workspace_configuration_for(str(tmp_path))

        assert gopls_config is not None
        assert "-.worktrees" in gopls_config["gopls"]["directoryFilters"]
        assert rust_config is not None
        assert ".worktrees" in rust_config["rust-analyzer"]["files"]["excludeDirs"]

    @pytest.mark.asyncio()
    async def test_touch_file_no_extension(self) -> None:
        """File without extension should be skipped."""
        manager = LSPManager(enabled=True)
        await manager.touch_file("/src/Makefile", wait=True)
        assert len(manager._clients) == 0

    def test_get_diagnostics_empty(self) -> None:
        manager = LSPManager(enabled=True)
        assert manager.get_diagnostics() == {}
        assert manager.get_diagnostics("/src/foo.py") == {}

    def test_has_pending_diagnostics_checks_active_clients(self) -> None:
        manager = LSPManager(enabled=True)
        client = MagicMock()
        client.has_pending_diagnostics.side_effect = lambda uri: uri.endswith("foo.py")
        manager._clients["pyright:/src"] = client

        assert manager.has_pending_diagnostics(["/src/foo.py"])
        assert not manager.has_pending_diagnostics(["/src/bar.py"])

    @pytest.mark.asyncio()
    async def test_cleanup_idempotent(self) -> None:
        """Cleanup should be safe to call multiple times."""
        manager = LSPManager(enabled=True)
        await manager.cleanup()
        await manager.cleanup()  # Should not raise

    @pytest.mark.asyncio()
    async def test_max_concurrent_servers(self) -> None:
        """Manager should respect max_concurrent_servers."""
        manager = LSPManager(
            enabled=True,
            max_concurrent_servers=2,
        )
        # Simulate having 2 clients already
        manager._clients["a:root1"] = MagicMock()
        manager._clients["b:root2"] = MagicMock()

        # Trying to spawn another should be rejected (returns without spawning)
        # We need the server command to not be found to test the limit path
        with patch("cognis.tools.executor.lsp.manager.get_servers_for_extension") as mock_servers:
            mock_server = MagicMock()
            mock_server.server_id = "new-server"
            mock_server.root_markers = ()
            mock_server.language_id.return_value = "test"
            mock_server.extensions = frozenset({".test"})
            mock_servers.return_value = [mock_server]

            await manager.touch_file("/src/foo.test", wait=False)
            # Should still be at 2 — new server not spawned
            assert len(manager._clients) == 2


class TestBrokenRetry:
    """Test broken server retry logic."""

    @pytest.mark.asyncio()
    async def test_broken_server_skipped(self) -> None:
        manager = LSPManager(enabled=True)
        client_key = "pyright:/tmp"
        manager._broken[client_key] = monotonic() + 3600  # Broken for 1 hour

        with patch("cognis.tools.executor.lsp.manager.get_servers_for_extension") as mock_servers:
            mock_server = MagicMock()
            mock_server.server_id = "pyright"
            mock_server.root_markers = ()
            mock_servers.return_value = [mock_server]

            with patch("cognis.tools.executor.lsp.manager._find_project_root", return_value="/tmp"):
                await manager.touch_file("/tmp/foo.py", wait=True)

        assert len(manager._clients) == 0

    @pytest.mark.asyncio()
    async def test_broken_retry_after_expired(self) -> None:
        manager = LSPManager(enabled=True)
        client_key = "pyright:/tmp"
        # Broken in the past — should be retried
        manager._broken[client_key] = monotonic() - 1

        # After touch_file processes it, the broken entry should be removed
        with patch("cognis.tools.executor.lsp.manager.get_servers_for_extension") as mock_servers:
            mock_servers.return_value = []  # No servers to simplify test
            await manager.touch_file("/tmp/foo.py", wait=True)

        # The broken entry should have been cleared or no longer present
        # (since no server matches, nothing re-breaks it)


class TestFirstTouchWait:
    """Test first-touch wait semantics."""

    @pytest.mark.asyncio()
    async def test_first_touch_waits_for_spawn_and_diagnostics(self, tmp_path: Path) -> None:
        project = tmp_path / "first-touch"
        project.mkdir()
        file_path = project / "app.py"
        file_path.write_text("x = 1\n")

        manager = LSPManager(enabled=True)
        fake_client = MagicMock()
        fake_client.is_alive = True
        fake_client.process = MagicMock()
        fake_client.process.pid = 123
        fake_client.server_id = "pyright"
        fake_client.server_name = "Pyright"
        fake_client.did_open = AsyncMock()
        fake_client.did_change = AsyncMock()
        fake_client.wait_for_diagnostics = AsyncMock(return_value=[])
        fake_client.get_diagnostics = MagicMock(return_value={})

        with patch("cognis.tools.executor.lsp.manager.get_servers_for_extension") as mock_servers:
            mock_server = MagicMock()
            mock_server.server_id = "pyright"
            mock_server.root_markers = ("pyproject.toml",)
            mock_server.language_id.return_value = "python"
            mock_servers.return_value = [mock_server]
            with (
                patch.object(manager, "_spawn_client", AsyncMock(return_value=fake_client)),
                patch(
                    "cognis.tools.executor.lsp.manager._should_skip_server_for_path",
                    return_value=False,
                ),
            ):
                await manager.touch_file(str(file_path), wait=True)

        fake_client.did_open.assert_awaited_once()
        fake_client.wait_for_diagnostics.assert_awaited_once()


def _prepared_client(
    server_id: str,
    *,
    supports: bool = True,
    alive: bool = True,
) -> tuple[PreparedClient, MagicMock]:
    client = MagicMock()
    client.server_id = server_id
    client.is_alive = alive
    client.supports.return_value = supports
    entry = PreparedClient(
        client=client,
        client_key=f"{server_id}:/project",
        uri="file:///project/a.py",
        language_id="python",
        version=0,
    )
    return entry, client


class TestPrepareAndQuery:
    """Query preparation and capability-gated fanout."""

    @pytest.mark.asyncio()
    async def test_prepare_file_syncs_without_waiting_for_diagnostics(self, tmp_path: Path) -> None:
        project = tmp_path / "prep"
        project.mkdir()
        target = project / "app.py"
        target.write_text("x = 1\n")
        manager = LSPManager(enabled=True)

        fake_client = MagicMock()
        fake_client.is_alive = True
        fake_client.server_id = "pyright"
        fake_client.did_open = AsyncMock()
        fake_client.did_change = AsyncMock()
        fake_client.wait_for_diagnostics = AsyncMock()
        fake_client.wait_for_progress_idle = AsyncMock(return_value=True)

        with (
            patch("cognis.tools.executor.lsp.manager.get_servers_for_extension") as mock_servers,
            patch.object(manager, "_spawn_client", AsyncMock(return_value=fake_client)),
            patch(
                "cognis.tools.executor.lsp.manager._should_skip_server_for_path",
                return_value=False,
            ),
        ):
            mock_server = MagicMock()
            mock_server.server_id = "pyright"
            mock_server.root_markers = ()
            mock_server.language_id.return_value = "python"
            mock_servers.return_value = [mock_server]

            first = await manager.prepare_file(str(target))
            second = await manager.prepare_file(str(target))

        assert [entry.version for entry in first] == [0]
        assert [entry.version for entry in second] == [1]
        assert first[0].language_id == "python"
        fake_client.did_open.assert_awaited_once()
        fake_client.did_change.assert_awaited_once()
        fake_client.wait_for_diagnostics.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_prepare_file_disabled(self) -> None:
        manager = LSPManager(enabled=False)
        assert await manager.prepare_file("/src/a.py") == []

    @pytest.mark.asyncio()
    async def test_prepare_file_waits_for_cold_start_progress_once(self, tmp_path: Path) -> None:
        project = tmp_path / "cold"
        project.mkdir()
        target = project / "app.py"
        target.write_text("x = 1\n")
        manager = LSPManager(enabled=True)

        fake_client = MagicMock()
        fake_client.is_alive = True
        fake_client.server_id = "pyright"
        fake_client.did_open = AsyncMock()
        fake_client.did_change = AsyncMock()
        fake_client.wait_for_progress_idle = AsyncMock(return_value=False)
        client_key = "pyright:" + str(project)

        async def spawn(*_: object, **__: object) -> MagicMock:
            # Mirrors _spawn_client registration: every spawn starts cold,
            # whichever path (query or read warm-up) triggered it.
            manager._clients[client_key] = fake_client
            manager._cold_clients.add(client_key)
            return fake_client

        with (
            patch("cognis.tools.executor.lsp.manager.get_servers_for_extension") as mock_servers,
            patch.object(manager, "_spawn_client", spawn),
            patch(
                "cognis.tools.executor.lsp.manager._should_skip_server_for_path",
                return_value=False,
            ),
        ):
            mock_server = MagicMock()
            mock_server.server_id = "pyright"
            mock_server.root_markers = ()
            mock_server.language_id.return_value = "python"
            mock_servers.return_value = [mock_server]

            # Spawned by a non-waiting warm-up (the read path): still cold.
            await manager.touch_file(str(target), wait=False, purpose="semantic")
            await asyncio.gather(*manager._spawning.values())
            assert manager._cold_clients == {client_key}
            fake_client.wait_for_progress_idle.assert_not_awaited()

            await manager.prepare_file(str(target))
            await manager.prepare_file(str(target))
            # A diagnostics sync (edit path) never triggers the query wait.
            manager._cold_clients.add(client_key)
            await manager.touch_file(str(target), wait=False, purpose="diagnostics")

        fake_client.wait_for_progress_idle.assert_awaited_once()
        assert manager._cold_clients == {client_key}
        fake_client.close = AsyncMock()
        await manager._remove_client(client_key)
        assert manager._cold_clients == set()

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("server_id", ["typescript", "pyright"])
    @pytest.mark.parametrize("diagnostics_timeout", [False, True])
    async def test_cold_typescript_waits_for_project_diagnostics_only_once(
        self, server_id: str, diagnostics_timeout: bool
    ) -> None:
        manager = LSPManager(enabled=True)
        entry, client = _prepared_client(server_id)
        client.wait_for_progress_idle = AsyncMock(return_value=True)
        manager._cold_clients.add(entry.client_key)
        diagnostics = AsyncMock(side_effect=TimeoutError if diagnostics_timeout else None)
        with (
            patch.object(manager, "_sync_file", AsyncMock(return_value=[entry])),
            patch.object(manager, "_wait_client_diagnostics", diagnostics),
        ):
            assert await manager.prepare_file("/project/a.ts") == [entry]
            assert await manager.prepare_file("/project/a.ts") == [entry]
        if server_id == "typescript":
            diagnostics.assert_awaited_once_with(client, entry.uri, entry.version)
        else:
            diagnostics.assert_not_awaited()
        client.wait_for_progress_idle.assert_awaited_once()
        assert manager._cold_clients == set()

    @pytest.mark.asyncio()
    async def test_query_reports_mixed_support(self) -> None:
        manager = LSPManager(enabled=True)
        ok_entry, ok_client = _prepared_client("pyright")
        unsupported_entry, unsupported_client = _prepared_client("ruff", supports=False)

        async def invoke(client: MagicMock) -> list[dict[str, object]]:
            return [{"uri": "file:///project/a.py"}] if client is ok_client else []

        outcomes = await manager.query(
            [ok_entry, unsupported_entry], "textDocument/definition", invoke
        )

        assert [(o.server_id, o.status) for o in outcomes] == [
            ("pyright", QueryStatus.OK),
            ("ruff", QueryStatus.UNSUPPORTED),
        ]
        assert outcomes[0].items == [{"uri": "file:///project/a.py"}]
        unsupported_client.supports.assert_called_once_with(
            "textDocument/definition", uri="file:///project/a.py", language_id="python"
        )
        assert manager._last_access["pyright:/project"] > 0

    @pytest.mark.asyncio()
    async def test_query_empty_timeout_and_failure(self) -> None:
        manager = LSPManager(enabled=True)
        empty_entry, empty_client = _prepared_client("a")
        slow_entry, slow_client = _prepared_client("b")
        failing_entry, failing_client = _prepared_client("c", alive=False)

        async def invoke(client: MagicMock) -> object:
            if client is empty_client:
                return None
            if client is slow_client:
                await asyncio.sleep(10)
            raise RuntimeError("boom")

        outcomes = await manager.query(
            [empty_entry, slow_entry, failing_entry],
            "textDocument/hover",
            invoke,
            timeout_s=0.01,
        )

        assert [o.status for o in outcomes] == [
            QueryStatus.EMPTY,
            QueryStatus.TIMEOUT,
            QueryStatus.FAILED,
        ]
        assert outcomes[2].message == "server exited"
        assert "boom" not in (outcomes[2].message or "")
        assert failing_client.is_alive is False

    @pytest.mark.asyncio()
    async def test_query_marks_oversized_reply_partial_even_with_malformed_entries(
        self,
    ) -> None:
        """Truncation is decided on the raw length, not on accepted dict count."""
        manager = LSPManager(enabled=True)
        entry, _ = _prepared_client("a")
        reply: list[object] = [{"uri": "file:///project/a.py"}] * 2000
        reply.insert(0, "malformed")
        reply.extend([{"uri": "file:///project/b.py"}] * 5)

        async def invoke(_: MagicMock) -> object:
            return reply

        outcomes = await manager.query([entry], "textDocument/references", invoke)
        assert outcomes[0].status is QueryStatus.PARTIAL
        assert len(outcomes[0].items) == 2000
        assert outcomes[0].message == "response truncated to 2000 items"

    @pytest.mark.asyncio()
    async def test_query_truncation_survives_real_client_normalization(self) -> None:
        """The flag is carried from the client method, not recomputed downstream."""
        from cognis.tools.executor.lsp.client import LSPClient

        manager = LSPManager(enabled=True)
        client = LSPClient("pyright", "cmd", [], "file:///project")
        client._closed = False
        client.process = MagicMock(returncode=None)
        client.capabilities = {"referencesProvider": True}
        reply: list[object] = ["malformed", *([{"uri": "file:///project/a.py"}] * 2500)]
        client._request = AsyncMock(return_value=reply)  # type: ignore[method-assign]
        entry = PreparedClient(
            client=client,
            client_key="pyright:/project",
            uri="file:///project/a.py",
            language_id="python",
            version=0,
        )

        outcomes = await manager.query(
            [entry],
            "textDocument/references",
            lambda c: c.references("/project/a.py", 0, 0),
        )
        assert outcomes[0].status is QueryStatus.PARTIAL
        assert len(outcomes[0].items) == 2000

    @pytest.mark.asyncio()
    async def test_query_normalizes_scalar_results(self) -> None:
        manager = LSPManager(enabled=True)
        entry, _ = _prepared_client("a")

        async def invoke(_: MagicMock) -> object:
            return {"contents": "doc"}

        outcomes = await manager.query([entry], "textDocument/hover", invoke)
        assert outcomes[0].status is QueryStatus.OK
        assert outcomes[0].items == [{"contents": "doc"}]

    @pytest.mark.asyncio()
    async def test_query_with_no_clients(self) -> None:
        manager = LSPManager(enabled=True)

        async def invoke(_: MagicMock) -> object:
            raise AssertionError("must not be called")

        assert await manager.query([], "textDocument/hover", invoke) == []

    @pytest.mark.asyncio()
    async def test_dead_client_is_replaced_on_prepare(self, tmp_path: Path) -> None:
        """A crashed client is removed so the next prepare respawns with fresh state."""
        project = tmp_path / "respawn"
        project.mkdir()
        target = project / "app.py"
        target.write_text("x = 1\n")
        manager = LSPManager(enabled=True)

        dead = MagicMock()
        dead.is_alive = False
        dead.close = AsyncMock()
        manager._clients[f"pyright:{project}"] = dead
        manager._opened_files[f"pyright:{project}"] = {"file:///stale"}

        fresh = MagicMock()
        fresh.is_alive = True
        fresh.did_open = AsyncMock()
        fresh.wait_for_progress_idle = AsyncMock(return_value=True)

        with (
            patch("cognis.tools.executor.lsp.manager.get_servers_for_extension") as mock_servers,
            patch.object(manager, "_spawn_client", AsyncMock(return_value=fresh)),
            patch(
                "cognis.tools.executor.lsp.manager._should_skip_server_for_path",
                return_value=False,
            ),
        ):
            mock_server = MagicMock()
            mock_server.server_id = "pyright"
            mock_server.root_markers = ()
            mock_server.language_id.return_value = "python"
            mock_servers.return_value = [mock_server]

            prepared = await manager.prepare_file(str(target))

        assert prepared[0].client is fresh
        dead.close.assert_awaited_once()
        fresh.did_open.assert_awaited_once()


class TestAggregateStatus:
    def test_aggregate_rules(self) -> None:
        def outcome(server: str, status: QueryStatus, items: int = 0) -> QueryOutcome:
            return QueryOutcome(server_id=server, status=status, items=[{}] * items)

        assert aggregate_status([]) is QueryStatus.UNSUPPORTED
        assert aggregate_status([outcome("a", QueryStatus.OK, 1)]) is QueryStatus.OK
        assert (
            aggregate_status(
                [outcome("a", QueryStatus.OK, 1), outcome("b", QueryStatus.UNSUPPORTED)]
            )
            is QueryStatus.OK
        )
        assert (
            aggregate_status([outcome("a", QueryStatus.OK, 1), outcome("b", QueryStatus.TIMEOUT)])
            is QueryStatus.PARTIAL
        )
        assert (
            aggregate_status([outcome("a", QueryStatus.EMPTY), outcome("b", QueryStatus.FAILED)])
            is QueryStatus.EMPTY
        )
        assert (
            aggregate_status(
                [outcome("a", QueryStatus.UNSUPPORTED), outcome("b", QueryStatus.UNSUPPORTED)]
            )
            is QueryStatus.UNSUPPORTED
        )
        assert (
            aggregate_status([outcome("a", QueryStatus.TIMEOUT), outcome("b", QueryStatus.FAILED)])
            is QueryStatus.TIMEOUT
        )
        assert aggregate_status([outcome("a", QueryStatus.FAILED)]) is QueryStatus.FAILED


class TestLSPManagerStatus:
    """Test the status() method for /lsp command."""

    def test_status_disabled(self) -> None:
        manager = LSPManager(enabled=False)
        status = manager.status()
        assert status["config"]["enabled"] is False
        assert status["totals"]["active_server_count"] == 0

    def test_status_empty(self) -> None:
        manager = LSPManager(enabled=True)
        status = manager.status()
        assert status["config"]["enabled"] is True
        assert status["active_servers"] == []
        assert status["broken_servers"] == []
        assert status["totals"]["files_tracked"] == 0
        assert status["totals"]["total_errors"] == 0
        assert status["totals"]["total_warnings"] == 0

    def test_status_with_broken_server(self) -> None:
        manager = LSPManager(enabled=True)
        manager._broken["pyright:/tmp/project"] = monotonic() + 300
        status = manager.status()
        assert len(status["broken_servers"]) == 1
        assert status["broken_servers"][0]["client_key"] == "pyright:/tmp/project"
        assert status["broken_servers"][0]["retry_in_seconds"] > 0

    def test_status_with_mock_client(self) -> None:
        """Test status with a mock LSP client."""
        manager = LSPManager(enabled=True, max_concurrent_servers=4)

        mock_client = MagicMock()
        mock_client.server_id = "pyright"
        mock_client.server_name = "Pyright"
        mock_client.is_alive = True
        mock_client.process = MagicMock()
        mock_client.process.pid = 12345
        mock_client.get_diagnostics.return_value = {
            "file:///src/foo.py": [
                MagicMock(severity=MagicMock(value=1)),  # error
                MagicMock(severity=MagicMock(value=2)),  # warning
            ],
            "file:///src/bar.py": [
                MagicMock(severity=MagicMock(value=1)),  # error
            ],
        }

        client_key = "pyright:/tmp/project"
        manager._clients[client_key] = mock_client
        manager._opened_files[client_key] = {
            "file:///src/foo.py",
            "file:///src/bar.py",
            "file:///src/baz.py",
        }
        manager._last_access[client_key] = monotonic() - 45

        status = manager.status()
        assert status["config"]["max_concurrent_servers"] == 4
        assert len(status["active_servers"]) == 1

        srv = status["active_servers"][0]
        assert srv["server_id"] == "pyright"
        assert srv["server_name"] == "Pyright"
        assert srv["root_path"] == "/tmp/project"
        assert srv["pid"] == 12345
        assert srv["alive"] is True
        assert srv["file_count"] == 3
        assert srv["error_count"] == 2
        assert srv["warning_count"] == 1
        assert srv["idle_seconds"] >= 44

        assert status["totals"]["active_server_count"] == 1
        assert status["totals"]["files_tracked"] == 3
        assert status["totals"]["total_errors"] == 2
        assert status["totals"]["total_warnings"] == 1


class TestAvailableServers:
    """Test the available_servers() method."""

    @pytest.mark.asyncio()
    async def test_available_servers_returns_all_definitions(self) -> None:
        """Should return an entry for every built-in server."""
        manager = LSPManager(enabled=True)
        with patch("cognis.tools.executor.lsp.manager.shutil.which", return_value=None):
            results = await manager.available_servers()
        # Should have entries for all builtin servers
        from cognis.tools.executor.lsp.servers import BUILTIN_SERVERS

        assert len(results) == len(BUILTIN_SERVERS)
        ids = {r["server_id"] for r in results}
        assert "pyright" in ids
        assert "yaml" in ids
        assert "typescript" in ids

    @pytest.mark.asyncio()
    async def test_available_servers_detects_path(self) -> None:
        """Should mark servers found on PATH as available."""
        manager = LSPManager(enabled=True)

        def mock_which(cmd: str) -> str | None:
            if cmd == "gopls":
                return "/usr/local/bin/gopls"
            return None

        with (
            patch("cognis.tools.executor.lsp.manager.shutil.which", side_effect=mock_which),
            patch(
                "cognis.tools.executor.lsp.install.NpmInstall.detect",
                new=AsyncMock(return_value=None),
            ),
        ):
            results = await manager.available_servers()

        gopls = next(r for r in results if r["server_id"] == "gopls")
        assert gopls["available"] is True
        assert gopls["path"] == "/usr/local/bin/gopls"

        pyright = next(r for r in results if r["server_id"] == "pyright")
        assert pyright["available"] is False

    @pytest.mark.asyncio()
    async def test_available_servers_shows_active(self) -> None:
        """Should mark active servers."""
        manager = LSPManager(enabled=True)
        manager._clients["pyright:/tmp/project"] = MagicMock()

        with patch("cognis.tools.executor.lsp.manager.shutil.which", return_value=None):
            results = await manager.available_servers()

        pyright = next(r for r in results if r["server_id"] == "pyright")
        assert pyright["active"] is True

        gopls = next(r for r in results if r["server_id"] == "gopls")
        assert gopls["active"] is False
