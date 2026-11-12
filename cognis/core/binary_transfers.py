"""Call-scoped native binary staging; publication remains owned by artifact revisions."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from time import monotonic

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.artifacts.store import ArtifactStore, sanitize_artifact_filename
from cognis.store.coordination import database_now, database_now_expression
from cognis.store.models import BinaryTransferRow, BinaryTransferScopeRow, Session
from cognis.transfers.models import (
    TransferGrant,
    TransferReceipt,
    UploadDescriptor,
    UploadRequest,
)


class TransferRejected(ValueError):
    """A scoped capability, state transition, or integrity check was rejected."""


async def lock_transfer_publication(
    db: AsyncSession,
    staged: BinaryTransferRow,
    *,
    owner_email: str,
    session_id: str | None,
) -> BinaryTransferRow:
    """Fence cleanup and cancellation inside the artifact publication transaction."""
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE; acquire its writer lock before reading the
        # receipt, without committing a separate intermediate claim.
        await db.execute(
            update(BinaryTransferScopeRow)
            .where(BinaryTransferScopeRow.scope_id == staged.scope_id)
            .values(state=BinaryTransferScopeRow.state)
        )
    scope = await db.scalar(
        select(BinaryTransferScopeRow)
        .where(BinaryTransferScopeRow.scope_id == staged.scope_id)
        .with_for_update()
    )
    upload = await db.scalar(
        select(BinaryTransferRow)
        .where(BinaryTransferRow.transfer_id == staged.transfer_id)
        .with_for_update()
    )
    if (
        scope is None
        or upload is None
        or (scope.owner_email, scope.session_id) != (owner_email, session_id)
        or upload.state not in {"uploaded", "consumed"}
        or (scope.state not in {"active", "finished"} and upload.state != "consumed")
    ):
        raise TransferRejected("Transfer is no longer publishable")
    return upload


class BinaryTransferService:
    """Own authorization, durable state and cleanup for opaque-byte upload slots."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore
    ) -> None:
        self.sessions = session_factory
        self.store = store
        # Admission counts, not file size, bound this receiver's resource usage.
        self._uploads = asyncio.Semaphore(4)

    async def issue(
        self,
        *,
        owner_email: str,
        agent_id: str | None,
        conversation_id: str | None,
        session_id: str,
        call_id: str,
        tool_name: str,
        executor_id: str,
    ) -> tuple[str, TransferGrant]:
        """Issue an internal capability only after the router authorizes the call."""
        token = secrets.token_urlsafe(32)
        scope_id = uuid.uuid4().hex
        async with self.sessions() as db:
            now = await database_now(db)
            acting_session = await db.scalar(
                select(Session)
                .where(Session.session_id == session_id, Session.user_email == owner_email)
                .with_for_update()
            )
            if acting_session is None:
                raise TransferRejected("Transfer session is unavailable")
            # A new authorized attempt fences unfinished output from the old attempt.
            await db.execute(
                update(BinaryTransferScopeRow)
                .where(
                    BinaryTransferScopeRow.session_id == session_id,
                    BinaryTransferScopeRow.call_id == call_id,
                    BinaryTransferScopeRow.state.in_(("active", "finished")),
                )
                .values(state="cancelled")
            )
            db.add(
                BinaryTransferScopeRow(
                    scope_id=scope_id,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    owner_email=owner_email,
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    executor_id=executor_id,
                    state="active",
                    expires_at=now + timedelta(hours=1),
                )
            )
            await db.commit()
        return scope_id, TransferGrant(
            url=f"{self.store.base_url.rstrip('/')}/api/v1/transfers", token=token
        )

    async def _scope(
        self, db: AsyncSession, token: str, *, admission: bool
    ) -> BinaryTransferScopeRow:
        scope = await db.scalar(
            select(BinaryTransferScopeRow).where(
                BinaryTransferScopeRow.token_hash == hashlib.sha256(token.encode()).hexdigest()
            )
        )
        if scope is None or scope.state not in {"active", "finished"}:
            raise TransferRejected("Transfer authorization is unavailable")
        if admission:
            now = await database_now(db)
            expires = (
                scope.expires_at.replace(tzinfo=UTC)
                if scope.expires_at.tzinfo is None
                else scope.expires_at
            )
            if scope.state != "active" or expires <= now:
                raise TransferRejected("Transfer admission has expired")
        return scope

    def _descriptor(self, row: BinaryTransferRow) -> UploadDescriptor:
        return UploadDescriptor(
            transfer_id=row.transfer_id,
            url=f"{self.store.base_url.rstrip('/')}/api/v1/transfers/{row.transfer_id}/content",
            state=row.state,
            receipt=(
                TransferReceipt(
                    transfer_id=row.transfer_id, size_bytes=row.size_bytes, sha256=row.sha256
                )
                if row.sha256 is not None and row.state in {"uploaded", "consumed"}
                else None
            ),
        )

    async def reserve(self, token: str, request: UploadRequest) -> UploadDescriptor:
        """Reserve one replay-safe immutable destination; metadata is never authority."""
        filename = sanitize_artifact_filename(request.filename)
        if filename == ".metadata.json" or "\r" in request.mime_type or "\n" in request.mime_type:
            raise TransferRejected("Invalid transfer filename")
        async with self.sessions() as db:
            scope = await self._scope(db, token, admission=True)
            row = await db.scalar(
                select(BinaryTransferRow).where(
                    BinaryTransferRow.scope_id == scope.scope_id,
                    BinaryTransferRow.slot == request.slot,
                )
            )
            if row is None:
                published = scope.tool_name == "artifact_publish"
                namespace = "artifacts" if published else "attachments"
                row = BinaryTransferRow(
                    transfer_id=uuid.uuid4().hex,
                    scope_id=scope.scope_id,
                    slot=request.slot,
                    artifact_id=ArtifactStore.generate_id("art" if published else "att"),
                    namespace=namespace,
                    filename=filename,
                    mime_type=request.mime_type,
                    size_bytes=request.size_bytes,
                    state="issued",
                )
                db.add(row)
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    return await self.reserve(token, request)
            if (row.filename, row.mime_type, row.size_bytes) != (
                filename,
                request.mime_type,
                request.size_bytes,
            ):
                raise TransferRejected("Upload slot metadata changed")
            return self._descriptor(row)

    async def status(self, token: str, transfer_id: str) -> UploadDescriptor:
        """Reconcile uncertain HTTP completion without resubmitting bytes."""
        async with self.sessions() as db:
            scope = await self._scope(db, token, admission=False)
            row = await db.get(BinaryTransferRow, transfer_id)
            if row is None or row.scope_id != scope.scope_id:
                raise TransferRejected("Transfer is unavailable")
            return self._descriptor(row)

    async def receive(
        self, token: str, transfer_id: str, content: AsyncIterator[bytes]
    ) -> TransferReceipt:
        """Claim, stream, verify, then record one complete immutable upload."""
        async with self._uploads:
            async with self.sessions() as db:
                scope = await self._scope(db, token, admission=True)
                result = await db.execute(
                    update(BinaryTransferRow)
                    .where(
                        BinaryTransferRow.transfer_id == transfer_id,
                        BinaryTransferRow.scope_id == scope.scope_id,
                        BinaryTransferRow.state == "issued",
                    )
                    .values(state="receiving", updated_at=database_now_expression(db))
                    .returning(BinaryTransferRow)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    raise TransferRejected("Upload slot is not available for writing")
                await db.commit()
            checked_at = 0.0

            async def check_current() -> None:
                nonlocal checked_at
                now = monotonic()
                if now - checked_at < 0.5:
                    return
                async with self.sessions() as db:
                    current = await db.get(BinaryTransferScopeRow, scope.scope_id)
                    if current is None or current.state != "active":
                        raise TransferRejected("Transfer was cancelled")
                    await db.execute(
                        update(BinaryTransferRow)
                        .where(
                            BinaryTransferRow.transfer_id == transfer_id,
                            BinaryTransferRow.state == "receiving",
                        )
                        .values(updated_at=database_now_expression(db))
                    )
                    await db.execute(
                        update(BinaryTransferScopeRow)
                        .where(
                            BinaryTransferScopeRow.scope_id == scope.scope_id,
                            BinaryTransferScopeRow.state == "active",
                        )
                        .values(expires_at=(await database_now(db)) + timedelta(hours=1))
                    )
                    await db.commit()
                checked_at = now

            async def bounded_idle_body() -> AsyncIterator[bytes]:
                iterator = content.__aiter__()
                while True:
                    try:
                        async with asyncio.timeout(60):
                            chunk = await anext(iterator)
                    except StopAsyncIteration:
                        return
                    yield chunk

            try:
                total, digest = await self.store.async_receive_stream(
                    row.namespace,
                    row.artifact_id,
                    row.filename,
                    bounded_idle_body(),
                    row.mime_type,
                    scope.owner_email,
                    expected_size=row.size_bytes,
                    check_current=check_current,
                    validate_json=scope.tool_name == "artifact_publish",
                )
                async with self.sessions() as db:
                    # Scope row locking serializes completion against cancellation.
                    current = await db.scalar(
                        select(BinaryTransferScopeRow)
                        .where(BinaryTransferScopeRow.scope_id == scope.scope_id)
                        .with_for_update()
                    )
                    if current is None or current.state != "active":
                        raise TransferRejected("Transfer was cancelled")
                    result = await db.execute(
                        update(BinaryTransferRow)
                        .where(
                            BinaryTransferRow.transfer_id == transfer_id,
                            BinaryTransferRow.state == "receiving",
                        )
                        .values(
                            state="uploaded", sha256=digest, updated_at=database_now_expression(db)
                        )
                        .returning(BinaryTransferRow.transfer_id)
                    )
                    if result.scalar_one_or_none() is None:
                        raise TransferRejected("Upload completion lost its state")
                    await db.commit()
                return TransferReceipt(transfer_id=transfer_id, size_bytes=total, sha256=digest)
            except BaseException:
                async with self.sessions() as db:
                    current_row = await db.scalar(
                        select(BinaryTransferRow)
                        .where(BinaryTransferRow.transfer_id == transfer_id)
                        .with_for_update()
                    )
                    preserve = current_row is not None and current_row.state in {
                        "uploaded",
                        "consumed",
                    }
                    if current_row is not None and current_row.state == "receiving":
                        current_row.state = "failed"
                    await db.commit()
                # A lost commit acknowledgement must reconcile before deleting bytes
                # that another controller may already be entitled to consume.
                if not preserve:
                    await self.store.async_delete_object(row.namespace, row.artifact_id)
                raise

    async def claim(
        self, receipt: TransferReceipt, *, owner_email: str, session_id: str, call_id: str
    ) -> BinaryTransferRow:
        """Verify receiver-owned facts and bind a receipt to the accepted tool call."""
        async with self.sessions() as db:
            row = await db.get(BinaryTransferRow, receipt.transfer_id)
            scope = await db.get(BinaryTransferScopeRow, row.scope_id) if row else None
            if (
                row is None
                or scope is None
                or (scope.state not in {"active", "finished"} and row.state != "consumed")
                or (scope.owner_email, scope.session_id, scope.call_id)
                != (owner_email, session_id, call_id)
                or (row.size_bytes, row.sha256) != (receipt.size_bytes, receipt.sha256)
                or row.state not in {"uploaded", "consumed"}
            ):
                raise TransferRejected("Transfer receipt is not valid for this call")
            if row.state == "consumed":
                return row
        try:
            metadata = await self.store.async_load_metadata(
                row.namespace, row.artifact_id, row.filename
            )
        except FileNotFoundError:
            raise TransferRejected(
                "Transfer bytes are unavailable on this controller; shared storage is required."
            ) from None
        if metadata is None:
            raise TransferRejected(
                "Transfer bytes are unavailable on this controller; shared storage is required."
            )
        if metadata.size != row.size_bytes or metadata.owner_email != owner_email:
            raise TransferRejected("Transfer storage metadata does not match its receipt")
        # Verification is read-only. Consumption commits atomically with the
        # artifact record, so a crash here cannot strand a successful upload.
        return row

    async def close_scope(self, scope_id: str, *, cancelled: bool = False) -> None:
        """Revoke admission; published receipts remain reconcilable after success."""
        async with self.sessions() as db:
            await db.execute(
                update(BinaryTransferScopeRow)
                .where(
                    BinaryTransferScopeRow.scope_id == scope_id,
                    BinaryTransferScopeRow.state == "active",
                )
                .values(state="cancelled" if cancelled else "finished")
            )
            await db.commit()
        if cancelled:
            await self.cleanup(scope_id=scope_id)

    async def cleanup(self, *, scope_id: str | None = None) -> None:
        """Reclaim abandoned bytes without racing active writes or publication."""
        async with self.sessions() as db:
            now = await database_now(db)
            await db.execute(
                update(BinaryTransferScopeRow)
                .where(
                    BinaryTransferScopeRow.state == "active",
                    BinaryTransferScopeRow.expires_at < now,
                    ~select(BinaryTransferRow.transfer_id)
                    .where(
                        BinaryTransferRow.scope_id == BinaryTransferScopeRow.scope_id,
                        BinaryTransferRow.state == "receiving",
                        BinaryTransferRow.updated_at > now - timedelta(minutes=2),
                    )
                    .exists(),
                )
                .values(state="expired")
            )
            query = (
                select(BinaryTransferRow)
                .join(BinaryTransferScopeRow)
                .where(
                    (BinaryTransferScopeRow.state.in_(("cancelled", "expired")))
                    | (
                        (BinaryTransferScopeRow.state == "finished")
                        & (BinaryTransferScopeRow.expires_at < now)
                    ),
                    BinaryTransferRow.state.in_(
                        ("issued", "uploaded", "failed", "receiving", "deleting")
                    ),
                    BinaryTransferRow.updated_at < now - timedelta(minutes=2),
                )
            )
            if scope_id is not None:
                query = query.where(BinaryTransferScopeRow.scope_id == scope_id)
            rows = list((await db.scalars(query)).all())
            await db.commit()
        for row in rows:
            async with self.sessions() as db:
                claimed = await db.execute(
                    update(BinaryTransferRow)
                    .where(
                        BinaryTransferRow.transfer_id == row.transfer_id,
                        BinaryTransferRow.state.in_(
                            ("issued", "uploaded", "failed", "receiving", "deleting")
                        ),
                    )
                    .values(state="deleting", updated_at=database_now_expression(db))
                    .returning(BinaryTransferRow.transfer_id)
                )
                if claimed.scalar_one_or_none() is None:
                    continue
                await db.commit()
            await self.store.async_discard_staged_object(row.namespace, row.artifact_id)
            async with self.sessions() as db:
                await db.execute(
                    update(BinaryTransferRow)
                    .where(
                        BinaryTransferRow.transfer_id == row.transfer_id,
                        BinaryTransferRow.state == "deleting",
                    )
                    .values(state="deleted")
                )
                await db.commit()
