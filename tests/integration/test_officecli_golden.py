from __future__ import annotations

from pathlib import Path

import pytest

from cognis.models.tool import ExecutorHandle
from cognis.tools.executor.officecli.install import OfficeCliRuntimeConfig, ensure_officecli
from cognis.tools.executor.officecli.manifest import OFFICECLI_CERTIFIED_VERSION
from cognis.tools.executor.officecli.runner import run_officecli
from cognis.tools.registry import ToolExecutionContext

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "operations", "read_view"),
    [
        (
            "docx",
            [
                {
                    "verb": "add",
                    "parent": "/body",
                    "type": "paragraph",
                    "props": {"text": "OfficeCLI DOCX golden smoke"},
                }
            ],
            "text",
        ),
        (
            "xlsx",
            [
                {
                    "verb": "set",
                    "path": "/Sheet1/A1",
                    "props": {"value": "OfficeCLI XLSX golden smoke"},
                }
            ],
            "text",
        ),
        (
            "pptx",
            [
                {
                    "verb": "add",
                    "parent": "/",
                    "type": "slide",
                    "props": {"title": "OfficeCLI PPTX golden smoke"},
                }
            ],
            "text",
        ),
    ],
)
async def test_officecli_certified_runtime_create_patch_read_validate(
    fmt: str,
    operations: list[dict[str, object]],
    read_view: str,
    tmp_path: Path,
) -> None:
    status = await ensure_officecli(OfficeCliRuntimeConfig(cache_dir=tmp_path / "cache"))
    if not status.available or not status.command:
        pytest.skip(f"Certified OfficeCLI runtime unavailable: {status.error}")
    assert status.version == OFFICECLI_CERTIFIED_VERSION

    from cognis.tools.executor.officecli.handlers import (
        handle_office_create,
        handle_office_patch,
        handle_office_read,
        handle_office_validate,
    )

    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={
            "working_directory": str(tmp_path),
            "officecli": status.metadata(),
        },
    )
    source = tmp_path / f"source.{fmt}"
    create_result = await handle_office_create(
        {
            "format": fmt,
            "output_path": str(source),
            "publish_artifact": False,
        },
        context,
    )
    assert create_result.is_error is False
    assert source.exists()

    patched = tmp_path / f"patched.{fmt}"
    patch_result = await handle_office_patch(
        {
            "source_path": str(source),
            "operations": operations,
            "output_path": str(patched),
            "publish_artifact": False,
            "validate": True,
        },
        context,
    )
    assert patch_result.is_error is False
    assert patched.exists()

    read_result = await handle_office_read(
        {"source_path": str(patched), "view": read_view},
        context,
    )
    assert read_result.is_error is False
    assert "OfficeCLI" in read_result.output

    validate_result = await handle_office_validate({"source_path": str(patched)}, context)
    assert validate_result.is_error is False


@pytest.mark.asyncio
async def test_officecli_certified_runtime_render_html(tmp_path: Path) -> None:
    status = await ensure_officecli(OfficeCliRuntimeConfig(cache_dir=tmp_path / "cache"))
    if not status.available or not status.command:
        pytest.skip(f"Certified OfficeCLI runtime unavailable: {status.error}")

    source = tmp_path / "render.docx"
    create = await run_officecli(
        "create",
        [str(source)],
        officecli_path=status.command,
        timeout_seconds=60,
    )
    assert create.exit_code == 0
    add = await run_officecli(
        "add",
        [
            str(source),
            "/body",
            "--type",
            "paragraph",
            "--prop",
            "text=OfficeCLI HTML render smoke",
        ],
        officecli_path=status.command,
        timeout_seconds=60,
    )
    assert add.exit_code == 0

    from cognis.tools.executor.officecli.handlers import handle_office_render

    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="local"),
        runtime_metadata={
            "working_directory": str(tmp_path),
            "officecli": status.metadata(),
        },
    )
    result = await handle_office_render(
        {"source_path": str(source), "render": "html", "output_filename": "render.html"},
        context,
    )

    assert result.is_error is False
    assert result.attachments
    assert result.attachments[0]["filename"] == "render.html"
