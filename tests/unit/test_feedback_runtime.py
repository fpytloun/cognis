from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from PIL import Image
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from cognis import json_codec
from cognis.api.authentication import AuthenticatedUser
from cognis.api.routes.feedback import feedback_error_response, public_router, router
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.events import EventBus
from cognis.core.feedback.agents import ExecutionScope, FeedbackAgents
from cognis.core.feedback.comment_links import create_comment_link
from cognis.core.feedback.primitives import Actor, FeedbackError, Payloads, Tokens, digest
from cognis.core.feedback.service import FeedbackService
from cognis.core.feedback.sources import Sources
from cognis.core.feedback.submissions import DispatchCommand
from cognis.core.task_queue import TaskQueue
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.models import feedback as dto
from cognis.models.feedback_tools import FeedbackReadArguments
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState, pin_effective_workflow
from cognis.store import feedback_models as rows
from cognis.store.database import create_engine, create_session_factory
from cognis.store.direct_turns import DirectTurnPayloadV1, DirectTurnStore
from cognis.store.models import (
    ArtifactRecordRow,
    Conversation,
    DirectTurnRequestRow,
    NotificationRow,
    Session,
    StepRun,
    Task,
)
from cognis.store.queries import create_agent, create_conversation, create_task, create_user
from cognis.tools.builtin.artifact_tools import _handle_artifact_get_url

OWNER = "feedback-owner@test.invalid"
OTHER = "feedback-other@test.invalid"


