"""Uvicorn integration that drains the application before transports close."""

from __future__ import annotations

import socket
from types import FrameType

import uvicorn

from cognis.core.shutdown import ShutdownCoordinator


class DrainingServer(uvicorn.Server):
    """Run the controller drain before Uvicorn closes active connections."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        shutdown_coordinator: ShutdownCoordinator,
    ) -> None:
        super().__init__(config)
        self._shutdown_coordinator = shutdown_coordinator

    def handle_exit(
        self,
        sig: int,
        frame: FrameType | None,
    ) -> None:
        repeated = self.should_exit
        super().handle_exit(sig, frame)
        if repeated:
            self.force_exit = True
            self._shutdown_coordinator.force()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await self._shutdown_coordinator.drain()
        finally:
            await super().shutdown(sockets=sockets)


def run_server(config: uvicorn.Config, coordinator: ShutdownCoordinator) -> None:
    """Run one signal-aware draining Uvicorn server."""

    server = DrainingServer(config, shutdown_coordinator=coordinator)
    server.run()


__all__ = ["DrainingServer", "run_server"]
