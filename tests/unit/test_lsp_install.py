"""Unit tests for LSP auto-install strategies."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognis.tools.executor.lsp.install import (
    NpmInstall,
    ToolchainInstall,
    resolve_command,
)


class TestNpmInstall:
    """Test npm installation strategy."""

    @pytest.mark.asyncio()
    async def test_detect_cached(self, tmp_path: Path) -> None:
        """Should detect an already-installed package."""
        strategy = NpmInstall(
            package="pyright",
            version="1.1.390",
            entry_point="node_modules/pyright/dist/pyright-langserver.js",
        )
        # Create the expected entry point
        entry = tmp_path / "pyright" / "1.1.390" / "node_modules" / "pyright" / "dist"
        entry.mkdir(parents=True)
        (entry / "pyright-langserver.js").touch()

        result = await strategy.detect("pyright", tmp_path)
        assert result is not None
        assert result.name == "pyright-langserver.js"

    @pytest.mark.asyncio()
    async def test_detect_not_cached(self, tmp_path: Path) -> None:
        """Should return None when not installed."""
        strategy = NpmInstall(
            package="pyright",
            version="1.1.390",
            entry_point="node_modules/pyright/dist/pyright-langserver.js",
        )
        result = await strategy.detect("pyright", tmp_path)
        assert result is None

    @pytest.mark.asyncio()
    async def test_install_no_npm(self, tmp_path: Path) -> None:
        """Should return None when npm is not available."""
        strategy = NpmInstall(
            package="pyright",
            version="1.1.390",
            entry_point="node_modules/pyright/dist/pyright-langserver.js",
        )
        with patch("cognis.tools.executor.lsp.install.shutil.which", return_value=None):
            result = await strategy.install("pyright", tmp_path)
        assert result is None


class TestToolchainInstall:
    """Test toolchain installation strategy."""

    @pytest.mark.asyncio()
    async def test_detect_cached(self, tmp_path: Path) -> None:
        """Should detect an already-installed binary."""
        strategy = ToolchainInstall(
            command=["go", "install", "golang.org/x/tools/gopls@latest"],
            binary_name="gopls",
            env_overrides={"GOBIN": "{cache_dir}"},
        )
        server_dir = tmp_path / "gopls"
        server_dir.mkdir()
        (server_dir / "gopls").touch()

        result = await strategy.detect("gopls", tmp_path)
        assert result is not None
        assert result.name == "gopls"

    @pytest.mark.asyncio()
    async def test_detect_not_cached(self, tmp_path: Path) -> None:
        strategy = ToolchainInstall(
            command=["go", "install", "golang.org/x/tools/gopls@latest"],
            binary_name="gopls",
            env_overrides={"GOBIN": "{cache_dir}"},
        )
        result = await strategy.detect("gopls", tmp_path)
        assert result is None

    @pytest.mark.asyncio()
    async def test_install_no_base_command(self, tmp_path: Path) -> None:
        """Should return None when the base command is not found."""
        strategy = ToolchainInstall(
            command=["go", "install", "golang.org/x/tools/gopls@latest"],
            binary_name="gopls",
            env_overrides={"GOBIN": "{cache_dir}"},
        )
        with patch("cognis.tools.executor.lsp.install.shutil.which", return_value=None):
            result = await strategy.install("gopls", tmp_path)
        assert result is None


class TestResolveCommand:
    """Test command resolution logic."""

    @pytest.mark.asyncio()
    async def test_found_on_path(self, tmp_path: Path) -> None:
        """Should return PATH binary when available."""
        with patch(
            "cognis.tools.executor.lsp.install.shutil.which",
            return_value="/usr/bin/pyright-langserver",
        ):
            result = await resolve_command(
                "pyright-langserver",
                "pyright",
                None,
                cache_dir=tmp_path,
            )
        assert result == "/usr/bin/pyright-langserver"

    @pytest.mark.asyncio()
    async def test_not_found_no_strategy(self, tmp_path: Path) -> None:
        """Should return None when not on PATH and no install strategy."""
        with patch("cognis.tools.executor.lsp.install.shutil.which", return_value=None):
            result = await resolve_command(
                "some-server",
                "some-server",
                None,
                cache_dir=tmp_path,
            )
        assert result is None

    @pytest.mark.asyncio()
    async def test_found_in_cache(self, tmp_path: Path) -> None:
        """Should check cache when not on PATH."""
        mock_strategy = MagicMock()
        mock_strategy.detect = AsyncMock(return_value=tmp_path / "cached-binary")
        mock_strategy.install = AsyncMock()

        with patch("cognis.tools.executor.lsp.install.shutil.which", return_value=None):
            result = await resolve_command(
                "test-server",
                "test-server",
                mock_strategy,
                cache_dir=tmp_path,
            )
        assert result == str(tmp_path / "cached-binary")
        mock_strategy.detect.assert_awaited_once()
        mock_strategy.install.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_auto_install_disabled(self, tmp_path: Path) -> None:
        """Should not install when auto_install=False."""
        mock_strategy = MagicMock()
        mock_strategy.detect = AsyncMock(return_value=None)
        mock_strategy.install = AsyncMock()

        with patch("cognis.tools.executor.lsp.install.shutil.which", return_value=None):
            result = await resolve_command(
                "test-server",
                "test-server",
                mock_strategy,
                auto_install=False,
                cache_dir=tmp_path,
            )
        assert result is None
        mock_strategy.install.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_auto_install_triggered(self, tmp_path: Path) -> None:
        """Should auto-install when not on PATH and not cached."""
        installed_path = tmp_path / "installed-binary"
        mock_strategy = MagicMock()
        mock_strategy.detect = AsyncMock(return_value=None)
        mock_strategy.install = AsyncMock(return_value=installed_path)

        with patch("cognis.tools.executor.lsp.install.shutil.which", return_value=None):
            result = await resolve_command(
                "test-server",
                "test-server",
                mock_strategy,
                auto_install=True,
                cache_dir=tmp_path,
            )
        assert result == str(installed_path)
        mock_strategy.install.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_auto_install_failure(self, tmp_path: Path) -> None:
        """Should return None when auto-install fails."""
        mock_strategy = MagicMock()
        mock_strategy.detect = AsyncMock(return_value=None)
        mock_strategy.install = AsyncMock(return_value=None)

        with patch("cognis.tools.executor.lsp.install.shutil.which", return_value=None):
            result = await resolve_command(
                "test-server",
                "test-server",
                mock_strategy,
                auto_install=True,
                cache_dir=tmp_path,
            )
        assert result is None
