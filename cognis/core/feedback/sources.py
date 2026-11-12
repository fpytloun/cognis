"""Resolve exact content and independently enforce feedback/content capabilities."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.artifacts.store import ArtifactStore
from cognis.core.feedback.diff_anchors import validate_diff_lines
from cognis.core.feedback.primitives import (
    Actor,
    FeedbackError,
    Tokens,
    aware,
    digest,
    encode,
    identifier,
    source_actor,
)
from cognis.core.project_access import (
    can_read_conversation,
)
from cognis.models import feedback as dto
from cognis.models.deliverable import DeliverableStatus
from cognis.store import feedback_models as rows
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.document_people import direct_grant, person_may_read_document
from cognis.store.models import ArtifactRecordRow, Conversation, DeliverableRow, Session, StepRun
from cognis.store.queries import get_deliverable_lineage_head

SOURCE_ADAPTER: TypeAdapter[dto.FeedbackSourceRef] = TypeAdapter(dto.FeedbackSourceRef)
CONTEXT_ADAPTER: TypeAdapter[dto.FeedbackContext] = TypeAdapter(dto.FeedbackContext)


def canonical_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_slice(text: str, start: int, end: int) -> str:
    try:
        return text.encode("utf-16-le")[start * 2 : end * 2].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise FeedbackError(422, "invalid_anchor", "Selection splits a Unicode character") from exc


def describe_text(text: str) -> dto.CanonicalText:
    length = utf16_length(text)
    return dto.CanonicalText(
        text_digest=digest(text.encode()), length=length, available_ranges=[(0, length)]
    )


def rich_text(value: Any) -> str:
    """Version-one deterministic traversal; offsets never refer to pretty-printed JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(rich_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(rich_text(value[key]) for key in sorted(value))
    return ""


@dataclass
class SourceEvidence:
    """Authorized bytes and metadata from the canonical source owner."""

    source: dto.FeedbackSourceRef
    owner_email: str
    project_id: str | None
    data: bytes
    format: str
    title: str | None
    text: str | None = None
    lineage_id: str | None = None
    revision: int | None = None
    latest_content_ref: str | None = None
    availability: str = "available"
    locator_kinds: list[str] = field(default_factory=lambda: ["whole"])
    rich_payload: dict[str, Any] | None = None
    producer_conversation_id: str | None = None
    producer_agent_id: str | None = None
    task_id: str | None = None
    step_name: str | None = None
    mime_type: str | None = None

    @property
    def content_digest(self) -> str:
        return digest(self.data)


EventLoader = Callable[[str, dto.FeedbackSourceRef], Awaitable[SourceEvidence]]


class Sources:
    """Content access is checked separately from every feedback permission."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        tokens: Tokens,
        event_loader: EventLoader,
    ) -> None:
        self.factory = factory
        self.store = store
        self.tokens = tokens
        self.event_loader = event_loader

    async def context(self, db: AsyncSession, actor: Actor, context: dto.FeedbackContext) -> None:
        if context.kind == "chat":
            conversation = await db.get(Conversation, context.conversation_id)
            if (
                actor.user_email is None
                or conversation is None
                or conversation.user_email != actor.user_email
                or conversation.status in {"deleted", "archived"}
            ):
                raise FeedbackError(404, "not_found", "Originating conversation is unavailable")

    async def content_access(
        self, db: AsyncSession, subject: rows.FeedbackSubjectRow, email: str
    ) -> bool:
        source = SOURCE_ADAPTER.validate_python(subject.source)
        if source.kind == "document":
            return await self.document_access(db, email, source.content_ref)
        session = await db.get(Session, source.source_event.session_id)
        if session is None:
            return False
        conversation = await db.get(Conversation, session.conversation_id)
        return bool(
            conversation
            and conversation.status != "deleted"
            and await can_read_conversation(db, conversation, email)
        )

    async def authorize(
        self,
        db: AsyncSession,
        actor: Actor,
        subject_id: str,
        capability: str = "read",
    ) -> tuple[rows.FeedbackSubjectRow, dto.FeedbackCapabilities]:
        subject = await db.get(rows.FeedbackSubjectRow, subject_id)
        if subject is None:
            raise FeedbackError(404, "not_found", "Feedback source is unavailable")
        if actor.share_id:
            share = await db.get(rows.FeedbackShareRow, actor.share_id)
            if (
                share is None
                or share.subject_id != subject_id
                or share.revoked
                or share.source != subject.source
                or share.content_digest != subject.content_digest
                or aware(share.expires_at) <= datetime.now(UTC)
                or not await self.content_access(db, subject, share.owner_email)
            ):
                raise FeedbackError(404, "not_found", "Comment share is unavailable")
            caps = dto.FeedbackCapabilities(
                read=True, comment=share.allow_comments and actor.guest_session_id is not None
            )
        elif actor.user_email and await self.content_access(db, subject, actor.user_email):
            if subject.owner_email == actor.user_email:
                caps = dto.FeedbackCapabilities(
                    read=True, comment=True, moderate=True, dispatch=True, disclose=True
                )
            else:
                grant = await db.get(rows.FeedbackGrantRow, (subject.subject_id, actor.user_email))
                source = SOURCE_ADAPTER.validate_python(subject.source)
                shared_comment = False
                if source.kind == "document":
                    model = (
                        DeliverableRow
                        if source.content_ref.startswith("dlv_")
                        else ArtifactRecordRow
                    )
                    document = await db.get(model, source.content_ref)
                    access = (
                        await direct_grant(db, document, actor.user_email) if document else None
                    )
                    shared_comment = bool(access and access.allow_comments)
                caps = dto.FeedbackCapabilities.model_validate(
                    grant.capabilities if grant else {"read": True, "comment": shared_comment}
                )
        else:
            raise FeedbackError(404, "not_found", "Feedback source is unavailable")
        if not getattr(caps, capability, False):
            raise FeedbackError(403, "feedback_forbidden", "Feedback operation is not permitted")
        return subject, caps

    async def document_access(self, db: AsyncSession, email: str, content_ref: str) -> bool:
        """Authorize exact document metadata without loading its private bytes."""
        if content_ref.startswith("dlv_"):
            document = await db.get(DeliverableRow, content_ref)
            # Exact reads include historical states; only lineage heads must be live.
            return bool(
                document
                and document.status in DeliverableStatus
                and (await person_may_read_document(db, document, email))
            )
        artifact = await db.get(ArtifactRecordRow, content_ref)
        return bool(
            artifact
            and artifact.deleted_at is None
            and artifact.status not in {"deleted", "expired", "deleting"}
            and (artifact.expires_at is None or aware(artifact.expires_at) > datetime.now(UTC))
            and (await person_may_read_document(db, artifact, email))
        )

    async def document(self, email: str, source: dto.DocumentSourceRef) -> SourceEvidence:
        async with self.factory() as db:
            if source.content_ref.startswith("dlv_"):
                document = await db.get(DeliverableRow, source.content_ref)
                if (
                    document is None
                    or not document.owner_email
                    or not await self.document_access(db, email, source.content_ref)
                ):
                    raise FeedbackError(404, "not_found", "Document is unavailable")
                head = await get_deliverable_lineage_head(
                    db, document.lineage_id or document.deliverable_id
                )
                latest = head.deliverable_id if head is not None else None
                step = None
                if document.step_run_id:
                    step = await db.get(StepRun, document.step_run_id)
                artifact = None
            else:
                artifact = await db.get(ArtifactRecordRow, source.content_ref)
                if (
                    artifact is None
                    or artifact.deleted_at is not None
                    or artifact.status in {"deleted", "expired", "deleting"}
                    or artifact.namespace == "feedback"
                    or (artifact.expires_at and aware(artifact.expires_at) <= datetime.now(UTC))
                    or not artifact.owner_email
                    or not (await person_may_read_document(db, artifact, email))
                ):
                    raise FeedbackError(404, "not_found", "Document is unavailable")
                latest = await db.scalar(
                    select(ArtifactRecordRow.artifact_id)
                    .where(
                        ArtifactRecordRow.lineage_id == artifact.lineage_id,
                        ArtifactRecordRow.deleted_at.is_(None),
                    )
                    .order_by(ArtifactRecordRow.revision.desc())
                    .limit(1)
                )
                document, step = None, None
                if artifact.session_id:
                    step = await db.scalar(
                        select(StepRun).where(StepRun.session_id == artifact.session_id)
                    )
        # Never hold metadata transactions over payload I/O.
        text: str | None
        if document is not None:
            await hydrate_deliverable_payload(document, self.store)
            rich = document.rich_payload
            data = encode(
                {"format": document.format, "content": document.content, "rich_payload": rich}
            )
            text = canonical_text(rich_text(rich) if rich else document.content)
            return SourceEvidence(
                source=source,
                owner_email=document.owner_email or email,
                project_id=document.project_id,
                data=data,
                format=document.format,
                title=document.title,
                text=text,
                lineage_id=document.lineage_id or document.deliverable_id,
                revision=document.revision,
                latest_content_ref=latest or document.deliverable_id,
                locator_kinds=["whole", "rich", "text"] if rich else ["whole", "text"],
                rich_payload=rich,
                producer_conversation_id=document.conversation_id,
                producer_agent_id=document.owner_agent_id,
                task_id=step.task_id if step else None,
                step_name=step.step_name if step else None,
            )
        assert artifact is not None
        if artifact.size_bytes > 50 * 1024 * 1024:
            raise FeedbackError(413, "source_too_large", "Source exceeds the feedback byte limit")
        try:
            data, mime = await self.store.async_load(
                artifact.namespace, artifact.object_id, artifact.filename
            )
        except FileNotFoundError as exc:
            raise FeedbackError(410, "source_unavailable", "Source bytes are unavailable") from exc
        if len(data) > 50 * 1024 * 1024:
            raise FeedbackError(413, "source_too_large", "Source exceeds the feedback byte limit")
        mime = mime.split(";")[0]
        if mime.startswith("image/"):
            format_, kinds, text = "image", ["whole", "image"], None
        elif mime == "application/pdf":
            format_, kinds, text = "pdf", ["whole", "pdf"], None
        elif mime == "text/html" or artifact.filename.lower().endswith((".html", ".htm")):
            format_, kinds, text = "html", ["whole", "html"], None
        else:
            try:
                text = canonical_text(data.decode("utf-8"))
                format_, kinds = "text", ["whole", "text"]
            except UnicodeDecodeError:
                text, format_, kinds = None, "binary", ["whole"]
        return SourceEvidence(
            source=source,
            owner_email=artifact.owner_email or email,
            project_id=artifact.project_id,
            data=data,
            format=format_,
            title=artifact.filename,
            mime_type=mime,
            text=text,
            lineage_id=artifact.lineage_id or artifact.artifact_id,
            revision=artifact.revision,
            latest_content_ref=latest or artifact.artifact_id,
            locator_kinds=kinds,
            producer_conversation_id=artifact.conversation_id,
            producer_agent_id=artifact.owner_agent_id,
            task_id=step.task_id if step else None,
            step_name=step.step_name if step else None,
        )

    async def load(self, email: str, source: dto.FeedbackSourceRef) -> SourceEvidence:
        if source.kind == "document":
            return await self.document(email, source)
        return await self.event_loader(email, source)

    async def resolve(
        self, actor: Actor, request: dto.ResolveSourceRequest
    ) -> dto.ResolvedFeedbackSource:
        if actor.share_id:
            if request.context.kind != "standalone":
                raise FeedbackError(404, "not_found", "Public feedback requires standalone context")
            async with self.factory() as db:
                share = await db.get(rows.FeedbackShareRow, actor.share_id)
                if share is None:
                    raise FeedbackError(404, "not_found", "Comment share is unavailable")
                public_subject, caps = await self.authorize(db, actor, share.subject_id)
                if public_subject.source != request.source.model_dump(mode="json"):
                    raise FeedbackError(
                        404, "not_found", "Only the exact shared document is available"
                    )
            evidence = await self.load(public_subject.owner_email, request.source)
            if evidence.content_digest != share.content_digest:
                raise FeedbackError(410, "source_unavailable", "Exact shared source is unavailable")
            return self.descriptor(actor, request.context, public_subject, evidence, caps)
        if actor.user_email is None:
            raise FeedbackError(403, "feedback_forbidden", "A signed-in source is required")
        async with self.factory() as db:
            await self.context(db, actor, request.context)
        evidence = await self.load(actor.user_email, request.source)
        subject = await self.subject_for(evidence)
        async with self.factory() as db:
            _, caps = await self.authorize(db, actor, subject.subject_id)
        return self.descriptor(actor, request.context, subject, evidence, caps)

    async def subject_for(self, evidence: SourceEvidence) -> rows.FeedbackSubjectRow:
        """Return the comment subject of one exact source, creating it on first use."""
        source_data = evidence.source.model_dump(mode="json")
        key = digest(encode(source_data))
        async with self.factory() as db:
            subject = await db.scalar(
                select(rows.FeedbackSubjectRow).where(rows.FeedbackSubjectRow.source_key == key)
            )
            if subject is None:
                subject = rows.FeedbackSubjectRow(
                    subject_id=identifier("fbs"),
                    source_key=key,
                    owner_email=evidence.owner_email,
                    project_id=evidence.project_id,
                    source=source_data,
                    content_digest=evidence.content_digest,
                    lineage_id=evidence.lineage_id,
                )
                db.add(subject)
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    subject = await db.scalar(
                        select(rows.FeedbackSubjectRow).where(
                            rows.FeedbackSubjectRow.source_key == key
                        )
                    )
                    assert subject is not None
            if subject.content_digest != evidence.content_digest:
                raise FeedbackError(409, "source_changed", "Exact source content changed")
            return subject

    def descriptor(
        self,
        actor: Actor,
        context: dto.FeedbackContext,
        subject: rows.FeedbackSubjectRow,
        evidence: SourceEvidence,
        caps: dto.FeedbackCapabilities,
    ) -> dto.ResolvedFeedbackSource:
        return dto.ResolvedFeedbackSource(
            subject_id=subject.subject_id,
            source=evidence.source,
            source_token=self.tokens.seal(
                {
                    "kind": "source",
                    "actor": source_actor(actor),
                    "subject_id": subject.subject_id,
                    "digest": subject.content_digest,
                    "context": context.model_dump(mode="json"),
                    "exp": (datetime.now(UTC) + timedelta(hours=1)).timestamp(),
                }
            ),
            content_digest=evidence.content_digest,
            availability=evidence.availability,
            context=context,
            lineage_id=evidence.lineage_id,
            revision=evidence.revision,
            latest_content_ref=evidence.latest_content_ref,
            format=evidence.format,
            title=evidence.title,
            canonical_text=(
                describe_text(evidence.text)
                if evidence.text is not None and evidence.format not in {"rich", "html", "pdf"}
                else None
            ),
            capabilities=caps,
            permitted_locator_kinds=evidence.locator_kinds,
        )

    async def validate_anchor(
        self, actor: Actor, anchor: dto.AnchorInput, context: dto.FeedbackContext
    ) -> tuple[rows.FeedbackSubjectRow, SourceEvidence]:
        token = self.tokens.open(anchor.source_token, kind="source", actor=source_actor(actor))
        if token["context"] != context.model_dump(mode="json"):
            raise FeedbackError(409, "stale_context", "Anchor belongs to another feedback context")
        async with self.factory() as db:
            await self.context(db, actor, context)
            subject, _ = await self.authorize(db, actor, token["subject_id"], "comment")
        source = SOURCE_ADAPTER.validate_python(subject.source)
        evidence = await self.load(
            subject.owner_email if actor.share_id else actor.identity, source
        )
        if (
            anchor.content_digest != evidence.content_digest
            or token["digest"] != evidence.content_digest
        ):
            raise FeedbackError(409, "source_changed", "Anchor source changed")
        locator = anchor.locator
        if locator.kind not in evidence.locator_kinds:
            raise FeedbackError(422, "unsupported_anchor", "Anchor is unsupported for this source")
        if isinstance(locator, dto.RichLocator) and locator.table_range is not None:
            from cognis.core.feedback.rich_tables import validate_table_range

            validate_table_range(evidence.rich_payload, locator)
        range_ = getattr(locator, "range", None)
        if range_ is not None and locator.kind != "html":
            text = evidence.text
            if locator.kind == "pdf":
                text = await asyncio.to_thread(self.pdf_page_text, evidence.data, locator.page)
            elif locator.kind == "rich":
                text = self.rich_field_text(
                    evidence.rich_payload,
                    dto.RichFieldTextScope(
                        kind="rich_field",
                        block_id=locator.block_id,
                        identity=locator.identity,
                        item_key=locator.item_key,
                        field_path=locator.field_path,
                    ),
                )
            if text is None:
                raise FeedbackError(422, "invalid_anchor", "Canonical text is unavailable")
            self.validate_range(text, range_)
        if locator.kind == "html" and locator.capture_id:
            async with self.factory() as db:
                capture = await db.get(rows.FeedbackCaptureRow, locator.capture_id)
                if (
                    capture is None
                    or capture.actor_id != actor.identity
                    or capture.subject_id != subject.subject_id
                    or capture.share_id != actor.share_id
                    or capture.status != "ready"
                    or capture.render_state_digest != locator.render_state_digest
                    or capture.viewport != locator.viewport.model_dump(mode="json")
                ):
                    raise FeedbackError(
                        422, "invalid_capture", "Confirmed source-bound capture required"
                    )
        if locator.kind == "diff" and locator.scope == "lines":
            if evidence.availability != "available" or evidence.text is None:
                raise FeedbackError(
                    422, "partial_evidence", "Only file-level feedback is available"
                )
            validate_diff_lines(evidence.text, locator)
        if isinstance(locator, (dto.ImageLocator, dto.PdfLocator)):
            await asyncio.to_thread(self.validate_geometry, evidence.data, locator)
        return subject, evidence

    async def scoped_text(
        self, actor: Actor, request: dto.ScopedTextRequest
    ) -> dto.ScopedCanonicalFeedbackText:
        resolved = await self.resolve(
            actor, dto.ResolveSourceRequest(source=request.source, context=request.context)
        )
        async with self.factory() as db:
            subject, _ = await self.authorize(db, actor, resolved.subject_id)
        evidence = await self.load(
            subject.owner_email if actor.share_id else actor.identity, request.source
        )
        if evidence.content_digest != resolved.content_digest:
            raise FeedbackError(409, "source_changed", "Source changed during text extraction")
        scope = request.scope
        text = await self.text_for_scope(evidence, scope)
        if len(text.encode()) > 8 * 1024 * 1024:
            raise FeedbackError(413, "text_budget", "Scoped text exceeds the extraction budget")
        descriptor = describe_text(text)
        if request.start > descriptor.length:
            raise FeedbackError(422, "invalid_range", "Text window lies outside the scope")
        end = min(descriptor.length, request.start + request.limit)
        # Windows must not split supplementary Unicode characters.
        try:
            selected = utf16_slice(text, request.start, end)
        except FeedbackError:
            if end == request.start:
                raise
            selected = utf16_slice(text, request.start, end - 1)
            end -= 1
        return dto.ScopedCanonicalFeedbackText(
            scope=scope,
            content_digest=evidence.content_digest,
            descriptor=descriptor,
            text_page=dto.CanonicalTextPage(
                text_digest=descriptor.text_digest,
                start=request.start,
                end=end,
                total_length=descriptor.length,
                text=selected,
            ),
        )

    async def text_for_scope(self, evidence: SourceEvidence, scope: dto.FeedbackTextScope) -> str:
        """Extract the same canonical field/page used by selection validation."""
        if scope.kind == "pdf_page":
            if evidence.format != "pdf":
                raise FeedbackError(422, "unsupported_scope", "A PDF page requires a PDF source")
            return await asyncio.to_thread(self.pdf_page_text, evidence.data, scope.page)
        if scope.kind == "rich_field":
            return self.rich_field_text(evidence.rich_payload, scope)
        if evidence.format in {"rich", "pdf", "html"} or evidence.text is None:
            raise FeedbackError(422, "unsupported_scope", "Select an exact field or page")
        return evidence.text

    @staticmethod
    def rich_field_text(payload: dict[str, Any] | None, scope: dto.RichFieldTextScope) -> str:
        from cognis.core.feedback.rich_tables import rich_block

        block, _ = rich_block(payload, scope.block_id, scope.identity)
        value: Any = block
        if scope.item_key is not None:
            items = value.get("items")
            candidates = (
                [
                    item
                    for item in items or []
                    if isinstance(item, dict) and item.get("key") == scope.item_key
                ]
                if isinstance(items, list)
                else []
            )
            if len(candidates) != 1:
                raise FeedbackError(422, "scope_unavailable", "Rich item identity is unavailable")
            value = candidates[0]
        try:
            for key in scope.field_path:
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FeedbackError(422, "scope_unavailable", "Rich field is unavailable") from exc
        if not isinstance(value, str):
            raise FeedbackError(422, "scope_unavailable", "Select one authored text field")
        value = canonical_text(value)
        if scope.field_path and scope.field_path[-1] in {"body", "content", "markdown"}:
            from markdown_it import MarkdownIt

            parts: list[str] = []
            for token in MarkdownIt("commonmark").parse(value):
                if token.type == "inline":
                    parts.append(
                        "".join(
                            "\n" if child.type in {"softbreak", "hardbreak"} else child.content
                            for child in token.children or []
                            if child.type not in {"html_inline"}
                        )
                    )
                elif token.type in {"fence", "code_block"}:
                    parts.append(token.content.rstrip("\n"))
            return "\n".join(parts)
        return str(value)

    @staticmethod
    def validate_range(text: str, range_: dto.TextRange) -> None:
        if (
            digest(text.encode()) != range_.text_digest
            or range_.end > utf16_length(text)
            or utf16_slice(text, range_.start, range_.end) != range_.exact
        ):
            raise FeedbackError(422, "invalid_anchor", "Exact text selection does not match")

    @staticmethod
    def pdf_page_text(data: bytes, page: int) -> str:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if page > len(reader.pages):
            raise FeedbackError(422, "invalid_anchor", "PDF page is unavailable")
        return canonical_text(str(reader.pages[page - 1].extract_text() or ""))

    @staticmethod
    def validate_geometry(data: bytes, locator: dto.ImageLocator | dto.PdfLocator) -> None:
        width: float
        height: float
        if locator.kind == "image":
            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(data)) as image:
                width, height = ImageOps.exif_transpose(image).size
            if (width, height) != (locator.intrinsic_width, locator.intrinsic_height):
                raise FeedbackError(422, "invalid_anchor", "Image dimensions do not match")
        else:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            if locator.page > len(reader.pages):
                raise FeedbackError(422, "invalid_anchor", "PDF page is unavailable")
            page = reader.pages[locator.page - 1]
            width, height = float(page.cropbox.width), float(page.cropbox.height)
            if (
                abs(width - locator.page_width_pt) > 0.1
                or abs(height - locator.page_height_pt) > 0.1
            ):
                raise FeedbackError(422, "invalid_anchor", "PDF dimensions do not match")
        for shape in locator.shapes:
            points: list[dto.Point]
            if shape.kind == "pin":
                points = [shape.point]
            elif shape.kind == "arrow":
                points = [shape.from_, shape.to]
            elif shape.kind == "freehand":
                points = shape.points
            else:
                points = [
                    dto.Point(x=shape.x + shape.width, y=shape.y + shape.height),
                ]
            if any(point.x > width or point.y > height for point in points):
                raise FeedbackError(422, "invalid_anchor", "Shape lies outside the source")
