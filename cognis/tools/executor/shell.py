"""Executor-native shell tools."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import time
from typing import Any, cast

from cognis.models.tool import ToolResult
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

_DEFAULT_TIMEOUT_MS = 120_000
_DEFAULT_BACKGROUND_TIMEOUT_MS = 5_000
_MAX_FOREGROUND_TIMEOUT_MS = 3_600_000
_FOREGROUND_TIMEOUT_CLEANUP_GRACE_SECONDS = 2
_PROCESS_KILL_WAIT_SECONDS = 5
_FOREGROUND_OUTPUT_HEAD_CHARS = 100_000
_FOREGROUND_OUTPUT_TAIL_CHARS = 300_000
_SHELL_OVERRIDE_ENV = "COGNIS_EXECUTOR_SHELL"
SHELL_MANAGER_KEY = "shell_session_manager"
_MAX_BACKGROUND_OUTPUT_CHARS = 200_000
# Hard blocks are reserved for clearly unsafe shell patterns. Source-file rewrite
# shortcuts are advisory so intentional commands can still run.
_BLOCKED_EDIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = ()
_SOURCE_REWRITE_ADVISORY = (
    "Prefer dedicated edit tools for rewriting source files. "
    "Use shell or interpreter rewrites only when they are necessary and intentional."
)
_BACKGROUND_SHELL_OUTPUT_REMINDER = (
    "Completion reminder: the parent conversation will be notified/resumed when this "
    "background command finishes. If there is nothing else independently actionable, "
    "end this turn now instead of polling or duplicating the same work."
)
_BACKGROUND_SHELL_OUTPUT_SHORT_REMINDER = (
    "Background command is running; use bash_output only when you need new output."
)
_SOURCE_REWRITE_ADVISORY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'(^|\s)sed\s+-i(?:[\s\'"]|$)'),
    re.compile(r'(^|\s)perl\s+-pi(?:[\s\'"]|$)'),
    re.compile(r'(^|\s)ruby\s+-pi(?:[\s\'"]|$)'),
    re.compile(
        r"(^|\s)python(?:3)?\s+-c\s+.*(write_text|write_bytes|open\s*\([^\)]*,\s*['\"](?:w|a|x|w\+|a\+|x\+)['\"]).*",
        re.DOTALL,
    ),
    re.compile(
        r"(^|\s)python(?:3)?\s+.*<<[-~]?['\"]?(?:PY|EOF)['\"]?.*(write_text|write_bytes|open\s*\([^\)]*,\s*['\"](?:w|a|x|w\+|a\+|x\+)['\"]).*",
        re.DOTALL,
    ),
    re.compile(r"(?:>>|>)\s*[^\s]+\.(?:py|js|jsx|ts|tsx|json|md|ya?ml|html|css|scss|toml)\b"),
    re.compile(r"(^|\s)tee\s+[^\n]*\.(?:py|js|jsx|ts|tsx|json|md|ya?ml|html|css|scss|toml)\b"),
)
_SHELL_PARSE_ERROR_PATTERN = re.compile(
    r"syntax error near unexpected token|parse error near|unexpected EOF|unexpected end of file",
    re.IGNORECASE,
)
_BACKGROUND_COMPLETION_CALLBACK_KEY = "background_shell_completion_callback"
BackgroundShellCompletionCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class _ForegroundOutputBuffer:
    head_limit: int = _FOREGROUND_OUTPUT_HEAD_CHARS
    tail_limit: int = _FOREGROUND_OUTPUT_TAIL_CHARS
    head: str = ""
    tail: str = ""
    total_chars: int = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self.total_chars += len(text)
        remaining_head = self.head_limit - len(self.head)
        if remaining_head > 0:
            self.head += text[:remaining_head]
            text = text[remaining_head:]
        if text:
            self.tail = (self.tail + text)[-self.tail_limit :]

    def render(self) -> str:
        retained = len(self.head) + len(self.tail)
        if self.total_chars <= retained:
            return self.head + self.tail
        omitted = self.total_chars - retained
        marker = (
            f"\n... foreground output truncated; omitted {omitted} chars "
            f"between head {len(self.head)} chars and tail {len(self.tail)} chars ...\n"
        )
        return self.head + marker + self.tail


@dataclass(slots=True)
class _BackgroundShellSession:
    shell_id: str
    command: str
    description: str | None
    cwd: str
    process: asyncio.subprocess.Process
    executor_id: str | None = None
    executor_type: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    call_id: str | None = None
    agent_id: str | None = None
    advisory: str | None = None
    completion_callback: BackgroundShellCompletionCallback | None = None
    created_at: float = field(default_factory=time)
    last_activity_at: float = field(default_factory=time)
    output: str = ""
    base_offset: int = 0
    exit_code: int | None = None
    completion_reason: str | None = None
    completion_notified: bool = False
    completion_notify_in_progress: bool = False
    completion_notify_enabled: bool = True
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    readers: list[asyncio.Task[Any]] = field(default_factory=list)

    async def append(self, text: str) -> None:
        if not text:
            return
        async with self.lock:
            self.output += text
            self.last_activity_at = time()
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

    async def status_snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        current = now if now is not None else time()
        async with self.lock:
            output_chars = len(self.output)
            tail = self.output[-1200:]
            cursor = self.base_offset + output_chars
            trimmed_chars = self.base_offset
        done = self.done.is_set()
        status = "completed" if done else "running"
        if done and self.completion_reason == "killed":
            status = "killed"
        elif done and self.exit_code not in {None, 0}:
            status = "failed"
        return {
            "shell_id": self.shell_id,
            "command": self.command,
            "description": self.description,
            "cwd": self.cwd,
            "pid": self.process.pid,
            "executor_id": self.executor_id,
            "executor_type": self.executor_type,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "call_id": self.call_id,
            "agent_id": self.agent_id,
            "advisory": self.advisory,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "runtime_seconds": max(0.0, current - self.created_at),
            "idle_seconds": max(0.0, current - self.last_activity_at),
            "output_chars": output_chars,
            "trimmed_chars": trimmed_chars,
            "cursor": cursor,
            "status": status,
            "done": done,
            "exit_code": self.exit_code,
            "completion_reason": self.completion_reason,
            "output_tail": tail,
        }


class _BackgroundShellManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _BackgroundShellSession] = {}

    async def start(
        self,
        *,
        shell_id: str,
        command: str,
        description: str | None,
        cwd: str,
        process: asyncio.subprocess.Process,
        executor_id: str | None = None,
        executor_type: str | None = None,
        conversation_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        call_id: str | None = None,
        agent_id: str | None = None,
        advisory: str | None = None,
        completion_callback: BackgroundShellCompletionCallback | None = None,
    ) -> _BackgroundShellSession:
        session = _BackgroundShellSession(
            shell_id=shell_id,
            command=command,
            description=description,
            cwd=cwd,
            process=process,
            executor_id=executor_id,
            executor_type=executor_type,
            conversation_id=conversation_id,
            session_id=session_id,
            turn_id=turn_id,
            call_id=call_id,
            agent_id=agent_id,
            advisory=advisory,
            completion_callback=completion_callback,
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
        session.completion_notify_enabled = False
        await _kill_process_tree(session.process)
        await self._finalize_session(session, reason="killed")
        return ToolResult(
            output=f"Stopped background shell {shell_id}.",
            metadata={"shell_id": shell_id, "status": "killed", "exit_code": session.exit_code},
        )

    async def cleanup(self) -> None:
        for session in list(self._sessions.values()):
            session.completion_notify_enabled = False
            await _kill_process_tree(session.process)
            await self._finalize_session(session, reason="cleanup")

    def set_completion_callback(self, callback: BackgroundShellCompletionCallback | None) -> None:
        for session in self._sessions.values():
            session.completion_callback = callback

    async def notify_pending_completions(self) -> None:
        for session in list(self._sessions.values()):
            if session.done.is_set():
                await self._notify_completion(session)

    async def list_statuses(self, *, include_completed: bool = False) -> list[dict[str, Any]]:
        now = time()
        statuses: list[dict[str, Any]] = []
        for session in list(self._sessions.values()):
            if not include_completed and session.done.is_set():
                continue
            statuses.append(await session.status_snapshot(now=now))
        statuses.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return statuses

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
            await self._finalize_session(session, reason="completed")

    async def _finalize_session(
        self, session: _BackgroundShellSession, *, reason: str = "completed"
    ) -> None:
        if session.done.is_set():
            return
        session.exit_code = session.process.returncode
        session.completion_reason = reason
        session.done.set()
        await self._notify_completion(session)

    async def _notify_completion(self, session: _BackgroundShellSession) -> None:
        async with session.lock:
            if (
                not session.completion_notify_enabled
                or session.completion_notified
                or session.completion_notify_in_progress
                or session.completion_callback is None
            ):
                return
            callback = session.completion_callback
            session.completion_notify_in_progress = True
        failed = False
        try:
            await callback(await session.status_snapshot())
        except Exception:
            failed = True
        else:
            async with session.lock:
                session.completion_notified = True
        finally:
            async with session.lock:
                session.completion_notify_in_progress = False
        if failed:
            async with session.lock:
                should_retry = (
                    not session.completion_notified
                    and session.completion_callback is not None
                    and session.completion_callback is not callback
                )
            if should_retry:
                await self._notify_completion(session)


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


def _shell_source_rewrite_advisory(command: str) -> str | None:
    normalized = command.strip()
    if any(pattern.search(normalized) for pattern in _SOURCE_REWRITE_ADVISORY_PATTERNS):
        return _SOURCE_REWRITE_ADVISORY
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


async def list_background_shell_statuses(
    runtime_metadata: dict[str, Any], *, include_completed: bool = False
) -> list[dict[str, Any]]:
    """Return background shell statuses stored in runtime metadata."""

    manager = runtime_metadata.get(SHELL_MANAGER_KEY)
    if not isinstance(manager, _BackgroundShellManager):
        return []
    return await manager.list_statuses(include_completed=include_completed)


def set_background_shell_completion_callback(
    runtime_metadata: dict[str, Any],
    callback: BackgroundShellCompletionCallback | None,
) -> None:
    """Install or clear the background shell completion callback."""

    if callback is None:
        runtime_metadata.pop(_BACKGROUND_COMPLETION_CALLBACK_KEY, None)
    else:
        runtime_metadata[_BACKGROUND_COMPLETION_CALLBACK_KEY] = callback
    manager = runtime_metadata.get(SHELL_MANAGER_KEY)
    if isinstance(manager, _BackgroundShellManager):
        manager.set_completion_callback(callback)


async def notify_pending_background_shell_completions(runtime_metadata: dict[str, Any]) -> None:
    """Retry completion notifications that could not be sent while disconnected."""

    manager = runtime_metadata.get(SHELL_MANAGER_KEY)
    if isinstance(manager, _BackgroundShellManager):
        await manager.notify_pending_completions()


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
    with contextlib.suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_KILL_WAIT_SECONDS)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if sys.platform != "win32" and hasattr(process, "pid"):
            os.killpg(process.pid, signal.SIGTERM)
        elif hasattr(process, "terminate"):
            process.terminate()
        else:
            await _kill_process_tree(process)
            return
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=_FOREGROUND_TIMEOUT_CLEANUP_GRACE_SECONDS)
        return
    await _kill_process_tree(process)


async def _cleanup_process_tree(process: asyncio.subprocess.Process) -> None:
    await _terminate_process_tree(process)


def _parse_timeout(timeout_ms: Any, *, run_in_background: bool) -> tuple[int | None, str | None]:
    try:
        if isinstance(timeout_ms, bool):
            raise TypeError
        parsed_timeout_ms = int(timeout_ms)
    except (TypeError, ValueError):
        return None, "Timeout must be an integer number of milliseconds."

    if not run_in_background and parsed_timeout_ms > _MAX_FOREGROUND_TIMEOUT_MS:
        return (
            None,
            f"Foreground bash timeout may not exceed {_MAX_FOREGROUND_TIMEOUT_MS} ms. "
            "Use run_in_background=true for longer-running commands.",
        )

    return max(1, math.ceil(parsed_timeout_ms / 1000)), None


def _command_metadata(command: str, cwd: str, *, ok: bool, exit_code: int | None) -> dict[str, Any]:
    return {
        "program": command.split(maxsplit=1)[0] if command.strip() else "",
        "cwd": cwd,
        "ok": ok,
        "exit_code": exit_code,
    }


async def _read_process_stream(
    stream: asyncio.StreamReader | None,
    *,
    stream_name: str,
    chunks: _ForegroundOutputBuffer,
    context: ToolExecutionContext,
    mirror_chunks: _ForegroundOutputBuffer | None = None,
) -> None:
    if stream is None:
        return
    while True:
        data = await stream.read(4096)
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        chunks.append(text)
        if mirror_chunks is not None:
            mirror_chunks.append(text)
        if context.output_chunk_callback is not None:
            await context.output_chunk_callback(text, stream_name)


async def handle_bash(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute a shell command and return its output."""
    command = str(arguments.get("command", ""))
    raw_description = arguments.get("description")
    description = (
        str(raw_description).strip()
        if isinstance(raw_description, str) and raw_description.strip()
        else None
    )
    timeout_ms = arguments.get("timeout", _DEFAULT_TIMEOUT_MS)
    workdir = arguments.get("workdir")
    env = arguments.get("env")
    run_in_background = bool(arguments.get("run_in_background", False))

    if not command.strip():
        return ToolResult(output="No command provided.", is_error=True)

    blocked_message = _blocked_shell_edit_message(command)
    if blocked_message is not None:
        return ToolResult(output=blocked_message, is_error=True)
    advisory = _shell_source_rewrite_advisory(command)

    timeout_seconds, timeout_error = _parse_timeout(timeout_ms, run_in_background=run_in_background)
    if timeout_error is not None:
        return ToolResult(output=timeout_error, is_error=True)
    assert timeout_seconds is not None

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
        runtime_access = context.runtime_metadata.get("runtime_access")
        runtime_access = runtime_access if isinstance(runtime_access, dict) else {}
        raw_callback = context.runtime_metadata.get(_BACKGROUND_COMPLETION_CALLBACK_KEY)
        shared_metadata = context.shared_runtime_metadata
        if raw_callback is None and shared_metadata is not None:
            raw_callback = shared_metadata.get(_BACKGROUND_COMPLETION_CALLBACK_KEY)
        callback = (
            cast(BackgroundShellCompletionCallback, raw_callback)
            if callable(raw_callback)
            else None
        )
        session = await manager.start(
            shell_id=shell_id,
            command=command,
            description=description,
            cwd=resolved_cwd,
            process=process,
            executor_id=context.executor_handle.executor_id,
            executor_type=context.executor_handle.executor_type,
            conversation_id=(
                runtime_access.get("conversation_id")
                if isinstance(runtime_access.get("conversation_id"), str)
                else None
            ),
            session_id=(
                runtime_access.get("session_id")
                if isinstance(runtime_access.get("session_id"), str)
                else None
            ),
            turn_id=(
                context.runtime_metadata.get("turn_id")
                if isinstance(context.runtime_metadata.get("turn_id"), str)
                else None
            ),
            call_id=(
                context.runtime_metadata.get("tool_call_id")
                if isinstance(context.runtime_metadata.get("tool_call_id"), str)
                else None
            ),
            agent_id=(
                runtime_access.get("agent_id")
                if isinstance(runtime_access.get("agent_id"), str)
                else None
            ),
            advisory=advisory,
            completion_callback=callback,
        )
        await asyncio.sleep(min(timeout_seconds, _DEFAULT_BACKGROUND_TIMEOUT_MS // 1000))
        initial_output, cursor, done, exit_code, _ = await session.snapshot(0)
        status = "completed" if done else "running"
        preview = initial_output.strip() or "(no initial output yet)"
        description_line = f"Description: {description}\n" if description else ""
        advisory_line = f"Advisory: {advisory}\n" if advisory else ""
        reminder = _background_shell_reminder(context)
        return ToolResult(
            output=(
                advisory_line + f"Started background shell {shell_id}.\n"
                f"Status: {status}\n"
                f"{description_line}"
                f"Use bash_output with shell_id='{shell_id}' to read output and bash_kill to stop it.\n\n"
                f"{reminder}\n\n"
                f"Initial output:\n{preview}"
            ),
            is_error=done and exit_code not in {None, 0},
            metadata={
                "shell_id": shell_id,
                "status": status,
                "cursor": cursor,
                "description": description,
                "pid": process.pid,
                "executor_id": context.executor_handle.executor_id,
                "executor_type": context.executor_handle.executor_type,
                "advisory": advisory,
                "commands": [
                    _command_metadata(
                        command, resolved_cwd, ok=not done or exit_code == 0, exit_code=exit_code
                    )
                ],
            },
        )

    output_chunks = _ForegroundOutputBuffer()
    stderr_chunks = _ForegroundOutputBuffer()
    try:
        if (
            not hasattr(process, "stdout")
            or not hasattr(process, "stderr")
            or not hasattr(process, "wait")
        ):
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
            output_chunks.append(stdout_data.decode("utf-8", errors="replace"))
            stderr_text = stderr_data.decode("utf-8", errors="replace")
            output_chunks.append(stderr_text)
            stderr_chunks.append(stderr_text)
        else:
            await asyncio.wait_for(
                asyncio.gather(
                    _read_process_stream(
                        process.stdout,
                        stream_name="stdout",
                        chunks=output_chunks,
                        context=context,
                    ),
                    _read_process_stream(
                        process.stderr,
                        stream_name="stderr",
                        chunks=output_chunks,
                        context=context,
                        mirror_chunks=stderr_chunks,
                    ),
                    process.wait(),
                ),
                timeout=timeout_seconds,
            )
    except TimeoutError:
        await _cleanup_process_tree(process)
        return ToolResult(
            output=f"Command timed out after {timeout_seconds}s; sent SIGTERM, then SIGKILL if still running.",
            is_error=True,
            metadata={
                "status": "timed_out",
                "timeout_seconds": timeout_seconds,
                "process_cleanup": "terminated_then_killed",
                "commands": [
                    _command_metadata(command, resolved_cwd, ok=False, exit_code=process.returncode)
                ],
            },
        )
    except asyncio.CancelledError:
        await _cleanup_process_tree(process)
        raise

    terminal_text = output_chunks.render()
    stderr_text = stderr_chunks.render()
    exit_code = process.returncode or 0
    shell_hint = _shell_parse_error_hint(stderr_text)

    parts: list[str] = []
    if advisory:
        parts.append(f"Advisory: {advisory}")
    if terminal_text:
        parts.append(terminal_text)
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
            ],
            "advisory": advisory,
        },
    )


