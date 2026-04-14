"""Unit tests for executor-native tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cognis.models.tool import ExecutorHandle
from cognis.tools.executor.definitions import (
    ALL_EXECUTOR_TOOLS,
    executor_tool_definitions,
    executor_tool_handlers,
)
from cognis.tools.executor.filesystem import (
    handle_edit,
    handle_list_directory,
    handle_multiedit,
    handle_patch,
    handle_read,
    handle_write,
)
from cognis.tools.executor.search import handle_glob, handle_grep
from cognis.tools.executor.shell import handle_bash
from cognis.tools.registry import ToolExecutionContext

_DUMMY_CONTEXT = ToolExecutionContext(
    executor_handle=ExecutorHandle(
        executor_id="test",
        executor_type="in_process",
    )
)


def _context(
    scope_id: str = "scope-1", runtime_metadata: dict[str, Any] | None = None
) -> ToolExecutionContext:
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(
            executor_id="test",
            executor_type="in_process",
        ),
        runtime_metadata=runtime_metadata or {},
        execution_scope_id=scope_id,
    )


class TestDefinitions:
    """Test tool definition registry."""

    def test_executor_tool_definitions_returns_all(self) -> None:
        defs = executor_tool_definitions()
        # Web tools are dynamic, but the static executor registry now also
        # includes browser and document tools.
        assert len(defs) >= 11
        names = {d.name for d in defs}
        assert {
            "read",
            "write",
            "edit",
            "patch",
            "multiedit",
            "list_directory",
            "glob",
            "grep",
            "bash",
            "document_generate",
            "artifact_publish",
        }.issubset(names)

    def test_all_definitions_have_executor_source(self) -> None:
        for tool in ALL_EXECUTOR_TOOLS:
            assert tool.source.type == "executor"

    def test_handlers_match_definitions(self) -> None:
        handlers = executor_tool_handlers()
        defs = executor_tool_definitions()
        for d in defs:
            assert d.name in handlers, f"Missing handler for {d.name}"

    def test_write_tools_are_non_bypassable(self) -> None:
        write_tools = {"write", "edit", "patch", "multiedit", "bash"}
        for tool in ALL_EXECUTOR_TOOLS:
            if tool.name in write_tools:
                assert tool.non_bypassable, f"{tool.name} should be non_bypassable"
            if tool.name in {"read", "glob", "grep", "list_directory"}:
                assert tool.read_only, f"{tool.name} should be read_only"


class TestReadTool:
    """Test the read filesystem tool."""

    @pytest.fixture()
    def tmp_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        return f

    @pytest.mark.asyncio()
    async def test_read_file(self, tmp_file: Path) -> None:
        result = await handle_read({"file_path": str(tmp_file)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "1: line1" in result.output
        assert "5: line5" in result.output

    @pytest.mark.asyncio()
    async def test_read_with_offset(self, tmp_file: Path) -> None:
        result = await handle_read(
            {"file_path": str(tmp_file), "offset": 3, "limit": 2}, _DUMMY_CONTEXT
        )
        assert not result.is_error
        assert "3: line3" in result.output
        assert "4: line4" in result.output
        assert "line1" not in result.output

    @pytest.mark.asyncio()
    async def test_read_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").touch()
        (tmp_path / "subdir").mkdir()
        result = await handle_read({"file_path": str(tmp_path)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "subdir/" in result.output
        assert "a.txt" in result.output

    @pytest.mark.asyncio()
    async def test_read_nonexistent(self) -> None:
        result = await handle_read({"file_path": "/nonexistent/path"}, _DUMMY_CONTEXT)
        assert result.is_error


class TestWriteTool:
    """Test the write filesystem tool."""

    @pytest.mark.asyncio()
    async def test_write_new_file(self, tmp_path: Path) -> None:
        target = tmp_path / "new.txt"
        result = await handle_write({"file_path": str(target), "content": "hello"}, _context())
        assert not result.is_error
        assert target.read_text() == "hello"

    @pytest.mark.asyncio()
    async def test_write_creates_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c.txt"
        result = await handle_write({"file_path": str(target), "content": "deep"}, _context())
        assert not result.is_error
        assert target.read_text() == "deep"

    @pytest.mark.asyncio()
    async def test_write_existing_file_requires_prior_read(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.txt"
        target.write_text("old")

        result = await handle_write({"file_path": str(target), "content": "new"}, _context())

        assert result.is_error
        assert "Use the read tool first" in result.output

    @pytest.mark.asyncio()
    async def test_write_existing_file_fails_after_external_change(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.txt"
        target.write_text("old")
        context = _context()
        await handle_read({"file_path": str(target)}, context)
        target.write_text("user change")

        result = await handle_write({"file_path": str(target), "content": "new"}, context)

        assert result.is_error
        assert "modified since it was last read" in result.output

    @pytest.mark.asyncio()
    async def test_write_succeeds_when_lsp_fails(self, tmp_path: Path) -> None:
        class _BrokenLSP:
            async def touch_file(self, *_: object, **__: object) -> None:
                raise RuntimeError("boom")

            def get_diagnostics(self, *_: object, **__: object) -> dict[str, list[object]]:
                return {}

        target = tmp_path / "lsp.txt"
        context = ToolExecutionContext(
            executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
            runtime_metadata={"lsp_manager": _BrokenLSP()},
        )

        result = await handle_write({"file_path": str(target), "content": "hello"}, context)

        assert not result.is_error
        assert target.read_text() == "hello"


class TestEditTool:
    """Test the edit filesystem tool."""

    @pytest.fixture()
    def tmp_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "edit.txt"
        f.write_text("hello world\nfoo bar\nhello world\n")
        return f

    @pytest.mark.asyncio()
    async def test_edit_single_match(self, tmp_file: Path) -> None:
        context = _context()
        await handle_read({"file_path": str(tmp_file)}, context)
        result = await handle_edit(
            {"file_path": str(tmp_file), "old_string": "foo bar", "new_string": "baz qux"},
            context,
        )
        assert not result.is_error
        assert "baz qux" in tmp_file.read_text()

    @pytest.mark.asyncio()
    async def test_edit_multiple_matches_fails(self, tmp_file: Path) -> None:
        context = _context()
        await handle_read({"file_path": str(tmp_file)}, context)
        result = await handle_edit(
            {"file_path": str(tmp_file), "old_string": "hello world", "new_string": "hi"},
            context,
        )
        assert result.is_error
        assert "2 matches" in result.output

    @pytest.mark.asyncio()
    async def test_edit_replace_all(self, tmp_file: Path) -> None:
        context = _context()
        await handle_read({"file_path": str(tmp_file)}, context)
        result = await handle_edit(
            {
                "file_path": str(tmp_file),
                "old_string": "hello world",
                "new_string": "hi",
                "replace_all": True,
            },
            context,
        )
        assert not result.is_error
        assert tmp_file.read_text().count("hi") == 2

    @pytest.mark.asyncio()
    async def test_edit_not_found(self, tmp_file: Path) -> None:
        context = _context()
        await handle_read({"file_path": str(tmp_file)}, context)
        result = await handle_edit(
            {"file_path": str(tmp_file), "old_string": "nonexistent", "new_string": "x"},
            context,
        )
        assert result.is_error

    @pytest.mark.asyncio()
    async def test_edit_requires_prior_read(self, tmp_file: Path) -> None:
        result = await handle_edit(
            {"file_path": str(tmp_file), "old_string": "foo bar", "new_string": "baz qux"},
            _context(),
        )

        assert result.is_error
        assert "Use the read tool first" in result.output


class TestMultieditTool:
    """Test the multiedit filesystem tool."""

    @pytest.mark.asyncio()
    async def test_multiedit(self, tmp_path: Path) -> None:
        f = tmp_path / "multi.txt"
        f.write_text("aaa\nbbb\nccc\n")
        context = _context()
        await handle_read({"file_path": str(f)}, context)
        result = await handle_multiedit(
            {
                "file_path": str(f),
                "edits": [
                    {"old_string": "aaa", "new_string": "AAA"},
                    {"old_string": "ccc", "new_string": "CCC"},
                ],
            },
            context,
        )
        assert not result.is_error
        content = f.read_text()
        assert "AAA" in content
        assert "CCC" in content

    @pytest.mark.asyncio()
    async def test_multiedit_stale_read_fails(self, tmp_path: Path) -> None:
        f = tmp_path / "multi.txt"
        f.write_text("aaa\nbbb\nccc\n")
        context = _context()
        await handle_read({"file_path": str(f)}, context)
        f.write_text("user changed\n")

        result = await handle_multiedit(
            {
                "file_path": str(f),
                "edits": [{"old_string": "user", "new_string": "agent"}],
            },
            context,
        )

        assert result.is_error
        assert "modified since it was last read" in result.output

    @pytest.mark.asyncio()
    async def test_freshness_is_scope_local(self, tmp_path: Path) -> None:
        target = tmp_path / "scope.txt"
        target.write_text("old")
        context_a = _context("scope-a")
        context_b = _context("scope-b")
        await handle_read({"file_path": str(target)}, context_a)

        result = await handle_write({"file_path": str(target), "content": "new"}, context_b)

        assert result.is_error
        assert "Use the read tool first" in result.output


class TestPatchTool:
    """Test the patch filesystem tool."""

    @pytest.mark.asyncio()
    async def test_patch_rejects_apply_patch_format(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")

        result = await handle_patch(
            {
                "patch_text": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
                )
            },
            _DUMMY_CONTEXT,
        )

        assert result.is_error
        assert "Unsupported patch format" in result.output

    @pytest.mark.asyncio()
    async def test_patch_reports_missing_apply_patch_target(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.txt"

        result = await handle_patch(
            {
                "patch_text": (
                    f"*** Begin Patch\n*** Update File: {missing}\n@@\n-hello\n+hi\n*** End Patch\n"
                )
            },
            _DUMMY_CONTEXT,
        )

        assert result.is_error
        assert f"Update File target does not exist: {missing}" in result.output

    @pytest.mark.asyncio()
    async def test_patch_requires_prior_read(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")

        result = await handle_patch(
            {"patch_text": f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-hello\n+hi\n"},
            _context(),
        )

        assert result.is_error
        assert "Use the read tool first" in result.output


class TestGlobTool:
    """Test the glob search tool."""

    @pytest.mark.asyncio()
    async def test_glob_finds_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").touch()
        (tmp_path / "b.py").touch()
        (tmp_path / "c.txt").touch()
        result = await handle_glob({"pattern": "*.py", "path": str(tmp_path)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "a.py" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output

    @pytest.mark.asyncio()
    async def test_glob_no_matches(self, tmp_path: Path) -> None:
        result = await handle_glob({"pattern": "*.xyz", "path": str(tmp_path)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "No files found" in result.output

    @pytest.mark.asyncio()
    async def test_glob_defaults_to_home_when_path_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home_dir = tmp_path / "home"
        cwd_dir = tmp_path / "cwd"
        home_dir.mkdir()
        cwd_dir.mkdir()
        (home_dir / "home.py").touch()
        (cwd_dir / "cwd.py").touch()
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.chdir(cwd_dir)

        result = await handle_glob({"pattern": "*.py"}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert "home.py" in result.output
        assert "cwd.py" not in result.output


class TestGrepTool:
    """Test the grep search tool."""

    @pytest.mark.asyncio()
    async def test_grep_finds_content(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def hello():\n    pass\n")
        (tmp_path / "b.py").write_text("def world():\n    pass\n")
        result = await handle_grep({"pattern": "hello", "path": str(tmp_path)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "a.py" in result.output
        assert "hello" in result.output

    @pytest.mark.asyncio()
    async def test_grep_no_matches(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("nothing here\n")
        result = await handle_grep(
            {"pattern": "nonexistent", "path": str(tmp_path)}, _DUMMY_CONTEXT
        )
        assert not result.is_error
        assert "No matches" in result.output

    @pytest.mark.asyncio()
    async def test_grep_defaults_to_home_when_path_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home_dir = tmp_path / "home"
        cwd_dir = tmp_path / "cwd"
        home_dir.mkdir()
        cwd_dir.mkdir()
        (home_dir / "home.py").write_text("needle\n")
        (cwd_dir / "cwd.py").write_text("needle\n")
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.chdir(cwd_dir)

        result = await handle_grep({"pattern": "needle"}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert "home.py" in result.output
        assert "cwd.py" not in result.output


class TestBashTool:
    """Test the bash shell tool."""

    def test_resolve_shell_prefers_bash_over_sh_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.shell import _resolve_shell_path

        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("SHELL", "/bin/sh")
        monkeypatch.delenv("COGNIS_EXECUTOR_SHELL", raising=False)
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/bash" if name == "bash" else None
        )

        assert _resolve_shell_path() == "/usr/bin/bash"

    def test_resolve_shell_honors_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.shell import _resolve_shell_path

        monkeypatch.setenv("COGNIS_EXECUTOR_SHELL", "/custom/shell")
        assert _resolve_shell_path() == "/custom/shell"

    def test_resolve_shell_keeps_user_zsh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.shell import _resolve_shell_path

        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("SHELL", "/bin/zsh")
        monkeypatch.delenv("COGNIS_EXECUTOR_SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/bash")

        assert _resolve_shell_path() == "/bin/zsh"

    def test_resolve_shell_keeps_non_sh_user_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.shell import _resolve_shell_path

        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        monkeypatch.delenv("COGNIS_EXECUTOR_SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/bash")

        assert _resolve_shell_path() == "/usr/bin/fish"

    def test_resolve_shell_uses_darwin_zsh_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.shell import _resolve_shell_path

        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.delenv("COGNIS_EXECUTOR_SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: None)

        assert _resolve_shell_path() == "/bin/zsh"

    def test_shell_command_args_use_windows_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.shell import _shell_command_args

        monkeypatch.setattr("sys.platform", "win32")
        assert _shell_command_args("cmd.exe", "echo hello") == ["cmd.exe", "/c", "echo hello"]

    @pytest.mark.asyncio()
    async def test_bash_echo(self) -> None:
        result = await handle_bash({"command": "echo hello"}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "hello" in result.output

    @pytest.mark.asyncio()
    async def test_bash_exit_code(self) -> None:
        result = await handle_bash({"command": "exit 1"}, _DUMMY_CONTEXT)
        assert result.is_error
        assert "Exit code: 1" in result.output

    @pytest.mark.asyncio()
    async def test_bash_workdir(self, tmp_path: Path) -> None:
        result = await handle_bash({"command": "pwd", "workdir": str(tmp_path)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert str(tmp_path) in result.output

    @pytest.mark.asyncio()
    async def test_bash_empty_command(self) -> None:
        result = await handle_bash({"command": ""}, _DUMMY_CONTEXT)
        assert result.is_error

    @pytest.mark.asyncio()
    async def test_bash_invalid_override_reports_missing_shell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_EXECUTOR_SHELL", "/missing/shell")

        result = await handle_bash({"command": "echo hello"}, _DUMMY_CONTEXT)

        assert result.is_error
        assert "Shell executable not found" in result.output

    @pytest.mark.asyncio()
    async def test_bash_invalid_workdir_reports_missing_directory(self) -> None:
        result = await handle_bash(
            {"command": "echo hello", "workdir": "/missing/workdir"}, _DUMMY_CONTEXT
        )

        assert result.is_error
        assert "Working directory not found" in result.output

    @pytest.mark.asyncio()
    async def test_bash_defaults_to_home_when_workdir_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home_dir = tmp_path / "home"
        cwd_dir = tmp_path / "cwd"
        home_dir.mkdir()
        cwd_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.chdir(cwd_dir)

        result = await handle_bash({"command": "pwd"}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert str(home_dir) in result.output


class TestListDirectoryTool:
    """Test the list_directory tool."""

    @pytest.mark.asyncio()
    async def test_list_directory(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").touch()
        (tmp_path / "subdir").mkdir()
        result = await handle_list_directory({"path": str(tmp_path)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "file.txt" in result.output
        assert "subdir/" in result.output

    @pytest.mark.asyncio()
    async def test_list_directory_ignores_defaults(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").touch()
        (tmp_path / "node_modules").mkdir()
        result = await handle_list_directory({"path": str(tmp_path)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "file.txt" in result.output
        assert "node_modules" not in result.output

    @pytest.mark.asyncio()
    async def test_list_directory_defaults_to_home_when_path_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home_dir = tmp_path / "home"
        cwd_dir = tmp_path / "cwd"
        home_dir.mkdir()
        cwd_dir.mkdir()
        (home_dir / "file.txt").touch()
        (cwd_dir / "other.txt").touch()
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.chdir(cwd_dir)

        result = await handle_list_directory({}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert "file.txt" in result.output
        assert "other.txt" not in result.output


class TestResolvePath:
    """Test the shared resolve_path utility."""

    def test_tilde_expansion(self) -> None:
        from cognis.tools.executor.paths import resolve_path

        result = resolve_path("~/some/dir")
        assert "~" not in str(result)
        assert str(result).startswith("/")

    def test_absolute_path_unchanged(self) -> None:
        from cognis.tools.executor.paths import resolve_path

        result = resolve_path("/tmp/test")
        assert str(result) == "/tmp/test"

    def test_env_var_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.paths import resolve_path

        monkeypatch.setenv("MY_TEST_DIR", "/opt/data")
        result = resolve_path("$MY_TEST_DIR/file.txt")
        assert str(result) == "/opt/data/file.txt"

    def test_tilde_and_env_var_combined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.paths import resolve_path

        monkeypatch.setenv("SUBDIR", "projects")
        result = resolve_path("~/$SUBDIR/repo")
        assert "~" not in str(result)
        assert "projects/repo" in str(result)

    def test_relative_path_preserved(self) -> None:
        from cognis.tools.executor.paths import resolve_path

        result = resolve_path("relative/path")
        assert str(result) == "relative/path"

    def test_omitted_path_defaults_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor.paths import resolve_path

        monkeypatch.setenv("HOME", "/tmp/home-default")
        result = resolve_path(None, default_to_home=True)
        assert str(result) == "/tmp/home-default"


class TestRegistryIntegration:
    """Test that executor tools integrate with the registry."""

    def test_source_priority_includes_executor(self) -> None:
        from cognis.tools.registry import SOURCE_PRIORITIES

        assert "executor" in SOURCE_PRIORITIES
        assert SOURCE_PRIORITIES["executor"] == 400
        assert SOURCE_PRIORITIES["builtin"] > SOURCE_PRIORITIES["executor"]

    def test_static_tool_definitions_includes_executor(self) -> None:
        from cognis.api.runtime_support import static_tool_definitions

        defs = static_tool_definitions()
        names = {d.name for d in defs}
        assert "read" in names
        assert "write" in names
        assert "bash" in names
        assert "glob" in names
        assert "grep" in names
