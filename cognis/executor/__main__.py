"""Entry point for ``python -m cognis.executor``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Cognis executor runner")
    parser.add_argument("--controller-url", required=True)
    parser.add_argument("--token", required=False, default="")
    args = parser.parse_args()

    if not args.controller_url.startswith("wss://") and not _is_localhost(args.controller_url):
        sys.stderr.write(
            "ERROR: Remote executor connections require wss:// (TLS). Use ws:// only for localhost connections.\n"
        )
        raise SystemExit(1)

    stdin_token = ""
    if not sys.stdin.isatty():
        with contextlib.suppress(OSError):
            stdin_token = sys.stdin.read().strip()

    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorConfig

    config = ExecutorConfig(
        executor_id="remote",
        controller_url=args.controller_url,
        controller_token=stdin_token or args.token,
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
