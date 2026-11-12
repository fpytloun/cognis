"""Bind the lifespan-owned capture to the existing delivery observer."""

from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.llm.usage_delivery import UsageDeliveryMiddleware


class UsageDeliveryLifecycleMiddleware:
    """Resolve capture after lifespan startup, without duplicating ASGI observation."""

    def __init__(self, app: ASGIApp, *, capture: Callable[[], UsageCapture | None]) -> None:
        self.app = app
        self.capture = capture

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        capture = self.capture()
        if capture is None:
            # Pre-start health probes and ASGI apps without a running lifespan
            # have no collector or inference services to observe yet.
            await self.app(scope, receive, send)
            return
        await UsageDeliveryMiddleware(self.app, capture=capture)(scope, receive, send)
