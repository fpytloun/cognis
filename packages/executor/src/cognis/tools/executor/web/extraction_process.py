"""Killable process isolation for CPU-heavy web extraction."""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
from typing import Any

import httpx

from cognis.models.tool import ToolResult


def _document_worker(
    result_queue: Any,
    html: str,
    url: str,
    output_format: str,
    options: dict[str, Any],
) -> None:
    try:
        from cognis.tools.executor.web.extraction import extract_document

        document = extract_document(
            html,
            url=url,
            output_format=output_format,
            options=options,
        )
        result_queue.put(("ok", {"content": document.content, "document": document.as_dict()}))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _response_worker(
    result_queue: Any,
    content: bytes,
    headers: dict[str, str],
    status_code: int,
    url: str,
    output_format: str,
    requested_url: str,
    options: dict[str, Any],
) -> None:
    try:
        from cognis.tools.executor.web.headers import format_response_result

        response = httpx.Response(
            status_code,
            content=content,
            headers=headers,
            request=httpx.Request("GET", url),
        )
        result = format_response_result(
            response,
            output_format,
            requested_url=requested_url,
            source_url=url,
            options=options,
        )
        result_queue.put(("ok", result.model_dump(mode="json")))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


async def extract_document_in_process(
    html: str,
    *,
    url: str,
    output_format: str,
    options: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    return await _run_process(
        _document_worker,
        (html, url, output_format, options),
        timeout=timeout,
    )


async def format_response_in_process(
    response: httpx.Response,
    *,
    output_format: str,
    requested_url: str,
    source_url: str,
    options: dict[str, Any],
    timeout: float,
) -> ToolResult:
    payload = await _run_process(
        _response_worker,
        (
            (
                bytes(response.content)
                if isinstance(response.content, bytes | bytearray)
                else str(response.text).encode()
            ),
            dict(response.headers),
            response.status_code,
            source_url,
            output_format,
            requested_url,
            options,
        ),
        timeout=timeout,
    )
    return ToolResult.model_validate(payload)


async def _run_process(
    target: Any,
    args: tuple[Any, ...],
    *,
    timeout: float,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    result_queue = None
    process = None
    try:
        result_queue = context.Queue(maxsize=1)
        process = context.Process(target=target, args=(result_queue, *args), daemon=True)
        process.start()
    except Exception as exc:
        if result_queue is not None:
            result_queue.close()
        raise RuntimeError(f"web extraction process failed to start: {type(exc).__name__}") from exc
    try:
        status, payload = await asyncio.to_thread(result_queue.get, True, timeout)
    except queue.Empty as exc:
        raise TimeoutError(f"web extraction timed out after {timeout:g}s") from exc
    finally:
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, 1.0)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 1.0)
        result_queue.close()
        result_queue.join_thread()
    if status != "ok":
        raise RuntimeError(str(payload))
    if not isinstance(payload, dict):
        raise RuntimeError("web extraction returned an invalid payload")
    return payload
