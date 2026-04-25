"""Unit tests for executor-native tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cognis.models.tool import ExecutorHandle
from cognis.tools.executor import filesystem as filesystem_module
from cognis.tools.executor.definitions import (
    ALL_EXECUTOR_TOOLS,
    executor_tool_definitions,
    executor_tool_handlers,
)
from cognis.tools.executor.filesystem import (
    handle_apply_patch,
    handle_artifact_save,
    handle_edit,
    handle_list_directory,
    handle_multiedit,
    handle_read,
    handle_write,
)
from cognis.tools.executor.lsp.tool import handle_lsp
from cognis.tools.executor.search import handle_glob, handle_grep
from cognis.tools.executor.shell import handle_bash, handle_bash_kill, handle_bash_output
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
        assert "patch" not in names
        assert {
            "read",
            "write",
            "artifact_save",
            "edit",
            "apply_patch",
            "multiedit",
            "list_directory",
            "lsp",
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
        assert "patch" not in handlers
        for d in defs:
            assert d.name in handlers, f"Missing handler for {d.name}"

    def test_write_tools_are_non_bypassable(self) -> None:
        write_tools = {"write", "artifact_save", "edit", "apply_patch", "multiedit", "bash"}
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

    @pytest.mark.asyncio()
    async def test_read_relative_path_uses_working_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested.txt"
        target.write_text("hello\n")
        context = _context(
            runtime_metadata={"workspace_root": str(tmp_path), "working_directory": str(tmp_path)}
        )

        result = await handle_read({"file_path": "nested.txt"}, context)

        assert not result.is_error
        assert "1: hello" in result.output

    @pytest.mark.asyncio()
    async def test_read_allows_explicit_paths_outside_workspace_root(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret\n")
        context = _context(
            runtime_metadata={"workspace_root": str(tmp_path), "working_directory": str(tmp_path)}
        )

        result = await handle_read({"file_path": str(outside)}, context)

        assert not result.is_error
        assert "1: secret" in result.output

    @pytest.mark.asyncio()
    async def test_read_binary_file_returns_attachment_analysis_request(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "photo.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")

        result = await handle_read({"file_path": str(target)}, _context())

        assert not result.is_error
        assert result.attachments is not None
        assert result.attachments[0]["filename"] == "photo.png"
        assert result.metadata is not None
        assert "attachment_analysis_request" in result.metadata

    @pytest.mark.asyncio()
    async def test_read_svg_file_returns_text(self, tmp_path: Path) -> None:
        target = tmp_path / "icon.svg"
        target.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">\n  <text x="0" y="12">hello</text>\n</svg>\n',
            encoding="utf-8",
        )

        result = await handle_read(
            {"file_path": str(target), "offset": 1, "limit": 2},
            _context(),
        )

        assert result.is_error is False
        assert result.attachments is None
        assert result.metadata is not None
        assert "attachment_analysis_request" not in result.metadata
        assert '1: <svg xmlns="http://www.w3.org/2000/svg">' in result.output
        assert '2:   <text x="0" y="12">hello</text>' in result.output


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
    async def test_write_existing_file_preserves_crlf(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.txt"
        target.write_bytes(b"old\r\nvalue\r\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_write({"file_path": str(target), "content": "new\nvalue\n"}, context)

        assert not result.is_error
        assert target.read_bytes() == b"new\r\nvalue\r\n"
        assert result.metadata is not None
        diff = result.metadata["file_diffs"][0]["diff"]
        assert "-old\r\n" in diff
        assert "+new\r\n" in diff

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

    @pytest.mark.asyncio()
    async def test_write_formats_python_file_when_formatter_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "format_me.py"
        context = _context()

        async def fake_exec(*command: str, **_: object):
            class _Proc:
                async def communicate(self) -> tuple[bytes, bytes]:
                    target.write_text("x = 1\n")
                    return b"", b""

            assert command == ("ruff", "format", str(target))
            return _Proc()

        monkeypatch.setattr(
            filesystem_module, "_formatter_command", lambda _path: ["ruff", "format", str(target)]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        result = await handle_write({"file_path": str(target), "content": "x=1\n"}, context)

        assert not result.is_error
        assert target.read_text() == "x = 1\n"
        assert result.metadata is not None
        assert result.metadata["file_diffs"][0]["diff"].endswith("+x = 1\n")

        follow_up = await handle_edit(
            {"file_path": str(target), "old_string": "x = 1", "new_string": "x = 2"},
            context,
        )

        assert follow_up.is_error
        assert "Use the read tool first" in follow_up.output

    @pytest.mark.asyncio()
    async def test_artifact_save_writes_binary_artifact_content(self, tmp_path: Path) -> None:
        target = tmp_path / "saved.png"

        result = await handle_artifact_save(
            {
                "file_path": str(target),
                "source_artifact_id": "att-1",
                "source_artifact_content_b64": "cG5nLWJ5dGVz",
                "source_artifact_filename": "photo.png",
                "source_artifact_mime_type": "image/png",
            },
            _context(),
        )

        assert not result.is_error
        assert target.read_bytes() == b"png-bytes"

    @pytest.mark.asyncio()
    async def test_artifact_save_requires_controller_resolution(self, tmp_path: Path) -> None:
        target = tmp_path / "saved.png"

        result = await handle_artifact_save(
            {"file_path": str(target), "source_artifact_id": "att-1"},
            _context(),
        )

        assert result.is_error
        assert "Provide source_artifact_id" in result.output


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
    async def test_edit_diff_metadata_preserves_crlf(self, tmp_path: Path) -> None:
        target = tmp_path / "crlf.txt"
        target.write_bytes(b"alpha\r\nbeta\r\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {"file_path": str(target), "old_string": "beta", "new_string": "gamma"},
            context,
        )

        assert not result.is_error
        assert target.read_bytes() == b"alpha\r\ngamma\r\n"
        assert result.metadata is not None
        diff = result.metadata["file_diffs"][0]["diff"]
        assert " alpha\r\n" in diff
        assert "+gamma\r\n" in diff
        assert "+alpha\n" not in diff

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
    async def test_multiedit_preserves_crlf(self, tmp_path: Path) -> None:
        f = tmp_path / "multi-crlf.txt"
        f.write_bytes(b"aaa\r\nbbb\r\nccc\r\n")
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
        assert f.read_bytes() == b"AAA\r\nbbb\r\nCCC\r\n"

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


class TestApplyPatchTool:
    """Test the apply_patch filesystem tool."""

    @pytest.mark.asyncio()
    async def test_apply_patch_apply_patch_update_success(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"
        assert result.metadata is not None
        assert result.metadata["file_diffs"][0]["diff"]

    @pytest.mark.asyncio()
    async def test_apply_patch_accepts_patch_text(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_rejects_legacy_patch_text_key(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patch_text": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
                )
            },
            context,
        )

        assert result.is_error
        assert "Empty apply_patch payload" in result.output

    @pytest.mark.asyncio()
    async def test_apply_patch_add_file_success(self, tmp_path: Path) -> None:
        target = tmp_path / "new.txt"

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Add File: {target}\n+hello\n+world\n*** End Patch\n"
                )
            },
            _context(),
        )

        assert not result.is_error
        assert target.read_text() == "hello\nworld\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_add_empty_file_success(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.txt"

        result = await handle_apply_patch(
            {"patchText": f"*** Begin Patch\n*** Add File: {target}\n*** End Patch\n"},
            _context(),
        )

        assert not result.is_error
        assert target.read_text() == ""

    @pytest.mark.asyncio()
    async def test_apply_patch_add_file_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "new.txt"

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Add File: {target}\n+hello\n*** End Patch\n"
                )
            },
            _context(),
        )

        assert not result.is_error
        assert target.read_text() == "hello\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_add_file_fails_if_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")

        result = await handle_apply_patch(
            {"patchText": f"*** Begin Patch\n*** Add File: {target}\n+hi\n*** End Patch\n"},
            _context(),
        )

        assert result.is_error
        assert "already exists" in result.output

    @pytest.mark.asyncio()
    async def test_apply_patch_formats_updated_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "format_patch.py"
        target.write_text("x=1\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        calls: list[Path] = []

        async def fake_format(path: Path) -> None:
            calls.append(path)

        monkeypatch.setattr(filesystem_module, "_maybe_format_file", fake_format)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-x=1\n+x=2\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert calls == [target]

    @pytest.mark.asyncio()
    async def test_apply_patch_delete_success(self, tmp_path: Path) -> None:
        target = tmp_path / "delete.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {"patchText": f"*** Begin Patch\n*** Delete File: {target}\n*** End Patch\n"},
            context,
        )

        assert not result.is_error
        assert not target.exists()

    @pytest.mark.asyncio()
    async def test_apply_patch_native_update_file_success(self, tmp_path: Path) -> None:
        target = tmp_path / "native.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "operation": {
                    "type": "update_file",
                    "path": str(target),
                    "diff": "@@\n-hello\n+hi\n",
                }
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_native_create_file_success(self, tmp_path: Path) -> None:
        target = tmp_path / "native-new.txt"

        result = await handle_apply_patch(
            {
                "operation": {
                    "type": "create_file",
                    "path": str(target),
                    "diff": "@@\n+hello\n+world\n",
                }
            },
            _context(),
        )

        assert not result.is_error
        assert target.read_text() == "hello\nworld\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_native_delete_file_success(self, tmp_path: Path) -> None:
        target = tmp_path / "native-delete.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {"operation": {"type": "delete_file", "path": str(target)}},
            context,
        )

        assert not result.is_error
        assert not target.exists()

    @pytest.mark.asyncio()
    async def test_apply_patch_move_rename_only_success(self, tmp_path: Path) -> None:
        source = tmp_path / "old.txt"
        dest = tmp_path / "new.txt"
        source.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(source)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {source}\n*** Move to: {dest}\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert not source.exists()
        assert dest.read_text() == "hello\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_move_with_content_edit_success(self, tmp_path: Path) -> None:
        source = tmp_path / "old.txt"
        dest = tmp_path / "new.txt"
        source.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(source)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {source}\n*** Move to: {dest}\n@@\n-hello\n+hi\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert not source.exists()
        assert dest.read_text() == "hi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_move_creates_parent_directories(self, tmp_path: Path) -> None:
        source = tmp_path / "old.txt"
        dest = tmp_path / "nested" / "new.txt"
        source.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(source)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {source}\n*** Move to: {dest}\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert not source.exists()
        assert dest.read_text() == "hello\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_move_fails_if_destination_exists(self, tmp_path: Path) -> None:
        source = tmp_path / "old.txt"
        dest = tmp_path / "new.txt"
        source.write_text("hello\n")
        dest.write_text("existing\n")
        context = _context()
        await handle_read({"file_path": str(source)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {source}\n*** Move to: {dest}\n*** End Patch\n"
                )
            },
            context,
        )

        assert result.is_error
        assert "destination already exists" in result.output

    @pytest.mark.asyncio()
    async def test_apply_patch_requires_prior_read(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")

        result = await handle_apply_patch(
            {
                "patchText": f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
            },
            _context(),
        )

        assert result.is_error
        assert "Use the read tool first" in result.output

    @pytest.mark.asyncio()
    async def test_apply_patch_ambiguous_hunk_fails(self, tmp_path: Path) -> None:
        target = tmp_path / "ambiguous.txt"
        target.write_text("hello\nhello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
            },
            context,
        )

        assert result.is_error
        assert "multiple locations" in result.output

    @pytest.mark.asyncio()
    async def test_apply_patch_no_write_on_prevalidation_failure(self, tmp_path: Path) -> None:
        target = tmp_path / "new.txt"

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Add File: {target}\n+hello\n*** Delete File: {tmp_path / 'missing.txt'}\n*** End Patch\n"
                )
            },
            _context(),
        )

        assert result.is_error
        assert not target.exists()

    @pytest.mark.asyncio()
    async def test_apply_patch_detects_phase_b_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "race.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        original = filesystem_module._stage_patch_operations
        calls = 0

        async def _wrapped_stage(operations: object, ctx: object):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_text("changed\n")
            return await original(operations, ctx)

        monkeypatch.setattr(filesystem_module, "_stage_patch_operations", _wrapped_stage)

        result = await handle_apply_patch(
            {
                "patchText": f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
            },
            context,
        )

        assert result.is_error
        assert "modified since it was last read" in result.output
        assert target.read_text() == "changed\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_unified_diff_regression_success(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {"patchText": f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-hello\n+hi\n"},
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_unified_diff_uses_hunk_location(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\nkeep\nhello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {"patchText": (f"--- a/{target}\n+++ b/{target}\n@@ -3 +3 @@\n-hello\n+hi\n")},
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hello\nkeep\nhi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_reports_read_failure_during_prevalidation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        def _broken_read(_: Path) -> str:
            raise OSError("denied")

        monkeypatch.setattr(filesystem_module, "_read_text_file", _broken_read)

        result = await handle_apply_patch(
            {
                "patchText": f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
            },
            context,
        )

        assert result.is_error
        assert "denied" in result.output

    @pytest.mark.asyncio()
    async def test_apply_patch_accepts_end_of_file_marker(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End of File\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_accepts_no_newline_marker(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n\\ No newline at end of file\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi"

    @pytest.mark.asyncio()
    async def test_apply_patch_no_newline_marker_can_add_final_newline(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n\\ No newline at end of file\n+hello\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hello\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_unified_diff_accepts_no_newline_marker(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-hello\n\\ No newline at end of file\n+hi\n\\ No newline at end of file\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi"

    @pytest.mark.asyncio()
    async def test_apply_patch_rejects_unified_diff_rename_headers(self, tmp_path: Path) -> None:
        source = tmp_path / "old.txt"
        target = tmp_path / "new.txt"
        source.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(source)}, context)

        result = await handle_apply_patch(
            {"patchText": (f"--- a/{source}\n+++ b/{target}\n@@ -1 +1 @@\n-hello\n+hi\n")},
            context,
        )

        assert result.is_error
        assert "rename/add/delete operations are not supported" in result.output


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

    @pytest.mark.asyncio()
    async def test_glob_fd_searches_only_requested_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import search as search_module

        captured: list[str] = []

        class _Process:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"", b"")

        async def _fake_exec(*args: str, **_: object) -> _Process:
            captured.extend(args)
            return _Process()

        monkeypatch.setattr(search_module, "_FD_PATH", "/usr/bin/fdfind")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        await handle_glob({"pattern": "*.py", "path": str(tmp_path)}, _DUMMY_CONTEXT)

        assert str(tmp_path) in captured
        assert "." not in captured


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

    @pytest.mark.asyncio()
    async def test_grep_accepts_single_file_path(self, tmp_path: Path) -> None:
        target = tmp_path / "a.py"
        other = tmp_path / "b.py"
        target.write_text("def hello():\n    pass\n")
        other.write_text("def hello():\n    pass\n")

        result = await handle_grep({"pattern": "hello", "path": str(target)}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert "a.py" in result.output
        assert "b.py" not in result.output

    @pytest.mark.asyncio()
    async def test_grep_ignores_include_for_single_file_path(self, tmp_path: Path) -> None:
        target = tmp_path / "+layout.svelte"
        other = tmp_path / "other.ts"
        target.write_text('<div class="bg-slate-900"></div>\n')
        other.write_text("const color = 'bg-slate-900'\n")

        result = await handle_grep(
            {
                "pattern": "bg-slate",
                "path": str(target),
                "include": "*.ts",
            },
            _DUMMY_CONTEXT,
        )

        assert not result.is_error
        assert "+layout.svelte" in result.output
        assert "other.ts" not in result.output

    @pytest.mark.asyncio()
    async def test_grep_rg_uses_end_of_options_separator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import search as search_module

        captured: list[str] = []

        class _Process:
            returncode = 1

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"", b"")

        async def _fake_exec(*args: str, **_: object) -> _Process:
            captured.extend(args)
            return _Process()

        pattern = "--|theme|color|font|tailwind|tokens|:root|background|surface|accent"

        monkeypatch.setattr(search_module, "_RG_PATH", "/usr/bin/rg")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        result = await handle_grep({"pattern": pattern, "path": str(tmp_path)}, _DUMMY_CONTEXT)

        assert not result.is_error
        separator_index = captured.index("--")
        assert captured[separator_index + 1] == pattern
        assert captured[separator_index + 2] == str(tmp_path)


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
    async def test_bash_rejects_python_file_rewrite_one_liner(self) -> None:
        result = await handle_bash(
            {"command": "python -c \"from pathlib import Path; Path('x.py').write_text('x=1')\""},
            _DUMMY_CONTEXT,
        )

        assert result.is_error
        assert "Use edit, multiedit, apply_patch, or write" in result.output

    @pytest.mark.asyncio()
    async def test_bash_allows_python_read_only_one_liner(self) -> None:
        result = await handle_bash(
            {"command": "python -c \"print(open('/dev/null').read())\""},
            _DUMMY_CONTEXT,
        )

        assert not result.is_error

    @pytest.mark.asyncio()
    async def test_bash_rejects_shell_redirection_to_source_file(self) -> None:
        result = await handle_bash({"command": "printf 'x' > test.py"}, _DUMMY_CONTEXT)

        assert result.is_error
        assert "shell redirection" in result.output

    @pytest.mark.asyncio()
    async def test_bash_defaults_to_runtime_working_directory(self, tmp_path: Path) -> None:
        context = _context(
            runtime_metadata={"workspace_root": str(tmp_path), "working_directory": str(tmp_path)}
        )

        result = await handle_bash({"command": "pwd"}, context)

        assert not result.is_error
        assert str(tmp_path) in result.output

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
    async def test_bash_shell_parse_errors_include_quoting_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import shell as shell_module

        class _Process:
            returncode = 2

            async def communicate(self) -> tuple[bytes, bytes]:
                return (
                    b"",
                    b"/bin/bash: -c: line 1: syntax error near unexpected token `('\n",
                )

        async def _fake_create_process(**_: object) -> _Process:
            return _Process()

        monkeypatch.setattr(shell_module, "_create_process", _fake_create_process)

        result = await handle_bash(
            {"command": "git diff -- ui/src/routes/(app)/+layout.svelte", "workdir": str(tmp_path)},
            _DUMMY_CONTEXT,
        )

        assert result.is_error
        assert "parsed by the shell" in result.output
        assert "Quote literal paths" in result.output

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

    @pytest.mark.asyncio()
    async def test_background_bash_uses_shared_runtime_metadata(self) -> None:
        shared_runtime_metadata: dict[str, Any] = {}
        start_ctx = ToolExecutionContext(
            executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
            runtime_metadata={},
            shared_runtime_metadata=shared_runtime_metadata,
        )
        start = await handle_bash(
            {
                "command": "python -u -c \"import time; print('hello'); time.sleep(5)\"",
                "run_in_background": True,
            },
            start_ctx,
        )

        assert not start.is_error
        shell_id = str((start.metadata or {}).get("shell_id"))
        assert shell_id.startswith("shell_")

        await asyncio.sleep(0.2)
        read_ctx = ToolExecutionContext(
            executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
            runtime_metadata={},
            shared_runtime_metadata=shared_runtime_metadata,
        )
        output = await handle_bash_output({"shell_id": shell_id}, read_ctx)
        assert not output.is_error
        assert "hello" in output.output or "no new output" in output.output.lower()

        stopped = await handle_bash_kill({"shell_id": shell_id}, read_ctx)
        assert not stopped.is_error
        assert shell_id in stopped.output


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


class TestLspTool:
    @pytest.mark.asyncio()
    async def test_lsp_definition_returns_results(self, tmp_path: Path) -> None:
        target = tmp_path / "sample.py"
        target.write_text("value = 1\n")

        class _FakeLsp:
            async def touch_file(self, *_: object, **__: object) -> None:
                return None

            async def has_clients(self, *_: object, **__: object) -> bool:
                return True

            async def definition(self, *_: object, **__: object) -> list[dict[str, object]]:
                return [{"uri": "file:///tmp/sample.py", "range": {}}]

        context = _context(runtime_metadata={"lsp_manager": _FakeLsp()})
        result = await handle_lsp(
            {
                "operation": "goToDefinition",
                "file_path": str(target),
                "line": 1,
                "character": 1,
            },
            context,
        )

        assert not result.is_error
        assert '"uri": "file:///tmp/sample.py"' in result.output

    @pytest.mark.asyncio()
    async def test_lsp_requires_available_server(self, tmp_path: Path) -> None:
        target = tmp_path / "sample.py"
        target.write_text("value = 1\n")

        class _FakeLsp:
            async def touch_file(self, *_: object, **__: object) -> None:
                return None

            async def has_clients(self, *_: object, **__: object) -> bool:
                return False

        context = _context(runtime_metadata={"lsp_manager": _FakeLsp()})
        result = await handle_lsp(
            {
                "operation": "goToDefinition",
                "file_path": str(target),
                "line": 1,
                "character": 1,
            },
            context,
        )

        assert result.is_error
        assert "No LSP server available" in result.output

    @pytest.mark.asyncio()
    async def test_lsp_requires_position_for_definition(self, tmp_path: Path) -> None:
        target = tmp_path / "sample.py"
        target.write_text("value = 1\n")

        result = await handle_lsp(
            {"operation": "goToDefinition", "file_path": str(target)},
            _context(runtime_metadata={"lsp_manager": object()}),
        )

        assert result.is_error
        assert "requires both line and character" in result.output

    @pytest.mark.asyncio()
    async def test_lsp_workspace_symbol_requires_query(self, tmp_path: Path) -> None:
        target = tmp_path / "sample.py"
        target.write_text("value = 1\n")

        result = await handle_lsp(
            {"operation": "workspaceSymbol", "file_path": str(target), "query": ""},
            _context(runtime_metadata={"lsp_manager": object()}),
        )

        assert result.is_error
        assert "requires a non-empty query" in result.output


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