class AdmissionScheduler:
    """Exercise the real durable admission transaction without starting an LLM."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.store = DirectTurnStore(factory)
        self.calls: list[dict[str, Any]] = []
        self.after_acceptance_failure = False

    async def submit_turn(self, conversation_id: str, content: str, **kwargs: Any) -> None:
        from cognis.store.models import Conversation

        self.calls.append(kwargs)
        async with self.factory() as db:
            conversation = await db.get(Conversation, conversation_id)
            assert conversation is not None
        await self.store.admit(
            conversation_id=conversation_id,
            session_id=None,
            agent_id=conversation.agent_id,
            user_id=kwargs["user_email"],
            idempotency_scope=kwargs["idempotency_scope"],
            idempotency_key=kwargs["idempotency_key"],
            payload=DirectTurnPayloadV1(
                content=content,
                attachments=kwargs["attachments"],
                metadata={"user_message_metadata": kwargs["user_message_metadata"]},
            ).model_dump(mode="json"),
            transaction_participant=kwargs["admission_transaction_participant"],
        )
        if self.after_acceptance_failure:
            raise TimeoutError("Acceptance response was lost")


@dataclass
class Harness:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    store: ArtifactStore
    service: FeedbackService
    scheduler: AdmissionScheduler
    actor: Actor
    agent_id: str
    conversation_id: str

    async def source(
        self,
        data: bytes = b"Original text",
        mime: str = "text/plain",
        filename: str = "review.txt",
        *,
        context: dto.FeedbackContext | None = None,
        task_id: str | None = None,
    ) -> dto.ResolvedFeedbackSource:
        artifact_id = f"art_{uuid4().hex}"
        await self.store.async_save("artifacts", artifact_id, filename, data, mime)
        async with self.factory() as db:
            session_id = None
            if task_id:
                session_id = uuid4().hex
                db.add(
                    Session(
                        session_id=session_id,
                        agent_id=self.agent_id,
                        user_email=OWNER,
                        conversation_id=self.conversation_id,
                    )
                )
                await db.flush()
                db.add(
                    StepRun(
                        step_run_id=uuid4().hex,
                        task_id=task_id,
                        step_name="revise",
                        step_type="run",
                        agent_id=self.agent_id,
                        session_id=session_id,
                        attempt_number=1,
                        status="completed",
                    )
                )
                await db.flush()
            db.add(
                ArtifactRecordRow(
                    artifact_id=artifact_id,
                    namespace="artifacts",
                    object_id=artifact_id,
                    filename=filename,
                    owner_email=OWNER,
                    owner_agent_id=self.agent_id,
                    conversation_id=self.conversation_id,
                    session_id=session_id,
                    mime_type=mime,
                    size_bytes=len(data),
                    status="permanent",
                    lineage_id=artifact_id,
                )
            )
            await db.commit()
        return await self.service.sources.resolve(
            self.actor,
            dto.ResolveSourceRequest(
                source=dto.DocumentSourceRef(kind="document", content_ref=artifact_id),
                context=context or dto.StandaloneFeedbackContext(kind="standalone"),
            ),
        )

    async def thread(
        self, source: dto.ResolvedFeedbackSource, body: str = "Please revise"
    ) -> dto.FeedbackThread:
        return await self.service.threads.create(
            self.actor,
            dto.ThreadCreateRequest(
                creation_key=uuid4().hex,
                context=source.context,
                anchor=dto.AnchorInput(
                    source_token=source.source_token,
                    content_digest=source.content_digest,
                    locator=dto.WholeLocator(kind="whole"),
                ),
                body=body,
            ),
        )

    def app(self) -> FastAPI:
        app = FastAPI()
        app.state.feedback = self.service
        app.add_exception_handler(FeedbackError, feedback_error_response)
        app.include_router(router)
        app.include_router(public_router)

        @app.middleware("http")
        async def test_identity(request: Request, call_next: Any) -> Any:
            email = request.headers.get("x-test-user")
            if email:
                request.state.user = AuthenticatedUser(
                    email=email, role=request.headers.get("x-test-role", "user")
                )
            return await call_next(request)

        return app


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def harness(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Harness]:
    control = None
    schema = f"feedback_runtime_{uuid4().hex}"
    if request.param == "postgresql":
        url = os.getenv("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
        control = create_async_engine(url, poolclass=NullPool)
        async with control.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}}
        )
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'feedback.db'}")
    try:
        await run_schema_bootstrap(engine)
        factory = create_session_factory(engine)
        agent_id = uuid4().hex
        async with factory() as db:
            await create_user(db, OWNER, "Feedback owner", "!")
            await create_user(db, OTHER, "Other reviewer", "!", role="admin")
            await create_agent(
                db, agent_id=agent_id, owner_email=OWNER, name="Feedback agent", status="active"
            )
            conversation = await create_conversation(db, OWNER, agent_id, "web")
            await db.commit()
        store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "objects")))
        sources = Sources(factory, store, Tokens(uuid4().hex), AsyncMock())
        payloads = Payloads(factory, store)
        agents = FeedbackAgents(sources, payloads, AsyncMock(return_value=False))
        scheduler = AdmissionScheduler(factory)
        service = FeedbackService(sources, payloads, agents, scheduler, AsyncMock())
        yield Harness(
            engine,
            factory,
            store,
            service,
            scheduler,
            Actor.user(OWNER),
            agent_id,
            conversation.conversation_id,
        )
    finally:
        await engine.dispose()
        if control is not None:
            async with control.begin() as db:
                await db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await control.dispose()


@pytest.mark.asyncio
async def test_get_url_comment_permission_uses_captured_actor_and_viewer_mode(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness
    monkeypatch.setattr(ArtifactStore, "base_url", property(lambda _: "https://feedback.test"))
    source = await h.source()
    result = await _handle_artifact_get_url(
        {"artifact_id": source.source.content_ref, "allow_comments": True},
        artifact_store=h.store,
        session_factory=h.factory,
        user_email=OWNER,
        accessor_agent_id=h.agent_id,
        comment_sharing=h.service.public,
        comment_actor_email=OWNER,
    )
    assert not result.is_error, result.output
    payload = json.loads(result.output)
    assert payload["url"] == payload["comment_url"]
    assert payload["mode"] == "view"
    assert payload["allow_comments"] is True
    denied = await _handle_artifact_get_url(
        {"artifact_id": source.source.content_ref, "allow_comments": True},
        artifact_store=h.store,
        session_factory=h.factory,
        user_email=OWNER,
        accessor_agent_id=h.agent_id,
        comment_sharing=h.service.public,
        comment_actor_email=OTHER,
    )
    assert denied.is_error
    download = await _handle_artifact_get_url(
        {"artifact_id": source.source.content_ref, "allow_comments": True, "mode": "download"},
        artifact_store=h.store,
        session_factory=h.factory,
        user_email=OWNER,
        accessor_agent_id=h.agent_id,
        comment_sharing=h.service.public,
        comment_actor_email=OWNER,
    )
    assert download.is_error


@pytest.mark.asyncio
async def test_agent_comment_link_pins_source_and_bounds_expiration(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness
    monkeypatch.setattr(ArtifactStore, "base_url", property(lambda _: "https://feedback.test"))
    source = await h.source()
    from cognis.store.models import ArtifactRecordRow

    limit = datetime.now(UTC) + timedelta(seconds=120)
    async with h.factory() as db:
        row = await db.get(ArtifactRecordRow, source.source.content_ref)
        assert row is not None
        row.expires_at = limit
        await db.commit()
    result = await create_comment_link(
        h.service.public,
        content_ref=source.source.content_ref,
        user_email=OWNER,
        agent_id=h.agent_id,
        ttl_seconds=604800,
    )
    assert result["allow_comments"] is True
    assert result["comment_link_status"] == "ready"
    assert datetime.fromisoformat(result["comment_link_expires_at"]) <= limit
    from urllib.parse import unquote, urlsplit

    token = unquote(urlsplit(result["comment_url"]).fragment)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()),
        base_url="http://feedback.test",
    ) as client:
        exchange = await client.post(
            "/api/v1/public/feedback/exchange",
            json={"share_token": token, "create_guest": True, "display_name": "Reviewer"},
        )
        assert exchange.status_code == 200, exchange.text
    assert not h.scheduler.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_actor", ["agent", "user", "missing_user"])
async def test_agent_comment_link_does_not_promote_read_or_account_authority(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    foreign_actor: str,
) -> None:
    h = harness
    monkeypatch.setattr(ArtifactStore, "base_url", property(lambda _: "https://feedback.test"))
    source = await h.source()
    create = AsyncMock(wraps=h.service.public.create_share)
    monkeypatch.setattr(h.service.public, "create_share", create)
    with pytest.raises(FeedbackError):
        await create_comment_link(
            h.service.public,
            content_ref=source.source.content_ref,
            user_email=OTHER
            if foreign_actor == "user"
            else None
            if foreign_actor == "missing_user"
            else OWNER,
            agent_id=uuid4().hex if foreign_actor == "agent" else h.agent_id,
        )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_discussion_versions_private_drafts_and_staging(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness
    # Standalone comments still exercise first explicit staging (never auto-inclusion).
    context = dto.StandaloneFeedbackContext(kind="standalone")
    source = await h.source(context=context)
    thread = await h.thread(source)
    entry = thread.entries[0]
    assert thread.state == "open" and not h.scheduler.calls
    edited = await h.service.threads.edit(
        h.actor, entry.entry_id, dto.EntryEditRequest(expected_version=1, body="Revised")
    )
    assert (await h.service.threads.entry(h.actor, entry.entry_version_id)).body == "Please revise"
    assert edited.version == 2
    with pytest.raises(FeedbackError):
        await h.service.threads.edit(
            h.actor, entry.entry_id, dto.EntryEditRequest(expected_version=1, body="Stale edit")
        )
    with pytest.raises(FeedbackError):
        await h.service.threads.thread(Actor.user(OTHER), thread.thread_id)
    draft_key = uuid4().hex
    draft = await h.service.threads.save_draft(
        h.actor,
        draft_key,
        dto.DraftRequest(expected_version=0, context=context, body="Unsaved review"),
    )
    assert draft.version == 1
    with pytest.raises(FeedbackError):
        await h.service.threads.draft(Actor.user(OTHER), draft_key)
    staged = await h.service.threads.stage(
        h.actor,
        h.conversation_id,
        entry.entry_id,
        dto.StagingUpdateRequest(
            expected_version=0, entry_version_id=edited.entry_version_id, inclusion="included"
        ),
    )
    assert staged.inclusion == "prepared"
    staged = await h.service.threads.stage(
        h.actor,
        h.conversation_id,
        entry.entry_id,
        dto.StagingUpdateRequest(
            expected_version=staged.staging_version,
            entry_version_id=edited.entry_version_id,
            inclusion="included",
        ),
    )
    snapshot = await h.service.threads.staging(h.actor, h.conversation_id)
    assert snapshot.items == [staged]
    async with h.factory() as db:
        assert await db.scalar(select(func.count()).select_from(DirectTurnRequestRow)) == 0
    current = await h.service.threads.thread(h.actor, thread.thread_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()), base_url="http://feedback.test"
    ) as client:
        malformed = await client.post(
            "/api/v1/feedback/sources/resolve",
            headers={"x-test-user": OWNER},
            json={"source": {}},
        )
        assert malformed.status_code == 422
        assert malformed.json()["detail"]["code"] == "invalid_request"
        public_malformed = await client.post(
            "/api/v1/public/feedback/guest-sessions",
            json={"unrecognized": True},
        )
        assert public_malformed.status_code == 422
        assert public_malformed.json()["detail"]["code"] == "invalid_request"
        assert "unrecognized" not in public_malformed.text
        hidden = await client.post(
            f"/api/v1/feedback/threads/{thread.thread_id}/moderation",
            headers={"x-test-user": OWNER},
            json={
                "expected_version": current.version,
                "action": "hide",
                "reason": "Private moderation",
            },
        )
        assert hidden.status_code == 200, hidden.text
    owner_history = await h.service.continuity.history(h.actor, source.subject_id, None, 100)
    assert any(item.reason == "Private moderation" for item in owner_history.items)
    original_authorize = h.service.sources.authorize

    async def reader_scope(
        db: AsyncSession, actor: Actor, subject_id: str, capability: str = "read"
    ):
        if actor.identity == OTHER and capability == "read":
            subject, _ = await original_authorize(db, h.actor, subject_id)
            return subject, dto.FeedbackCapabilities(read=True)
        return await original_authorize(db, actor, subject_id, capability)

    monkeypatch.setattr(h.service.sources, "authorize", reader_scope)
    reader_history = await h.service.continuity.history(
        Actor.user(OTHER), source.subject_id, None, 100
    )
    assert not reader_history.items
    current = await h.service.threads.thread(h.actor, thread.thread_id)
    await h.service.threads.change_thread(
        h.actor,
        thread.thread_id,
        dto.ModerationRequest(
            expected_version=current.version, action="restore", reason="Private restoration"
        ),
    )
    reader_history = await h.service.continuity.history(
        Actor.user(OTHER), source.subject_id, None, 100
    )
    assert all(item.kind != "moderation" for item in reader_history.items)
    owner_history = await h.service.continuity.history(h.actor, source.subject_id, None, 100)
    assert {item.reason for item in owner_history.items if item.kind == "moderation"} == {
        "Private moderation",
        "Private restoration",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime", "filename"),
    [("video/mp4", "review.mp4"), ("audio/mpeg", "review.mp3"), ("image/png", "review.png")],
)
async def test_public_source_preserves_artifact_media_type(
    harness: Harness, mime: str, filename: str
) -> None:
    h = harness
    data = b"\x00\xffpublic media"
    source = await h.source(data, mime, filename)
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=source.source,
            expected_content_digest=source.content_digest,
            allow_comments=False,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()), base_url="http://feedback.test"
    ) as client:
        exchange = await client.post(
            "/api/v1/public/feedback/exchange", json={"share_token": share.share_token}
        )
        assert exchange.status_code == 200
        headers = {"X-Cognis-Feedback-Capability": exchange.json()["capability"]}
        published = await client.get("/api/v1/public/feedback/source", headers=headers)
        assert published.status_code == 200
        assert published.json()["mime_type"] == mime
        assert published.json()["filename"] == filename
        raw = await client.get(published.json()["bytes_path"], headers=headers)
        assert raw.status_code == 200
        assert raw.headers["content-type"] == mime
        assert raw.content == data


@pytest.mark.asyncio
@pytest.mark.parametrize("html_source", [False, True])
async def test_public_capability_headers_isolation_and_revocation(
    harness: Harness,
    html_source: bool,
    standalone_ui_build: Path,
) -> None:
    h = harness
    from cognis.store.queries import upsert_user_ui_state

    async with h.factory() as db:
        await upsert_user_ui_state(
            db,
            OWNER,
            "ui.preferences",
            {"display": {"theme_family": "gruvbox", "theme": "dark", "language": "cs"}},
        )
        await db.commit()
    source = await h.source(
        b"<script>alert('raw')</script><p>Review</p>" if html_source else b"Original text",
        "text/html" if html_source else "text/plain",
        "review.html" if html_source else "review.txt",
    )
    internal = await h.thread(source, "Internal feedback must stay private")
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=source.source,
            expected_content_digest=source.content_digest,
            allow_comments=True,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()), base_url="http://feedback.test"
    ) as client:
        exchange = await client.post(
            "/api/v1/public/feedback/exchange",
            json={
                "share_token": share.share_token,
                "create_guest": True,
                "display_name": "Reviewer",
            },
        )
        assert exchange.status_code == 200, exchange.text
        value = exchange.json()
        headers = {
            "X-Cognis-Feedback-Capability": value["capability"],
            "X-Cognis-Feedback-Guest": value["guest_session"]["guest_capability"],
        }
        viewer = await client.get("/api/v1/public/feedback/viewer", headers=headers)
        assert viewer.status_code == 200, viewer.text
        assert "data-feedback-share-token" in viewer.text
        publication = BeautifulSoup(viewer.text, "html.parser").select_one(
            "#cognis-deliverable-payload"
        )
        assert publication is not None
        assert json_codec.loads(publication.text)["themeFamily"] == "gruvbox"
        published = await client.get("/api/v1/public/feedback/source", headers=headers)
        assert published.status_code == 200
        assert published.json()["theme_family"] == "gruvbox"
        assert "display" not in published.json() and "language" not in published.json()
        assert h.conversation_id not in viewer.text and h.agent_id not in viewer.text
        assert "<script>alert('raw')</script>" not in viewer.text
        if html_source:
            # The existing client RawHtmlFrame hydrates the opaque-origin iframe.
            assert "data:text/html;base64," in viewer.text
            raw = await client.get("/api/v1/public/feedback/source/bytes", headers=headers)
            policies = {
                directive.strip() for directive in raw.headers["content-security-policy"].split(";")
            }
            assert "sandbox" in policies
            assert "default-src 'none'" in policies
            assert raw.headers["content-disposition"].startswith("attachment")
        bootstrapped = await client.post(
            "/api/v1/public/feedback/guest-sessions",
            headers={"X-Cognis-Feedback-Capability": value["capability"]},
            json={"display_name": "Viewer"},
        )
        assert bootstrapped.status_code == 201, bootstrapped.text
        assert bootstrapped.json()["guest_capability"]
        assert (await client.get("/api/v1/public/feedback/threads")).status_code == 401
        assert (
            await client.get(
                "/api/v1/public/feedback/threads",
                headers={"Authorization": f"Bearer {value['capability']}"},
            )
        ).status_code == 401
        assert (
            await client.get(
                f"/api/v1/public/feedback/threads/{internal.thread_id}", headers=headers
            )
        ).status_code == 404
        resolved = await client.post(
            "/api/v1/public/feedback/sources/resolve",
            headers=headers,
            json={"source": source.source.model_dump(), "context": {"kind": "standalone"}},
        )
        assert resolved.status_code == 200, resolved.text
        created = await client.post(
            "/api/v1/public/feedback/threads",
            headers=headers,
            json={
                "creation_key": uuid4().hex,
                "body": "Guest feedback",
                "anchor": {
                    "source_token": resolved.json()["source_token"],
                    "content_digest": source.content_digest,
                    "locator": {"kind": "whole"},
                },
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["entries"][0]["author"]["kind"] == "guest_unverified"
        assert "submission_id" not in created.text and "conversation_id" not in created.text
        listing = await client.get("/api/v1/public/feedback/threads", headers=headers)
        assert len(listing.json()["items"]) == 1
        cross_origin = await client.get(
            "/api/v1/public/feedback/threads",
            headers={**headers, "Origin": "https://unrelated.test"},
        )
        assert cross_origin.status_code == 403
        artifact_id = source.source.content_ref
        async with h.factory() as db:
            artifact = await db.get(ArtifactRecordRow, artifact_id)
        await h.store.async_save(
            "artifacts", artifact_id, artifact.filename, b"Changed pinned bytes", artifact.mime_type
        )
        assert (
            await client.get("/api/v1/public/feedback/viewer", headers=headers)
        ).status_code == 410
        assert (
            await client.get("/api/v1/public/feedback/source/bytes", headers=headers)
        ).status_code == 410
        await h.service.public.update_share(
            h.actor, share.share_id, dto.ShareUpdateRequest(expected_version=1, revoked=True)
        )
        assert (
            await client.get("/api/v1/public/feedback/threads", headers=headers)
        ).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("guest_capture", [False, True])
async def test_capture_adoption_reencodes_and_enforces_exact_binding(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, guest_capture: bool
) -> None:
    h = harness
    source = await h.source(b"<p>Review the visible page</p>", "text/html", "review.html")
    actor = h.actor
    if guest_capture:
        share = await h.service.public.create_share(
            h.actor,
            source.subject_id,
            dto.ShareRequest(
                source=source.source,
                expected_content_digest=source.content_digest,
                allow_comments=True,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
        exchanged = await h.service.public.exchange(share.share_token)
        guest = await h.service.public.guest(
            exchanged.capability, dto.GuestSessionRequest(display_name="Capture reviewer")
        )
        actor, _ = await h.service.public.actor(exchanged.capability, guest.guest_capability)
        source = await h.service.sources.resolve(
            actor, dto.ResolveSourceRequest(source=source.source, context=source.context)
        )
    request = dto.CaptureRequest(
        creation_key=uuid4().hex,
        source_token=source.source_token,
        content_digest=source.content_digest,
        viewport=dto.Viewport(
            width_css_px=640,
            height_css_px=480,
            device_pixel_ratio=1,
            scroll_x_css_px=0,
            scroll_y_css_px=0,
        ),
        render_state_digest=digest(b"confirmed test viewport"),
    )
    capture = await h.service.captures.create(actor, request)
    image = BytesIO()
    Image.new("RGB", (80, 60), "white").save(image, format="PNG")
    with pytest.raises(FeedbackError):
        await h.service.captures.adopt(actor, capture.capture_id, 1, 80, 60, b"<svg/>")
    ready = await h.service.captures.adopt(actor, capture.capture_id, 1, 80, 60, image.getvalue())
    assert ready.status == "ready" and ready.trust == "untrusted_user_provided"
    with pytest.raises(FeedbackError):
        await h.service.captures.get(Actor.user(OTHER), capture.capture_id)
    row = await h.service.captures.get(actor, capture.capture_id)
    pixels, mime = await h.service.payloads.get_bytes(row.payload_id)
    assert mime == "image/png" and Image.open(BytesIO(pixels)).size == (80, 60)
    with pytest.raises(FeedbackError):
        await h.service.captures.adopt(actor, capture.capture_id, 1, 80, 60, image.getvalue())
    if guest_capture:
        with pytest.raises(FeedbackError):
            await h.service.captures.get(h.actor, capture.capture_id)
    thread = await h.service.threads.create(
        actor,
        dto.ThreadCreateRequest(
            creation_key=uuid4().hex,
            context=source.context,
            body="Observed capture feedback",
            anchor=dto.AnchorInput(
                source_token=source.source_token,
                content_digest=source.content_digest,
                locator=dto.HtmlLocator(
                    kind="html",
                    capture_id=capture.capture_id,
                    viewport=request.viewport,
                    render_state_digest=request.render_state_digest,
                ),
            ),
        ),
    )
    assert (
        await h.service.captures.get(h.actor, capture.capture_id)
    ).capture_id == capture.capture_id
    original_authorize = h.service.sources.authorize

    async def reader_scope(db, actor, subject_id, capability="read"):
        if actor.identity == OTHER and capability == "read":
            subject, _ = await original_authorize(db, h.actor, subject_id)
            return subject, dto.FeedbackCapabilities(read=True)
        return await original_authorize(db, actor, subject_id, capability)

    monkeypatch.setattr(h.service.sources, "authorize", reader_scope)
    assert (
        await h.service.captures.get(Actor.user(OTHER), capture.capture_id)
    ).capture_id == capture.capture_id
    owner_source = await h.service.sources.resolve(
        h.actor, dto.ResolveSourceRequest(source=source.source, context=source.context)
    )
    replacement = await h.service.captures.create(
        h.actor,
        request.model_copy(
            update={
                "creation_key": uuid4().hex,
                "source_token": owner_source.source_token,
            }
        ),
    )
    await h.service.captures.adopt(h.actor, replacement.capture_id, 1, 80, 60, image.getvalue())
    thread = await h.service.threads.change_thread(
        h.actor,
        thread.thread_id,
        dto.ReattachRequest(
            expected_version=thread.version,
            reason="Review the new observed capture",
            anchor=dto.AnchorInput(
                source_token=owner_source.source_token,
                content_digest=owner_source.content_digest,
                locator=dto.HtmlLocator(
                    kind="html",
                    capture_id=replacement.capture_id,
                    viewport=request.viewport,
                    render_state_digest=request.render_state_digest,
                ),
            ),
        ),
    )
    assert (
        await h.service.captures.get(Actor.user(OTHER), replacement.capture_id)
    ).capture_id == replacement.capture_id
    await h.service.threads.change_thread(
        h.actor,
        thread.thread_id,
        dto.ModerationRequest(
            expected_version=thread.version, action="hide", reason="Private review"
        ),
    )
    with pytest.raises(FeedbackError):
        await h.service.captures.get(Actor.user(OTHER), capture.capture_id)
    with pytest.raises(FeedbackError):
        await h.service.captures.get(Actor.user(OTHER), replacement.capture_id)


async def _running_packet(
    h: Harness,
    source: dto.ResolvedFeedbackSource,
    mode: dto.UpdateMode | None = "answer_only",
    *,
    body: str = "Please revise",
) -> tuple[dto.SubmissionReceipt, ExecutionScope, str]:
    thread = await h.thread(source, body)
    entry = thread.entries[0]
    snapshot = await h.service.threads.staging(h.actor, h.conversation_id)
    staged = next((item for item in snapshot.items if item.entry_id == entry.entry_id), None)
    if staged is None:
        staged = await h.service.threads.stage(
            h.actor,
            h.conversation_id,
            entry.entry_id,
            dto.StagingUpdateRequest(
                expected_version=0, entry_version_id=entry.entry_version_id, inclusion="prepared"
            ),
        )
    staged = await h.service.threads.stage(
        h.actor,
        h.conversation_id,
        entry.entry_id,
        dto.StagingUpdateRequest(
            expected_version=staged.staging_version,
            entry_version_id=entry.entry_version_id,
            inclusion="included",
        ),
    )
    preflight = await h.service.submissions.preflight(
        h.actor,
        dto.FeedbackPreflightRequest(
            context=source.context,
            destination=dto.OriginChatDestination(
                kind="origin_chat", conversation_id=h.conversation_id
            ),
            selection=[
                dto.SelectedVersion(
                    entry_version_id=entry.entry_version_id,
                    expected_entry_version=1,
                    staging_version=staged.staging_version,
                )
            ],
            **({"update_mode": mode} if mode is not None else {}),
        ),
    )
    pending = await h.service.submissions.prepare(
        h.actor,
        dto.IncludedFeedback(
            creation_key=uuid4().hex,
            preflight_id=preflight.preflight_id,
            manifest_hash=preflight.manifest_hash,
        ),
        DispatchCommand(
            content="Address selected feedback",
            chat_mode="build",
            client_txn_id=uuid4().hex,
            origin_conversation_id=h.conversation_id,
        ),
        standalone=False,
    )
    receipt = await h.service.submissions.dispatch(h.actor, pending.submission_id)
    session_id = uuid4().hex
    async with h.factory() as db:
        db.add(
            Session(
                session_id=session_id,
                agent_id=h.agent_id,
                user_email=OWNER,
                conversation_id=h.conversation_id,
            )
        )
        await db.flush()
        request = await db.scalar(
            select(DirectTurnRequestRow).where(
                DirectTurnRequestRow.turn_id == receipt.admission.turn_id
            )
        )
        request.status, request.session_id = "running", session_id
        await db.commit()
    return (
        receipt,
        await h.service.agents.scope(session_id, receipt.admission.turn_id),
        entry.entry_version_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ("Why does this say Original text?", "answer"),
        ("Change Original text to Revised text.", "revision"),
        ("Create an independent summary of this document.", "separate"),
    ],
)
async def test_request_driven_feedback_allows_answers_and_scoped_results(
    harness: Harness, body: str, outcome: str
) -> None:
    from cognis.core.artifact_revisions import ContentActor, write_artifact_document
    from cognis.core.feedback.authoring import prepare_result_write
    from cognis.models.feedback_tools import FeedbackRecordArguments

    h = harness
    source = await h.source(
        context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    )
    receipt, scope, entry_version = await _running_packet(h, source, None, body=body)
    notice = h.scheduler.calls[0]["contextual_messages"]
    assert any(body in item["content"] for item in notice)
    guidance = "\n".join(
        item["content"] for item in notice if not item["message_metadata"].get("untrusted")
    )
    assert "answer questions" in guidance
    assert "when edits are requested and authorized" in guidance
    assert "Sending comments alone does not request a source revision" in guidance
    assert "Requested update mode:" not in guidance
    async with h.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ArtifactRecordRow)) == 1

    result_ref = None
    if outcome != "answer":
        supersedes = source.source.content_ref if outcome == "revision" else None
        await h.service.agents.before_write(scope, supersedes)
        participant = await prepare_result_write(h.service.agents, scope)
        result, _ = await write_artifact_document(
            session_factory=h.factory,
            artifact_store=h.store,
            actor=ContentActor(
                user_email=OWNER,
                agent_id=h.agent_id,
                conversation_id=h.conversation_id,
                session_id=scope.session_id,
            ),
            content=b"Revised text" if outcome == "revision" else b"Independent summary",
            filename="result.txt",
            mime_type="text/plain",
            kind="file",
            purpose="artifact_write",
            supersedes=supersedes,
            transaction_participant=participant,
        )
        result_ref = result.artifact_id
        async with h.factory() as db:
            bindings = (
                await db.scalars(
                    select(rows.FeedbackWorkLinkRow).where(
                        rows.FeedbackWorkLinkRow.submission_id == receipt.submission_id,
                        rows.FeedbackWorkLinkRow.kind.in_(
                            ["result_revises_source", "result_separate_result"]
                        ),
                    )
                )
            ).all()
            assert [(item.reference_id, item.kind) for item in bindings] == [
                (
                    result_ref,
                    "result_revises_source" if outcome == "revision" else "result_separate_result",
                )
            ]
    disposition = await h.service.agents.record(
        scope,
        FeedbackRecordArguments(
            submission_id=receipt.submission_id,
            manifest_hash=receipt.manifest_hash,
            action="disposition",
            entry_version_id=entry_version,
            disposition="addressed",
            reason="Answered the question" if outcome == "answer" else "Created requested result",
            result_ref=result_ref,
        ),
    )
    assert disposition["disposition"] == "addressed"
    original = await h.service.sources.load(OWNER, source.source)
    assert original.data == b"Original text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("other_agent", "revision_owner_mismatch"),
        ("new_head", "source_head_changed"),
    ],
)
async def test_request_driven_revision_preserves_source_authority(
    harness: Harness, change: str, code: str
) -> None:
    from cognis.core.artifact_revisions import ContentActor, write_artifact_document

    h = harness
    source = await h.source(
        context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    )
    if change == "other_agent":
        recipient = uuid4().hex
        async with h.factory() as db:
            await create_agent(
                db, agent_id=recipient, owner_email=OWNER, name="Recipient", status="active"
            )
            await db.execute(
                update(Conversation)
                .where(Conversation.conversation_id == h.conversation_id)
                .values(agent_id=recipient)
            )
            await db.commit()
        h.agent_id = recipient
    _, scope, _ = await _running_packet(h, source, "follow_request")
    target = source.source.content_ref
    if change == "new_head":
        await write_artifact_document(
            session_factory=h.factory,
            artifact_store=h.store,
            actor=ContentActor(
                user_email=OWNER,
                agent_id=h.agent_id,
                conversation_id=h.conversation_id,
                session_id=None,
            ),
            content=b"Concurrent revision",
            filename="review.txt",
            mime_type="text/plain",
            kind="file",
            purpose="artifact_write",
            supersedes=target,
        )
    with pytest.raises(FeedbackError) as forbidden:
        await h.service.agents.before_write(scope, target)
    assert forbidden.value.code == code


@pytest.mark.asyncio
async def test_agent_source_reads_preserve_html_pdf_and_webp(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from cognis.models.artifact import ArtifactKind
    from cognis.models.tool import ToolCall, ToolResult
    from cognis.tools.builtin import artifact_tools

    h = harness
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (Canonical PDF feedback) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    pdf = BytesIO()
    writer.write(pdf)
    image = BytesIO()
    Image.new("RGB", (16, 16), "white").save(image, format="WEBP")
    for data, mime, name in [
        (b"<html><p>Exact HTML source</p></html>", "text/html", "source.html"),
        (pdf.getvalue(), "application/pdf", "source.pdf"),
        (image.getvalue(), "image/webp", "source.webp"),
    ]:
        context = dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
        source = await h.source(data, mime, name, context=context)
        receipt, scope, _ = await _running_packet(h, source)
        request = FeedbackReadArguments(
            submission_id=receipt.submission_id,
            manifest_hash=receipt.manifest_hash,
            view="source",
            source_id=source.subject_id,
        )
        call = ToolCall(
            call_id=uuid4().hex, name="feedback_submission_read", arguments=request.model_dump()
        )
        if mime == "text/html":
            value = await h.service.agents.handle(call, scope.session_id, scope.turn_id)
            assert not value.is_error and json.loads(value.output)["text"] == data.decode()
            continue
        url = f"https://feedback.test/read/{source.source.content_ref}"
        with monkeypatch.context() as patch:
            patch.setattr(h.store, "async_get_public_url", AsyncMock(return_value=url))
            analyze = AsyncMock(return_value=ToolResult(output="Authorized source analysis"))
            patch.setattr(artifact_tools, "analyze_attachment_ref", analyze)
            value = await h.service.agents.handle(call, scope.session_id, scope.turn_id)
            assert not value.is_error
            attachment = analyze.await_args.kwargs["attachment"]
            assert attachment.kind == (
                ArtifactKind.PDF if mime == "application/pdf" else ArtifactKind.IMAGE
            )
            assert attachment.mime_type == mime and analyze.await_args.kwargs["content"] == data
            if mime == "application/pdf":
                assert attachment.url == url
        if mime == "application/pdf":
            with monkeypatch.context() as patch:
                patch.setattr(
                    artifact_tools,
                    "_get_attachment_analysis_route",
                    AsyncMock(return_value=(None, None)),
                )
                value = await h.service.agents.handle(
                    call, scope.session_id, scope.turn_id, llm=SimpleNamespace()
                )
            assert not value.is_error and "Canonical PDF feedback" in value.output
            for configured_route in [False, True]:
                model_id, provider_id = uuid4().hex, uuid4().hex
                with monkeypatch.context() as patch:
                    patch.setattr(
                        h.store,
                        "async_get_public_url",
                        AsyncMock(side_effect=RuntimeError("No signed URL available")),
                    )
                    patch.setattr(
                        artifact_tools,
                        "_get_attachment_analysis_route",
                        AsyncMock(
                            return_value=(model_id, provider_id)
                            if configured_route
                            else (None, None)
                        ),
                    )
                    patch.setattr(
                        artifact_tools,
                        "_get_model_info",
                        AsyncMock(return_value=SimpleNamespace(supports_pdf_input=True)),
                    )
                    generate = AsyncMock()
                    patch.setattr(artifact_tools, "_generate_analysis", generate)
                    value = await h.service.agents.handle(
                        call,
                        scope.session_id,
                        scope.turn_id,
                        llm=SimpleNamespace(),
                        current_model=None if configured_route else model_id,
                        current_provider_id=None if configured_route else provider_id,
                    )
                    assert not value.is_error and "Canonical PDF feedback" in value.output
                    assert value.metadata["fallback"] == "pdf_text_extraction"
                    assert value.metadata["used_attachment_analysis_route"] == configured_route
                    generate.assert_not_awaited()
        with pytest.raises(FeedbackError):
            await h.service.agents.read(
                ExecutionScope(
                    session_id=scope.session_id,
                    agent_id=scope.agent_id,
                    user_email=scope.user_email,
                    conversation_id=scope.conversation_id,
                    turn_id=uuid4().hex,
                ),
                request,
            )


@pytest.mark.asyncio
async def test_unique_quote_mapping_persists_a_valid_new_anchor(harness: Harness) -> None:
    from cognis.core.artifact_revisions import ContentActor, write_artifact_document

    h = harness
    actor = ContentActor(
        user_email=OWNER, agent_id=h.agent_id, conversation_id=h.conversation_id, session_id=None
    )
    original, _ = await write_artifact_document(
        session_factory=h.factory,
        artifact_store=h.store,
        actor=actor,
        content=b"Before target after",
        filename="source.txt",
        mime_type="text/plain",
        kind="file",
        purpose="artifact_write",
    )
    source = await h.service.sources.resolve(
        h.actor,
        dto.ResolveSourceRequest(
            source=dto.DocumentSourceRef(kind="document", content_ref=original.artifact_id),
            context=dto.StandaloneFeedbackContext(kind="standalone"),
        ),
    )
    thread = await h.service.threads.create(
        h.actor,
        dto.ThreadCreateRequest(
            creation_key=uuid4().hex,
            context=source.context,
            body="Revise this exact quote",
            anchor=dto.AnchorInput(
                source_token=source.source_token,
                content_digest=source.content_digest,
                locator=dto.TextLocator(
                    kind="text",
                    range=dto.TextRange(
                        representation="feedback-text-v1",
                        text_digest=digest(b"Before target after"),
                        start=7,
                        end=13,
                        exact="target",
                    ),
                ),
            ),
        ),
    )
    revised, _ = await write_artifact_document(
        session_factory=h.factory,
        artifact_store=h.store,
        actor=actor,
        content=b"Added\nBefore target after",
        filename="source.txt",
        mime_type="text/plain",
        kind="file",
        purpose="artifact_write",
        supersedes=original.artifact_id,
    )
    target = await h.service.sources.resolve(
        h.actor,
        dto.ResolveSourceRequest(
            source=dto.DocumentSourceRef(kind="document", content_ref=revised.artifact_id),
            context=source.context,
        ),
    )
    async with h.factory() as db:
        original_anchor = (await db.get(rows.FeedbackThreadRow, thread.thread_id)).anchor_id
    mapped = await h.service.continuity.mapping(h.actor, original_anchor, target.subject_id)
    assert mapped.category == "remapped" and mapped.mapped_anchor_id != original_anchor
    async with h.factory() as db:
        anchor = await db.get(rows.FeedbackAnchorRow, mapped.mapped_anchor_id)
    assert anchor.content_digest == target.content_digest
    payload = dto.FeedbackAnchor.model_validate(await h.service.payloads.get(anchor.payload_id))
    assert payload.locator.range.start == 13 and payload.locator.range.exact == "target"
    assert (
        await h.service.continuity.mapping(h.actor, original_anchor, target.subject_id)
    ) == mapped


@pytest.mark.asyncio
async def test_addressed_revision_requires_an_actual_revised_source(harness: Harness) -> None:
    from cognis.core.artifact_revisions import ContentActor, write_artifact_document
    from cognis.core.feedback.authoring import prepare_result_write
    from cognis.models.feedback_tools import FeedbackRecordArguments

    h = harness
    actor = ContentActor(
        user_email=OWNER, agent_id=h.agent_id, conversation_id=h.conversation_id, session_id=None
    )
    original, _ = await write_artifact_document(
        session_factory=h.factory,
        artifact_store=h.store,
        actor=actor,
        content=b"Original source",
        filename="source.txt",
        mime_type="text/plain",
        kind="file",
        purpose="artifact_write",
    )
    source = await h.service.sources.resolve(
        h.actor,
        dto.ResolveSourceRequest(
            source=dto.DocumentSourceRef(kind="document", content_ref=original.artifact_id),
            context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id),
        ),
    )
    receipt, scope, entry_version_id = await _running_packet(h, source, "revise_source")
    participant = await prepare_result_write(h.service.agents, scope)
    actor = ContentActor(
        user_email=OWNER,
        agent_id=h.agent_id,
        conversation_id=h.conversation_id,
        session_id=scope.session_id,
    )
    separate, _ = await write_artifact_document(
        session_factory=h.factory,
        artifact_store=h.store,
        actor=actor,
        content=b"Unrelated side result",
        filename="result.txt",
        mime_type="text/plain",
        kind="file",
        purpose="artifact_write",
        transaction_participant=participant,
    )
    request = FeedbackRecordArguments(
        submission_id=receipt.submission_id,
        manifest_hash=receipt.manifest_hash,
        action="disposition",
        entry_version_id=entry_version_id,
        disposition="addressed",
        reason="Actual revision required",
        result_ref=separate.artifact_id,
    )
    with pytest.raises(FeedbackError) as unrelated:
        await h.service.agents.record(scope, request)
    assert unrelated.value.code == "result_required"
    async with h.factory() as db:
        assert await db.scalar(select(func.count()).select_from(rows.FeedbackDispositionRow)) == 0
    revised, _ = await write_artifact_document(
        session_factory=h.factory,
        artifact_store=h.store,
        actor=actor,
        content=b"Revised selected source",
        filename="source.txt",
        mime_type="text/plain",
        kind="file",
        purpose="artifact_write",
        supersedes=original.artifact_id,
        transaction_participant=participant,
    )
    value = await h.service.agents.record(
        scope, request.model_copy(update={"result_ref": revised.artifact_id})
    )
    assert value["disposition"] == "addressed"


@pytest.mark.asyncio
async def test_library_send_of_guest_comment_leaves_nothing_ready_in_chat(
    harness: Harness,
) -> None:
    h = harness
    source = await h.source(
        context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    )
    thread = await h.thread(source)
    entry = thread.entries[0]
    async with h.factory() as db:
        await db.execute(
            update(rows.FeedbackEntryRow)
            .where(rows.FeedbackEntryRow.entry_id == entry.entry_id)
            .values(author_kind="guest_unverified")
        )
        await db.commit()
    assert len((await h.service.threads.staging(h.actor, h.conversation_id)).items) == 1
    # Selecting the guest comment is the owner's review; no separate acknowledgement.
    preflight = await h.service.submissions.preflight(
        h.actor,
        dto.FeedbackPreflightRequest(
            context=dto.StandaloneFeedbackContext(kind="standalone"),
            destination=dto.ConversationDestination(
                kind="existing_followup", conversation_id=h.conversation_id
            ),
            selection=[
                dto.SelectedVersion(
                    entry_version_id=entry.entry_version_id, expected_entry_version=1
                )
            ],
        ),
    )
    assert preflight.warnings == ["Guest feedback is unverified user input."]
    submission = await h.service.submissions.prepare(
        h.actor,
        dto.IncludedFeedback(
            creation_key=uuid4().hex,
            preflight_id=preflight.preflight_id,
            manifest_hash=preflight.manifest_hash,
        ),
        DispatchCommand(),
        standalone=True,
    )
    receipt = await h.service.submissions.dispatch(h.actor, submission.submission_id)
    assert receipt.delivery == "accepted"
    assert (await h.service.threads.staging(h.actor, h.conversation_id)).items == []
    # Explicitly choosing the sent comment again in the chat still includes it.
    again = await h.service.threads.stage(
        h.actor,
        h.conversation_id,
        entry.entry_id,
        dto.StagingUpdateRequest(
            expected_version=0, entry_version_id=entry.entry_version_id, inclusion="included"
        ),
    )
    assert again.inclusion == "included"
    ready = (await h.service.threads.staging(h.actor, h.conversation_id)).items
    assert [item.entry_version_id for item in ready] == [entry.entry_version_id]


@pytest.mark.asyncio
async def test_chat_snapshot_is_atomic_immutable_and_replayable(harness: Harness) -> None:
    h = harness
    context = dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    source = await h.source(context=context)
    thread = await h.thread(source)
    entry = thread.entries[0]
    prepared = (await h.service.threads.staging(h.actor, h.conversation_id)).items
    assert len(prepared) == 1 and prepared[0].inclusion == "prepared"
    assert prepared[0].staging_version == 1
    staged = await h.service.threads.stage(
        h.actor,
        h.conversation_id,
        entry.entry_id,
        dto.StagingUpdateRequest(
            expected_version=1, entry_version_id=entry.entry_version_id, inclusion="included"
        ),
    )
    preflight = await h.service.submissions.preflight(
        h.actor,
        dto.FeedbackPreflightRequest(
            context=context,
            destination=dto.OriginChatDestination(
                kind="origin_chat", conversation_id=h.conversation_id
            ),
            selection=[
                dto.SelectedVersion(
                    entry_version_id=entry.entry_version_id,
                    expected_entry_version=1,
                    staging_version=staged.staging_version,
                )
            ],
            update_mode="answer_only",
        ),
    )
    submission = await h.service.submissions.prepare(
        h.actor,
        dto.IncludedFeedback(
            creation_key=uuid4().hex,
            preflight_id=preflight.preflight_id,
            manifest_hash=preflight.manifest_hash,
        ),
        DispatchCommand(
            content="One message",
            chat_mode="build",
            client_txn_id=uuid4().hex,
            origin_conversation_id=h.conversation_id,
        ),
        standalone=False,
    )
    assert len((await h.service.threads.staging(h.actor, h.conversation_id)).items) == 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()), base_url="http://feedback.test"
    ) as client:
        checked = await client.post(
            f"/api/v1/feedback/submissions/{submission.submission_id}/check-delivery",
            headers={"x-test-user": OWNER},
        )
        assert checked.status_code == 200 and checked.json()["delivery"] == "pending"
        assert not h.scheduler.calls
    h.scheduler.after_acceptance_failure = True
    with pytest.raises(TimeoutError):
        await h.service.submissions.dispatch(h.actor, submission.submission_id)
    receipt = await h.service.submissions.receipt(h.actor, submission.submission_id)
    assert receipt.delivery == "accepted" and receipt.work == "queued"
    assert (await h.service.staging(h.actor, h.conversation_id)).receipts == [receipt]
    assert (await h.service.threads.staging(h.actor, h.conversation_id)).items == []
    replay = await h.service.submissions.dispatch(h.actor, submission.submission_id)
    assert replay == receipt and len(h.scheduler.calls) == 1
    assert h.scheduler.calls[0]["one_shot_chat_mode"] == "build"
    # The agent must actually receive the comments: every context message carries
    # timestamped envelope metadata, and the selected comment body is inline.
    call = h.scheduler.calls[0]
    assert call["user_message_metadata"]["ts"]
    context_messages = call["contextual_messages"]
    assert all(item["message_metadata"]["ts"] for item in context_messages)
    assert any(
        entry.body in item["content"] and item["message_metadata"].get("untrusted") is True
        for item in context_messages
    )
    stored = (await h.service.submissions.detail(h.actor, submission.submission_id)).manifest
    assert [item.entry.entry_version_id for item in stored.entries] == [entry.entry_version_id]
    # Sending leaves the comment open for the agent to answer; a later edit is ready again.
    sent = await h.service.threads.thread(h.actor, thread.thread_id)
    assert sent.state == "open"
    await h.service.threads.edit(
        h.actor,
        entry.entry_id,
        dto.EntryEditRequest(expected_version=1, body="Later review", context=context),
    )
    prepared = (await h.service.threads.staging(h.actor, h.conversation_id)).items
    assert len(prepared) == 1 and prepared[0].inclusion == "prepared"
    assert prepared[0].entry_version_id != entry.entry_version_id
    assert (
        await h.service.submissions.detail(h.actor, submission.submission_id)
    ).manifest == stored
    assert (await h.service.threads.thread(h.actor, thread.thread_id)).state == "open"
    async with h.factory() as db:
        db.add(
            Session(
                session_id=uuid4().hex,
                agent_id=h.agent_id,
                user_email=OWNER,
                conversation_id=h.conversation_id,
            )
        )
        await db.commit()
        session = await db.scalar(select(Session))
    scope = await h.service.agents.scope(session.session_id, receipt.admission.turn_id)
    read = await h.service.agents.read(
        scope,
        FeedbackReadArguments(
            submission_id=submission.submission_id,
            manifest_hash=receipt.manifest_hash,
        ),
    )
    assert read["entries"][0]["entry"]["body"] == "Please revise"
    wrong_turn = await h.service.agents.scope(session.session_id, uuid4().hex)
    with pytest.raises(FeedbackError):
        await h.service.agents.read(
            wrong_turn,
            FeedbackReadArguments(
                submission_id=submission.submission_id,
                manifest_hash=receipt.manifest_hash,
            ),
        )
    with pytest.raises(FeedbackError) as forbidden:
        await h.service.agents.before_write(scope, source.source.content_ref)
    assert forbidden.value.code == "update_mode_forbidden"
    with pytest.raises(FeedbackError) as unrelated:
        await h.service.agents.result_proof(scope, stored, source.source.content_ref)
    assert unrelated.value.code == "result_provenance_mismatch"
    async with h.factory() as db:
        assert await db.scalar(select(func.count()).select_from(DirectTurnRequestRow)) == 1
        request = await db.scalar(
            select(DirectTurnRequestRow).where(DirectTurnRequestRow.turn_id == scope.turn_id)
        )
        request.status = "running"
        request.session_id = scope.session_id
        await db.commit()
    await _assert_explicit_child_admission(h, scope, receipt)
    await _assert_atomic_document_causality(h, scope, receipt, stored)


async def _assert_atomic_document_causality(
    h: Harness,
    scope: ExecutionScope,
    receipt: dto.SubmissionReceipt,
    manifest: dto.SubmissionManifest,
) -> None:
    from cognis.core.artifact_revisions import ContentActor, write_artifact_document
    from cognis.core.feedback.authoring import prepare_result_write

    participant = await prepare_result_write(h.service.agents, scope)
    assert participant is not None
    actor = ContentActor(
        user_email=OWNER,
        agent_id=h.agent_id,
        conversation_id=h.conversation_id,
        session_id=scope.session_id,
    )

    async def abort(db: AsyncSession, content_ref: str, created: bool) -> None:
        await participant(db, content_ref, created)
        raise RuntimeError("Abort canonical write transaction")

    with pytest.raises(RuntimeError, match="Abort canonical write transaction"):
        await write_artifact_document(
            session_factory=h.factory,
            artifact_store=h.store,
            actor=actor,
            content=b"Rolled back result",
            filename="rollback.txt",
            mime_type="text/plain",
            kind="file",
            purpose="artifact_write",
            transaction_participant=abort,
            source_tool_call_id=uuid4().hex,
        )
    async with h.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ArtifactRecordRow)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(rows.FeedbackWorkLinkRow)
                .where(rows.FeedbackWorkLinkRow.kind.like("result_%"))
            )
            == 0
        )
    saved, _ = await write_artifact_document(
        session_factory=h.factory,
        artifact_store=h.store,
        actor=actor,
        content=b"Canonical side result",
        filename="result.txt",
        mime_type="text/plain",
        kind="file",
        purpose="artifact_write",
        transaction_participant=participant,
        source_tool_call_id=uuid4().hex,
    )
    assert (
        await h.service.agents.result_proof(scope, manifest, saved.artifact_id) == "separate_result"
    )
    results = await h.service.agents.results(h.actor, receipt.submission_id)
    assert [item.content_ref for item in results.items] == [saved.artifact_id]
    history = await h.service.continuity.history(h.actor, manifest.sources[0].subject_id, None, 100)
    assert any(
        item.kind == "result" and item.result_ref == saved.artifact_id for item in history.items
    )
    async with h.factory() as db:
        await participant(db, saved.artifact_id, False)
        with pytest.raises(FeedbackError) as replayed:
            await participant(db, manifest.sources[0].source.content_ref, False)
        assert replayed.value.code == "result_replay_mismatch"
    async with h.factory() as db:
        request = await db.scalar(
            select(DirectTurnRequestRow).where(DirectTurnRequestRow.turn_id == scope.turn_id)
        )
        request.status = "cancelled"
        request.cancel_requested_at = request.created_at
        await db.commit()
    with pytest.raises(FeedbackError) as cancelled:
        await write_artifact_document(
            session_factory=h.factory,
            artifact_store=h.store,
            actor=actor,
            content=b"Must not commit",
            filename="cancelled.txt",
            mime_type="text/plain",
            kind="file",
            purpose="artifact_write",
            transaction_participant=participant,
            source_tool_call_id=uuid4().hex,
        )
    assert cancelled.value.code == "execution_changed"
    async with h.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ArtifactRecordRow)) == 2


async def _assert_explicit_child_admission(
    h: Harness, scope: ExecutionScope, receipt: dto.SubmissionReceipt
) -> None:
    from cognis.core.feedback.child_admission import ChildTarget, prepare_child_admission

    admission = await prepare_child_admission(
        h.service.agents,
        scope,
        [
            dto.FeedbackPacketReference(
                submission_id=receipt.submission_id,
                manifest_hash=receipt.manifest_hash,
            )
        ],
    )
    session_id, child_turn = uuid4().hex, uuid4().hex
    child_scope = ExecutionScope(
        session_id=session_id,
        agent_id=h.agent_id,
        user_email=OWNER,
        conversation_id=h.conversation_id,
        turn_id=child_turn,
    )
    async with h.factory() as db:
        child = Session(
            session_id=session_id,
            agent_id=h.agent_id,
            user_email=OWNER,
            conversation_id=h.conversation_id,
            parent_session_id=scope.session_id,
            delegation_mode="delegate_sync",
        )
        db.add(child)
        await db.flush()
        packet = await db.get(rows.FeedbackSubmissionRow, receipt.submission_id)
        assert not await h.service.agents.allowed(db, child_scope, packet)
        await admission.session(child_turn)(db, child, True)
        assert await h.service.agents.allowed(db, child_scope, packet)
        await db.rollback()
    async with h.factory() as db:
        assert await db.get(Session, session_id) is None
        assert (
            await db.scalar(
                select(rows.FeedbackWorkLinkRow).where(
                    rows.FeedbackWorkLinkRow.reference_id == session_id
                )
            )
            is None
        )
        child = Session(
            session_id=session_id,
            agent_id=h.agent_id,
            user_email=OWNER,
            conversation_id=h.conversation_id,
            parent_session_id=scope.session_id,
            delegation_mode="delegate_sync",
        )
        db.add(child)
        await db.flush()
        await admission.session(child_turn)(db, child, True)
        await db.commit()
    reading = FeedbackReadArguments(
        submission_id=receipt.submission_id,
        manifest_hash=receipt.manifest_hash,
    )
    assert (await h.service.agents.read(child_scope, reading))["entries"]
    wrong_turn = await h.service.agents.scope(session_id, uuid4().hex)
    with pytest.raises(FeedbackError):
        await h.service.agents.read(wrong_turn, reading)
    async with h.factory() as db:
        child = await db.get(Session, session_id)
        with pytest.raises(FeedbackError) as replayed:
            await admission.session(uuid4().hex)(db, child, False)
        assert replayed.value.code == "child_replay_mismatch"

    async with h.factory() as db:
        child_conversation = await create_conversation(db, OWNER, h.agent_id, "web")
        await db.commit()
    registry = WorkflowRegistry(h.factory)
    workflow = await registry.create(
        Workflow(
            workflow_id=uuid4().hex,
            owner_email=OWNER,
            name="Explicit feedback child",
            steps=[StepDefinition(name="work", type="run")],
        )
    )
    queue = TaskQueue(
        session_factory=h.factory,
        workflow_engine=AsyncMock(),
        workflow_registry=registry,
        event_bus=EventBus(),
    )
    task = await queue.submit(
        agent_id=h.agent_id,
        title="Explicit feedback child",
        created_by=OWNER,
        source_type="agent",
        source_session_id=scope.session_id,
        workflow_id=workflow.workflow_id,
        creation_participant=admission.task,
    )
    queued = await h.service.agents.child(
        h.actor,
        receipt.submission_id,
        dto.ChildWorkRequest(
            manifest_hash=receipt.manifest_hash,
            child_task_id=task.task_id,
            child_attempt_number=1,
        ),
    )
    assert queued.work == "queued"
    task_session = uuid4().hex
    async with h.factory() as db:
        db.add(
            Session(
                session_id=task_session,
                agent_id=h.agent_id,
                user_email=OWNER,
                conversation_id=h.conversation_id,
            )
        )
        await db.flush()
        db.add(
            StepRun(
                step_run_id=uuid4().hex,
                task_id=task.task_id,
                step_name="work",
                step_type="run",
                agent_id=h.agent_id,
                session_id=task_session,
                status="running",
                attempt_number=1,
            )
        )
        await db.commit()
    task_scope = await h.service.agents.scope(task_session, uuid4().hex)
    assert (await h.service.agents.read(task_scope, reading))["entries"]
    async with h.factory() as db:
        task_row = await db.get(Task, task.task_id)
        task_row.attempt_number = 2
        await db.commit()
    with pytest.raises(FeedbackError):
        await h.service.agents.read(task_scope, reading)
    previous_attempt = await h.service.agents.child(
        h.actor,
        receipt.submission_id,
        dto.ChildWorkRequest(
            manifest_hash=receipt.manifest_hash,
            child_task_id=task.task_id,
            child_attempt_number=1,
        ),
    )
    assert previous_attempt.work == "cancelled"
    turns: list[str] = []

    async def bind_turn(db: AsyncSession, request: DirectTurnRequestRow, created: bool) -> None:
        await admission.bind(
            db,
            ChildTarget(
                kind="conversation",
                reference_id=request.conversation_id,
                turn_id=request.turn_id,
                created=created,
            ),
        )
        turns.append(request.turn_id)

    for _ in range(2):
        await h.scheduler.store.admit(
            conversation_id=child_conversation.conversation_id,
            session_id=None,
            agent_id=h.agent_id,
            user_id=OWNER,
            idempotency_scope=receipt.submission_id,
            idempotency_key=uuid4().hex,
            payload=DirectTurnPayloadV1(
                content=admission.notice, attachments=[], metadata={}
            ).model_dump(mode="json"),
            transaction_participant=bind_turn,
        )
    assert len(set(turns)) == 2
    with pytest.raises(FeedbackError) as ambiguous:
        await h.service.agents.child(
            h.actor,
            receipt.submission_id,
            dto.ChildWorkRequest(
                manifest_hash=receipt.manifest_hash,
                child_conversation_id=child_conversation.conversation_id,
            ),
        )
    assert ambiguous.value.code == "child_execution_required"
    async with h.factory() as db:
        request = await db.scalar(
            select(DirectTurnRequestRow).where(DirectTurnRequestRow.turn_id == turns[1])
        )
        request.status = "failed"
        await db.commit()
    first = await h.service.agents.child(
        h.actor,
        receipt.submission_id,
        dto.ChildWorkRequest(
            manifest_hash=receipt.manifest_hash,
            child_conversation_id=child_conversation.conversation_id,
            child_turn_id=turns[0],
        ),
    )
    second = await h.service.agents.child(
        h.actor,
        receipt.submission_id,
        dto.ChildWorkRequest(
            manifest_hash=receipt.manifest_hash,
            child_conversation_id=child_conversation.conversation_id,
            child_turn_id=turns[1],
        ),
    )
    assert first.turn_id == turns[0] and first.work == "queued"
    assert second.turn_id == turns[1] and second.work == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change_prompt", [False, True])
async def test_task_revision_atomic_receipt_and_safety_generation(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    change_prompt: bool,
) -> None:
    h = harness
    workflow = Workflow(
        workflow_id=uuid4().hex,
        name="Feedback revision",
        steps=[StepDefinition(name="revise", type="run")],
    )
    state = pin_effective_workflow(WorkflowState(status="paused"), workflow)
    async with h.factory() as db:
        task = await create_task(
            db,
            created_by=OWNER,
            agent_id=h.agent_id,
            title="Review task",
            status="paused",
            workflow_id=workflow.workflow_id,
            workflow_state=state.model_dump(mode="json"),
        )
        prompt = NotificationRow(
            notification_id=uuid4().hex,
            notification_type="gate",
            user_email=OWNER,
            conversation_id=h.conversation_id,
            task_id=task.task_id,
            payload={"question": "Continue?"},
        )
        db.add(prompt)
        await db.commit()
    source = await h.source(task_id=task.task_id)
    thread = await h.thread(source)
    queue = TaskQueue(
        session_factory=h.factory,
        workflow_engine=AsyncMock(),
        workflow_registry=WorkflowRegistry(h.factory),
        event_bus=EventBus(),
    )
    claims = []
    monkeypatch.setattr(queue, "_get_pending_interaction", lambda _: None)
    monkeypatch.setattr(queue, "_launch_claimed_task_run", lambda task, claim: claims.append(claim))
    h.service = FeedbackService(
        h.service.sources, h.service.payloads, h.service.agents, h.scheduler, queue
    )
    preflight = await h.service.submissions.preflight(
        h.actor,
        dto.FeedbackPreflightRequest(
            context=source.context,
            destination=dto.TaskRevisionDestination(
                kind="task_revision", task_id=task.task_id, expected_attempt=1, target_step="revise"
            ),
            selection=[
                dto.SelectedVersion(
                    entry_version_id=thread.entries[0].entry_version_id, expected_entry_version=1
                )
            ],
            update_mode="answer_only",
        ),
    )
    assert preflight.task_consequences.pending_interactions[0].revision == 1
    submission = await h.service.submissions.prepare(
        h.actor,
        dto.IncludedFeedback(
            creation_key=uuid4().hex,
            preflight_id=preflight.preflight_id,
            manifest_hash=preflight.manifest_hash,
        ),
        DispatchCommand(),
        standalone=True,
    )
    if change_prompt:
        async with h.factory() as db:
            await db.execute(
                update(NotificationRow)
                .where(NotificationRow.notification_id == prompt.notification_id)
                .values(revision=2, payload={"question": "Changed safety prompt"})
            )
            await db.commit()
        with pytest.raises(FeedbackError, match="Task safety state changed"):
            await h.service.submissions.dispatch(h.actor, submission.submission_id)
        rejected = await h.service.submissions.receipt(h.actor, submission.submission_id)
        assert rejected.delivery == "rejected"
        assert rejected.rejection.code == "task_state_changed"
        async with h.factory() as db:
            assert (await db.get(Task, task.task_id)).attempt_number == 1
            assert (await db.get(NotificationRow, prompt.notification_id)).status == "pending"
            assert (
                await db.scalar(select(func.count()).select_from(rows.TaskRevisionReceiptRow)) == 0
            )
        return
    try:
        receipt = await h.service.submissions.dispatch(h.actor, submission.submission_id)
        assert receipt.delivery == "accepted"
        assert receipt.admission.kind == "task_attempt" and receipt.admission.attempt_number == 2
        assert receipt.work == "working" and len(claims) == 1
        replay = await h.service.submissions.dispatch(h.actor, submission.submission_id)
        assert replay.admission == receipt.admission and len(claims) == 1
        async with h.factory() as db:
            assert (await db.get(Task, task.task_id)).attempt_number == 2
            resolved = await db.get(NotificationRow, prompt.notification_id)
            assert resolved.status == "resolved" and resolved.revision == 2
            assert resolved.resolution["decision"] == "revise"
            await db.execute(update(Task).where(Task.task_id == task.task_id).values(status="done"))
            await db.commit()
        assert (
            await h.service.submissions.receipt(h.actor, submission.submission_id)
        ).work == "completed"
        async with h.factory() as db:
            await db.execute(
                update(Task)
                .where(Task.task_id == task.task_id)
                .values(attempt_number=3, status="running")
            )
            await db.commit()
        assert (
            await h.service.submissions.receipt(h.actor, submission.submission_id)
        ).work == "completed"
    finally:
        for claim in claims:
            await queue._execution_store.release(claim)


@pytest.mark.asyncio
async def test_removed_comment_leaves_listing_and_can_be_restored(harness: Harness) -> None:
    h = harness
    source = await h.source()
    kept, removed = await h.thread(source, "Keep"), await h.thread(source, "Remove")
    hidden = await h.service.threads.change_thread(
        h.actor,
        removed.thread_id,
        dto.ModerationRequest(expected_version=removed.version, action="hide", reason="Removed"),
    )
    page = await h.service.threads.list(h.actor, source.subject_id)
    assert [thread.thread_id for thread in page.items] == [kept.thread_id]
    await h.service.threads.change_thread(
        h.actor,
        removed.thread_id,
        dto.ModerationRequest(expected_version=hidden.version, action="restore", reason="Undo"),
    )
    page = await h.service.threads.list(h.actor, source.subject_id)
    assert {thread.thread_id for thread in page.items} == {kept.thread_id, removed.thread_id}


@pytest.mark.asyncio
async def test_staging_omits_resolved_comments_but_keeps_their_subject(harness: Harness) -> None:
    h = harness
    context = dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    source = await h.source(context=context)
    thread = await h.thread(source)
    snapshot = await h.service.threads.staging(h.actor, h.conversation_id)
    assert [item.entry_id for item in snapshot.items] == [thread.entries[0].entry_id]
    resolved = await h.service.threads.change_thread(
        h.actor,
        thread.thread_id,
        dto.ResolutionRequest(expected_version=thread.version, state="resolved", reason=""),
    )
    snapshot = await h.service.threads.staging(h.actor, h.conversation_id)
    assert snapshot.items == []
    assert snapshot.subject_ids == [source.subject_id]
    await h.service.threads.change_thread(
        h.actor,
        thread.thread_id,
        dto.ResolutionRequest(expected_version=resolved.version, state="open", reason=""),
    )
    snapshot = await h.service.threads.staging(h.actor, h.conversation_id)
    assert [item.entry_id for item in snapshot.items] == [thread.entries[0].entry_id]


@pytest.mark.asyncio
async def test_agent_answers_sent_comments_with_reply_and_resolve(harness: Harness) -> None:
    """Sent comments stay open until the receiving agent replies to or resolves each one."""
    import json

    from cognis.models.tool import ToolCall

    h = harness
    context = dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    source = await h.source(b"Draft text", "text/plain", "draft.txt", context=context)
    receipt, scope, _ = await _running_packet(h, source)
    sent = await h.service.agents.sent_comments(scope)
    assert len(sent) == 1
    async with h.factory() as db:
        submission = await db.get(rows.FeedbackSubmissionRow, receipt.submission_id)
        assert submission is not None
        read_arguments = {
            "submission_id": submission.submission_id,
            "manifest_hash": submission.manifest_hash,
            "view": "manifest",
        }
    manifest = dto.SubmissionManifest.model_validate(
        await h.service.submissions.payloads.get(submission.payload_id)
    )
    notice = h.service.submissions.notice(submission, manifest)
    assert submission.submission_id in notice
    assert submission.manifest_hash in notice
    # The exact reference supplied in the notice works; a wrong hash is still refused.
    read = await h.service.agents.handle(
        ToolCall(call_id=uuid4().hex, name="feedback_submission_read", arguments=read_arguments),
        scope.session_id,
        scope.turn_id,
    )
    assert not read.is_error
    refused = await h.service.agents.handle(
        ToolCall(
            call_id=uuid4().hex,
            name="feedback_submission_read",
            arguments={**read_arguments, "manifest_hash": "f" * 64},
        ),
        scope.session_id,
        scope.turn_id,
    )
    assert refused.is_error
    thread_id = sent[0].entry.thread_id
    assert [item.entry.thread_id for item in await h.service.agents.open_comments(scope)] == [
        thread_id
    ]
    assert (await h.service.threads.thread(h.actor, thread_id)).state == "open"

    async def answer(arguments: dict[str, object], call_id: str | None = None) -> dict:
        call = ToolCall(call_id=call_id or uuid4().hex, name="comment_reply", arguments=arguments)
        value = await h.service.agents.handle(call, scope.session_id, scope.turn_id)
        return {"error": value.is_error, **json.loads(value.output)}

    # A comment that was not sent with this request is refused.
    assert (await answer({"comment_id": "fbt_unknown", "reply": "Done"}))["error"]
    # Neither a reply nor a resolve is an argument error.
    call = ToolCall(call_id=uuid4().hex, name="comment_reply", arguments={"comment_id": thread_id})
    assert (await h.service.agents.handle(call, scope.session_id, scope.turn_id)).is_error

    replied = await answer({"comment_id": thread_id, "reply": "Changed the title."}, "call-1")
    assert replied == {
        "error": False,
        "comment_id": thread_id,
        "state": "open",
        "replied": True,
        "remaining_comments": 0,
    }
    # A retried tool call does not duplicate the reply.
    again = await answer({"comment_id": thread_id, "reply": "Changed the title."}, "call-1")
    assert again["replied"] is False
    thread = await h.service.threads.thread(h.actor, thread_id)
    agent_entries = [entry for entry in thread.entries if entry.author.kind == "agent"]
    assert len(agent_entries) == 1 and agent_entries[0].body == "Changed the title."
    assert thread.state == "open"

    resolved = await answer({"comment_id": thread_id, "resolve": True})
    assert resolved["state"] == "resolved" and resolved["replied"] is False
    assert (await h.service.threads.thread(h.actor, thread_id)).state == "resolved"
    assert receipt.delivery == "accepted"


@pytest.mark.asyncio
async def test_resolving_without_reply_answers_the_comment(harness: Harness) -> None:
    import json

    from cognis.models.tool import ToolCall

    h = harness
    context = dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    source = await h.source(b"Other text", "text/plain", "other.txt", context=context)
    _, scope, _ = await _running_packet(h, source)
    thread_id = (await h.service.agents.sent_comments(scope))[0].entry.thread_id
    call = ToolCall(
        call_id=uuid4().hex,
        name="comment_reply",
        arguments={"comment_id": thread_id, "resolve": True},
    )
    value = await h.service.agents.handle(call, scope.session_id, scope.turn_id)
    assert json.loads(value.output)["remaining_comments"] == 0
    assert await h.service.agents.open_comments(scope) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "expected"),
    [("task", "task_revision"), ("library", "new_followup"), (None, "new_followup")],
)
async def test_task_document_destination_suggestion_follows_where_it_is_viewed(
    harness: Harness, surface: str | None, expected: str
) -> None:
    """Task output revises from the task view and starts a new chat elsewhere."""
    h = harness
    workflow = Workflow(
        workflow_id=uuid4().hex,
        name="Feedback destinations",
        steps=[StepDefinition(name="revise", type="run")],
    )
    state = pin_effective_workflow(WorkflowState(status="completed"), workflow)
    async with h.factory() as db:
        task = await create_task(
            db,
            created_by=OWNER,
            agent_id=h.agent_id,
            title="Destination task",
            status="completed",
            workflow_id=workflow.workflow_id,
            workflow_state=state.model_dump(mode="json"),
        )
        await db.commit()
    source = await h.source(task_id=task.task_id)
    options = await h.service.submissions.destinations(
        h.actor,
        dto.DestinationRequest(
            context=dto.StandaloneFeedbackContext(kind="standalone"),
            subject_ids=[source.subject_id],
            surface=surface,
        ),
    )
    suggested = [option for option in options.options if option.recommended]
    assert len(suggested) == 1
    assert suggested[0].destination is not None
    assert suggested[0].destination.kind == expected
    assert all(option.description for option in options.options if option.available)
    kinds = {option.destination.kind for option in options.options if option.destination}
    assert {"task_revision", "new_followup"} <= kinds


@pytest.mark.asyncio
async def test_chat_lists_comments_made_elsewhere_on_its_documents(harness: Harness) -> None:
    """A Library comment on a document made in a chat appears in that chat's comment list."""
    h = harness
    source = await h.source(b"Made in chat", "text/plain", "made.txt")
    assert source.context.kind == "standalone"
    thread = await h.service.threads.create(
        h.actor,
        dto.ThreadCreateRequest(
            creation_key=uuid4().hex,
            context=source.context,
            body="Commented from the Library",
            anchor=dto.AnchorInput(
                source_token=source.source_token,
                content_digest=source.content_digest,
                locator=dto.WholeLocator(kind="whole"),
            ),
        ),
    )
    snapshot = await h.service.threads.staging(h.actor, h.conversation_id)
    assert source.subject_id in snapshot.subject_ids
    assert snapshot.items == []
    page = await h.service.threads.list(h.actor, source.subject_id)
    assert thread.thread_id in {item.thread_id for item in page.items}
    # The owner's Library listing reports the open comment on the document.
    from cognis.core.library import LibraryFilters, browse

    async with h.factory() as db:
        listed = await browse(db, OWNER, LibraryFilters(kind="artifact"))
    counts = {item.id: item.open_comments for item in listed.items}
    assert counts.get(source.source.content_ref) == 1


