"""Metadata-only video RPC. Media uses scoped HTTP transfers, never WebSockets."""

from typing import Any
from urllib.parse import urlsplit

import httpx

from cognis.executor.usage_observer import ExecutorUsageObserver
from cognis.models.video import VideoOutput, VideoRequest
from cognis.providers.video import VideoConnection, VideoHTTPAdapter, VideoInput


def ticket_url(ticket: dict[str, Any]) -> str:
    # Only the trusted controller issues these URLs, never a model/provider response.
    url = ticket["url"]
    if not isinstance(url, str):
        raise ValueError("Invalid controller transfer URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Invalid controller transfer URL")
    if parsed.path != "/api/v1/videos/transfer" or parsed.query or parsed.fragment:
        raise ValueError("Invalid transfer path")
    return url


async def video_rpc(
    params: dict[str, Any],
    *,
    usage_observer: ExecutorUsageObserver | None = None,
) -> dict[str, Any]:
    """No automatic retry: loss of a submit response is an uncertain paid operation."""
    async with httpx.AsyncClient(timeout=1200, follow_redirects=False) as client:
        adapter = VideoHTTPAdapter(
            VideoConnection.model_validate(params["connection"]),
            client,
            usage_binding=usage_observer.binding if usage_observer is not None else None,
        )
        operation = params["operation"]
        if operation == "capabilities":
            return (await adapter.capabilities()).model_dump(mode="json")
        if operation == "collect":
            ticket = params["output_ticket"]
            response = await client.put(
                ticket_url(ticket),
                headers={"X-Cognis-Video-Transfer": ticket["token"]},
                content=adapter.output_stream(
                    VideoOutput.model_validate(params["output"]),
                    ticket["max_bytes"],
                    output_index=params.get("output_index"),
                ),
            )
            if response.status_code != 200:
                raise ValueError("Video transfer failed")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Invalid controller transfer response")
            return result
        if operation == "poll":
            result = await adapter.poll(params["handle"])
        elif operation == "submit":
            result = await adapter.submit(
                VideoRequest.model_validate(params["request"]),
                [VideoInput.model_validate(value) for value in params["inputs"]],
            )
        else:
            raise ValueError("Unknown video RPC operation")
        return result.model_dump(mode="json")
