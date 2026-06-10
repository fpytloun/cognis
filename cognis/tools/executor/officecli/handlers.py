"""Handlers for executor-native Office document tools."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import shutil
import tempfile
from pathlib import Path
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.officecli.install import OFFICECLI_RUNTIME_METADATA_KEY
from cognis.tools.executor.officecli.runner import run_officecli
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

_MAX_INPUT_BYTES = 100 * 1024 * 1024
_OFFICE_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


async def handle_office_read(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    with tempfile.TemporaryDirectory(prefix="cognis-office-read-") as tmp:
        source = _materialize_source(arguments, Path(tmp), context)
        view = str(arguments.get("view") or "text")
        args = [str(source.path), "json" if view == "json" else view]
        _add_view_options(args, arguments)
        parse_json = view in {"json", "stats", "issues"}
        result = await _run(context, "view", args, arguments, parse_json=parse_json)
        return _command_tool_result(result, {"view": view, **source.metadata})


async def handle_office_get(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    with tempfile.TemporaryDirectory(prefix="cognis-office-get-") as tmp:
        source = _materialize_source(arguments, Path(tmp), context)
        args = [str(source.path), str(arguments["object_path"])]
        if depth := arguments.get("depth"):
            args.extend(["--depth", str(depth)])
        parse_json = bool(arguments.get("json", True))
        if parse_json:
            args.append("--json")
        result = await _run(context, "get", args, arguments, parse_json=parse_json)
        return _command_tool_result(result, source.metadata)


async def handle_office_query(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    with tempfile.TemporaryDirectory(prefix="cognis-office-query-") as tmp:
        source = _materialize_source(arguments, Path(tmp), context)
        args = [str(source.path), str(arguments["selector"])]
        if limit := arguments.get("limit"):
            args.extend(["--limit", str(limit)])
        parse_json = bool(arguments.get("json", True))
        if parse_json:
            args.append("--json")
        result = await _run(context, "query", args, arguments, parse_json=parse_json)
        return _command_tool_result(result, source.metadata)


async def handle_office_validate(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    with tempfile.TemporaryDirectory(prefix="cognis-office-validate-") as tmp:
        source = _materialize_source(arguments, Path(tmp), context)
        result = await _run(
            context, "validate", [str(source.path), "--json"], arguments, parse_json=True
        )
        metadata = {"valid": result.exit_code == 0, **source.metadata}
        return _command_tool_result(result, metadata, error_on_nonzero=False)


async def handle_office_render(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    with tempfile.TemporaryDirectory(prefix="cognis-office-render-") as tmp:
        tmp_path = Path(tmp)
        source = _materialize_source(arguments, tmp_path, context)
        render = str(arguments["render"])
        suffix = _render_suffix(render)
        filename = _safe_filename(arguments.get("output_filename"), f"{source.path.stem}.{suffix}")
        output_path = tmp_path / filename
        args = [str(source.path), render]
        if render != "html":
            args.extend(["-o", str(output_path)])
        _add_view_options(args, arguments)
        result = await _run(context, "view", args, arguments, parse_json=False)
        if result.exit_code != 0 or result.timed_out:
            return _command_tool_result(result, source.metadata)
        if render == "html" and not output_path.exists() and result.stdout:
            output_path.write_text(result.stdout, encoding="utf-8")
        if not output_path.exists():
            return ToolResult(
                output="OfficeCLI render did not produce the expected output file.", is_error=True
            )
        return _artifact_result(output_path, filename, arguments, source.metadata)


async def handle_office_create(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    with tempfile.TemporaryDirectory(prefix="cognis-office-create-"):
        fmt = _infer_format(arguments)
        output_path = _requested_output_path(arguments, context)
        filename = _create_filename(arguments, fmt, output_path=output_path)
        output_path = output_path or _default_output_path(arguments, context, filename)
        if output_path.suffix.lower().lstrip(".") != fmt:
            output_path = output_path.with_suffix(f".{fmt}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = await _run(context, "create", [str(output_path)], arguments, parse_json=False)
        if result.exit_code != 0 or result.timed_out:
            return _command_tool_result(result, {"output_path": str(output_path)})
        op_error = await _apply_operations(
            context, output_path, arguments, arguments.get("operations") or []
        )
        if op_error is not None:
            return op_error
        return _artifact_or_path_result(output_path, filename, arguments, {"format": fmt})


async def handle_office_patch(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    with tempfile.TemporaryDirectory(prefix="cognis-office-patch-") as tmp:
        tmp_path = Path(tmp)
        source = _materialize_source(arguments, tmp_path, context)
        expected = arguments.get("expected_base_sha256") or arguments.get("expected_sha256")
        if expected and str(expected).lower() != source.sha256:
            return ToolResult(
                output=f"Stale Office document input: expected {expected}, got {source.sha256}.",
                is_error=True,
            )
        output_path = _requested_output_path(arguments, context)
        in_place = bool(arguments.get("in_place"))
        if in_place:
            if source.source_kind == "artifact":
                return ToolResult(
                    output="office_patch cannot modify source artifacts in place.", is_error=True
                )
            target_path = source.path
        else:
            filename = _safe_filename(
                arguments.get("output_filename"), f"{source.path.stem}-patched{source.path.suffix}"
            )
            target_path = output_path or _default_output_path(arguments, context, filename)
            if target_path != source.path:
                shutil.copy2(source.path, target_path)
        op_error = await _apply_operations(
            context, target_path, arguments, arguments.get("operations") or []
        )
        if op_error is not None:
            return op_error
        if arguments.get("validate", True):
            validation = await _run(
                context, "validate", [str(target_path)], arguments, parse_json=False
            )
            if validation.exit_code != 0 or validation.timed_out:
                return _command_tool_result(
                    validation, {"operation": "validate", **source.metadata}
                )
        filename = _safe_filename(arguments.get("output_filename"), target_path.name)
        return _artifact_or_path_result(target_path, filename, arguments, source.metadata)


class _Source:
    def __init__(self, path: Path, source_kind: str, sha256: str, metadata: dict[str, Any]) -> None:
        self.path = path
        self.source_kind = source_kind
        self.sha256 = sha256
        self.metadata = metadata


def _materialize_source(
    arguments: dict[str, Any], tmp_path: Path, context: ToolExecutionContext
) -> _Source:
    provided = [key for key in ("source_path", "source_artifact_id") if arguments.get(key)]
    if len(provided) != 1:
        raise ValueError("Exactly one of source_path or source_artifact_id is required.")
    if artifact_id := arguments.get("source_artifact_id"):
        b64 = arguments.get("source_artifact_content_b64")
        if not isinstance(b64, str) or not b64:
            raise ValueError(
                "source_artifact_id requires controller-side artifact materialization."
            )
        content = base64.b64decode(b64)
        _check_size(content)
        filename = _safe_filename(arguments.get("source_artifact_filename"), "source.docx")
        path = tmp_path / filename
        path.write_bytes(content)
        sha = _sha256_bytes(content)
        _check_expected(arguments, sha)
        return _Source(
            path,
            "artifact",
            sha,
            {"source_kind": "artifact", "source_artifact_id": str(artifact_id), "base_sha256": sha},
        )
    path = resolve_path(str(arguments["source_path"]), context=context)
    if not path.is_file():
        raise ValueError(f"Office source file not found: {arguments['source_path']}")
    if path.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError(f"Office source exceeds {_MAX_INPUT_BYTES} bytes.")
    sha = _sha256_file(path)
    _check_expected(arguments, sha)
    return _Source(
        path, "path", sha, {"source_kind": "path", "source_path": str(path), "base_sha256": sha}
    )


async def _run(
    context: ToolExecutionContext,
    command: str,
    args: list[str],
    arguments: dict[str, Any],
    *,
    parse_json: bool,
) -> Any:
    runtime = context.runtime_metadata.get(OFFICECLI_RUNTIME_METADATA_KEY) or {}
    command_path = runtime.get("command")
    if not runtime.get("available") or not command_path:
        return ToolResult(
            output=f"OfficeCLI unavailable: {runtime.get('error') or 'not configured'}",
            is_error=True,
        )
    return await run_officecli(
        command,
        args,
        officecli_path=str(command_path),
        timeout_seconds=float(arguments.get("timeout_seconds") or 60),
        parse_json=parse_json,
    )


def _command_tool_result(
    result: Any, metadata: dict[str, Any], *, error_on_nonzero: bool = True
) -> ToolResult:
    if isinstance(result, ToolResult):
        return result
    output: dict[str, Any] = {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        **metadata,
    }
    if result.json_data is not None:
        output["data"] = result.json_data
    if result.json_error:
        output["json_error"] = result.json_error
    is_error = (
        result.timed_out or bool(result.json_error) or (error_on_nonzero and result.exit_code != 0)
    )
    return ToolResult(output=json.dumps(output, sort_keys=True, default=str), is_error=is_error)


async def _apply_operations(
    context: ToolExecutionContext, path: Path, arguments: dict[str, Any], operations: list[Any]
) -> ToolResult | None:
    for index, raw in enumerate(operations):
        if not isinstance(raw, dict):
            return ToolResult(output=f"Invalid Office operation at index {index}.", is_error=True)
        verb = str(raw.get("verb") or "")
        args = _operation_args(path, raw)
        result = await _run(context, verb, args, arguments, parse_json=False)
        if isinstance(result, ToolResult):
            return result
        if result.exit_code != 0 or result.timed_out:
            return _command_tool_result(result, {"failed_operation_index": index})
    return None


def _operation_args(path: Path, op: dict[str, Any]) -> list[str]:
    verb = str(op.get("verb") or "")
    target = op.get("path") or op.get("parent") or op.get("selector") or "/"
    args = [str(path), str(target)]
    if verb == "add":
        element_type = op.get("type")
        if element_type:
            args.extend(["--type", str(element_type)])
        if from_path := op.get("from_path"):
            args.extend(["--from", str(from_path)])
    for flag in ("before", "after", "index"):
        if op.get(flag) is not None:
            args.extend([f"--{flag}", str(op[flag])])
    props = op.get("props") or {}
    if isinstance(props, dict):
        for key, value in props.items():
            args.extend(["--prop", f"{key}={_prop_value(value)}"])
    return args


def _add_view_options(args: list[str], arguments: dict[str, Any]) -> None:
    mapping = {
        "start": "--start",
        "end": "--end",
        "max_lines": "--max-lines",
        "limit": "--limit",
        "page": "--page",
        "issue_type": "--type",
        "width": "--screenshot-width",
        "height": "--screenshot-height",
    }
    for key, flag in mapping.items():
        if arguments.get(key) is not None:
            args.extend([flag, str(arguments[key])])


def _artifact_or_path_result(
    path: Path, filename: str, arguments: dict[str, Any], metadata: dict[str, Any]
) -> ToolResult:
    if arguments.get("publish_artifact", True):
        return _artifact_result(path, filename, arguments, metadata)
    return ToolResult(
        output=json.dumps(
            {"output_path": str(path), "output_sha256": _sha256_file(path), **metadata},
            sort_keys=True,
        )
    )


def _artifact_result(
    path: Path, filename: str, arguments: dict[str, Any], metadata: dict[str, Any]
) -> ToolResult:
    content = path.read_bytes()
    attachment = {
        "filename": filename,
        "mime_type": _mime_type(filename),
        "content_b64": base64.b64encode(content).decode("ascii"),
        "kind": "file",
        "purpose": str(arguments.get("purpose") or "officecli_output"),
    }
    output = {
        "filename": filename,
        "size_bytes": len(content),
        "output_sha256": _sha256_bytes(content),
        **metadata,
    }
    if arguments.get("output_path"):
        output["output_path"] = str(path)
    return ToolResult(output=json.dumps(output, sort_keys=True), attachments=[attachment])


def _requested_output_path(arguments: dict[str, Any], context: ToolExecutionContext) -> Path | None:
    if output_path := arguments.get("output_path"):
        return resolve_path(str(output_path), context=context)
    return None


def _default_output_path(
    arguments: dict[str, Any], context: ToolExecutionContext, filename: str
) -> Path:
    if arguments.get("publish_artifact", True):
        return Path(tempfile.mkdtemp(prefix="cognis-office-output-")) / filename
    return resolve_path(filename, context=context)


def _infer_format(arguments: dict[str, Any]) -> str:
    if fmt := arguments.get("format"):
        return str(fmt)
    for key in ("output_filename", "output_path"):
        if value := arguments.get(key):
            suffix = Path(str(value)).suffix.lower().lstrip(".")
            if suffix in {"docx", "xlsx", "pptx"}:
                return suffix
    return "docx"


def _create_filename(arguments: dict[str, Any], fmt: str, *, output_path: Path | None) -> str:
    raw = arguments.get("output_filename")
    if raw:
        filename = _safe_filename(raw, f"document.{fmt}")
        if Path(filename).suffix.lower().lstrip(".") != fmt:
            return f"{Path(filename).stem}.{fmt}"
        return filename
    if output_path is not None:
        filename = _safe_filename(output_path.name, f"document.{fmt}")
        if Path(filename).suffix.lower().lstrip(".") != fmt:
            return f"{Path(filename).stem}.{fmt}"
        return filename
    return f"document.{fmt}"


def _render_suffix(render: str) -> str:
    return {"screenshot": "png", "forms": "json"}.get(render, render)


def _safe_filename(value: Any, fallback: str) -> str:
    name = Path(str(value or fallback)).name
    return name or fallback


def _mime_type(filename: str) -> str:
    return (
        _OFFICE_MIME_TYPES.get(Path(filename).suffix.lower())
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )


def _check_size(content: bytes) -> None:
    if len(content) > _MAX_INPUT_BYTES:
        raise ValueError(f"Office artifact exceeds {_MAX_INPUT_BYTES} bytes.")


def _check_expected(arguments: dict[str, Any], actual: str) -> None:
    expected = arguments.get("expected_sha256")
    if expected and str(expected).lower() != actual:
        raise ValueError(f"Office source SHA256 mismatch: expected {expected}, got {actual}.")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _prop_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)