@pytest.mark.asyncio
async def test_document_listing_counts_are_live_owner_only_and_cross_revision(
    harness: Harness,
) -> None:
    from cognis.api.routes.library import router as library_router
    from cognis.store import queries as q

    h = harness
    source = await h.source()
    first = await h.thread(source)
    removed = await h.thread(source)
    await h.service.threads.change_thread(
        h.actor,
        removed.thread_id,
        dto.ModerationRequest(expected_version=removed.version, action="hide", reason="Removed"),
    )
    old_ref = source.source.content_ref
    revision_ref = f"art_{uuid4().hex}"
    deliverable_ref = f"dlv_{uuid4().hex}"
    async with h.factory() as db:
        db.add(
            ArtifactRecordRow(
                artifact_id=revision_ref,
                namespace="artifacts",
                object_id=revision_ref,
                filename="review-v2.txt",
                owner_email=OWNER,
                owner_agent_id=h.agent_id,
                mime_type="text/plain",
                size_bytes=0,
                status="permanent",
                lineage_id=old_ref,
                revision=2,
            )
        )
        await q.create_deliverable(
            db,
            deliverable_id=deliverable_ref,
            conversation_id=h.conversation_id,
            content="Review this deliverable",
            artifact_store=h.store,
            published_owner_email=OWNER,
        )
        await db.commit()
    deliverable = await h.service.sources.resolve(
        h.actor,
        dto.ResolveSourceRequest(
            source=dto.DocumentSourceRef(kind="document", content_ref=deliverable_ref),
            context=dto.StandaloneFeedbackContext(kind="standalone"),
        ),
    )
    await h.thread(deliverable)
    app = h.app()
    app.state.session_factory = h.factory
    app.include_router(library_router)
    refs = [old_ref, revision_ref, deliverable_ref, f"art_{uuid4().hex}"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://feedback.test"
    ) as client:
        path = "/api/v1/library/comment-counts"
        assert (await client.post(path, json={"refs": refs})).status_code == 401
        owner = {"x-test-user": OWNER}
        response = await client.post(path, headers=owner, json={"refs": refs})
        assert response.status_code == 200, response.text
        assert response.json() == {"counts": dict(zip(refs, [1, 1, 1, 0], strict=True))}
        # A global admin is not the document's owner.
        response = await client.post(
            path, headers={"x-test-user": OTHER, "x-test-role": "admin"}, json={"refs": refs}
        )
        assert response.json() == {"counts": dict.fromkeys(refs, 0)}
        assert (await client.post(path, headers=owner, json={"refs": refs * 26})).status_code == 422
        await h.service.threads.change_thread(
            h.actor,
            first.thread_id,
            dto.ResolutionRequest(expected_version=first.version, state="resolved", reason="Done"),
        )
        response = await client.post(path, headers=owner, json={"refs": refs})
        assert response.json() == {"counts": dict(zip(refs, [0, 0, 1, 0], strict=True))}


