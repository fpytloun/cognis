"""Entry point for ``python -m cognis.executor``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path


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


def _run_browser_install(args: argparse.Namespace) -> int:
    """Pre-install browser runtimes/binaries on the local host."""
    log_level = args.log_level or os.environ.get("COGNIS_LOG_LEVEL", "info")
    _setup_logging(log_level)

    from cognis.tools.executor.browser.install import (
        SUPPORTED_RUNTIMES,
        ensure_browser_runtime,
    )

    if args.all_defaults:
        targets: list[tuple[str, str, str | None]] = [
            ("playwright", "chromium", None),
            ("playwright", "chromium", "chrome"),
            ("patchright", "chromium", "chrome"),
        ]
    elif args.runtime == "all":
        targets = [(rt, args.engine, args.channel or None) for rt in SUPPORTED_RUNTIMES]
    else:
        targets = [(args.runtime, args.engine, args.channel or None)]

    async def _install_all() -> int:
        failures = 0
        for rt, eng, ch in targets:
            target_label = f"{rt}/{eng}" + (f"@{ch}" if ch else "")
            sys.stdout.write(f"Installing {target_label} ...\n")
            sys.stdout.flush()
            ok, reason = await ensure_browser_runtime(
                runtime=rt,
                engine=eng,
                channel=ch,
                auto_install=True,
            )
            if ok:
                sys.stdout.write(f"  -> {reason}\n")
            else:
                sys.stderr.write(f"  -> FAILED: {reason}\n")
                failures += 1
        return failures

    try:
        return asyncio.run(_install_all())
    except ValueError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Cognis executor runner")
    subparsers = parser.add_subparsers(dest="command")

    install_parser = subparsers.add_parser(
        "browser-install",
        help="Pre-install browser runtimes/binaries on this host (fleet pre-warm).",
    )
    install_parser.add_argument(
        "--runtime",
        default="all",
        help="Browser runtime to install: playwright, patchright, or all (default: all).",
    )
    install_parser.add_argument(
        "--engine",
        default="chromium",
        help="Browser engine: chromium, firefox, or webkit (default: chromium).",
    )
    install_parser.add_argument(
        "--channel",
        default="",
        help="Browser channel (chrome, msedge, chrome-beta, ...). Leave empty for bundled.",
    )
    install_parser.add_argument(
        "--all-defaults",
        action="store_true",
        help="Pre-install the default fleet matrix: chromium + chrome (both runtimes).",
    )
    install_parser.add_argument(
        "--log-level",
        default="",
        help="Log level: debug, info, warning, error (default: info).",
    )

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
    parser.add_argument(
        "--workdir",
        required=False,
        default="",
        help="Executor working directory (or COGNIS_EXECUTOR_WORKDIR env var; default: user home)",
    )
    args = parser.parse_args()

    if args.command == "browser-install":
        rc = _run_browser_install(args)
        raise SystemExit(rc)

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

    try:
        os.chdir(_resolve_workdir(args.workdir))
    except ValueError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        raise SystemExit(1) from exc
    except OSError as exc:
        sys.stderr.write(f"ERROR: Failed to change executor working directory: {exc}\n")
        raise SystemExit(1) from exc

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


def _resolve_workdir(cli_workdir: str | None) -> str:
    raw = cli_workdir or os.environ.get("COGNIS_EXECUTOR_WORKDIR") or str(Path.home())
    path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve(strict=False)
    if not path.is_dir():
        raise ValueError(f"Executor working directory does not exist or is not a directory: {raw}")
    return str(path)


if __name__ == "__main__":
    main()
