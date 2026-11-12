"""Content-free observation at an existing native HTTP submission boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from cognis.providers.llm.usage_binding import UsageBinding


class HTTPObservationError(Exception):
    """Typed status evidence without provider exception or body text."""

    def __init__(self, status_code: int):
        super().__init__()
        self.status_code = status_code


async def observed_http(
    dispatch: Callable[[], Awaitable[Any]],
    *,
    binding: UsageBinding | None,
    parameters: Mapping[str, Any],
) -> Any:
    """Offer two observations without changing native HTTP result/error behavior."""
    attempt = (
        binding.begin(
            operation="native",
            streaming=False,
            parameters=parameters,
        )
        if binding is not None
        else None
    )
    try:
        response = await dispatch()
    except BaseException as exc:
        if attempt is not None:
            attempt.finish(error=exc)
        raise
    if attempt is not None:
        try:
            characters = parameters.get("input_characters")
            attempt.accumulator.report_extra(
                "input_characters", characters, source="adapter", derived=True
            )
            headers = response.headers
            attempt.observe(
                {
                    "request_id": headers.get("request-id") or headers.get("x-request-id"),
                },
                generated=False,
            )
            characters = headers.get("x-character-count")
            if isinstance(characters, str) and characters.isdecimal() and len(characters) <= 18:
                attempt.accumulator.report_extra("billed_characters", int(characters))
                if binding is not None and binding.media_operation == "synthesize":
                    attempt.accumulator.report_extra("input_characters", int(characters))
            if "application/json" in headers.get("content-type", ""):
                attempt.observe(response.json(), generated=False)
            status = response.status_code
            if status >= 400:
                attempt.finish(error=HTTPObservationError(status))
            else:
                attempt.finish()
        except Exception:
            attempt.capture.failed_events += 1
            attempt.finish()
    return response
