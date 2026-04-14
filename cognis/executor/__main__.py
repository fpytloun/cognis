"""Entry point for ``python -m cognis.executor``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys


def _setup_logging(level_name: str) -> None:
    """Configure logging for the executor process."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    # Quiet noisy third-party loggers unless debug
    if level > logging.DEBUG:
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


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
    parser.add_argument(
        "--log-level",
        required=False,
        default="",
        help="Log level: debug, info, warning, error (or COGNIS_LOG_LEVEL env var, default: info)",
    )
    args = parser.parse_args()

    log_level = args.log_level or os.environ.get("COGNIS_LOG_LEVEL", "info")
    _setup_logging(log_level)

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
    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        asyncio.run(runner.run())


def _is_localhost(url: str) -> bool:
    for prefix in ("ws://localhost", "ws://127.0.0.1", "ws://[::1]", "ws://0.0.0.0"):
        if url.startswith(prefix):
            return True
    return False


if __name__ == "__main__":
    main()
