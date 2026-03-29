"""Unit tests for executor-native tools."""

from __future__ import annotations

from pathlib import Path

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


class TestDefinitions:
    """Test tool definition registry."""

    def test_executor_tool_definitions_returns_all(self) -> None:
        defs = executor_tool_definitions()
        assert len(defs) == 10
        names = {d.name for d in defs}
        assert names == {
            "read",
            "write",
            "edit",
            "patch",
            "multiedit",
            "list_directory",
            "glob",
            "grep",
            "bash",
            "web_fetch",
        }

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
            if tool.name in {"read", "glob", "grep", "list_directory", "web_fetch"}:
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
        result = await handle_write({"file_path": str(target), "content": "hello"}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert target.read_text() == "hello"

    @pytest.mark.asyncio()
    async def test_write_creates_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c.txt"
        result = await handle_write({"file_path": str(target), "content": "deep"}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert target.read_text() == "deep"


class TestEditTool:
    """Test the edit filesystem tool."""

    @pytest.fixture()
    def tmp_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "edit.txt"
        f.write_text("hello world\nfoo bar\nhello world\n")
        return f

    @pytest.mark.asyncio()
    async def test_edit_single_match(self, tmp_file: Path) -> None:
        result = await handle_edit(
            {"file_path": str(tmp_file), "old_string": "foo bar", "new_string": "baz qux"},
            _DUMMY_CONTEXT,
        )
        assert not result.is_error
        assert "baz qux" in tmp_file.read_text()

    @pytest.mark.asyncio()
    async def test_edit_multiple_matches_fails(self, tmp_file: Path) -> None:
        result = await handle_edit(
            {"file_path": str(tmp_file), "old_string": "hello world", "new_string": "hi"},
            _DUMMY_CONTEXT,
        )
        assert result.is_error
        assert "2 matches" in result.output

    @pytest.mark.asyncio()
    async def test_edit_replace_all(self, tmp_file: Path) -> None:
        result = await handle_edit(
            {
                "file_path": str(tmp_file),
                "old_string": "hello world",
                "new_string": "hi",
                "replace_all": True,
            },
            _DUMMY_CONTEXT,
        )
        assert not result.is_error
        assert tmp_file.read_text().count("hi") == 2

    @pytest.mark.asyncio()
    async def test_edit_not_found(self, tmp_file: Path) -> None:
        result = await handle_edit(
            {"file_path": str(tmp_file), "old_string": "nonexistent", "new_string": "x"},
            _DUMMY_CONTEXT,
        )
        assert result.is_error


class TestMultieditTool:
    """Test the multiedit filesystem tool."""

    @pytest.mark.asyncio()
    async def test_multiedit(self, tmp_path: Path) -> None:
        f = tmp_path / "multi.txt"
        f.write_text("aaa\nbbb\nccc\n")
        result = await handle_multiedit(
            {
                "file_path": str(f),
                "edits": [
                    {"old_string": "aaa", "new_string": "AAA"},
                    {"old_string": "ccc", "new_string": "CCC"},
                ],
            },
            _DUMMY_CONTEXT,
        )
        assert not result.is_error
        content = f.read_text()
        assert "AAA" in content
        assert "CCC" in content


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


class TestBashTool:
    """Test the bash shell tool."""

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
