"""Direct ChatGPT Codex Responses transport."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from cognis.providers.llm.codex import CODEX_RESPONSES_URL, CodexAuth

DEFAULT_CODEX_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=90.0, write=30.0, pool=30.0)
DEFAULT_CODEX_STREAM_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

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
    headers["x-client-request-id"] = str(uuid.uuid4())
    return headers


class DirectCodexResponsesStream:
    """Async iterator over Codex SSE data events."""

    def __init__(self, response: httpx.Response) -> None:
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

    def __init__(
        self,
        auth: CodexAuth,
        *,
        client_factory: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._auth = auth
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None

    def update_auth(self, auth: CodexAuth) -> None:
        """Use refreshed OAuth credentials for subsequent requests."""

        self._auth = auth

    def _http_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None or client.is_closed:
            client = self._client_factory()
            self._client = client
        return client

    async def aclose(self) -> None:
        """Close the shared HTTP connection pool."""

        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def responses(self, **kwargs: Any) -> Any:
        payload, stream = _codex_payload(kwargs)
        headers = _codex_headers(self._auth, kwargs)
        timeout = kwargs.get("timeout") or (
            DEFAULT_CODEX_STREAM_HTTP_TIMEOUT if stream else DEFAULT_CODEX_HTTP_TIMEOUT
        )
        client = self._http_client()
        if stream:
            request = client.build_request(
                "POST",
                CODEX_RESPONSES_URL,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            response = await client.send(request, stream=True)
            try:
                if response.status_code >= 400:
                    await response.aread()
                _raise_for_status(response)
            except Exception:
                await response.aclose()
                raise
            return DirectCodexResponsesStream(response)
        response = await client.post(
            CODEX_RESPONSES_URL,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        _raise_for_status(response)
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Direct Codex transport returned a non-object response")
        return data
