from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from cognis.api.middleware import KnowledgebaseDocumentUploadLimitMiddleware


async def _invoke(
    *,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]],
    max_body_bytes: int,
    path: str = "/api/v1/knowledgebases/kb-1/documents",
    method: str = "POST",
) -> tuple[list[dict[str, Any]], list[bytes]]:
    persisted: list[bytes] = []
    sent: list[dict[str, Any]] = []
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def downstream(
        scope: dict[str, Any],
        receive_call: Callable[[], Awaitable[dict[str, Any]]],
        send_call: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        del scope
        body = bytearray()
        while True:
            message = await receive_call()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        persisted.append(bytes(body))
        await send_call({"type": "http.response.start", "status": 200, "headers": []})
        await send_call({"type": "http.response.body", "body": b"ok"})

    middleware = KnowledgebaseDocumentUploadLimitMiddleware(
        downstream, max_body_bytes=max_body_bytes
    )
    await middleware(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers,
        },
        receive,
        send,
    )
    return sent, persisted


@pytest.mark.asyncio
async def test_chunked_oversized_body_is_rejected_before_downstream_persistence() -> None:
    sent, persisted = await _invoke(
        chunks=[b"12345", b"67890", b"x"],
        headers=[(b"content-type", b"multipart/form-data; boundary=test")],
        max_body_bytes=10,
    )

    assert sent[0]["status"] == 413
    assert persisted == []


@pytest.mark.asyncio
async def test_content_length_and_multipart_part_counts_are_rejected_pre_parser() -> None:
    sent, persisted = await _invoke(
        chunks=[],
        headers=[
            (b"content-type", b"multipart/form-data; boundary=test"),
            (b"content-length", b"11"),
        ],
        max_body_bytes=10,
    )
    assert sent[0]["status"] == 413
    assert persisted == []

    parts = b"".join(
        [
            (
                b"--test\r\n"
                b'Content-Disposition: form-data; name="files[]"; '
                + f'filename="{index}.txt"'.encode()
                + b"\r\nContent-Type: text/plain\r\n\r\nx\r\n"
            )
            for index in range(26)
        ]
    )
    parts += b"--test--\r\n"
    sent, persisted = await _invoke(
        chunks=[parts],
        headers=[(b"content-type", b"multipart/form-data; boundary=test")],
        max_body_bytes=len(parts) + 1,
    )
    assert sent[0]["status"] == 400
    assert persisted == []


@pytest.mark.asyncio
async def test_valid_boundary_split_and_unrelated_requests_pass_through() -> None:
    body = (
        b"--split-boundary\r\n"
        b'Content-Disposition:form-data; name="files[]"; filename="one.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
        b"content-disposition: form-data; is ordinary file content\r\n"
        b"--split-boundary--\r\n"
    )
    sent, persisted = await _invoke(
        chunks=[body[:13], body[13:67], body[67:]],
        headers=[
            (
                b"content-type",
                b"multipart/form-data; boundary=split-boundary",
            )
        ],
        max_body_bytes=len(body),
    )
    assert sent[0]["status"] == 200
    assert persisted == [body]

    sent, persisted = await _invoke(
        chunks=[b"unrelated"],
        headers=[(b"content-type", b"application/octet-stream")],
        max_body_bytes=1,
        path="/api/v1/artifacts/upload",
    )
    assert sent[0]["status"] == 200
    assert persisted == [b"unrelated"]
