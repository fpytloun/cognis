"""Executor-native shell tool: bash."""

from __future__ import annotations

import asyncio
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.registry import ToolExecutionContext

_DEFAULT_TIMEOUT_MS = 120_000
_MAX_OUTPUT_SIZE = 50_000


async def handle_bash(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute a shell command and return its output."""
    command = arguments.get("command", "")
    timeout_ms = arguments.get("timeout", _DEFAULT_TIMEOUT_MS)
    workdir = arguments.get("workdir")

    if not command.strip():
        return ToolResult(output="No command provided.", is_error=True)

    timeout_seconds = max(1, timeout_ms // 1000)

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        return ToolResult(
            output=f"Command timed out after {timeout_seconds}s.",
            is_error=True,
        )
    except FileNotFoundError:
        return ToolResult(
            output=f"Working directory not found: {workdir}",
            is_error=True,
        )
    except OSError as exc:
        return ToolResult(output=f"Command execution failed: {exc}", is_error=True)

    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
    exit_code = process.returncode or 0

    parts: list[str] = []
    if stdout_text:
        parts.append(stdout_text)
    if stderr_text:
        parts.append(f"STDERR:\n{stderr_text}")
    if exit_code != 0:
        parts.append(f"\nExit code: {exit_code}")

    output = "\n".join(parts) if parts else "(no output)"

    if len(output) > _MAX_OUTPUT_SIZE:
        output = (
            output[:_MAX_OUTPUT_SIZE] + f"\n[truncated: {len(output)} chars -> {_MAX_OUTPUT_SIZE}]"
        )

    return ToolResult(output=output, is_error=exit_code != 0)
