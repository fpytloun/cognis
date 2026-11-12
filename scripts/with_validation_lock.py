#!/usr/bin/env python3
"""Serialize local validation across worktrees on Linux and macOS."""

from __future__ import annotations

import argparse
import fcntl
import math
import os
import stat
import sys
import time
from pathlib import Path


def nonnegative_seconds(value: str) -> float:
    """Parse a finite, nonnegative duration."""
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return seconds


def main() -> int:
    """Acquire the shared lock, report contention, and replace this process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=nonnegative_seconds, default=3600)
    parser.add_argument(
        "--lock-file", type=Path, default=Path(f"/tmp/cognis-validation-{os.getuid()}.lock")
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    try:
        descriptor = os.open(args.lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("validation lock must be a regular file owned by the current user")
            started = time.monotonic()
            next_status = started
            waiting = False
            acquired = False
            while True:
                now = time.monotonic()
                if waiting and now - started >= args.timeout:
                    break
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    waiting = True
                    if now >= next_status:
                        lock.seek(0)
                        owner = lock.read(32).strip()
                        owner = owner if owner.isdecimal() else "pending"
                        sys.stderr.write(
                            f"Waiting for validation lock {args.lock_file}; locked by PID {owner}; "
                            f"waited {now - started:.1f}s / {args.timeout:g}s.\n"
                        )
                        sys.stderr.flush()
                        next_status = now + 300
                    remaining = args.timeout - (now - started)
                    if remaining <= 0:
                        break
                    time.sleep(min(0.25, remaining))
                else:
                    # A suspended waiter can resume even after the owner exits.
                    # Recheck after flock too, before admitting its command.
                    if waiting and time.monotonic() - started >= args.timeout:
                        break
                    acquired = True
                    break

            if not acquired:
                sys.stderr.write("Validation lock wait timed out; command was not started.\n")
                return 75

            lock.seek(0)
            lock.truncate()
            lock.write(f"{os.getpid()}\n")
            lock.flush()
            sys.stderr.write(
                f"Acquired validation lock {args.lock_file}; locked by PID {os.getpid()}.\n"
            )
            sys.stderr.flush()
            # exec preserves signals and exit codes. Keep the kernel lock attached
            # to the command (and inheriting children), not an expiring PID file.
            os.set_inheritable(lock.fileno(), True)
            os.execvp(command[0], command)
    except OSError as exc:
        sys.stderr.write(f"Validation lock/command failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
