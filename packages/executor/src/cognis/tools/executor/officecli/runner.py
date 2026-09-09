"""Safe OfficeCLI subprocess boundary."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_OUTPUT_BYTES = 256_000
_MAX_ERROR_CHARS = 4_000


@dataclass(frozen=True)
class OfficeCliCommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    json_data: Any | None = None
    json_error: str | None = None
    timed_out: bool = False


async def run_officecli(
    command: str,
    args: list[str],
    *,
    officecli_path: str,
    timeout_seconds: float = 60,
    parse_json: bool = False,
    cwd: str | None = None,
) -> OfficeCliCommandResult:
    argv = [officecli_path, command, *args]
    env = dict(os.environ)
    env["OFFICECLI_SKIP_UPDATE"] = "1"
    env.setdefault("OFFICECLI_NO_AUTO_RESIDENT", "1")
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    workdir = cwd
    cleanup: tempfile.TemporaryDirectory[str] | None = None
    if workdir is None:
        cleanup = tempfile.TemporaryDirectory(prefix="cognis-officecli-cwd-")
        workdir = cleanup.name
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            return OfficeCliCommandResult(
                argv=argv,
                exit_code=-1,
                stdout=_decode(stdout_b),
                stderr=_truncate(_decode(stderr_b)),
                timed_out=True,
            )
    finally:
        if cleanup is not None:
            cleanup.cleanup()
    stdout = _decode(stdout_b)
    stderr = _truncate(_decode(stderr_b))
    json_data = None
    json_error = None
    if parse_json and stdout.strip():
        try:
            json_data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            json_error = f"Invalid JSON from OfficeCLI: {exc}"
    return OfficeCliCommandResult(
        argv=argv,
        exit_code=int(proc.returncode or 0),
        stdout=stdout,
        stderr=stderr,
        json_data=json_data,
        json_error=json_error,
    )


def output_path_arg(path: Path) -> list[str]:
    return ["-o", str(path)]


def _decode(data: bytes) -> str:
    if len(data) > _MAX_OUTPUT_BYTES:
        data = data[:_MAX_OUTPUT_BYTES] + b"\n...[truncated]"
    return data.decode("utf-8", errors="replace")


def _truncate(value: str) -> str:
    return value if len(value) <= _MAX_ERROR_CHARS else value[:_MAX_ERROR_CHARS] + "...[truncated]"
