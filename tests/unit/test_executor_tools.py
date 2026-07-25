"""Unit tests for executor-native tools."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from cognis.models.tool import ExecutorHandle
from cognis.tools.executor import filesystem as filesystem_module
from cognis.tools.executor.definitions import (
    ALL_EXECUTOR_TOOLS,
    BASH_TOOL,
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
    handle_skill_asset_materialize,
    handle_write,
)
from cognis.tools.executor.lsp.tool import handle_lsp
from cognis.tools.executor.lsp.types import (
    DiagnosticCollection,
    DiagnosticFreshness,
    DiagnosticWaitResult,
)
from cognis.tools.executor.search import handle_glob, handle_grep
from cognis.tools.executor.shell import (
    handle_bash,
    handle_bash_kill,
    handle_bash_output,
    list_background_shell_statuses,
)
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
            "skill_asset_materialize",
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
        write_tools = {
            "write",
            "artifact_save",
            "skill_asset_materialize",
            "edit",
            "apply_patch",
            "multiedit",
            "bash",
        }
        for tool in ALL_EXECUTOR_TOOLS:
            if tool.name in write_tools:
                assert tool.non_bypassable, f"{tool.name} should be non_bypassable"
            if tool.name in {"read", "glob", "grep", "list_directory"}:
                assert tool.read_only, f"{tool.name} should be read_only"

    def test_apply_patch_schema_is_openai_responses_function_compatible(self) -> None:
        tool = next(tool for tool in ALL_EXECUTOR_TOOLS if tool.name == "apply_patch")
        forbidden_top_level = {"oneOf", "anyOf", "allOf", "enum", "not"}

        assert tool.parameters.get("type") == "object"
        assert not (forbidden_top_level & set(tool.parameters))
        assert "patchText" in tool.parameters.get("properties", {})
        assert "operation" not in tool.parameters.get("properties", {})
        assert tool.parameters.get("required") == ["patchText"]


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
        (tmp_path / ".git").mkdir()
        (tmp_path / "__pycache__").mkdir()
        result = await handle_read({"file_path": str(tmp_path)}, _DUMMY_CONTEXT)
        assert not result.is_error
        assert "subdir/" in result.output
        assert "a.txt" in result.output
        assert ".git/" not in result.output
        assert "__pycache__/" not in result.output

    @pytest.mark.asyncio()
    async def test_read_ipynb_renders_cells_and_summarizes_outputs(self, tmp_path: Path) -> None:
        notebook = tmp_path / "notebook.ipynb"
        notebook.write_text(
            json.dumps(
                {
                    "cells": [
                        {"cell_type": "markdown", "source": ["# Title\n"]},
                        {
                            "cell_type": "code",
                            "source": ["print('hi')\n"],
                            "outputs": [
                                {
                                    "output_type": "stream",
                                    "name": "stdout",
                                    "text": ["hi\n"],
                                }
                            ],
                        },
                    ]
                }
            )
        )

        result = await handle_read({"file_path": str(notebook)}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert "Notebook: notebook.ipynb" in result.output
        assert "## Cell 1 [markdown]" in result.output
        assert "# Title" in result.output
        assert "Output 1: stream stdout" in result.output

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
    async def test_write_formatter_records_fresh_stamp_and_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "format_me.py"
        context = _context()

        async def fake_exec(*command: str, **_: object):
            class _Proc:
                returncode = 0

                async def communicate(self) -> tuple[bytes, bytes]:
                    target.write_text(target.read_text().replace("x=", "x = "))
                    return b"", b""

            assert command == ("ruff", "format", str(target))
            return _Proc()

        monkeypatch.setattr(
            filesystem_module, "_formatter_command", lambda _path: ["ruff", "format", str(target)]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        result = await handle_write({"file_path": str(target), "content": "x=1\n"}, context)

        assert not result.is_error
        assert "Formatter diff" in result.output
        assert target.read_text() == "x = 1\n"
        assert result.metadata is not None
        assert result.metadata["file_diffs"][0]["diff"].endswith("+x = 1\n")

        follow_up = await handle_edit(
            {"file_path": str(target), "old_string": "x = 1", "new_string": "x = 2"},
            context,
        )

        assert not follow_up.is_error
        assert target.read_text() == "x = 2\n"

    @pytest.mark.asyncio()
    async def test_write_does_not_run_global_ruff_without_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "plain.py"
        context = _context()

        monkeypatch.setattr(filesystem_module.shutil, "which", lambda _name: "/usr/bin/ruff")

        async def fail_exec(*_args: object, **_kwargs: object):
            raise AssertionError("formatter should not run without a ruff config")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_exec)

        result = await handle_write({"file_path": str(target), "content": "x=1\n"}, context)

        assert not result.is_error
        assert target.read_text() == "x=1\n"

    @pytest.mark.asyncio()
    async def test_edit_reports_closest_match_and_line_number_hint(self, tmp_path: Path) -> None:
        target = tmp_path / "sample.txt"
        target.write_text("alpha\n  beta\nomega\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "2:   beta",
                "new_string": "gamma",
            },
            context,
        )

        assert result.is_error
        assert "old_string not found" in result.output
        assert "oldString" not in result.output
        assert "Closest match starts at line" in result.output
        assert "line-number prefixes" in result.output

    @pytest.mark.asyncio()
    async def test_edit_uses_unambiguous_rstrip_normalized_fallback(self, tmp_path: Path) -> None:
        target = tmp_path / "sample.txt"
        target.write_text("value   \nother\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "value\n",
                "new_string": "done\n",
            },
            context,
        )

        assert not result.is_error
        assert "rstrip-normalized fallback" in result.output
        assert target.read_text() == "done\nother\n"

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

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_writes_attached_asset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
        target = tmp_path / "data" / "skill_assets" / "custom" / "youtube_transcript.py"
        context = _context(
            runtime_metadata={
                "skill_manifests": [
                    {
                        "skill_id": "youtube-transcript",
                        "asset_manifest": [
                            {
                                "filename": "assets/youtube_transcript.py",
                                "asset_id": "sa-script",
                                "content_b64": "cHJpbnQoJ2hpJykK",
                                "content_type": "text/x-python",
                            }
                        ],
                    }
                ]
            }
        )

        result = await handle_skill_asset_materialize(
            {
                "skill_id": "youtube-transcript",
                "asset_id": "sa-script",
                "target_path": str(target),
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "print('hi')\n"
        assert result.metadata is not None
        assert result.metadata["skill_asset"]["local_path"] == str(target)

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_overwrites_existing_without_prior_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
        target = tmp_path / "data" / "skill_assets" / "custom" / "youtube_transcript.py"
        target.parent.mkdir(parents=True)
        target.write_text("stale content\n")
        context = _context(
            runtime_metadata={
                "skill_manifests": [
                    {
                        "skill_id": "youtube-transcript",
                        "asset_manifest": [
                            {
                                "filename": "assets/youtube_transcript.py",
                                "asset_id": "sa-script",
                                "content_b64": "cHJpbnQoJ2hpJykK",
                                "content_type": "text/x-python",
                            }
                        ],
                    }
                ]
            }
        )

        result = await handle_skill_asset_materialize(
            {
                "skill_id": "youtube-transcript",
                "asset_id": "sa-script",
                "target_path": str(target),
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "print('hi')\n"
        assert "Use the read tool first" not in result.output

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_rejects_target_outside_managed_asset_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
        outside = tmp_path / "system" / "passwd"
        outside.parent.mkdir()
        outside.write_text("root:x:0:0\n")
        context = _context(
            runtime_metadata={
                "skill_manifests": [
                    {
                        "skill_id": "youtube-transcript",
                        "asset_manifest": [
                            {
                                "filename": "assets/youtube_transcript.py",
                                "asset_id": "sa-script",
                                "content_b64": "cHJpbnQoJ2hpJykK",
                                "content_type": "text/x-python",
                            }
                        ],
                    }
                ]
            }
        )

        result = await handle_skill_asset_materialize(
            {
                "skill_id": "youtube-transcript",
                "asset_id": "sa-script",
                "target_path": str(outside),
            },
            context,
        )

        assert result.is_error
        assert "managed skill asset directory" in result.output
        assert outside.read_text() == "root:x:0:0\n"

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_rejects_symlink_escape_from_managed_asset_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
        outside = tmp_path / "system" / "passwd"
        outside.parent.mkdir()
        outside.write_text("root:x:0:0\n")
        target = tmp_path / "data" / "skill_assets" / "custom" / "passwd"
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        context = _context(
            runtime_metadata={
                "skill_manifests": [
                    {
                        "skill_id": "youtube-transcript",
                        "asset_manifest": [
                            {
                                "filename": "assets/youtube_transcript.py",
                                "asset_id": "sa-script",
                                "content_b64": "cHJpbnQoJ2hpJykK",
                                "content_type": "text/x-python",
                            }
                        ],
                    }
                ]
            }
        )

        result = await handle_skill_asset_materialize(
            {
                "skill_id": "youtube-transcript",
                "asset_id": "sa-script",
                "target_path": str(target),
            },
            context,
        )

        assert result.is_error
        assert "managed skill asset directory" in result.output
        assert outside.read_text() == "root:x:0:0\n"

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_defaults_to_cognis_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
        context = _context(
            runtime_metadata={
                "skill_manifests": [
                    {
                        "skill_id": "youtube-transcript",
                        "asset_manifest": [
                            {
                                "filename": "assets/youtube_transcript.py",
                                "asset_id": "sa-script",
                                "content_b64": "cHJpbnQoJ2hpJykK",
                                "content_type": "text/x-python",
                            }
                        ],
                    }
                ]
            }
        )

        result = await handle_skill_asset_materialize(
            {"skill_id": "youtube-transcript", "asset_id": "sa-script"},
            context,
        )

        expected = (
            tmp_path
            / "data"
            / "skill_assets"
            / "youtube-transcript"
            / "sa-script"
            / "assets"
            / "youtube_transcript.py"
        )
        assert not result.is_error
        assert expected.read_text() == "print('hi')\n"
        assert result.metadata is not None
        assert result.metadata["skill_asset"]["local_path"] == str(expected)

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_rejects_directory_traversal_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
        target_dir = tmp_path / "data" / "skill_assets" / "assets"
        target_dir.mkdir(parents=True)
        context = _context(
            runtime_metadata={
                "skill_manifests": [
                    {
                        "skill_id": "bad-skill",
                        "asset_manifest": [
                            {
                                "filename": "../outside.py",
                                "asset_id": "sa-bad",
                                "content_b64": "cHJpbnQoJ2hpJykK",
                            }
                        ],
                    }
                ]
            }
        )

        result = await handle_skill_asset_materialize(
            {"skill_id": "bad-skill", "asset_id": "sa-bad", "target_path": str(target_dir)},
            context,
        )

        assert result.is_error
        assert "Unsafe skill asset filename" in result.output
        assert not (tmp_path / "outside.py").exists()

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_rejects_hash_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
        target = tmp_path / "data" / "skill_assets" / "tool.py"
        context = _context(
            runtime_metadata={
                "skill_manifests": [
                    {
                        "skill_id": "bad-hash",
                        "asset_manifest": [
                            {
                                "filename": "tool.py",
                                "asset_id": "sa-hash",
                                "content_b64": "cHJpbnQoJ2hpJykK",
                                "content_hash": "0" * 64,
                            }
                        ],
                    }
                ]
            }
        )

        result = await handle_skill_asset_materialize(
            {"skill_id": "bad-hash", "asset_id": "sa-hash", "target_path": str(target)},
            context,
        )

        assert result.is_error
        assert "Asset hash mismatch" in result.output
        assert not target.exists()

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_rejects_url_without_controller_origin(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "tool.py"
        context = _context(
            runtime_metadata={
                "skill_manifests": [
                    {
                        "skill_id": "url-skill",
                        "asset_manifest": [
                            {
                                "filename": "tool.py",
                                "asset_id": "sa-url",
                                "url": "https://controller.test/private/tool.py",
                            }
                        ],
                    }
                ]
            }
        )

        result = await handle_skill_asset_materialize(
            {"skill_id": "url-skill", "asset_id": "sa-url", "target_path": str(target)},
            context,
        )

        assert result.is_error
        assert "configured controller origin" in result.output
        assert not target.exists()

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_rejects_url_from_wrong_host(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "tool.py"
        context = _context(
            runtime_metadata={
                "controller_url": "wss://controller.test/api/executor/ws",
                "skill_manifests": [
                    {
                        "skill_id": "url-skill",
                        "asset_manifest": [
                            {
                                "filename": "tool.py",
                                "asset_id": "sa-url",
                                "url": "https://evil.test/private/tool.py",
                            }
                        ],
                    }
                ],
            }
        )

        result = await handle_skill_asset_materialize(
            {"skill_id": "url-skill", "asset_id": "sa-url", "target_path": str(target)},
            context,
        )

        assert result.is_error
        assert "host does not match" in result.output
        assert not target.exists()

    @pytest.mark.asyncio()
    async def test_skill_asset_materialize_reports_controller_http_status(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "tool.py"
        context = _context(
            runtime_metadata={
                "controller_url": "wss://controller.test/api/executor/ws",
                "skill_manifests": [
                    {
                        "skill_id": "url-skill",
                        "asset_manifest": [
                            {
                                "filename": "tool.py",
                                "asset_id": "sa-url",
                                "url": "https://controller.test/api/v1/artifacts/content/skills/ska/tool.py",
                            }
                        ],
                    }
                ],
            }
        )
        response = httpx.Response(
            404,
            content=b'{"error":{"message":"Artifact not found"}}',
            request=httpx.Request("GET", "https://controller.test/asset"),
        )
        client = AsyncMock()
        client.get.return_value = response

        with patch("httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value = client
            result = await handle_skill_asset_materialize(
                {"skill_id": "url-skill", "asset_id": "sa-url", "target_path": str(target)},
                context,
            )

        assert result.is_error
        assert "failed to fetch controller-provided asset URL (HTTP 404" in result.output
        assert "Artifact not found" in result.output
        assert not target.exists()


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

    @pytest.mark.asyncio()
    async def test_edit_uses_line_trimmed_fallback(self, tmp_path: Path) -> None:
        target = tmp_path / "trimmed.txt"
        target.write_text("alpha\n  beta  \nomega\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "alpha\nbeta\nomega\n",
                "new_string": "alpha\nBETA\nomega\n",
            },
            context,
        )

        assert not result.is_error
        assert "line-trimmed fallback" in result.output
        assert target.read_text() == "alpha\n  BETA\nomega\n"

    @pytest.mark.asyncio()
    async def test_edit_rejects_line_trimmed_fallback_with_replacement_line_count_mismatch(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "trimmed-mismatch.txt"
        target.write_text("alpha\n  beta  \nomega\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "alpha\nbeta\nomega\n",
                "new_string": "alpha\nBETA\nextra\nomega\n",
            },
            context,
        )

        assert result.is_error
        assert "same number of lines" in result.output
        assert target.read_text() == "alpha\n  beta  \nomega\n"

    @pytest.mark.asyncio()
    async def test_edit_uses_indentation_flexible_fallback_and_preserves_base_indent(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "indent.py"
        target.write_text("def f():\n    if ready:\n        return True\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "if ready:\n    return True\n",
                "new_string": "if ready:\n    return False\n",
            },
            context,
        )

        assert not result.is_error
        assert "indentation-flexible fallback" in result.output
        assert target.read_text() == "def f():\n    if ready:\n        return False\n"

    @pytest.mark.asyncio()
    async def test_edit_fallback_preserves_line_ending_when_old_string_omits_it(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "indent-no-terminal-old.py"
        target.write_text("def f():\n    if ready:\n        return True\n    return None\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "if ready:\n    return True",
                "new_string": "if ready:\n    return False",
            },
            context,
        )

        assert not result.is_error
        assert (
            target.read_text() == "def f():\n    if ready:\n        return False\n    return None\n"
        )

    @pytest.mark.asyncio()
    async def test_edit_uses_escaped_newline_fallback(self, tmp_path: Path) -> None:
        target = tmp_path / "escaped.txt"
        target.write_text("alpha\nbeta\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "alpha\\nbeta\\n",
                "new_string": "alpha\\ngamma\\n",
            },
            context,
        )

        assert not result.is_error
        assert "escaped-character fallback" in result.output
        assert target.read_text() == "alpha\ngamma\n"

    @pytest.mark.asyncio()
    async def test_edit_uses_escaped_newline_fallback_preserving_crlf(self, tmp_path: Path) -> None:
        target = tmp_path / "escaped-crlf.txt"
        target.write_bytes(b"alpha\r\nbeta\r\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "alpha\\nbeta\\n",
                "new_string": "alpha\\ngamma\\n",
            },
            context,
        )

        assert not result.is_error
        assert "escaped-character normalization" in result.output
        assert target.read_bytes() == b"alpha\r\ngamma\r\n"

    @pytest.mark.asyncio()
    async def test_edit_rejects_ambiguous_fallback_without_replace_all(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "ambiguous.txt"
        target.write_text("  beta  \n  beta  \n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "beta\n",
                "new_string": "gamma\n",
            },
            context,
        )

        assert result.is_error
        assert "Found 2 matches" in result.output
        assert "Candidate match start lines: 1, 2." in result.output

    @pytest.mark.asyncio()
    async def test_edit_replace_all_handles_ambiguous_fallback(self, tmp_path: Path) -> None:
        target = tmp_path / "replace-all-fallback.txt"
        target.write_text("value   \nvalue   \n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_edit(
            {
                "file_path": str(target),
                "old_string": "value\n",
                "new_string": "done\n",
                "replace_all": True,
            },
            context,
        )

        assert not result.is_error
        assert "rstrip-normalized fallback" in result.output
        assert target.read_text() == "done\ndone\n"


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
    async def test_multiedit_rejects_old_string_inside_previous_new_string(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "multi-overlap.txt"
        f.write_text("alpha\nbeta\n")
        context = _context()
        await handle_read({"file_path": str(f)}, context)

        result = await handle_multiedit(
            {
                "file_path": str(f),
                "edits": [
                    {"old_string": "alpha", "new_string": "alpha beta"},
                    {"old_string": "beta", "new_string": "gamma"},
                ],
            },
            context,
        )

        assert result.is_error
        assert "old_string is contained in new_string from edit 1" in result.output
        assert f.read_text() == "alpha\nbeta\n"

    @pytest.mark.asyncio()
    async def test_multiedit_uses_indentation_flexible_fallback(self, tmp_path: Path) -> None:
        f = tmp_path / "multi-indent.py"
        f.write_text("def f():\n    if ready:\n        return True\n")
        context = _context()
        await handle_read({"file_path": str(f)}, context)

        result = await handle_multiedit(
            {
                "file_path": str(f),
                "edits": [
                    {
                        "old_string": "if ready:\n    return True\n",
                        "new_string": "if ready:\n    return False\n",
                    }
                ],
            },
            context,
        )

        assert not result.is_error
        assert "indentation-flexible fallback" in result.output
        assert f.read_text() == "def f():\n    if ready:\n        return False\n"

    @pytest.mark.asyncio()
    async def test_multiedit_rejects_ambiguous_fallback(self, tmp_path: Path) -> None:
        f = tmp_path / "multi-ambiguous.txt"
        f.write_text("  beta  \n  beta  \n")
        context = _context()
        await handle_read({"file_path": str(f)}, context)

        result = await handle_multiedit(
            {
                "file_path": str(f),
                "edits": [{"old_string": "beta\n", "new_string": "gamma\n"}],
            },
            context,
        )

        assert result.is_error
        assert "Edit 1: Found 2 matches" in result.output
        assert "Candidate match start lines: 1, 2." in result.output
        assert f.read_text() == "  beta  \n  beta  \n"

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
    async def test_apply_patch_prefers_patch_text_over_empty_native_operation(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
                ),
                "operation": {
                    "type": "update_file",
                    "path": str(target),
                    "diff": "",
                },
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
            {"patchText": (f"*** Begin Patch\n*** Add File: {target}\n+hello\n*** End Patch\n")},
            _context(),
        )

        assert not result.is_error
        assert target.read_text() == "hello\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_add_file_overwrites_existing_like_codex(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "test.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {"patchText": f"*** Begin Patch\n*** Add File: {target}\n+hi\n*** End Patch\n"},
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"

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
    async def test_apply_patch_waits_for_fresh_lsp_even_when_lsp_already_pending(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "pending_lsp.py"
        target.write_text("x = 1\n")

        class _PendingLSP:
            def __init__(self) -> None:
                self.wait_calls = 0
                self.warm_calls = 0

            def has_pending_diagnostics(self, _paths: list[str]) -> bool:
                return True

            async def touch_file(self, _path: str, *, wait: bool = True, **_: object):
                if wait:
                    self.wait_calls += 1
                    return DiagnosticCollection(
                        waits=[
                            DiagnosticWaitResult(
                                server_id="ruff",
                                uri="file:///pending_lsp.py",
                                target_version=1,
                                status=DiagnosticFreshness.TIMEOUT,
                                duration_ms=1,
                            )
                        ]
                    )
                else:
                    self.warm_calls += 1
                    return DiagnosticCollection()

            def get_diagnostics(self, *_: object) -> dict[str, list[object]]:
                return {}

            def get_diagnostic_snapshots(self, *_: object) -> dict[str, list[object]]:
                return {}

        lsp = _PendingLSP()
        context = _context(runtime_metadata={"lsp_manager": lsp})
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-x = 1\n+x = 2\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "x = 2\n"
        assert lsp.wait_calls == 1
        assert lsp.warm_calls >= 1
        assert "cached diagnostics were not shown" in result.output

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
    async def test_apply_patch_move_overwrites_destination_like_codex(self, tmp_path: Path) -> None:
        source = tmp_path / "old.txt"
        dest = tmp_path / "new.txt"
        source.write_text("hello\n")
        dest.write_text("existing\n")
        context = _context()
        await handle_read({"file_path": str(source)}, context)
        await handle_read({"file_path": str(dest)}, context)

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
        assert result.metadata is not None
        diff = result.metadata["file_diffs"][0]["diff"]
        assert "-existing" in diff
        assert "+hello" in diff

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
    async def test_apply_patch_repeated_hunk_uses_first_match_like_codex(
        self, tmp_path: Path
    ) -> None:
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

        assert not result.is_error
        assert target.read_text() == "hi\nhello\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_repeated_hunks_advance_from_previous_match(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "ordered.txt"
        target.write_text("hello\nhello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n@@\n-hello\n+bye\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\nbye\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_repeated_update_file_sections_apply_sequentially(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "weekly.md"
        target.write_text("## Open\n- old A\n\n## Done\n- old B\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n"
                    f"*** Update File: {target}\n"
                    f"@@\n"
                    f"-- old A\n"
                    f"+- new A\n"
                    f"*** Update File: {target}\n"
                    f"@@\n"
                    f"-- old B\n"
                    f"+- new B\n"
                    f"*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "## Open\n- new A\n\n## Done\n- new B\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_update_accepts_implicit_first_hunk_like_codex(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "implicit.txt"
        target.write_text("hello\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n-hello\n+hi\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_add_then_update_same_file_uses_staged_content(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "new-then-update.txt"

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n"
                    f"*** Add File: {target}\n"
                    f"+hello\n"
                    f"*** Update File: {target}\n"
                    f"@@\n"
                    f"-hello\n"
                    f"+hi\n"
                    f"*** End Patch\n"
                )
            },
            _context(),
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_context_anchor_selects_later_repeated_block(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "anchored.txt"
        target.write_text("class One:\n    value = 1\n\nclass Two:\n    value = 1\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@ class Two:\n-    value = 1\n+    value = 2\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "class One:\n    value = 1\n\nclass Two:\n    value = 2\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_matches_with_codex_whitespace_tolerance(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "whitespace.txt"
        target.write_text("    hello   \n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": f"*** Begin Patch\n*** Update File: {target}\n@@\n-hello\n+hi\n*** End Patch\n"
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "hi\n"

    @pytest.mark.asyncio()
    async def test_apply_patch_matches_with_codex_unicode_punctuation_tolerance(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "unicode.txt"
        target.write_text("local import – avoids top‑level dep\n")
        context = _context()
        await handle_read({"file_path": str(target)}, context)

        result = await handle_apply_patch(
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {target}\n@@\n-local import - avoids top-level dep\n+local import ok\n*** End Patch\n"
                )
            },
            context,
        )

        assert not result.is_error
        assert target.read_text() == "local import ok\n"

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

        async def _wrapped_stage(operations: Any, ctx: Any):
            nonlocal calls
            calls += 1
            staged = await original(operations, ctx)
            target.write_text("changed\n")
            return staged

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
        assert calls == 1

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
    async def test_apply_patch_no_newline_marker_can_add_final_newline(
        self, tmp_path: Path
    ) -> None:
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
        assert str(tmp_path / "a.py") in result.output

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
        assert str(tmp_path / "a.py") in result.output

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
        assert result.metadata is not None
        assert result.metadata["content_trust"] == "untrusted"

    @pytest.mark.asyncio()
    async def test_grep_single_file_default_lifts_ten_match_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import search as search_module

        monkeypatch.setattr(search_module, "_RG_PATH", None)
        target = tmp_path / "a.py"
        target.write_text("\n".join(f"needle {index}" for index in range(15)) + "\n")

        result = await handle_grep({"pattern": "needle", "path": str(target)}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert "needle 14" in result.output

    @pytest.mark.asyncio()
    async def test_grep_directory_cap_has_actionable_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import search as search_module

        monkeypatch.setattr(search_module, "_RG_PATH", None)
        target = tmp_path / "a.py"
        target.write_text("\n".join(f"needle {index}" for index in range(12)) + "\n")

        result = await handle_grep({"pattern": "needle", "path": str(tmp_path)}, _DUMMY_CONTEXT)

        assert not result.is_error
        assert "increase max_per_file" in result.output
        assert str(target) in result.output

    @pytest.mark.asyncio()
    async def test_grep_case_insensitive_context_and_output_modes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import search as search_module

        monkeypatch.setattr(search_module, "_RG_PATH", None)
        target = tmp_path / "a.py"
        target.write_text("before\nNeedle\nAfter\n")

        content = await handle_grep(
            {
                "pattern": "needle",
                "path": str(tmp_path),
                "case_insensitive": True,
                "context_lines": 1,
            },
            _DUMMY_CONTEXT,
        )
        files = await handle_grep(
            {
                "pattern": "needle",
                "path": str(tmp_path),
                "case_insensitive": True,
                "output_mode": "files_with_matches",
            },
            _DUMMY_CONTEXT,
        )
        counts = await handle_grep(
            {
                "pattern": "needle",
                "path": str(tmp_path),
                "case_insensitive": True,
                "output_mode": "count",
            },
            _DUMMY_CONTEXT,
        )

        assert not content.is_error
        assert f"{target}-1- before" in content.output
        assert f"{target}:2: Needle" in content.output
        assert not files.is_error
        assert files.output.strip() == str(target)
        assert not counts.is_error
        assert f"{target}: 1" in counts.output
        assert "Total matches: 1" in counts.output

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
    async def test_grep_accepts_comma_separated_include_patterns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import search as search_module

        monkeypatch.setattr(search_module, "_RG_PATH", None)
        (tmp_path / "a.ts").write_text("needle\n")
        (tmp_path / "b.svelte").write_text("needle\n")
        (tmp_path / "c.py").write_text("needle\n")

        result = await handle_grep(
            {"pattern": "needle", "path": str(tmp_path), "include": "*.ts,*.svelte"},
            _DUMMY_CONTEXT,
        )

        assert not result.is_error
        assert "a.ts" in result.output
        assert "b.svelte" in result.output
        assert "c.py" not in result.output

    @pytest.mark.asyncio()
    async def test_grep_keeps_brace_include_patterns_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import search as search_module

        monkeypatch.setattr(search_module, "_RG_PATH", None)
        (tmp_path / "a.ts").write_text("needle\n")
        (tmp_path / "b.svelte").write_text("needle\n")
        (tmp_path / "c.py").write_text("needle\n")

        result = await handle_grep(
            {"pattern": "needle", "path": str(tmp_path), "include": "*.{ts,svelte}"},
            _DUMMY_CONTEXT,
        )

        assert not result.is_error
        assert "a.ts" in result.output
        assert "b.svelte" in result.output
        assert "c.py" not in result.output

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

    @pytest.mark.asyncio()
    async def test_grep_rg_uses_multiple_globs_for_comma_separated_include(
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

        monkeypatch.setattr(search_module, "_RG_PATH", "/usr/bin/rg")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        result = await handle_grep(
            {"pattern": "needle", "path": str(tmp_path), "include": "*.ts,*.svelte"},
            _DUMMY_CONTEXT,
        )

        assert not result.is_error
        glob_indexes = [index for index, value in enumerate(captured) if value == "--glob"]
        assert captured[glob_indexes[0] + 1] == "*.ts"
        assert captured[glob_indexes[1] + 1] == "*.svelte"


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
    async def test_bash_streams_foreground_output_chunks(self, tmp_path: Path) -> None:
        chunks: list[tuple[str, str | None]] = []
        context = _context()

        async def _on_chunk(delta: str, stream: str | None) -> None:
            chunks.append((delta, stream))

        context.output_chunk_callback = _on_chunk

        result = await handle_bash(
            {
                "command": "printf 'out\\n'; printf 'err\\n' >&2",
                "workdir": str(tmp_path),
            },
            context,
        )

        assert result.is_error is False
        assert "out" in result.output
        assert "err" in result.output
        assert ("out\n", "stdout") in chunks
        assert ("err\n", "stderr") in chunks

    @pytest.mark.asyncio()
    async def test_bash_warns_on_python_file_rewrite_one_liner(self, tmp_path: Path) -> None:
        result = await handle_bash(
            {
                "command": (
                    "python -c \"from pathlib import Path; Path('x.py').write_text('x=1')\""
                ),
                "workdir": str(tmp_path),
            },
            _DUMMY_CONTEXT,
        )

        assert not result.is_error
        assert "Prefer dedicated edit tools for rewriting source files." in result.output
        assert result.metadata is not None
        assert result.metadata["advisory"] == (
            "Prefer dedicated edit tools for rewriting source files. "
            "Use shell or interpreter rewrites only when they are necessary and intentional."
        )
        assert (tmp_path / "x.py").read_text() == "x=1"

    @pytest.mark.asyncio()
    async def test_bash_warns_on_embedded_python_file_rewrite(self, tmp_path: Path) -> None:
        result = await handle_bash(
            {
                "command": (
                    "python <<'PY'\nfrom pathlib import Path\nPath('x.py').write_text('x=1')\nPY"
                ),
                "workdir": str(tmp_path),
            },
            _DUMMY_CONTEXT,
        )

        assert not result.is_error
        assert "Prefer dedicated edit tools for rewriting source files." in result.output
        assert result.metadata is not None
        assert result.metadata["advisory"] == (
            "Prefer dedicated edit tools for rewriting source files. "
            "Use shell or interpreter rewrites only when they are necessary and intentional."
        )
        assert (tmp_path / "x.py").read_text() == "x=1"

    @pytest.mark.asyncio()
    async def test_bash_allows_python_read_only_one_liner(self) -> None:
        result = await handle_bash(
            {"command": "python -c \"print(open('/dev/null').read())\""},
            _DUMMY_CONTEXT,
        )

        assert not result.is_error

    @pytest.mark.asyncio()
    async def test_bash_warns_on_shell_redirection_to_source_file(self, tmp_path: Path) -> None:
        result = await handle_bash(
            {"command": "printf 'x' > test.py", "workdir": str(tmp_path)},
            _DUMMY_CONTEXT,
        )

        assert not result.is_error
        assert "Prefer dedicated edit tools for rewriting source files." in result.output
        assert result.metadata is not None
        assert result.metadata["advisory"] == (
            "Prefer dedicated edit tools for rewriting source files. "
            "Use shell or interpreter rewrites only when they are necessary and intentional."
        )
        assert (tmp_path / "test.py").read_text() == "x"

    @pytest.mark.asyncio()
    async def test_bash_warns_on_tee_to_source_file(self, tmp_path: Path) -> None:
        result = await handle_bash(
            {"command": "printf 'x' | tee test.py", "workdir": str(tmp_path)},
            _DUMMY_CONTEXT,
        )

        assert not result.is_error
        assert "Prefer dedicated edit tools for rewriting source files." in result.output
        assert result.metadata is not None
        assert result.metadata["advisory"] == (
            "Prefer dedicated edit tools for rewriting source files. "
            "Use shell or interpreter rewrites only when they are necessary and intentional."
        )
        assert (tmp_path / "test.py").read_text() == "x"

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
    async def test_bash_timeout_cleans_up_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import shell as shell_module

        class _Process:
            returncode: int | None = None

            async def communicate(self) -> tuple[bytes, bytes]:
                raise TimeoutError

        process = _Process()
        cleaned: list[_Process] = []

        async def _fake_create_process(**_: object) -> _Process:
            return process

        async def _fake_kill_process_tree(killed_process: _Process) -> None:
            killed_process.returncode = -9
            cleaned.append(killed_process)

        monkeypatch.setattr(shell_module, "_create_process", _fake_create_process)
        monkeypatch.setattr(shell_module, "_kill_process_tree", _fake_kill_process_tree)

        result = await handle_bash(
            {"command": "sleep 60", "timeout": 1, "workdir": str(tmp_path)}, _DUMMY_CONTEXT
        )

        assert result.is_error
        assert "timed out" in result.output
        assert cleaned == [process]
        assert result.metadata is not None
        assert result.metadata["status"] == "timed_out"
        assert result.metadata["process_cleanup"] == "terminated_then_killed"
        assert result.metadata["timeout_seconds"] == 1

    @pytest.mark.asyncio()
    async def test_bash_cancellation_cleans_up_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import shell as shell_module

        class _Process:
            returncode: int | None = None

            async def communicate(self) -> tuple[bytes, bytes]:
                await asyncio.Event().wait()
                return b"", b""

        process = _Process()
        cleaned = asyncio.Event()

        async def _fake_create_process(**_: object) -> _Process:
            return process

        async def _fake_kill_process_tree(killed_process: _Process) -> None:
            assert killed_process is process
            killed_process.returncode = -9
            cleaned.set()

        monkeypatch.setattr(shell_module, "_create_process", _fake_create_process)
        monkeypatch.setattr(shell_module, "_kill_process_tree", _fake_kill_process_tree)

        task = asyncio.create_task(
            handle_bash(
                {"command": "sleep 60", "timeout": 60000, "workdir": str(tmp_path)},
                _DUMMY_CONTEXT,
            )
        )
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    @pytest.mark.asyncio()
    async def test_bash_rejects_invalid_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cognis.tools.executor import shell as shell_module

        async def _fail_create_process(**_: object) -> object:
            raise AssertionError("process should not be created")

        monkeypatch.setattr(shell_module, "_create_process", _fail_create_process)

        result = await handle_bash({"command": "echo hello", "timeout": "soon"}, _DUMMY_CONTEXT)

        assert result.is_error
        assert "Timeout must be an integer" in result.output

    @pytest.mark.asyncio()
    async def test_bash_rejects_foreground_timeout_above_max(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import shell as shell_module

        async def _fail_create_process(**_: object) -> object:
            raise AssertionError("process should not be created")

        monkeypatch.setattr(shell_module, "_create_process", _fail_create_process)

        result = await handle_bash({"command": "sleep 1", "timeout": 3_600_001}, _DUMMY_CONTEXT)

        assert result.is_error
        assert "may not exceed 3600000 ms" in result.output
        assert "run_in_background=true" in result.output

    @pytest.mark.asyncio()
    async def test_bash_timeout_uses_ceiling_seconds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cognis.tools.executor import shell as shell_module

        observed_timeout: list[float | None] = []

        class _Process:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"ok", b""

        async def _fake_create_process(**_: object) -> _Process:
            return _Process()

        async def _fake_wait_for(awaitable: Any, *, timeout: float | None = None) -> Any:
            observed_timeout.append(timeout)
            return await awaitable

        monkeypatch.setattr(shell_module, "_create_process", _fake_create_process)
        monkeypatch.setattr(shell_module.asyncio, "wait_for", _fake_wait_for)

        result = await handle_bash(
            {"command": "echo ok", "timeout": 2500, "workdir": str(tmp_path)}, _DUMMY_CONTEXT
        )

        assert not result.is_error
        assert observed_timeout == [3]

    def test_bash_tool_timeout_allows_max_foreground_cleanup(self) -> None:
        from cognis.tools.executor import shell as shell_module

        max_foreground_timeout_ms = shell_module._MAX_FOREGROUND_TIMEOUT_MS
        cleanup_grace_seconds = shell_module._FOREGROUND_TIMEOUT_CLEANUP_GRACE_SECONDS
        max_timeout_seconds = math.ceil(max_foreground_timeout_ms / 1000)
        assert BASH_TOOL.timeout_seconds >= max_timeout_seconds + cleanup_grace_seconds
        assert cleanup_grace_seconds == 2

    def test_foreground_output_buffer_preserves_head_and_tail(self) -> None:
        from cognis.tools.executor.shell import _ForegroundOutputBuffer

        buffer = _ForegroundOutputBuffer(head_limit=5, tail_limit=5)
        buffer.append("abcdef")
        buffer.append("ghijkl")

        rendered = buffer.render()

        assert rendered.startswith("abcde")
        assert rendered.endswith("hijkl")
        assert "foreground output truncated" in rendered

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
        assert "notified/resumed" in start.output
        assert "end this turn now" in start.output
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

        filtered = await handle_bash_output(
            {"shell_id": shell_id, "filter_regex": "hello|ready"}, read_ctx
        )
        assert not filtered.is_error
        assert "filter_regex" in (filtered.metadata or {})

        stopped = await handle_bash_kill({"shell_id": shell_id}, read_ctx)
        assert not stopped.is_error
        assert shell_id in stopped.output

    @pytest.mark.asyncio()
    async def test_background_bash_status_includes_description_and_executor(self) -> None:
        shared_runtime_metadata: dict[str, Any] = {}
        ctx = ToolExecutionContext(
            executor_handle=ExecutorHandle(executor_id="exec-a", executor_type="in_process"),
            runtime_metadata={
                "runtime_access": {
                    "conversation_id": "conv-1",
                    "session_id": "sess-1",
                    "agent_id": "agent-1",
                },
                "turn_id": "turn-1",
                "tool_call_id": "call-1",
            },
            shared_runtime_metadata=shared_runtime_metadata,
        )

        start = await handle_bash(
            {
                "command": "python -u -c \"import time; print('ready'); time.sleep(5)\"",
                "description": "Run slow regression tests",
                "run_in_background": True,
            },
            ctx,
        )

        assert not start.is_error
        shell_id = str((start.metadata or {}).get("shell_id"))

        statuses = await list_background_shell_statuses(shared_runtime_metadata)
        assert len(statuses) == 1
        status = statuses[0]
        assert status["shell_id"] == shell_id
        assert status["description"] == "Run slow regression tests"
        assert status["executor_id"] == "exec-a"
        assert status["executor_type"] == "in_process"
        assert status["conversation_id"] == "conv-1"
        assert isinstance(status["pid"], int)

        stopped = await handle_bash_kill({"shell_id": shell_id}, ctx)
        assert not stopped.is_error

    @pytest.mark.asyncio()
    async def test_background_bash_completion_callback_fires_once(self) -> None:
        notifications: list[dict[str, Any]] = []

        async def _completed(status: dict[str, Any]) -> None:
            notifications.append(status)

        ctx = ToolExecutionContext(
            executor_handle=ExecutorHandle(executor_id="exec-a", executor_type="in_process"),
            runtime_metadata={
                "runtime_access": {"conversation_id": "conv-1", "agent_id": "agent-1"},
                "background_shell_completion_callback": _completed,
            },
            shared_runtime_metadata={},
        )

        start = await handle_bash(
            {
                "command": "python -c \"print('done')\"",
                "description": "Quick background check",
                "run_in_background": True,
            },
            ctx,
        )

        assert not start.is_error
        for _ in range(20):
            if notifications:
                break
            await asyncio.sleep(0.05)

        assert len(notifications) == 1
        assert notifications[0]["description"] == "Quick background check"
        assert notifications[0]["status"] == "completed"
        assert notifications[0]["exit_code"] == 0

    @pytest.mark.asyncio()
    async def test_background_bash_status_preserves_source_rewrite_advisory(
        self, tmp_path: Path
    ) -> None:
        shared_runtime_metadata: dict[str, Any] = {}
        notifications: list[dict[str, Any]] = []

        async def _completed(status: dict[str, Any]) -> None:
            notifications.append(status)

        ctx = ToolExecutionContext(
            executor_handle=ExecutorHandle(executor_id="exec-a", executor_type="in_process"),
            runtime_metadata={"background_shell_completion_callback": _completed},
            shared_runtime_metadata=shared_runtime_metadata,
        )

        start = await handle_bash(
            {
                "command": "printf 'x' > test.py",
                "workdir": str(tmp_path),
                "run_in_background": True,
            },
            ctx,
        )

        assert not start.is_error
        assert start.metadata is not None
        assert start.metadata["advisory"] == (
            "Prefer dedicated edit tools for rewriting source files. "
            "Use shell or interpreter rewrites only when they are necessary and intentional."
        )
        shell_id = str(start.metadata.get("shell_id"))

        statuses = await list_background_shell_statuses(
            shared_runtime_metadata, include_completed=True
        )
        assert len(statuses) == 1
        assert statuses[0]["shell_id"] == shell_id
        assert statuses[0]["advisory"] == start.metadata["advisory"]

        for _ in range(20):
            if notifications:
                break
            await asyncio.sleep(0.05)

        assert len(notifications) == 1
        assert notifications[0]["advisory"] == start.metadata["advisory"]
        assert (tmp_path / "test.py").read_text() == "x"

    @pytest.mark.asyncio()
    async def test_background_bash_kill_suppresses_completion_callback(self) -> None:
        notifications: list[dict[str, Any]] = []

        async def _completed(status: dict[str, Any]) -> None:
            notifications.append(status)

        ctx = ToolExecutionContext(
            executor_handle=ExecutorHandle(executor_id="exec-a", executor_type="in_process"),
            runtime_metadata={
                "runtime_access": {"conversation_id": "conv-1", "agent_id": "agent-1"},
                "background_shell_completion_callback": _completed,
            },
            shared_runtime_metadata={},
        )

        start = await handle_bash(
            {
                "command": 'python -u -c "import time; time.sleep(5)"',
                "description": "Long watcher",
                "run_in_background": True,
            },
            ctx,
        )

        assert not start.is_error
        shell_id = str((start.metadata or {}).get("shell_id"))
        stopped = await handle_bash_kill({"shell_id": shell_id}, ctx)
        assert not stopped.is_error
        await asyncio.sleep(0.05)
        assert notifications == []


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
            def __init__(self) -> None:
                self.touch_kwargs: dict[str, object] = {}

            async def touch_file(self, *_: object, **kwargs: object) -> None:
                self.touch_kwargs = kwargs
                return None

            async def has_clients(self, *_: object, **__: object) -> bool:
                return True

            async def definition(self, *_: object, **__: object) -> list[dict[str, object]]:
                return [{"uri": "file:///tmp/sample.py", "range": {}}]

        lsp = _FakeLsp()
        context = _context(runtime_metadata={"lsp_manager": lsp})
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
        assert lsp.touch_kwargs["purpose"] == "semantic"

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
