"""Serve command."""

from __future__ import annotations

import uvicorn

from cognis.api.app import create_app
from cognis.api.server import run_server
from cognis.config import load_config
from cognis.core.shutdown import ShutdownCoordinator


def serve() -> None:
    """Start the FastAPI server."""
    config = load_config()
    shutdown_coordinator = ShutdownCoordinator()
    server_config = uvicorn.Config(
        create_app(shutdown_coordinator=shutdown_coordinator),
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
        proxy_headers=False,
    )
    run_server(server_config, shutdown_coordinator)
