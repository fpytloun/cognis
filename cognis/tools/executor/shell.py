"""Executor-native shell tools."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import sys
from dataclasses import dataclass, field
from time import time
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

_DEFAULT_TIMEOUT_MS = 120_000
_DEFAULT_BACKGROUND_TIMEOUT_MS = 5_000
_SHELL_OVERRIDE_ENV = "COGNIS_EXECUTOR_SHELL"
SHELL_MANAGER_KEY = "shell_session_manager"
_MAX_BACKGROUND_OUTPUT_CHARS = 200_000
_BLOCKED_EDIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r'(^|\s)sed\s+-i(?:[\s\'"]|$)'),
        "Use edit, multiedit, apply_patch, or write instead of sed -i for file content changes.",
    ),
    (
        re.compile(r'(^|\s)perl\s+-pi(?:[\s\'"]|$)'),
        "Use edit, multiedit, apply_patch, or write instead of perl -pi for file content changes.",
    ),
    (
        re.compile(r'(^|\s)ruby\s+-pi(?:[\s\'"]|$)'),
        "Use edit, multiedit, apply_patch, or write instead of ruby -pi for file content changes.",
    ),
    (
        re.compile(
            r"(^|\s)python(?:3)?\s+-c\s+.*(write_text|write_bytes|open\s*\([^\)]*,\s*['\"](?:w|a|x|w\+|a\+|x\+)['\"]).*",
            re.DOTALL,
        ),
        "Use edit, multiedit, apply_patch, or write instead of Python one-liners that rewrite files.",
    ),
    (
        re.compile(
            r"(^|\s)python(?:3)?\s+.*<<[-~]?['\"]?(?:PY|EOF)['\"]?.*(write_text|write_bytes|open\s*\([^\)]*,\s*['\"](?:w|a|x|w\+|a\+|x\+)['\"]).*",
            re.DOTALL,
        ),
        "Use edit, multiedit, apply_patch, or write instead of embedded Python scripts that rewrite files.",
    ),
    (
        re.compile(r"(?:>>|>)\s*[^\s]+\.(?:py|js|jsx|ts|tsx|json|md|ya?ml|html|css|scss|toml)\b"),
        "Use edit, multiedit, apply_patch, or write instead of shell redirection to rewrite source files.",
    ),
    (
        re.compile(r"(^|\s)tee\s+[^\n]*\.(?:py|js|jsx|ts|tsx|json|md|ya?ml|html|css|scss|toml)\b"),
        "Use edit, multiedit, apply_patch, or write instead of tee to rewrite source files.",
    ),
)
_SHELL_PARSE_ERROR_PATTERN = re.compile(
    r"syntax error near unexpected token|parse error near|unexpected EOF|unexpected end of file",
    re.IGNORECASE,
)


@dataclass(slots=True)
class _BackgroundShellSession:
    shell_id: str
    command: str
    cwd: str
    process: asyncio.subprocess.Process
    created_at: float = field(default_factory=time)
    output: str = ""
    base_offset: int = 0
    exit_code: int | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    readers: list[asyncio.Task[Any]] = field(default_factory=list)

    async def append(self, text: str) -> None:
        if not text:
            return
        async with self.lock:
            self.output += text
            overflow = len(self.output) - _MAX_BACKGROUND_OUTPUT_CHARS
            if overflow > 0:
                self.output = self.output[overflow:]
                self.base_offset += overflow

    async def snapshot(self, cursor: int) -> tuple[str, int, bool, int | None, bool]:
        async with self.lock:
            effective_cursor = max(cursor, self.base_offset)
            start = max(0, effective_cursor - self.base_offset)
            chunk = self.output[start:]
            next_cursor = self.base_offset + len(self.output)
            truncated = cursor < self.base_offset
            return chunk, next_cursor, self.done.is_set(), self.exit_code, truncated


class _BackgroundShellManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _BackgroundShellSession] = {}

    async def start(
        self,
        *,
        shell_id: str,
        command: str,
        cwd: str,
        process: asyncio.subprocess.Process,
    ) -> _BackgroundShellSession:
        session = _BackgroundShellSession(
            shell_id=shell_id,
            command=command,
            cwd=cwd,
            process=process,
        )
        session.readers = [
            asyncio.create_task(self._consume_stream(session, process.stdout, prefix="")),
            asyncio.create_task(self._consume_stream(session, process.stderr, prefix="STDERR:\n")),
            asyncio.create_task(self._wait_for_exit(session)),
        ]
        self._sessions[shell_id] = session
        return session

    async def read(self, shell_id: str, *, cursor: int) -> ToolResult:
        session = self._sessions.get(shell_id)
        if session is None:
            return ToolResult(output=f"Unknown shell_id: {shell_id}", is_error=True)
        chunk, next_cursor, done, exit_code, truncated = await session.snapshot(cursor)
        status = "completed" if done else "running"
        prefix = ""
        if truncated:
            prefix = f"[output before cursor {cursor} was trimmed; earliest available cursor is {session.base_offset}]\n"
        payload = prefix + (chunk if chunk else "(no new output)")
        metadata = {
            "shell_id": shell_id,
            "status": status,
            "cursor": next_cursor,
            "exit_code": exit_code,
        }
        if done and exit_code not in {None, 0}:
            metadata["ok"] = False
        return ToolResult(
            output=payload, is_error=done and exit_code not in {None, 0}, metadata=metadata
        )

    async def kill(self, shell_id: str) -> ToolResult:
        session = self._sessions.get(shell_id)
        if session is None:
            return ToolResult(output=f"Unknown shell_id: {shell_id}", is_error=True)
        await _kill_process_tree(session.process)
        await self._finalize_session(session)
        return ToolResult(
            output=f"Stopped background shell {shell_id}.",
            metadata={"shell_id": shell_id, "status": "killed", "exit_code": session.exit_code},
        )

    async def cleanup(self) -> None:
        for session in list(self._sessions.values()):
            await _kill_process_tree(session.process)
            await self._finalize_session(session)

    async def _consume_stream(
        self,
        session: _BackgroundShellSession,
        stream: asyncio.StreamReader | None,
        *,
        prefix: str,
    ) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                if prefix:
                    text = prefix + text
                await session.append(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _wait_for_exit(self, session: _BackgroundShellSession) -> None:
        try:
            await session.process.wait()
        finally:
            await self._finalize_session(session)

    async def _finalize_session(self, session: _BackgroundShellSession) -> None:
        if session.done.is_set():
            return
        session.exit_code = session.process.returncode
        session.done.set()


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


def _background_shell_manager(context: ToolExecutionContext) -> _BackgroundShellManager:
    metadata = (
        context.shared_runtime_metadata
        if context.shared_runtime_metadata is not None
        else context.runtime_metadata
    )
    manager = metadata.get(SHELL_MANAGER_KEY)
    if isinstance(manager, _BackgroundShellManager):
        return manager
    manager = _BackgroundShellManager()
    metadata[SHELL_MANAGER_KEY] = manager
    return manager


def _shell_parse_error_hint(stderr_text: str) -> str | None:
    if not _SHELL_PARSE_ERROR_PATTERN.search(stderr_text):
        return None
    return (
        "Hint: This command is parsed by the shell. Quote literal paths or arguments "
        "containing parentheses, spaces, globs, $, or other shell metacharacters."
    )


async def cleanup_shell_manager(runtime_metadata: dict[str, Any]) -> None:
    """Stop any background shell sessions stored in runtime metadata."""

    manager = runtime_metadata.get(SHELL_MANAGER_KEY)
    if isinstance(manager, _BackgroundShellManager):
        await manager.cleanup()


async def _create_process(
    *,
    shell_path: str,
    command: str,
    cwd: str,
    env: dict[str, str] | None,
) -> asyncio.subprocess.Process:
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": cwd,
        "env": env,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_exec(*_shell_command_args(shell_path, command), **kwargs)


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError):
        await process.wait()


def _command_metadata(command: str, cwd: str, *, ok: bool, exit_code: int | None) -> dict[str, Any]:
    return {
        "program": command.split(maxsplit=1)[0] if command.strip() else "",
        "cwd": cwd,
        "ok": ok,
        "exit_code": exit_code,
    }


async def handle_bash(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute a shell command and return its output."""
    command = str(arguments.get("command", ""))
    timeout_ms = arguments.get("timeout", _DEFAULT_TIMEOUT_MS)
    workdir = arguments.get("workdir")
    env = arguments.get("env")
    run_in_background = bool(arguments.get("run_in_background", False))

    if not command.strip():
        return ToolResult(output="No command provided.", is_error=True)

    blocked_message = _blocked_shell_edit_message(command)
    if blocked_message is not None:
        return ToolResult(output=blocked_message, is_error=True)

    timeout_seconds = max(1, int(timeout_ms) // 1000)

    try:
        resolved_cwd = str(resolve_path(workdir, context=context, default_to_home=True))
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    if not os.path.isdir(resolved_cwd):
        return ToolResult(output=f"Working directory not found: {workdir}", is_error=True)

    shell_path = _resolve_shell_path()
    merged_env = (
        {**os.environ, **{str(key): str(value) for key, value in env.items()}}
        if isinstance(env, dict)
        else None
    )

    try:
        process = await _create_process(
            shell_path=shell_path,
            command=command,
            cwd=resolved_cwd,
            env=merged_env,
        )
    except FileNotFoundError:
        return ToolResult(output=f"Shell executable not found: {shell_path}", is_error=True)
    except OSError as exc:
        return ToolResult(output=f"Command execution failed: {exc}", is_error=True)

    if run_in_background:
        manager = _background_shell_manager(context)
        shell_id = f"shell_{os.urandom(6).hex()}"
        session = await manager.start(
            shell_id=shell_id,
            command=command,
            cwd=resolved_cwd,
            process=process,
        )
        await asyncio.sleep(min(timeout_seconds, _DEFAULT_BACKGROUND_TIMEOUT_MS // 1000))
        initial_output, cursor, done, exit_code, _ = await session.snapshot(0)
        status = "completed" if done else "running"
        preview = initial_output.strip() or "(no initial output yet)"
        return ToolResult(
            output=(
                f"Started background shell {shell_id}.\n"
                f"Status: {status}\n"
                f"Use bash_output with shell_id='{shell_id}' to read output and bash_kill to stop it.\n\n"
                f"Initial output:\n{preview}"
            ),
            is_error=done and exit_code not in {None, 0},
            metadata={
                "shell_id": shell_id,
                "status": status,
                "cursor": cursor,
                "commands": [
                    _command_metadata(
                        command, resolved_cwd, ok=not done or exit_code == 0, exit_code=exit_code
                    )
                ],
            },
        )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await _kill_process_tree(process)
        return ToolResult(output=f"Command timed out after {timeout_seconds}s.", is_error=True)

    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
    exit_code = process.returncode or 0
    shell_hint = _shell_parse_error_hint(stderr_text)

    parts: list[str] = []
    if stdout_text:
        parts.append(stdout_text)
    if stderr_text:
        parts.append(f"STDERR:\n{stderr_text}")
    if shell_hint:
        parts.append(shell_hint)
    if exit_code != 0:
        parts.append(f"\nExit code: {exit_code}")

    output = "\n".join(parts) if parts else "(no output)"
    return ToolResult(
        output=output,
        is_error=exit_code != 0,
        metadata={
            "commands": [
                _command_metadata(command, resolved_cwd, ok=exit_code == 0, exit_code=exit_code)
            ]
        },
    )


async def handle_bash_output(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Return new output for a background shell session."""
    shell_id = str(arguments.get("shell_id", "")).strip()
    cursor = int(arguments.get("cursor", 0) or 0)
    if not shell_id:
        return ToolResult(output="shell_id is required.", is_error=True)
    manager = _background_shell_manager(context)
    return await manager.read(shell_id, cursor=cursor)


async def handle_bash_kill(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Stop a background shell session."""
    shell_id = str(arguments.get("shell_id", "")).strip()
    if not shell_id:
        return ToolResult(output="shell_id is required.", is_error=True)
    manager = _background_shell_manager(context)
    return await manager.kill(shell_id)
