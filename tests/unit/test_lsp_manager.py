"""Unit tests for the LSP manager."""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from unittest.mock import MagicMock, patch

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
