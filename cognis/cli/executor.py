"""CLI commands for running a standalone executor process."""

from __future__ import annotations

import contextlib
import os

import typer

executor_app = typer.Typer(help="Executor management commands")


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
) -> None:
    """Run a standalone executor process that connects to a Cognis controller.

    Connection parameters can be provided via CLI flags or environment
    variables.  When both are set the CLI flag takes precedence.

    Environment variables::

        COGNIS_CONTROLLER_URL   WebSocket URL of the controller
        COGNIS_EXECUTOR_TOKEN   JWT authentication token
    """
    import asyncio

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
