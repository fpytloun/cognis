"""Direct ChatGPT Codex Responses transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from cognis.providers.llm.codex import CODEX_RESPONSES_URL, CodexAuth

DEFAULT_CODEX_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

_TRANSPORT_KWARGS = {
    "api_base",
    "api_key",
    "api_version",
    "base_url",
    "custom_llm_provider",
    "extra_headers",
    "timeout",
}


def _codex_payload(kwargs: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    model = kwargs.get("model")
    input_items = kwargs.get("input")
    stream = bool(kwargs.get("stream"))
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Direct Codex transport requires a model")
    if input_items is None:
        raise ValueError("Direct Codex transport requires Responses input")
    payload = {
        key: value
        for key, value in kwargs.items()
        if key not in _TRANSPORT_KWARGS and key not in {"model", "input"}
    }
    payload["model"] = model
    payload["input"] = input_items
    payload["stream"] = stream
    payload["store"] = False
    return payload, stream


def _codex_headers(auth: CodexAuth, kwargs: dict[str, Any]) -> dict[str, str]:
    headers = {
        **auth.headers,
        "Accept": "text/event-stream" if kwargs.get("stream") else "application/json",
        "Content-Type": "application/json",
    }
    extra_headers = kwargs.get("extra_headers")
    if isinstance(extra_headers, dict):
        headers.update({str(key): str(value) for key, value in extra_headers.items()})
    return headers


class DirectCodexResponsesStream:
    """Async iterator over Codex SSE data events with owned HTTP resources."""

    def __init__(self, client: httpx.AsyncClient, response: httpx.Response) -> None:
        self._client = client
        self._response = response

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        try:
            async for line in self._response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                if isinstance(event, dict):
                    yield event
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        await self._response.aclose()
        await self._client.aclose()


class DirectCodexHTTPStatusError(RuntimeError):
    """Normalized HTTP error from the direct Codex transport."""

    def __init__(self, response: httpx.Response, body: dict[str, Any] | None) -> None:
        self.response = response
        self.status_code = response.status_code
        self.body = body
        super().__init__(_codex_error_message(response.status_code, body))


class BadRequestError(DirectCodexHTTPStatusError):
    """Bad request error shape compatible with existing fallback classifiers."""


def _codex_error_body(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return {"error": {"message": text[:500]}} if text else None
    return data if isinstance(data, dict) else None


def _codex_error_message(status_code: int, body: dict[str, Any] | None) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        parts = [
            f"Direct Codex request failed: HTTP {status_code};",
            str(error.get("message") or "provider returned an error"),
        ]
        code = error.get("code") or error.get("type")
        param = error.get("param")
        if code:
            parts.append(f"code={code}")
        if param:
            parts.append(f"param={param}")
        return " ".join(parts)
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        return f"Direct Codex request failed: HTTP {status_code}; {detail.strip()}"
    return f"Direct Codex request failed: HTTP {status_code}"


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    body = _codex_error_body(response)
    if response.status_code == 400:
        raise BadRequestError(response, body)
    raise DirectCodexHTTPStatusError(response, body)


class DirectCodexTransport:
    """HTTP/SSE transport for the ChatGPT Codex Responses endpoint."""

    name = "direct_codex"

    def __init__(self, auth: CodexAuth) -> None:
        self._auth = auth

    async def responses(self, **kwargs: Any) -> Any:
        payload, stream = _codex_payload(kwargs)
        headers = _codex_headers(self._auth, kwargs)
        timeout = kwargs.get("timeout")
        client = httpx.AsyncClient(
            timeout=timeout if timeout is not None else DEFAULT_CODEX_HTTP_TIMEOUT
        )
        if stream:
            request = client.build_request(
                "POST", CODEX_RESPONSES_URL, json=payload, headers=headers
            )
            response = await client.send(request, stream=True)
            try:
                if response.status_code >= 400:
                    await response.aread()
                _raise_for_status(response)
            except Exception:
                await response.aclose()
                await client.aclose()
                raise
            return DirectCodexResponsesStream(client, response)
        try:
            response = await client.post(CODEX_RESPONSES_URL, json=payload, headers=headers)
            _raise_for_status(response)
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("Direct Codex transport returned a non-object response")
            return data
        finally:
            await client.aclose()
