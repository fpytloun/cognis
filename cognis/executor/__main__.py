"""Entry point for ``python -m cognis.executor``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Cognis executor runner")
    parser.add_argument(
        "--controller-url",
        required=False,
        default="",
        help="WebSocket URL of the Cognis controller (or COGNIS_CONTROLLER_URL env var)",
    )
    parser.add_argument(
        "--token",
        required=False,
        default="",
        help="JWT authentication token (or COGNIS_EXECUTOR_TOKEN env var)",
    )
    args = parser.parse_args()

    # Resolve: CLI flag > env var (> stdin for token)
    url = args.controller_url or os.environ.get("COGNIS_CONTROLLER_URL", "")

    stdin_token = ""
    if not sys.stdin.isatty():
        with contextlib.suppress(OSError):
            stdin_token = sys.stdin.read().strip()

    tok = args.token or os.environ.get("COGNIS_EXECUTOR_TOKEN", "") or stdin_token

    if not url:
        sys.stderr.write("ERROR: Provide --controller-url or set COGNIS_CONTROLLER_URL.\n")
        raise SystemExit(1)
    if not tok:
        sys.stderr.write("ERROR: Provide --token or set COGNIS_EXECUTOR_TOKEN.\n")
        raise SystemExit(1)

    if not url.startswith("wss://") and not _is_localhost(url):
        sys.stderr.write(
            "ERROR: Remote executor connections require wss:// (TLS). "
            "Use ws:// only for localhost connections.\n"
        )
        raise SystemExit(1)

    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorConfig

    config = ExecutorConfig(
        executor_id="remote",
        controller_url=url,
        controller_token=tok,
    )
    runner = ExecutorRunner(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner.run())


def _is_localhost(url: str) -> bool:
    for prefix in ("ws://localhost", "ws://127.0.0.1", "ws://[::1]", "ws://0.0.0.0"):
        if url.startswith(prefix):
            return True
    return False


if __name__ == "__main__":
    main()