@pytest.mark.asyncio
async def test_public_document_audio_and_downloads_are_pinned_and_bounded(harness: Harness) -> None:
    from cognis.core.audio_jobs import AudioJobService
    from cognis.core.speech import SpeechService
    from cognis.core.speech_models import AudioJobCreate
    from cognis.store.models import AudioJobRow

    h = harness
    source = await h.source()
    other = await h.source(b"Other private source")
    shares = [
        await h.service.public.create_share(
            h.actor,
            source.subject_id,
            dto.ShareRequest(
                source=source.source,
                expected_content_digest=source.content_digest,
                allow_comments=False,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
        for _ in range(2)
    ]
    app = h.app()
    app.state.audio_jobs = AudioJobService(
        SpeechService(session_factory=h.factory, artifacts=h.store, llm=None)
    )
    private = await app.state.audio_jobs.create(
        OWNER, AudioJobCreate(content_ref=other.source.content_ref, idempotency_key=uuid4().hex)
    )
    prefix = "/api/v1/public/feedback"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://feedback.test"
    ) as client:
        assert (await client.get(f"{prefix}/audio/source")).status_code == 401
        capabilities = []
        for share in shares:
            response = await client.post(
                f"{prefix}/exchange", json={"share_token": share.share_token}
            )
            assert response.status_code == 200, response.text
            capabilities.append({"X-Cognis-Feedback-Capability": response.json()["capability"]})
        headers = capabilities[0]
        download = await client.get(f"{prefix}/source/download", headers=headers)
        assert download.status_code == 200 and download.content == b"Original text"
        assert download.headers["cache-control"] == "no-store"
        assert (
            await client.get(f"{prefix}/source/download?format=pdf", headers=headers)
        ).status_code == 422
        assert (
            await client.get(f"{prefix}/audio/{private.job_id}", headers=headers)
        ).status_code == 404
        estimates = await client.get(f"{prefix}/audio/estimates", headers=headers)
        assert estimates.status_code == 200, estimates.text
        assert {value["length"] for value in estimates.json()} == {"shorter", "default", "longer"}
        payload = {"content_ref": source.source.content_ref, "idempotency_key": uuid4().hex}
        first = await client.post(f"{prefix}/audio", json=payload, headers=headers)
        assert first.status_code == 202, first.text
        retry = await client.post(
            f"{prefix}/audio",
            json={**payload, "idempotency_key": uuid4().hex},
            headers=capabilities[1],
        )
        assert retry.status_code == 202 and retry.json()["job_id"] == first.json()["job_id"]
        for changes in [
            {"content_ref": other.source.content_ref},
            {"voice": "unauthorized-voice"},
            {"target_minutes": 20},
            {"length": "longer"},
        ]:
            rejected = await client.post(
                f"{prefix}/audio", json={**payload, **changes}, headers=headers
            )
            assert rejected.status_code == 409, rejected.text
        async with h.factory() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AudioJobRow)
                    .where(AudioJobRow.deliverable_id == source.source.content_ref)
                )
                == 1
            )
        await h.service.public.update_share(
            h.actor,
            shares[0].share_id,
            dto.ShareUpdateRequest(expected_version=shares[0].version, revoked=True),
        )
        for path in ["/audio/source", f"/audio/{first.json()['job_id']}", "/source/download"]:
            assert (await client.get(prefix + path, headers=headers)).status_code == 404
        assert (
            await client.post(f"{prefix}/audio", json=payload, headers=headers)
        ).status_code == 404


