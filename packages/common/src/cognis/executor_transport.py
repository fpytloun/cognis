"""Negotiated, bounded WebSocket messages for executor JSON-RPC transfers."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TextIO

from cognis import json_codec as json

CHUNKED_JSON_PROTOCOL = "chunked-json-v1"
CHUNK_CHARS = 64 * 1024
CHUNK_IDLE_SECONDS = 60.0
_CHUNK_METHOD = "transport.chunk"


class JSONMessageSender:
    """Chunk large messages, allowing small control messages between chunks."""

    def __init__(self, send_text: Callable[[str], Awaitable[None]]) -> None:
        self.enabled = False
        self._send_text = send_text
        self._frame_lock = asyncio.Lock()
        self._transfer_lock = asyncio.Lock()

    async def send(
        self, payload: str, *, on_send_started: Callable[[], None] | None = None
    ) -> None:
        """Send a message; callers must retire the socket on interrupted sends."""
        if not self.enabled or len(payload) <= CHUNK_CHARS:
            async with self._frame_lock:
                if on_send_started is not None:
                    on_send_started()
                await self._send_text(payload)
            return
        async with self._transfer_lock:
            transfer_id = uuid.uuid4().hex
            for index, start in enumerate(range(0, len(payload), CHUNK_CHARS)):
                frame = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": _CHUNK_METHOD,
                        "params": {
                            "transfer_id": transfer_id,
                            "index": index,
                            "data": payload[start : start + CHUNK_CHARS],
                            "final": start + CHUNK_CHARS >= len(payload),
                        },
                    }
                )
                async with self._frame_lock:
                    if on_send_started is not None:
                        on_send_started()
                    await self._send_text(frame)
                # A fast socket may not yield. Let heartbeats and cancellation run.
                await asyncio.sleep(0)


class JSONMessageReceiver:
    """Reassemble one transfer per authenticated socket using a temporary file.

    There is no aggregate transport-size limit. Artifact-store policy still
    applies after dispatch. File I/O and large JSON decoding run off the event
    loop; only a bounded chunk is retained between receives.
    """

    def __init__(self) -> None:
        self.enabled = False
        self._file: TextIO | None = None
        self._transfer_id: str | None = None
        self._next_index = 0
        self._last_chunk_at = 0.0

    @property
    def receive_timeout(self) -> float | None:
        """Return remaining idle time, unaffected by unrelated control messages."""
        if self._transfer_id is None:
            return None
        return max(
            0.0, self._last_chunk_at + CHUNK_IDLE_SECONDS - asyncio.get_running_loop().time()
        )

    async def feed(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Return a complete RPC object, or None while awaiting further chunks."""
        if frame.get("method") != _CHUNK_METHOD:
            return frame
        params = frame.get("params")
        if not self.enabled or not isinstance(params, dict):
            raise ValueError("Unnegotiated or malformed executor chunk")
        transfer_id = params.get("transfer_id")
        index = params.get("index")
        data = params.get("data")
        final = params.get("final")
        if (
            not isinstance(transfer_id, str)
            or len(transfer_id) != 32
            or type(index) is not int
            or not isinstance(data, str)
            or not 0 < len(data) <= CHUNK_CHARS
            or type(final) is not bool
        ):
            raise ValueError("Malformed executor chunk")
        if self._transfer_id is None:
            if index != 0:
                raise ValueError("Executor chunk sequence must start at zero")
            self._transfer_id = transfer_id
        if transfer_id != self._transfer_id or index != self._next_index:
            raise ValueError("Out-of-order or overlapping executor chunks")
        await self._io(lambda: self._write(data))
        self._next_index += 1
        self._last_chunk_at = asyncio.get_running_loop().time()
        if not final:
            return None
        try:
            result = await self._io(self._decode)
            if not isinstance(result, dict) or result.get("method") == _CHUNK_METHOD:
                raise ValueError("Executor transfer must contain one JSON-RPC object")
            return result
        finally:
            await self.close()

    def _write(self, data: str) -> None:
        if self._file is None:
            # Lifetime spans feed calls; close() owns the connection-scoped file.
            self._file = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")  # noqa: SIM115
        self._file.write(data)

    def _decode(self) -> Any:
        assert self._file is not None
        self._file.seek(0)
        return json.loads(self._file.read())

    async def _io(self, operation: Callable[[], Any]) -> Any:
        # Do not close a temporary file while a cancelled worker still uses it.
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def close(self) -> None:
        """Discard partial state on disconnect, cancellation, or completion."""
        if self._file is not None:
            await self._io(self._file.close)
        self._file = None
        self._transfer_id = None
        self._next_index = 0
