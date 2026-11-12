"""Private payload persistence and narrowly scoped feedback identities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.artifacts.store import ArtifactStore
from cognis.store.feedback_models import FeedbackPayloadRow


def identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def affected(result: Result[Any]) -> int:
    """Read the affected-row count of an executed SQL DML statement."""
    return cast(CursorResult[Any], result).rowcount


def source_actor(actor: Actor) -> str:
    return f"share:{actor.share_id}" if actor.share_id else actor.identity


class FeedbackError(ValueError):
    """Content-free, stable transport error."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Actor:
    """Identity resolved from application auth or a checked share session."""

    identity: str
    label: str
    user_email: str | None = None
    share_id: str | None = None
    guest_session_id: str | None = None

    @classmethod
    def user(cls, email: str) -> Actor:
        return cls(identity=email, label=email, user_email=email)


class Tokens:
    """Authenticated context handles; tokens never independently grant content access."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def seal(self, payload: dict[str, Any]) -> str:
        body = encode(payload)
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")

    def public_share_secret(self, share_id: str) -> str:
        """Recover new link secrets without storing bearer tokens or reusing seal payloads."""
        body = b"feedback-public-share-v1\0" + share_id.encode("ascii")
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def open(self, value: str, *, kind: str, actor: str) -> dict[str, Any]:
        try:
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            body, signature = raw[:-32], raw[-32:]
            expected = hmac.new(self._secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            result = json.loads(body)
            if (
                not isinstance(result, dict)
                or result.get("kind") != kind
                or result.get("actor") != actor
                or result.get("exp", 0) < datetime.now(UTC).timestamp()
            ):
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise FeedbackError(409, "stale_context", "Feedback context must be refreshed") from exc


class Payloads:
    """Track staged objects before I/O and adopt them in their metadata transaction."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], store: ArtifactStore) -> None:
        self.factory = factory
        self.store = store

    async def put(self, actor: str, value: Any) -> str:
        return await self.put_bytes(actor, encode(value), "application/json")

    async def put_bytes(self, actor: str, data: bytes, mime_type: str) -> str:
        payload_id = identifier("fbp")
        async with self.factory() as db:
            db.add(
                FeedbackPayloadRow(
                    payload_id=payload_id,
                    owner_id=actor,
                    digest=digest(data),
                    mime_type=mime_type,
                    size_bytes=len(data),
                    adopted=False,
                )
            )
            await db.commit()
        await self.store.async_save("feedback", payload_id, "payload", data, mime_type)
        return payload_id

    async def get_bytes(self, payload_id: str) -> tuple[bytes, str]:
        async with self.factory() as db:
            row = await db.get(FeedbackPayloadRow, payload_id)
        if row is None:
            raise FeedbackError(410, "feedback_unavailable", "Feedback payload is unavailable")
        try:
            data, mime = await self.store.async_load("feedback", payload_id, "payload")
        except FileNotFoundError as exc:
            raise FeedbackError(
                410, "feedback_unavailable", "Feedback payload is unavailable"
            ) from exc
        if len(data) != row.size_bytes or not hmac.compare_digest(digest(data), row.digest):
            raise FeedbackError(503, "payload_integrity", "Feedback payload cannot be verified")
        return data, mime

    async def get(self, payload_id: str) -> Any:
        data, _ = await self.get_bytes(payload_id)
        return json.loads(data)

    async def adopt(self, db: AsyncSession, *payload_ids: str) -> None:
        result = await db.execute(
            update(FeedbackPayloadRow)
            .where(FeedbackPayloadRow.payload_id.in_(payload_ids))
            .values(adopted=True)
        )
        if affected(result) != len(set(payload_ids)):
            raise FeedbackError(409, "payload_expired", "Staged feedback payload expired")

    async def collect_staged(self, before: datetime) -> int:
        """Collect only old unadopted bytes; never delete a published packet."""
        async with self.factory() as db:
            ids = list(
                await db.scalars(
                    select(FeedbackPayloadRow.payload_id)
                    .where(
                        FeedbackPayloadRow.adopted.is_(False),
                        FeedbackPayloadRow.created_at < before,
                    )
                    .limit(100)
                )
            )
        deleted = 0
        for payload_id in ids:
            # Fence adoption before external deletion, without holding a SQL transaction.
            async with self.factory() as db:
                row = await db.get(FeedbackPayloadRow, payload_id, with_for_update=True)
                if row is None or row.adopted:
                    continue
                await db.delete(row)
                await db.commit()
            await self.store.async_delete_object("feedback", payload_id)
            deleted += 1
        return deleted
