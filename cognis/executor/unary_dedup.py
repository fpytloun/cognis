"""Executor-process bounded deduplication for terminal replay-safe unary RPCs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from time import monotonic
from typing import Any

REPLAY_SAFE_UNARY_METHODS = frozenset(
    {
        "tool.list",
        "lsp.status",
        "shell.background_status",
        "local_model.status",
        "local_model.show",
        "llm.discover_models",
    }
)


def is_replay_safe_unary_method(method: str) -> bool:
    return method in REPLAY_SAFE_UNARY_METHODS


class UnaryCallConflict(ValueError):
    """Stable call ID reused with a different payload."""


class UnaryDedupCache:
    def __init__(self, *, max_entries: int = 1024, ttl_seconds: float = 300.0) -> None:
        self.max_entries = max(1, max_entries)
        self.ttl_seconds = max(0.1, ttl_seconds)
        self._entries: OrderedDict[str, tuple[str, float, dict[str, Any]]] = OrderedDict()
        self._inflight: OrderedDict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = OrderedDict()

    @staticmethod
    def digest(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def get(self, call_id: str, payload_digest: str) -> dict[str, Any] | None:
        self._purge()
        entry = self._entries.get(call_id)
        if entry is None:
            return None
        digest, _, result = entry
        if digest != payload_digest:
            raise UnaryCallConflict(f"Unary call ID {call_id!r} conflicts with payload")
        self._entries.move_to_end(call_id)
        return dict(result)

    def join_or_claim(
        self, call_id: str, payload_digest: str
    ) -> tuple[asyncio.Future[dict[str, Any]] | None, bool]:
        """Return terminal future and whether this caller owns execution."""

        cached = self.get(call_id, payload_digest)
        if cached is not None:
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            future.set_result(cached)
            return future, False
        inflight = self._inflight.get(call_id)
        if inflight is not None:
            if inflight[0] != payload_digest:
                raise UnaryCallConflict(f"Unary call ID {call_id!r} conflicts with payload")
            return inflight[1], False
        future = asyncio.get_running_loop().create_future()
        self._inflight[call_id] = (payload_digest, future)
        while len(self._inflight) > self.max_entries:
            evicted_id, (evicted_digest, _) = next(iter(self._inflight.items()))
            self.evict(evicted_id, evicted_digest)
        return future, True

    def put(self, call_id: str, payload_digest: str, result: dict[str, Any]) -> None:
        self._purge()
        existing = self._entries.get(call_id)
        if existing is not None and existing[0] != payload_digest:
            raise UnaryCallConflict(f"Unary call ID {call_id!r} conflicts with payload")
        if existing is not None:
            return
        self._entries[call_id] = (
            payload_digest,
            monotonic() + self.ttl_seconds,
            dict(result),
        )
        self._entries.move_to_end(call_id)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        inflight = self._inflight.pop(call_id, None)
        if inflight is not None and not inflight[1].done():
            inflight[1].set_result(dict(result))

    def complete_error(
        self,
        call_id: str,
        payload_digest: str,
        *,
        code: int = -32098,
        message: str = "Replay-safe unary call terminated before completion",
    ) -> dict[str, Any]:
        """Publish a terminal JSON-RPC error for current and future joiners."""

        existing = self.get(call_id, payload_digest)
        if existing is not None:
            return existing
        frame = {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": None,
        }
        self.put(call_id, payload_digest, frame)
        return frame

    def evict(self, call_id: str, payload_digest: str) -> dict[str, Any]:
        """Reserve an evicted call ID with a replayable terminal error."""

        return self.complete_error(
            call_id,
            payload_digest,
            code=-32099,
            message="Replay-safe unary call was evicted before completion",
        )

    def cancel(self, call_id: str) -> None:
        """Cancel and remove one in-flight replay-safe call."""

        inflight = self._inflight.pop(call_id, None)
        if inflight is not None and not inflight[1].done():
            inflight[1].cancel()

    def fail(self, call_id: str, error: BaseException | None = None) -> None:
        """Terminate and remove one in-flight replay-safe call."""

        inflight = self._inflight.pop(call_id, None)
        if inflight is None or inflight[1].done():
            return
        inflight[1].set_result(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32098, "message": str(error or "Unary call failed")},
                "id": None,
            }
        )

    def cancel_inflight(self) -> None:
        """Drop calls owned by a connection that is being torn down."""

        for call_id in tuple(self._inflight):
            self.cancel(call_id)

    def _purge(self) -> None:
        now = monotonic()
        for key, (_, expires_at, _) in list(self._entries.items()):
            if expires_at <= now:
                del self._entries[key]
