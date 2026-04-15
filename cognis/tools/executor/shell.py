"""Executor-native shell tool: bash."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

_DEFAULT_TIMEOUT_MS = 120_000
_MAX_OUTPUT_SIZE = 50_000
_SHELL_OVERRIDE_ENV = "COGNIS_EXECUTOR_SHELL"
_BLOCKED_EDIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r'(^|\s)sed\s+-i(?:[\s\'"]|$)'),
        "Use edit, multiedit, patch, or write instead of sed -i for file content changes.",
    ),
    (
        re.compile(r'(^|\s)perl\s+-pi(?:[\s\'"]|$)'),
        "Use edit, multiedit, patch, or write instead of perl -pi for file content changes.",
    ),
    (
        re.compile(r'(^|\s)ruby\s+-pi(?:[\s\'"]|$)'),
        "Use edit, multiedit, patch, or write instead of ruby -pi for file content changes.",
    ),
    (
        re.compile(
            r"(^|\s)python(?:3)?\s+-c\s+.*(write_text|write_bytes|open\s*\([^\)]*,\s*['\"](?:w|a|x|w\+|a\+|x\+)['\"]).*",
            re.DOTALL,
        ),
        "Use edit, multiedit, patch, or write instead of Python one-liners that rewrite files.",
    ),
    (
        re.compile(
            r"(^|\s)python(?:3)?\s+.*<<[-~]?['\"]?(?:PY|EOF)['\"]?.*(write_text|write_bytes|open\s*\([^\)]*,\s*['\"](?:w|a|x|w\+|a\+|x\+)['\"]).*",
            re.DOTALL,
        ),
        "Use edit, multiedit, patch, or write instead of embedded Python scripts that rewrite files.",
    ),
    (
        re.compile(r"(?:>>|>)\s*[^\s]+\.(?:py|js|jsx|ts|tsx|json|md|ya?ml|html|css|scss|toml)\b"),
        "Use edit, multiedit, patch, or write instead of shell redirection to rewrite source files.",
    ),
    (
        re.compile(r"(^|\s)tee\s+[^\n]*\.(?:py|js|jsx|ts|tsx|json|md|ya?ml|html|css|scss|toml)\b"),
        "Use edit, multiedit, patch, or write instead of tee to rewrite source files.",
    ),
)


def _shell_name(path: str) -> str:
    return os.path.basename(path).lower()


def _resolve_shell_path() -> str:
    override = os.environ.get(_SHELL_OVERRIDE_ENV)
    if override:
        return override

    if sys.platform == "win32":
        return os.environ.get("COMSPEC") or "cmd.exe"

    env_shell = os.environ.get("SHELL")
    env_shell_name = _shell_name(env_shell) if env_shell else ""
    preferred_bash = shutil.which("bash")

    # Service environments often export /bin/sh even when bash is present.
    # Treat plain sh as weak evidence so the bash tool behaves closer to its name.
    if env_shell:
        if env_shell_name != "sh":
            return env_shell
        if preferred_bash is None:
            return env_shell

    if sys.platform == "darwin":
        return "/bin/zsh"
    if preferred_bash is not None:
        return preferred_bash
    return "/bin/sh"


def _shell_command_args(shell_path: str, command: str) -> list[str]:
    if sys.platform == "win32":
        return [shell_path, "/c", command]
    return [shell_path, "-c", command]


def _blocked_shell_edit_message(command: str) -> str | None:
    normalized = command.strip()
    for pattern, message in _BLOCKED_EDIT_PATTERNS:
        if pattern.search(normalized):
            return message
    return None


async def handle_bash(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute a shell command and return its output."""
    command = arguments.get("command", "")
    timeout_ms = arguments.get("timeout", _DEFAULT_TIMEOUT_MS)
    workdir = arguments.get("workdir")
    env = arguments.get("env")

    if not command.strip():
        return ToolResult(output="No command provided.", is_error=True)

    blocked_message = _blocked_shell_edit_message(command)
    if blocked_message is not None:
        return ToolResult(output=blocked_message, is_error=True)

    timeout_seconds = max(1, timeout_ms // 1000)

    try:
        resolved_cwd = str(resolve_path(workdir, default_to_home=True))
        if not os.path.isdir(resolved_cwd):
            return ToolResult(
                output=f"Working directory not found: {workdir}",
                is_error=True,
            )
        shell_path = _resolve_shell_path()
        merged_env = (
            {**os.environ, **{str(key): str(value) for key, value in env.items()}}
            if isinstance(env, dict)
            else None
        )
        process = await asyncio.create_subprocess_exec(
            *_shell_command_args(shell_path, command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resolved_cwd,
            env=merged_env,
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
            output=f"Shell executable not found: {shell_path}",
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
