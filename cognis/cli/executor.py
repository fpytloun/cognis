"""CLI commands for running a standalone executor process."""

from __future__ import annotations

import contextlib

import typer

executor_app = typer.Typer(help="Executor management commands")


@executor_app.command("run")
def run_executor(
    controller_url: str = typer.Option(
        ..., "--controller-url", help="WebSocket URL of the Cognis controller"
    ),
    token: str = typer.Option(..., "--token", help="JWT authentication token"),
) -> None:
    """Run a standalone executor process that connects to a Cognis controller."""
    import asyncio

    if not controller_url.startswith("wss://") and not _is_localhost(controller_url):
        typer.echo(
            "ERROR: Remote executor connections require wss:// (TLS). Use ws:// only for localhost connections.",
            err=True,
        )
        raise typer.Exit(code=1)

    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorConfig

    config = ExecutorConfig(
        executor_id="remote",
        controller_url=controller_url,
        controller_token=token,
    )
    runner = ExecutorRunner(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner.run())


def _is_localhost(url: str) -> bool:
    for prefix in ("ws://localhost", "ws://127.0.0.1", "ws://[::1]", "ws://0.0.0.0"):
        if url.startswith(prefix):
            return True
    return False