@pytest.mark.asyncio
async def test_public_audio_all_four_plans_deduplicate_concurrently_and_never_retry_failed(
    harness: Harness,
) -> None:
    import asyncio

    from cognis.core.audio_jobs import AudioJobService
    from cognis.core.public_document_audio import public_audio_request
    from cognis.core.speech import SpeechService
    from cognis.core.speech_models import AudioJobCreate
    from cognis.store.models import AudioJobRow

    h = harness
    source = await h.source()
    jobs = AudioJobService(SpeechService(session_factory=h.factory, artifacts=h.store, llm=None))
    ids = set()
    for mode, length in [
        ("narration", None),
        ("podcast", "shorter"),
        ("podcast", "default"),
        ("podcast", "longer"),
    ]:
        requests = [
            public_audio_request(
                source.source.content_ref,
                source.content_digest,
                AudioJobCreate(
                    content_ref=source.source.content_ref,
                    mode=mode,
                    length=length,
                    idempotency_key=uuid4().hex,
                ),
            )
            for _ in range(2)
        ]
        assert requests[0].idempotency_key == requests[1].idempotency_key
        first, second = await asyncio.gather(*(jobs.create(OWNER, item) for item in requests))
        assert first.job_id == second.job_id
        ids.add(first.job_id)
        async with h.factory() as db:
            row = await db.get(AudioJobRow, first.job_id)
            assert row is not None
            row.status = "failed"
            await db.commit()
        retry = await jobs.create(OWNER, requests[1])
        assert retry.job_id == first.job_id and retry.status == "failed"
    assert len(ids) == 4
    async with h.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AudioJobRow)
                .where(AudioJobRow.deliverable_id == source.source.content_ref)
            )
            == 4
        )
