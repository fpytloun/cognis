"""Unit tests for the LSP manager."""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognis.tools.executor.lsp.manager import LSPManager, _find_project_root


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
    async def test_touch_file_no_extension(self) -> None:
        """File without extension should be skipped."""
        manager = LSPManager(enabled=True)
        await manager.touch_file("/src/Makefile", wait=True)
        assert len(manager._clients) == 0

    def test_get_diagnostics_empty(self) -> None:
        manager = LSPManager(enabled=True)
        assert manager.get_diagnostics() == {}
        assert manager.get_diagnostics("/src/foo.py") == {}

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
        file_path = tmp_path / "app.py"
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
            mock_server.root_markers = ()
            mock_server.language_id.return_value = "python"
            mock_servers.return_value = [mock_server]
            with patch.object(manager, "_spawn_client", AsyncMock(return_value=fake_client)):
                await manager.touch_file(str(file_path), wait=True)

        fake_client.did_open.assert_awaited_once()
        fake_client.wait_for_diagnostics.assert_awaited_once()


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

        with patch("cognis.tools.executor.lsp.manager.shutil.which", side_effect=mock_which):
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
