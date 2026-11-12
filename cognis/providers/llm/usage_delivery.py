"""ASGI-local request delivery observations, separate from provider attempts."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cognis.models.usage import UsageClientSpan, UsageRequestStats, UsageServerSpan
from cognis.providers.llm.usage_capture import UsageCapture, UsageRequest
from cognis.providers.llm.usage_normalization import safe_error

_SCOPE_KEY = "cognis_usage_request"


def bind_usage_request(scope: Scope, request: UsageRequest) -> None:
    """Bind only from a trusted route after authentication, never raw headers."""
    scope.setdefault("state", {})[_SCOPE_KEY] = request


class UsageDeliveryMiddleware:
    """Observe actual awaited ASGI sends, without assuming client receipt.

    Composition owns installation and the capture dependency. A route explicitly
    binds its authenticated request object; unrelated routes produce no stats.
    No database, per-chunk event, telemetry HTTP call or asynchronous sink wait
    occurs here.
    """

    def __init__(self, app: ASGIApp, *, capture: UsageCapture) -> None:
        self.app = app
        self.capture = capture

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        received_at = datetime.now(UTC)
        first_sent_at: datetime | None = None
        last_sent_at: datetime | None = None
        first_sent: float | None = None
        last_sent: float | None = None
        status: int | None = None
        completed = False
        disconnected = False
        error: BaseException | None = None

        def bound() -> UsageRequest | None:
            request = scope.get("state", {}).get(_SCOPE_KEY)
            return request if isinstance(request, UsageRequest) else None

        async def observed_receive() -> Message:
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message: Message) -> None:
            nonlocal first_sent_at, last_sent_at, first_sent, last_sent, status, completed
            if message["type"] == "http.response.start":
                request = bound()
                if request is not None:
                    headers = list(message.get("headers", ()))
                    headers = [
                        (key, value)
                        for key, value in headers
                        if key.lower() != b"x-cognis-request-id"
                    ]
                    headers.append((b"x-cognis-request-id", request.request_id.encode("ascii")))
                    message = {**message, "headers": headers}
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            # A generator yield or attempted send is not a completed send.
            if message["type"] == "http.response.body":
                if message.get("body"):
                    now = time.monotonic()
                    observed_at = datetime.now(UTC)
                    if first_sent is None:
                        first_sent, first_sent_at = now, observed_at
                    last_sent, last_sent_at = now, observed_at
                if not message.get("more_body", False):
                    completed = True

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            error = exc
            raise
        finally:
            request = bound()
            if request is not None:
                if error is not None and request.http_metadata is not None:
                    request.http_metadata = request.http_metadata.model_copy(
                        update={"error": safe_error(error)}
                    )
                # All calculations use this process' monotonic clock. Sent is
                # explicitly not client-received; client timing stays absent.
                try:
                    outcome = (
                        "cancelled"
                        if disconnected
                        or (error is not None and safe_error(error).category == "cancelled")
                        else "error"
                        if error is not None
                        or (status is not None and status >= 400)
                        or request.gateway_outcome == "error"
                        else "success"
                        if completed
                        else "unknown"
                    )
                    self.capture.emit(
                        UsageRequestStats.model_validate(
                            {
                                "observation_id": uuid.uuid4().hex,
                                "request_id": request.request_id,
                                "cycle_id": request.cycle_id,
                                "request_started_at": request.started_at,
                                "observed_at": datetime.now(UTC),
                                "attribution": request.attribution,
                                "controller_owner_id": self.capture.controller_owner_id,
                                "coverage_epoch_id": self.capture.coverage_epoch_id,
                                "stats_scope": "server",
                                "logical_outcome": outcome,
                                "http_metadata": request.http_metadata.model_copy(
                                    update={"http_status_code": status}
                                )
                                if request.http_metadata is not None
                                else None,
                                "server": UsageServerSpan(
                                    received_at=received_at,
                                    first_generated_at=request.first_generated_at,
                                    first_sent_at=first_sent_at,
                                    last_sent_at=last_sent_at,
                                    completed_at=datetime.now(UTC) if completed else None,
                                    elapsed_ms=max(0, time.monotonic() - started) * 1000,
                                    ttft_ms=max(
                                        0.0, (request.first_generated_monotonic - started) * 1000
                                    )
                                    if request.first_generated_monotonic is not None
                                    else None,
                                    send_elapsed_ms=(last_sent - first_sent) * 1000
                                    if last_sent is not None and first_sent is not None
                                    else None,
                                ),
                                "error": safe_error(error) if error is not None else None,
                            }
                        )
                    )
                except Exception:
                    self.capture.failed_events += 1


def emit_client_span(
    *,
    capture: UsageCapture,
    request: UsageRequest,
    request_started_at: datetime,
    span: UsageClientSpan,
) -> bool:
    """Offer a reported client span after its request binding is authorized.

    The API owner must resolve the authenticated originating request; a readable
    foreign session or unverified correlation label is not that authority.
    This operation cannot set or replace the server logical outcome.
    """
    try:
        return capture.emit(
            UsageRequestStats(
                observation_id=uuid.uuid4().hex,
                request_id=request.request_id,
                request_started_at=request_started_at,
                observed_at=datetime.now(UTC),
                attribution=request.attribution,
                controller_owner_id=capture.controller_owner_id,
                coverage_epoch_id=capture.coverage_epoch_id,
                stats_scope="client",
                client=span,
            )
        )
    except Exception:
        capture.failed_events += 1
        return False
