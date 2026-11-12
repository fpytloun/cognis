"""Revocable public comment capabilities and bounded, untrusted pixel adoption."""

from __future__ import annotations

import asyncio
import hmac
import io
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from cognis.core.feedback.primitives import (
    Actor,
    FeedbackError,
    Payloads,
    Tokens,
    affected,
    aware,
    digest,
    encode,
    identifier,
    source_actor,
)
from cognis.core.feedback.sources import SOURCE_ADAPTER, Sources
from cognis.core.feedback.threads import Threads
from cognis.models import feedback as dto
from cognis.store import feedback_models as rows
from cognis.store.models import ArtifactRecordRow, User


class PublicFeedback:
    """Public tokens grant only the pinned viewer/discussion, never dispatch."""

    def __init__(self, sources: Sources, payloads: Payloads, tokens: Tokens) -> None:
        self.sources, self.payloads, self.tokens = sources, payloads, tokens
        self.factory = sources.factory

    async def rate(self, actor: Actor, operation: str, limit: int = 30) -> None:
        now = datetime.now(UTC)
        window = now.replace(second=0, microsecond=0)
        key = digest(encode([actor.identity, actor.share_id, operation, window.isoformat()]))
        async with self.factory() as db:
            insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
            statement = (
                insert(rows.FeedbackRateBucketRow)
                .values(
                    bucket_key=key,
                    window_start=int(window.timestamp()),
                    count=1,
                )
                .on_conflict_do_update(
                    index_elements=["bucket_key"],
                    set_={"count": rows.FeedbackRateBucketRow.count + 1},
                    where=rows.FeedbackRateBucketRow.count < limit,
                )
                .returning(rows.FeedbackRateBucketRow.count)
            )
            if await db.scalar(statement) is None:
                raise FeedbackError(429, "rate_limited", "Feedback rate limit exceeded")
            await db.execute(
                delete(rows.FeedbackRateBucketRow).where(
                    rows.FeedbackRateBucketRow.window_start
                    < int((now - timedelta(days=2)).timestamp())
                )
            )
            await db.commit()

    async def grant(
        self, actor: Actor, subject_id: str, request: dto.GrantRequest
    ) -> dto.GrantResponse:
        grant_payload = await self.payloads.put(actor.identity, request.model_dump(mode="json"))
        async with self.factory() as db:
            subject, _ = await self.sources.authorize(db, actor, subject_id)
            if actor.share_id or actor.user_email != subject.owner_email:
                raise FeedbackError(
                    403, "feedback_forbidden", "Only the source owner can grant feedback"
                )
            user = await db.get(User, request.principal_email)
            if user is None or not await self.sources.content_access(
                db, subject, request.principal_email
            ):
                raise FeedbackError(404, "not_found", "Recipient must already have source access")
            if (
                request.capabilities.comment
                or request.capabilities.moderate
                or request.capabilities.dispatch
                or request.capabilities.disclose
            ) and not request.capabilities.read:
                raise FeedbackError(
                    422, "invalid_grant", "Feedback operations require feedback read"
                )
            row = await db.get(rows.FeedbackGrantRow, (subject_id, request.principal_email))
            if row is None:
                if request.expected_version:
                    raise FeedbackError(409, "version_conflict", "Feedback grant changed")
                row = rows.FeedbackGrantRow(
                    subject_id=subject_id,
                    principal_email=request.principal_email,
                    version=1,
                    capabilities=request.capabilities.model_dump(mode="json"),
                )
                db.add(row)
            else:
                changed = await db.execute(
                    update(rows.FeedbackGrantRow)
                    .where(
                        rows.FeedbackGrantRow.subject_id == subject_id,
                        rows.FeedbackGrantRow.principal_email == request.principal_email,
                        rows.FeedbackGrantRow.version == request.expected_version,
                    )
                    .values(
                        version=request.expected_version + 1,
                        capabilities=request.capabilities.model_dump(mode="json"),
                    )
                )
                if affected(changed) != 1:
                    raise FeedbackError(409, "version_conflict", "Feedback grant changed")
            db.add(
                rows.FeedbackAuditRow(
                    audit_id=identifier("fba"),
                    subject_id=subject_id,
                    actor_id=actor.identity,
                    kind="grant_changed",
                    payload_id=grant_payload,
                )
            )
            await self.payloads.adopt(db, grant_payload)
            try:
                await db.commit()
            except IntegrityError as exc:
                raise FeedbackError(409, "version_conflict", "Feedback grant changed") from exc
            await db.refresh(row)
        return dto.GrantResponse(
            expected_version=request.expected_version,
            principal_email=request.principal_email,
            version=row.version,
            capabilities=request.capabilities,
        )

    def share_dto(self, row: rows.FeedbackShareRow, token: str | None = None) -> dto.ShareResponse:
        return dto.ShareResponse(
            share_id=row.share_id,
            version=row.version,
            share_token=token,
            allow_comments=row.allow_comments,
            discussion=row.discussion,
            expires_at=row.expires_at,
            revoked=row.revoked,
            can_copy=self._recover_share_token(row) is not None,
            link_mode=row.link_mode,
        )

    def _recover_share_token(self, row: rows.FeedbackShareRow) -> str | None:
        if (
            row.token_format != "derived_v1"
            or row.revoked
            or aware(row.expires_at) <= datetime.now(UTC)
        ):
            return None
        secret = self.tokens.public_share_secret(row.share_id)
        if not hmac.compare_digest(digest(secret.encode()), row.token_hash):
            return None
        return f"{row.share_id}.{secret}"

    async def copy_share(self, actor: Actor, share_id: str) -> dto.ShareResponse:
        """Owner-only recovery does not rotate, broaden, extend or revive a link."""
        async with self.factory() as db:
            row = await db.get(rows.FeedbackShareRow, share_id)
            if row is None:
                raise FeedbackError(404, "not_found", "Share is unavailable")
            subject, _ = await self.sources.authorize(db, actor, row.subject_id)
            if actor.share_id or actor.user_email != subject.owner_email:
                raise FeedbackError(403, "feedback_forbidden", "Only the owner can copy this link")
            token = self._recover_share_token(row)
            if token is None:
                raise FeedbackError(
                    409, "link_unrecoverable", "This link cannot be recovered; create another link"
                )
            return self.share_dto(row, token)

    async def create_share(
        self, actor: Actor, subject_id: str, request: dto.ShareRequest
    ) -> dto.ShareResponse:
        await self.rate(actor, "share", 10)
        if request.link_mode == "download" and request.allow_comments:
            raise FeedbackError(422, "invalid_share", "Download links do not enable commenting")
        async with self.factory() as db:
            subject, _ = await self.sources.authorize(db, actor, subject_id)
            if actor.share_id or actor.user_email != subject.owner_email:
                raise FeedbackError(
                    403, "feedback_forbidden", "Only the owner can share this source"
                )
            if subject.source.get("kind") != "document":
                raise FeedbackError(
                    422, "unsupported_share", "Public shares expose one document revision"
                )
            if (
                subject.source != request.source.model_dump(mode="json")
                or subject.content_digest != request.expected_content_digest
            ):
                raise FeedbackError(409, "source_changed", "Share revision or digest changed")
            if aware(request.expires_at) <= datetime.now(UTC):
                raise FeedbackError(422, "invalid_expiry", "Share expiry must be in the future")
            expires_at = aware(request.expires_at)
            if request.source.content_ref.startswith("art_"):
                artifact = await db.get(ArtifactRecordRow, request.source.content_ref)
                if artifact is not None and artifact.expires_at is not None:
                    expires_at = min(expires_at, aware(artifact.expires_at))
            share_id = identifier("fsh")
            secret = self.tokens.public_share_secret(share_id)
            row = rows.FeedbackShareRow(
                share_id=share_id,
                subject_id=subject_id,
                owner_email=subject.owner_email,
                token_hash=digest(secret.encode()),
                token_format="derived_v1",
                link_mode=request.link_mode,
                source=request.source.model_dump(mode="json"),
                content_digest=request.expected_content_digest,
                version=1,
                allow_comments=request.allow_comments,
                discussion=request.discussion,
                expires_at=expires_at,
                revoked=False,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return self.share_dto(row, f"{row.share_id}.{secret}")

    async def update_share(
        self, actor: Actor, share_id: str, request: dto.ShareUpdateRequest
    ) -> dto.ShareResponse:
        async with self.factory() as db:
            row = await db.get(rows.FeedbackShareRow, share_id)
            if row is None or row.owner_email != actor.user_email or actor.share_id:
                raise FeedbackError(404, "not_found", "Share is unavailable")
            await self.sources.authorize(db, actor, row.subject_id)
            values = request.model_dump(exclude={"expected_version"}, exclude_none=True)
            if row.link_mode == "download" and request.allow_comments:
                raise FeedbackError(422, "invalid_share", "Download links do not enable commenting")
            if request.expires_at is not None and aware(request.expires_at) <= datetime.now(UTC):
                raise FeedbackError(422, "invalid_expiry", "Share expiry must be in the future")
            changed = await db.execute(
                update(rows.FeedbackShareRow)
                .where(
                    rows.FeedbackShareRow.share_id == share_id,
                    rows.FeedbackShareRow.version == request.expected_version,
                )
                .values(**values, version=request.expected_version + 1)
            )
            if affected(changed) != 1:
                raise FeedbackError(409, "version_conflict", "Share changed")
            await db.commit()
            await db.refresh(row)
        return self.share_dto(row)

    async def exchange(self, token: str) -> dto.PublicExchangeResponse:
        try:
            share_id, secret = token.split(".", 1)
        except ValueError as exc:
            raise FeedbackError(404, "not_found", "Share is unavailable") from exc
        async with self.factory() as db:
            share = await db.get(rows.FeedbackShareRow, share_id)
            if (
                share is None
                or not secrets.compare_digest(digest(secret.encode()), share.token_hash)
                or share.revoked
                or aware(share.expires_at) <= datetime.now(UTC)
            ):
                raise FeedbackError(404, "not_found", "Share is unavailable")
        actor = Actor(identity=f"share:{share_id}", label="Public viewer", share_id=share_id)
        await self.rate(actor, "exchange", 60)
        share = await self.follow_latest(share)
        async with self.factory() as db:
            subject, _ = await self.sources.authorize(db, actor, share.subject_id)
        evidence = await self.sources.load(
            subject.owner_email, SOURCE_ADAPTER.validate_python(subject.source)
        )
        if evidence.content_digest != share.content_digest:
            raise FeedbackError(410, "source_unavailable", "Exact shared source is unavailable")
        expires = min(aware(share.expires_at), datetime.now(UTC) + timedelta(hours=1))
        capability = self.tokens.seal(
            {
                "kind": "public_share",
                "actor": "public",
                "share_id": share_id,
                "version": share.version,
                "exp": expires.timestamp(),
            }
        )
        descriptor = self.sources.descriptor(
            actor,
            dto.StandaloneFeedbackContext(kind="standalone"),
            subject,
            evidence,
            dto.FeedbackCapabilities(read=True, comment=share.allow_comments),
        )
        return dto.PublicExchangeResponse(
            link_mode=share.link_mode,
            share_id=share_id,
            capability=capability,
            expires_at=expires,
            source=dto.PublicSource(
                source_token=descriptor.source_token,
                content_ref=subject.source["content_ref"],
                content_digest=subject.content_digest,
                format=evidence.format,
                title=evidence.title,
            ),
            allow_comments=share.allow_comments,
            discussion=share.discussion,
            permitted_locator_kinds=evidence.locator_kinds,
        )

    async def follow_latest(self, share: rows.FeedbackShareRow) -> rows.FeedbackShareRow:
        """Point a public link at the newest revision of its document.

        Outside viewers always see the latest document, never an older revision. The share row
        moves to the newest revision's comment subject, so every existing check keeps comparing
        the link with exactly what it exposes. The version bump retires capabilities that were
        issued for the previous revision; comments made there stay on that revision.
        """
        source = SOURCE_ADAPTER.validate_python(share.source)
        if not isinstance(source, dto.DocumentSourceRef):
            return share
        current = await self.sources.load(share.owner_email, source)
        latest_ref = current.latest_content_ref
        if not latest_ref or latest_ref == source.content_ref:
            return share
        latest = await self.sources.load(
            share.owner_email, dto.DocumentSourceRef(kind="document", content_ref=latest_ref)
        )
        # A revision chain never crosses owners; refuse rather than publish someone else's file.
        if latest.owner_email != share.owner_email or latest.lineage_id != current.lineage_id:
            return share
        subject = await self.sources.subject_for(latest)
        async with self.factory() as db:
            await db.execute(
                update(rows.FeedbackShareRow)
                .where(
                    rows.FeedbackShareRow.share_id == share.share_id,
                    rows.FeedbackShareRow.version == share.version,
                )
                .values(
                    subject_id=subject.subject_id,
                    source=subject.source,
                    content_digest=subject.content_digest,
                    version=share.version + 1,
                )
            )
            await db.commit()
            # Another viewer may have moved it first; either way the stored row is current.
            moved = await db.get(rows.FeedbackShareRow, share.share_id, populate_existing=True)
        if moved is None or moved.revoked or aware(moved.expires_at) <= datetime.now(UTC):
            raise FeedbackError(404, "not_found", "Share is unavailable")
        return moved

    async def actor(
        self,
        capability: str,
        guest_token: str | None = None,
        verified_email: str | None = None,
    ) -> tuple[Actor, rows.FeedbackShareRow]:
        value = self.tokens.open(capability, kind="public_share", actor="public")
        async with self.factory() as db:
            share = await db.get(rows.FeedbackShareRow, value["share_id"])
            if (
                share is None
                or share.version != value["version"]
                or share.revoked
                or aware(share.expires_at) <= datetime.now(UTC)
            ):
                raise FeedbackError(404, "not_found", "Share is unavailable")
            actor = Actor(
                identity=f"share:{share.share_id}", label="Public viewer", share_id=share.share_id
            )
            if guest_token:
                try:
                    guest_id, secret = guest_token.split(".", 1)
                except ValueError as exc:
                    raise FeedbackError(
                        403, "invalid_guest", "Guest session is unavailable"
                    ) from exc
                guest = await db.get(rows.FeedbackGuestSessionRow, guest_id)
                if (
                    guest is None
                    or guest.share_id != share.share_id
                    or not secrets.compare_digest(digest(secret.encode()), guest.token_hash)
                    or guest.revoked
                    or aware(guest.expires_at) <= datetime.now(UTC)
                ):
                    raise FeedbackError(403, "invalid_guest", "Guest session is unavailable")
                actor = Actor(
                    identity=verified_email or guest_id,
                    label="Signed-in reviewer"
                    if verified_email
                    else guest.display_name or "Guest reviewer",
                    user_email=verified_email,
                    share_id=share.share_id,
                    guest_session_id=guest_id,
                )
            await self.sources.authorize(db, actor, share.subject_id)
        return actor, share

    async def guest(
        self, capability: str, request: dto.GuestSessionRequest
    ) -> dto.GuestSessionResponse:
        actor, share = await self.actor(capability)
        await self.rate(actor, "guest", 10)
        if not share.allow_comments:
            raise FeedbackError(403, "comments_disabled", "Comments are disabled")
        secret, guest_id = secrets.token_urlsafe(32), identifier("fbg")
        expires = min(aware(share.expires_at), datetime.now(UTC) + timedelta(days=7))
        async with self.factory() as db:
            db.add(
                rows.FeedbackGuestSessionRow(
                    guest_session_id=guest_id,
                    share_id=share.share_id,
                    token_hash=digest(secret.encode()),
                    display_name=request.display_name or "Guest reviewer",
                    expires_at=expires,
                )
            )
            await db.commit()
        return dto.GuestSessionResponse(
            guest_session_id=guest_id,
            guest_capability=f"{guest_id}.{secret}",
            expires_at=expires,
        )

    async def grants(self, actor: Actor, subject_id: str) -> dto.GrantListResponse:
        async with self.factory() as db:
            subject, _ = await self.sources.authorize(db, actor, subject_id)
            if actor.user_email != subject.owner_email or actor.share_id:
                raise FeedbackError(403, "feedback_forbidden", "Only the owner can list grants")
            grants = (
                await db.scalars(
                    select(rows.FeedbackGrantRow)
                    .where(
                        rows.FeedbackGrantRow.subject_id == subject_id,
                    )
                    .order_by(rows.FeedbackGrantRow.principal_email)
                )
            ).all()
        return dto.GrantListResponse(
            items=[
                dto.GrantResponse(
                    principal_email=grant.principal_email,
                    expected_version=grant.version,
                    version=grant.version,
                    capabilities=dto.FeedbackCapabilities.model_validate(grant.capabilities),
                )
                for grant in grants
            ]
        )

    async def shares(self, actor: Actor, subject_id: str) -> dto.ShareListResponse:
        async with self.factory() as db:
            subject, _ = await self.sources.authorize(db, actor, subject_id)
            if actor.user_email != subject.owner_email or actor.share_id:
                raise FeedbackError(403, "feedback_forbidden", "Only the owner can list shares")
            # Links follow the newest revision, so list them for the whole document.
            lineage_subjects = select(rows.FeedbackSubjectRow.subject_id).where(
                rows.FeedbackSubjectRow.owner_email == subject.owner_email,
                or_(
                    rows.FeedbackSubjectRow.subject_id == subject_id,
                    rows.FeedbackSubjectRow.lineage_id == subject.lineage_id
                    if subject.lineage_id
                    else rows.FeedbackSubjectRow.subject_id == subject_id,
                ),
            )
            shares = (
                await db.scalars(
                    select(rows.FeedbackShareRow)
                    .where(
                        rows.FeedbackShareRow.owner_email == subject.owner_email,
                        rows.FeedbackShareRow.subject_id.in_(lineage_subjects),
                    )
                    .order_by(rows.FeedbackShareRow.share_id)
                )
            ).all()
        return dto.ShareListResponse(items=[self.share_dto(share) for share in shares])

    async def publish_outcome(
        self, actor: Actor, share_id: str, request: dto.PublicOutcomeRequest
    ) -> dto.PublicOutcome:
        async with self.factory() as db:
            share = await db.get(rows.FeedbackShareRow, share_id)
            if share is None or share.owner_email != actor.user_email or actor.share_id:
                raise FeedbackError(404, "not_found", "Comment share is unavailable")
            await self.sources.authorize(db, actor, share.subject_id, "moderate")
            version = await db.get(rows.FeedbackEntryVersionRow, request.entry_version_id)
            entry = await db.get(rows.FeedbackEntryRow, version.entry_id) if version else None
            thread = await db.get(rows.FeedbackThreadRow, entry.thread_id) if entry else None
            if thread is None or thread.audience != f"public:{share_id}":
                raise FeedbackError(404, "not_found", "Public entry is unavailable")
        payload = await self.payloads.put(actor.identity, {"reason": request.reason})
        async with self.factory() as db:
            await self.sources.authorize(db, actor, share.subject_id, "moderate")
            db.add(
                rows.FeedbackPublicOutcomeRow(
                    outcome_id=identifier("fbpo"),
                    share_id=share_id,
                    entry_version_id=request.entry_version_id,
                    disposition=request.disposition,
                    payload_id=payload,
                )
            )
            await self.payloads.adopt(db, payload)
            await db.commit()
        return dto.PublicOutcome(
            entry_version_id=request.entry_version_id,
            thread_id=thread.thread_id,
            disposition=request.disposition,
            reason=request.reason,
        )

    async def outcomes(self, actor: Actor) -> dto.PublicOutcomePage:
        async with self.factory() as db:
            share = await db.get(rows.FeedbackShareRow, actor.share_id)
            if share is None:
                raise FeedbackError(404, "not_found", "Comment share is unavailable")
            await self.sources.authorize(db, actor, share.subject_id)
            candidates = (
                await db.execute(
                    select(
                        rows.FeedbackPublicOutcomeRow, rows.FeedbackEntryRow, rows.FeedbackThreadRow
                    )
                    .join(
                        rows.FeedbackEntryVersionRow,
                        rows.FeedbackEntryVersionRow.entry_version_id
                        == rows.FeedbackPublicOutcomeRow.entry_version_id,
                    )
                    .join(
                        rows.FeedbackEntryRow,
                        rows.FeedbackEntryRow.entry_id == rows.FeedbackEntryVersionRow.entry_id,
                    )
                    .join(
                        rows.FeedbackThreadRow,
                        rows.FeedbackThreadRow.thread_id == rows.FeedbackEntryRow.thread_id,
                    )
                    .where(
                        rows.FeedbackPublicOutcomeRow.share_id == share.share_id,
                        rows.FeedbackThreadRow.hidden.is_(False),
                    )
                    .order_by(rows.FeedbackPublicOutcomeRow.created_at.desc())
                    .limit(100)
                )
            ).all()
        items = []
        for outcome, entry, thread in candidates:
            if share.discussion == "own_only" and entry.author_id != actor.identity:
                continue
            payload = await self.payloads.get(outcome.payload_id)
            items.append(
                dto.PublicOutcome(
                    entry_version_id=outcome.entry_version_id,
                    thread_id=thread.thread_id,
                    disposition=outcome.disposition,
                    reason=payload["reason"],
                )
            )
        return dto.PublicOutcomePage(items=items)


class Captures:
    """Source-bound uploads of user-confirmed pixels; never a renderer or attachment API."""

    MAX_BYTES = 8 * 1024 * 1024
    MAX_PIXELS = 16_000_000

    def __init__(
        self, sources: Sources, payloads: Payloads, public: PublicFeedback, threads: Threads
    ) -> None:
        self.sources, self.payloads, self.public = sources, payloads, public
        self.threads = threads
        self.factory = sources.factory

    async def create(self, actor: Actor, request: dto.CaptureRequest) -> dto.CaptureResponse:
        await self.public.rate(actor, "capture", 10)
        token = self.sources.tokens.open(
            request.source_token, kind="source", actor=source_actor(actor)
        )
        async with self.factory() as db:
            subject, _ = await self.sources.authorize(db, actor, token["subject_id"], "comment")
            if (
                subject.content_digest != request.content_digest
                or token["digest"] != request.content_digest
            ):
                raise FeedbackError(409, "source_changed", "Capture source changed")
            if subject.source.get("kind") != "document":
                raise FeedbackError(
                    422, "invalid_capture", "Capture requires one document revision"
                )
            evidence_source = SOURCE_ADAPTER.validate_python(subject.source)
            existing = await db.scalar(
                select(rows.FeedbackCaptureRow).where(
                    rows.FeedbackCaptureRow.actor_id == actor.identity,
                    rows.FeedbackCaptureRow.creation_key == request.creation_key,
                )
            )
        evidence = await self.sources.load(
            subject.owner_email if actor.share_id else actor.identity, evidence_source
        )
        if evidence.format != "html":
            raise FeedbackError(422, "invalid_capture", "Observed capture is for HTML annotations")
        fingerprint = digest(encode(request.model_dump(mode="json")))
        if existing:
            if existing.request_hash != fingerprint:
                raise FeedbackError(409, "idempotency_conflict", "Capture key was already used")
            return self.response(existing)
        row = rows.FeedbackCaptureRow(
            capture_id=identifier("fbc"),
            subject_id=subject.subject_id,
            actor_id=actor.identity,
            share_id=actor.share_id,
            creation_key=request.creation_key,
            request_hash=fingerprint,
            viewport=request.viewport.model_dump(mode="json"),
            render_state_digest=request.render_state_digest,
            status="awaiting_pixels",
            version=1,
        )
        async with self.factory() as db:
            await self.sources.authorize(db, actor, subject.subject_id, "comment")
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return await self.create(actor, request)
            await db.refresh(row)
        return self.response(row)

    @classmethod
    def response(cls, row: rows.FeedbackCaptureRow) -> dto.CaptureResponse:
        return dto.CaptureResponse(
            capture_id=row.capture_id,
            status=row.status,
            version=row.version,
            upload_limit_bytes=cls.MAX_BYTES,
            width=row.width,
            height=row.height,
        )

    async def get(self, actor: Actor, capture_id: str) -> rows.FeedbackCaptureRow:
        async with self.factory() as db:
            row = await db.get(rows.FeedbackCaptureRow, capture_id)
            if row is None or (actor.share_id is not None and row.share_id != actor.share_id):
                raise FeedbackError(404, "not_found", "Capture is unavailable")
            await self.sources.authorize(db, actor, row.subject_id)
            if row.actor_id != actor.identity:
                capture_anchors = select(rows.FeedbackAnchorRow.anchor_id).where(
                    rows.FeedbackAnchorRow.capture_id == capture_id
                )
                mapped_origins = select(rows.FeedbackAnchorMappingRow.original_anchor_id).where(
                    rows.FeedbackAnchorMappingRow.mapped_anchor_id.in_(capture_anchors)
                )
                references = (
                    await db.scalars(
                        select(rows.FeedbackThreadRow).where(
                            or_(
                                rows.FeedbackThreadRow.anchor_id.in_(capture_anchors),
                                rows.FeedbackThreadRow.anchor_id.in_(mapped_origins),
                            )
                        )
                    )
                ).all()
                visible = False
                for reference in references:
                    try:
                        await self.threads.visible(db, actor, reference)
                    except FeedbackError:
                        continue
                    visible = True
                    break
                if not visible:
                    raise FeedbackError(404, "not_found", "Capture is unavailable")
        return row

    @classmethod
    def reencode(cls, data: bytes, width: int, height: int) -> bytes:
        from PIL import Image, UnidentifiedImageError

        if len(data) > cls.MAX_BYTES or width * height > cls.MAX_PIXELS:
            raise FeedbackError(413, "capture_budget", "Observed image exceeds capture limits")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "WEBP"} or image.size != (width, height):
                    raise FeedbackError(
                        422, "invalid_capture", "Image format or dimensions do not match"
                    )
                if (
                    image.width * image.height > cls.MAX_PIXELS
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise FeedbackError(
                        413, "capture_budget", "Animated or oversized images are not accepted"
                    )
                image.load()
                output = io.BytesIO()
                image.convert("RGBA").save(output, format="PNG")
                if output.tell() > cls.MAX_BYTES:
                    raise FeedbackError(
                        413, "capture_budget", "Normalized capture exceeds size limit"
                    )
                return output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise FeedbackError(
                422, "invalid_capture", "Raster image could not be decoded"
            ) from exc

    async def adopt(
        self, actor: Actor, capture_id: str, expected: int, width: int, height: int, data: bytes
    ) -> dto.CaptureResponse:
        row = await self.get(actor, capture_id)
        if row.actor_id != actor.identity or row.status != "awaiting_pixels":
            raise FeedbackError(
                409, "capture_immutable", "Capture is already adopted or unavailable"
            )
        await self.public.rate(actor, "pixels", 10)
        normalized = await asyncio.to_thread(self.reencode, data, width, height)
        payload = await self.payloads.put_bytes(actor.identity, normalized, "image/png")
        async with self.factory() as db:
            await self.sources.authorize(db, actor, row.subject_id, "comment")
            changed = await db.execute(
                update(rows.FeedbackCaptureRow)
                .where(
                    rows.FeedbackCaptureRow.capture_id == capture_id,
                    rows.FeedbackCaptureRow.actor_id == actor.identity,
                    rows.FeedbackCaptureRow.version == expected,
                    rows.FeedbackCaptureRow.status == "awaiting_pixels",
                )
                .values(
                    status="ready",
                    version=expected + 1,
                    width=width,
                    height=height,
                    payload_id=payload,
                )
            )
            if affected(changed) != 1:
                raise FeedbackError(409, "version_conflict", "Capture changed")
            await self.payloads.adopt(db, payload)
            await db.commit()
        return self.response(await self.get(actor, capture_id))