def _background_shell_reminder(context: ToolExecutionContext) -> str:
    if context.runtime_metadata.get("_background_shell_full_reminder_sent"):
        return _BACKGROUND_SHELL_OUTPUT_SHORT_REMINDER
    context.runtime_metadata["_background_shell_full_reminder_sent"] = True
    return _BACKGROUND_SHELL_OUTPUT_REMINDER


async def handle_bash_output(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Return new output for a background shell session."""
    shell_id = str(arguments.get("shell_id", "")).strip()
    cursor = int(arguments.get("cursor", 0) or 0)
    filter_regex = arguments.get("filter_regex")
    if not shell_id:
        return ToolResult(output="shell_id is required.", is_error=True)
    manager = _background_shell_manager(context)
    result = await manager.read(shell_id, cursor=cursor)
    if filter_regex is None or result.is_error:
        return result
    try:
        regex = re.compile(str(filter_regex), flags=re.IGNORECASE)
    except re.error as exc:
        return ToolResult(output=f"Invalid filter_regex: {exc}", is_error=True)
    filtered_lines = [line for line in result.output.splitlines() if regex.search(line)]
    metadata = dict(result.metadata or {})
    metadata["filter_regex"] = str(filter_regex)
    return result.model_copy(
        update={
            "output": "\n".join(filtered_lines) if filtered_lines else "(no matching output)",
            "metadata": metadata,
        }
    )


async def handle_bash_kill(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Stop a background shell session."""
    shell_id = str(arguments.get("shell_id", "")).strip()
    if not shell_id:
        return ToolResult(output="shell_id is required.", is_error=True)
    manager = _background_shell_manager(context)
    return await manager.kill(shell_id)
