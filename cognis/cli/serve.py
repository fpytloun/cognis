"""Serve command."""

from __future__ import annotations

import uvicorn

from cognis.api.app import create_app
from cognis.config import load_config


def serve() -> None:
    """Start the FastAPI server."""
    config = load_config()
    uvicorn.run(create_app(), host=config.host, port=config.port)
