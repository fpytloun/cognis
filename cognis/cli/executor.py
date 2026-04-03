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
    executor_id: str = typer.Option(
        ..., "--executor-id", help="Executor ID (must match DB registration)"
    ),
    tools: str = typer.Option("*", "--tools", help="Comma-separated tool names or '*' for all"),
    inference_endpoint: str | None = typer.Option(
        None, "--inference-endpoint", help="Local LLM endpoint URL"
    ),
    inference_model: str | None = typer.Option(
        None, "--inference-model", help="Default model for inference"
    ),
) -> None:
    """Run a standalone executor process that connects to a Cognis controller."""
    import asyncio

    # Enforce wss:// for non-localhost URLs
    if not controller_url.startswith("wss://") and not _is_localhost(controller_url):
        typer.echo(
            "ERROR: Remote executor connections require wss:// (TLS). "
            "Use ws:// only for localhost connections.",
            err=True,
        )
        raise typer.Exit(code=1)

    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorConfig, InferenceConfig

    inference: InferenceConfig | None = None
    if inference_endpoint:
        inference = InferenceConfig(
            type="openai_compatible",
            endpoint=inference_endpoint,
            default_model=inference_model,
            models=[inference_model] if inference_model else [],
        )

    config = ExecutorConfig(
        executor_id=executor_id,
        controller_url=controller_url,
        controller_token=token,
        inference=inference,
        metadata={"enabled_tools": tools},
    )

    runner = ExecutorRunner(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner.run())


def _is_localhost(url: str) -> bool:
    """Check if a URL points to localhost."""
    for prefix in ("ws://localhost", "ws://127.0.0.1", "ws://[::1]", "ws://0.0.0.0"):
        if url.startswith(prefix):
            return True
    return False
