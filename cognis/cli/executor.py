"""CLI commands for running a standalone executor process."""

from __future__ import annotations

import contextlib
import logging
import os
import sys

import typer

executor_app = typer.Typer(help="Executor management commands")

# Default fleet pre-warm matrix: (runtime, engine, channel)
_DEFAULT_PREWARM_TARGETS: tuple[tuple[str, str, str | None], ...] = (
    ("playwright", "chromium", None),
    ("playwright", "chromium", "chrome"),
    ("patchright", "chromium", "chrome"),
)


def _setup_logging(level_name: str) -> None:
    """Configure logging for the executor process."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    if level > logging.DEBUG:
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


@executor_app.command("run")
def run_executor(
    controller_url: str | None = typer.Option(
        None,
        "--controller-url",
        help="WebSocket URL of the Cognis controller (or COGNIS_CONTROLLER_URL env var)",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help="JWT authentication token (or COGNIS_EXECUTOR_TOKEN env var)",
    ),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        help="Log level: debug, info, warning, error (or COGNIS_LOG_LEVEL env var, default: info)",
    ),
) -> None:
    """Run a standalone executor process that connects to a Cognis controller.

    Connection parameters can be provided via CLI flags or environment
    variables.  When both are set the CLI flag takes precedence.

    Environment variables::

        COGNIS_CONTROLLER_URL   WebSocket URL of the controller
        COGNIS_EXECUTOR_TOKEN   JWT authentication token
        COGNIS_LOG_LEVEL        Log level (debug, info, warning, error)
    """
    import asyncio

    _setup_logging(log_level or os.environ.get("COGNIS_LOG_LEVEL", "info"))

    url = controller_url or os.environ.get("COGNIS_CONTROLLER_URL", "")
    tok = token or os.environ.get("COGNIS_EXECUTOR_TOKEN", "")

    if not url:
        typer.echo(
            "ERROR: Provide --controller-url or set COGNIS_CONTROLLER_URL.",
            err=True,
        )
        raise typer.Exit(code=1)
    if not tok:
        typer.echo(
            "ERROR: Provide --token or set COGNIS_EXECUTOR_TOKEN.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not url.startswith("wss://") and not _is_localhost(url):
        typer.echo(
            "ERROR: Remote executor connections require wss:// (TLS). "
            "Use ws:// only for localhost connections.",
            err=True,
        )
        raise typer.Exit(code=1)

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


@executor_app.command("browser-install")
def browser_install(
    runtime: str = typer.Option(
        "all",
        "--runtime",
        help="Browser runtime to install: playwright, patchright, or all.",
    ),
    engine: str = typer.Option(
        "chromium",
        "--engine",
        help="Browser engine: chromium, firefox, or webkit (chromium is the only one supported by patchright).",
    ),
    channel: str | None = typer.Option(
        None,
        "--channel",
        help="Browser channel (chrome, msedge, chrome-beta, ...). Leave unset for the bundled engine build.",
    ),
    all_defaults: bool = typer.Option(
        False,
        "--all-defaults",
        help="Pre-install the default fleet matrix: chromium + chrome (both runtimes).",
    ),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        help="Log level: debug, info, warning, error (or COGNIS_LOG_LEVEL env var, default: info).",
    ),
) -> None:
    """Pre-install browser runtimes/binaries on this host.

    Useful for fleet pre-warming, CI builds, and air-gapped deployments where
    you want to avoid the first-session install latency on remote executors.
    """
    import asyncio

    from cognis.tools.executor.browser.install import (
        SUPPORTED_RUNTIMES,
        ensure_browser_runtime,
    )

    _setup_logging(log_level or os.environ.get("COGNIS_LOG_LEVEL", "info"))

    targets: list[tuple[str, str, str | None]]
    if all_defaults:
        targets = list(_DEFAULT_PREWARM_TARGETS)
    elif runtime == "all":
        targets = [(rt, engine, channel) for rt in SUPPORTED_RUNTIMES]
    else:
        targets = [(runtime, engine, channel)]

    async def _install_all() -> int:
        failures = 0
        for rt, eng, ch in targets:
            target_label = f"{rt}/{eng}" + (f"@{ch}" if ch else "")
            typer.echo(f"Installing {target_label} ...")
            ok, reason = await ensure_browser_runtime(
                runtime=rt,
                engine=eng,
                channel=ch,
                auto_install=True,
            )
            if ok:
                typer.echo(f"  -> {reason}")
            else:
                typer.echo(f"  -> FAILED: {reason}", err=True)
                failures += 1
        return failures

    try:
        failures = asyncio.run(_install_all())
    except ValueError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if failures:
        raise typer.Exit(code=1)
