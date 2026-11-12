"""Private executor-owned execution receipts, not a second tool audit store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_PENDING_RESULT_BYTES = 2 * 1024 * 1024


class JournalCapacityError(OSError):
    """Admission backpressure with content-free capacity evidence."""

    def __init__(self, capacity: dict[str, int]) -> None:
        super().__init__("Executor receipt journal is full; new execution was not admitted")
        self.capacity = capacity


class CallJournal:
    """Commit acceptance before effects and terminal evidence before transport.

    Unacknowledged receipts are never evicted. Storage pressure rejects new
    admission instead. Acknowledged payloads are cached for up to fourteen days,
    or reclaimed earlier under pressure. Deduplication tombstones never expire.
    """

    def __init__(
        self, path: Path, *, max_bytes: int = 1024 * 1024 * 1024, max_calls: int = 100_000
    ) -> None:
        if max_bytes < _PENDING_RESULT_BYTES or max_calls < 1:
            raise ValueError("Receipt limits require at least 2 MiB and one retained call")
        self.path = path
        self.max_bytes = max_bytes
        self.max_calls = max_calls

    @classmethod
    def from_environment(cls, path: Path) -> CallJournal:
        """Read executor-local admission limits, failing startup on invalid values."""
        return cls(
            path,
            max_bytes=int(os.environ.get("COGNIS_EXECUTOR_RECEIPT_MAX_BYTES", str(1024**3))),
            max_calls=int(os.environ.get("COGNIS_EXECUTOR_RECEIPT_MAX_CALLS", "100000")),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise OSError("Executor receipt journal must not be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        if os.name == "posix":
            self.path.chmod(0o600)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS receipts ("
                "call_id TEXT PRIMARY KEY, digest TEXT NOT NULL, instance_id TEXT NOT NULL,"
                "scope TEXT NOT NULL, result TEXT, acknowledged INTEGER NOT NULL DEFAULT 0,"
                "settled_at REAL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS identity (id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS receipts_retained ON receipts(acknowledged,settled_at) "
                "WHERE acknowledged=0 OR result IS NOT NULL"
            )
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE receipts SET result=NULL WHERE acknowledged=1 "
                "AND result IS NOT NULL "
                "AND settled_at < cast(strftime('%s','now') AS REAL)-1209600"
            )
            if db.execute("SELECT id FROM identity").fetchone() is None:
                db.execute("INSERT INTO identity VALUES (?)", (uuid.uuid4().hex,))
            db.commit()
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _identity(params: dict[str, Any]) -> tuple[str, str]:
        metadata = params.get("runtime_metadata") or {}
        scope = json.dumps(
            {
                "execution_scope_id": params.get("execution_scope_id"),
                "session_id": metadata.get("session_id"),
                "turn_id": metadata.get("turn_id"),
            },
            sort_keys=True,
        )
        execution = {
            "scope": scope,
            "tool_name": params.get("tool_name"),
            "arguments": params.get("arguments"),
        }
        digest = hashlib.sha256(
            json.dumps(execution, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return digest, scope

    async def accept(self, params: dict[str, Any], instance_id: str) -> bool:
        """Return true only for the first committed admission of this identity."""
        return await asyncio.to_thread(self._accept, params, instance_id)

    def _accept(self, params: dict[str, Any], instance_id: str) -> bool:
        digest, scope = self._identity(params)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT digest FROM receipts WHERE call_id=?", (params["call_id"],)
            ).fetchone()
            if prior is not None:
                if prior["digest"] != digest:
                    raise ValueError("Conflicting executor call identity")
                return False
            count, size, pending, unresolved, unresolved_bytes = db.execute(
                "SELECT count(*),"
                "coalesce(sum(length(cast(result AS BLOB))),0),"
                "coalesce(sum(result IS NULL AND acknowledged=0),0),"
                "coalesce(sum(acknowledged=0),0),"
                "coalesce(sum(CASE WHEN acknowledged=0 THEN length(cast(result AS BLOB)) "
                "ELSE 0 END),0) FROM receipts WHERE acknowledged=0 OR result IS NOT NULL"
            ).fetchone()
            reservation = (pending + 1) * _PENDING_RESULT_BYTES
            if unresolved >= self.max_calls or unresolved_bytes + reservation > self.max_bytes:
                raise JournalCapacityError(
                    {
                        "retained_calls": count,
                        "result_bytes": size,
                        "pending_calls": pending,
                        "unacknowledged_calls": unresolved,
                        "unacknowledged_bytes": unresolved_bytes,
                        "required_reservation_bytes": reservation,
                        "max_bytes": self.max_bytes,
                        "max_calls": self.max_calls,
                    }
                )
            reclaimed = 0
            while count >= self.max_calls or size + reservation > self.max_bytes:
                rows = db.execute(
                    "SELECT call_id,length(cast(result AS BLOB)) AS size FROM receipts "
                    "WHERE acknowledged=1 AND result IS NOT NULL "
                    "ORDER BY settled_at,call_id LIMIT 128"
                ).fetchall()
                for row in rows:
                    db.execute("UPDATE receipts SET result=NULL WHERE call_id=?", (row["call_id"],))
                    count -= 1
                    size -= row["size"]
                    reclaimed += 1
                    if count < self.max_calls and size + reservation <= self.max_bytes:
                        break
            db.execute(
                "INSERT INTO receipts(call_id,digest,instance_id,scope) VALUES (?,?,?,?)",
                (params["call_id"], digest, instance_id, scope),
            )
        if reclaimed:
            logger.info("Reclaimed acknowledged executor receipt payloads count=%d", reclaimed)
        return True

    async def complete(
        self,
        call_id: str,
        result: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
        instance_id: str | None = None,
    ) -> None:
        """Persist the complete envelope before it can be sent to a controller."""
        try:
            await asyncio.to_thread(self._complete, call_id, result, params, instance_id)
        except (OSError, ValueError, sqlite3.Error) as exc:
            logger.error(
                "Executor receipt failed call_id=%s operation=complete error_type=%s",
                call_id,
                type(exc).__name__,
            )
            raise

    def _complete(
        self,
        call_id: str,
        result: dict[str, Any],
        params: dict[str, Any] | None,
        instance_id: str | None,
    ) -> None:
        encoded = json.dumps(result, separators=(",", ":"))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM receipts WHERE call_id=?", (call_id,)).fetchone()
            if prior is None:
                raise ValueError("Executor result has no committed acceptance")
            if prior["acknowledged"] and prior["result"] is None:
                raise ValueError("Terminal executor receipt payload was already reclaimed")
            if params is not None and (
                prior["digest"] != self._identity(params)[0] or prior["instance_id"] != instance_id
            ):
                raise ValueError("Terminal receipt does not belong to the accepting execution")
            if prior["result"] is not None and prior["result"] != encoded:
                raise ValueError("Conflicting terminal executor receipt")
            db.execute("UPDATE receipts SET result=? WHERE call_id=?", (encoded, call_id))

    async def fetch(
        self,
        call_id: str,
        instance_id: str,
        *,
        scope: dict[str, Any] | None = None,
        require_scope: bool = False,
    ) -> dict[str, Any] | None:
        """Read evidence; an old acceptance without a result stays indeterminate."""
        try:
            return await asyncio.to_thread(self._fetch, call_id, instance_id, scope, require_scope)
        except (OSError, ValueError, sqlite3.Error) as exc:
            logger.error(
                "Executor receipt failed call_id=%s operation=fetch error_type=%s",
                call_id,
                type(exc).__name__,
            )
            raise

    def _fetch(
        self,
        call_id: str,
        instance_id: str,
        scope: dict[str, Any] | None,
        require_scope: bool,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM receipts WHERE call_id=?", (call_id,)).fetchone()
            if row is None:
                return None
            if require_scope and not self._matches_scope(row["scope"], scope):
                return {
                    "call_id": call_id,
                    "executor_instance_id": instance_id,
                    "state": "indeterminate",
                }
            journal_id = db.execute("SELECT id FROM identity").fetchone()["id"]
            report: dict[str, Any] = {
                "call_id": call_id,
                "executor_instance_id": instance_id,
                "accepted_executor_instance_id": row["instance_id"],
                "journal_id": journal_id,
                "durable_receipt": True,
                "state": (
                    "terminal"
                    if row["result"] is not None
                    else "terminal_result_unavailable"
                    if row["acknowledged"]
                    else "indeterminate"
                ),
            }
            if row["result"] is not None:
                report["result"] = json.loads(row["result"])
            return report

    @staticmethod
    def _matches_scope(stored: str, scope: dict[str, Any] | None) -> bool:
        if not scope:
            return False
        expected = json.loads(stored)
        return all(
            isinstance(scope.get(key), str) and bool(scope[key]) and scope[key] == expected.get(key)
            for key in ("session_id", "turn_id")
        )

    async def acknowledge(
        self,
        call_id: str,
        *,
        scope: dict[str, Any] | None = None,
        require_scope: bool = False,
    ) -> bool:
        """Record idempotent canonical settlement, enabling payload reclamation."""
        return await asyncio.to_thread(self._acknowledge, call_id, scope, require_scope)

    def _acknowledge(
        self,
        call_id: str,
        scope: dict[str, Any] | None,
        require_scope: bool,
    ) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT scope,result,acknowledged FROM receipts WHERE call_id=?", (call_id,)
            ).fetchone()
            if (
                row is None
                or (require_scope and not self._matches_scope(row["scope"], scope))
                or (row["result"] is None and not row["acknowledged"])
            ):
                return False
            db.execute(
                "UPDATE receipts SET acknowledged=1,"
                "settled_at=coalesce(settled_at,cast(strftime('%s','now') AS REAL)) "
                "WHERE call_id=? AND result IS NOT NULL",
                (call_id,),
            )
        return True
