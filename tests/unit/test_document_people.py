from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text

from cognis.api.routes import artifact_documents, artifacts, deliverables
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.document_people import PersonGrantRequest, people, set_person
from cognis.core.feedback.primitives import Actor, FeedbackError
from cognis.core.library import item
from cognis.models import feedback as dto
from cognis.store.document_people import person_may_read_document
from cognis.store.feedback_models import FeedbackShareRow
from cognis.store.models import ArtifactRecordRow, DocumentUserGrant, User
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_deliverable,
    create_user,
    lock_artifact_revision_target,
)
from cognis.tools.builtin.artifact_tools import _handle_artifact_get_url
from tests.unit.test_deliverable_exports import offline_bundle as offline_bundle
from tests.unit.test_feedback_runtime import OTHER, OWNER, Harness
from tests.unit.test_feedback_runtime import harness as harness


async def document(
    h: Harness, kind: str, previous: str | None = None, *, rich: bool = False
) -> str:
    """Publish through the canonical writers, including their revision locks."""
    async with h.factory() as db:
        if kind == "deliverable":
            row = await create_deliverable(
                db,
                conversation_id=h.conversation_id,
                content="# Shared revision",
                title="Shared document",
                format="rich" if rich else "markdown",
                rich={"blocks": [{"type": "markdown", "content": "# Shared revision"}]}
                if rich
                else None,
                published_owner_email=OWNER,
                supersedes_deliverable_id=previous,
                artifact_store=h.store,
            )
            ref = row.deliverable_id
        else:
            ref = "art_" + uuid4().hex
            data = b"Shared revision"
            await h.store.async_save("artifacts", ref, "shared.txt", data, "text/plain")
            lineage = await lock_artifact_revision_target(db, previous) if previous else None
            await create_artifact_record(
                db,
                artifact_id=ref,
                namespace="artifacts",
                object_id=ref,
                filename="shared.txt",
                mime_type="text/plain",
                size_bytes=len(data),
                owner_email=OWNER,
                owner_agent_id=h.agent_id,
                purpose="artifact_write",
                kind="file",
                conversation_id=h.conversation_id,
                status="attached",
                lineage_id=lineage.lineage_id if lineage else None,
                revision=lineage.revision if lineage else 1,
                supersedes_artifact_id=previous,
            )
        await db.commit()
    return ref


async def resolve(h: Harness, email: str, ref: str) -> dto.ResolvedFeedbackSource:
    return await h.service.sources.resolve(
        Actor.user(email),
        dto.ResolveSourceRequest(
            source=dto.DocumentSourceRef(kind="document", content_ref=ref),
            context=dto.StandaloneFeedbackContext(kind="standalone"),
        ),
    )


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
async def test_people_follow_future_revisions_without_context_agent_or_reshare_access(
    harness: Harness, kind: Literal["artifact", "deliverable"]
) -> None:
    h = harness
    first = await document(h, kind)
    with pytest.raises(FeedbackError, match="unavailable"):
        await resolve(h, OTHER, first)
    async with h.factory() as db:
        page = await set_person(
            db, OWNER, kind, first, PersonGrantRequest(principal_email=OTHER, expected_version=0)
        )
    assert page.items[0].version == 1
    assert not page.items[0].allow_comments
    second = await document(h, kind, first)
    for ref in (first, second):
        source = await resolve(h, OTHER, ref)
        assert source.capabilities.model_dump() == {
            "read": True,
            "comment": False,
            "moderate": False,
            "dispatch": False,
            "disclose": False,
        }
        async with h.factory() as db:
            library_item = await item(db, OTHER, kind, ref)
        assert library_item is not None
        assert library_item.conversation_id is None
        assert library_item.task_id is None
    with pytest.raises(FeedbackError, match="conversation"):
        await h.service.sources.resolve(
            Actor.user(OTHER),
            dto.ResolveSourceRequest(
                source=dto.DocumentSourceRef(kind="document", content_ref=first),
                context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id),
            ),
        )
    async with h.factory() as db:
        with pytest.raises(LookupError):
            await people(db, OTHER, kind, first)
        recipient_agent = "people-reviewer-" + uuid4().hex
        await create_agent(db, agent_id=recipient_agent, owner_email=OTHER, name="Reviewer")
        await db.commit()
    result = await _handle_artifact_get_url(
        {"artifact_id": second},
        artifact_store=h.store,
        session_factory=h.factory,
        user_email=OTHER,
        accessor_agent_id=recipient_agent,
    )
    assert result.is_error, "A human-only grant must not grant agent runtime access"
    app = h.app()
    app.state.session_factory = h.factory
    app.state.artifact_store = h.store
    app.include_router(artifact_documents.router)
    app.include_router(artifacts.router)
    app.include_router(deliverables.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/v1/library/items/{kind}/{first}"
        headers = {"x-test-user": OTHER, "x-test-role": "admin"}
        assert (await client.get(path + "/people", headers=headers)).status_code == 404
        assert (
            await client.post(path + "/public-link", json={}, headers=headers)
        ).status_code == 404
        if kind == "artifact":
            link = await client.get(f"/api/v1/artifacts/{second}/signed-url", headers=headers)
            assert link.status_code == 200
            url = link.json()["url"]
            assert url.startswith(f"/api/v1/artifacts/{second}/bytes?")
            assert (await client.get(url)).status_code == 401
            response = await client.get(url, headers=headers)
            assert response.status_code == 200 and response.content == b"Shared revision"
            assert response.headers["cache-control"] == "private, no-store"
        else:
            response = await client.get(f"/api/v1/deliverables/{second}", headers=headers)
            assert response.status_code == 200
            assert (
                await client.post(
                    f"/api/v1/deliverables/{second}/share-link", json={}, headers=headers
                )
            ).status_code == 404

    async with h.factory() as db:
        await set_person(
            db,
            OWNER,
            kind,
            first,
            PersonGrantRequest(principal_email=OTHER, expected_version=1, allow_comments=True),
        )
    for ref in (first, second):
        assert (await resolve(h, OTHER, ref)).capabilities.comment
    source = await resolve(h, OTHER, first)
    thread = await h.service.threads.create(
        Actor.user(OTHER),
        dto.ThreadCreateRequest(
            creation_key=uuid4().hex,
            context=source.context,
            body="A shared comment",
            anchor=dto.AnchorInput(
                source_token=source.source_token,
                content_digest=source.content_digest,
                locator=dto.WholeLocator(kind="whole"),
            ),
        ),
    )
    assert thread.entries
    with pytest.raises(FeedbackError, match="owner"):
        await h.service.public.create_share(
            Actor.user(OTHER),
            source.subject_id,
            dto.ShareRequest(
                source=source.source,
                expected_content_digest=source.content_digest,
                allow_comments=False,
                discussion="own_only",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
    async with h.factory() as db:
        await set_person(
            db,
            OWNER,
            kind,
            second,
            PersonGrantRequest(principal_email=OTHER, expected_version=2, revoked=True),
        )
    for ref in (first, second):
        with pytest.raises(FeedbackError):
            await resolve(h, OTHER, ref)
    async with h.factory() as db:
        with pytest.raises(FeedbackError, match="changed"):
            await set_person(
                db,
                OWNER,
                kind,
                first,
                PersonGrantRequest(principal_email=OTHER, expected_version=1, allow_comments=True),
            )
    async with h.factory() as db:
        await set_person(
            db, OWNER, kind, first, PersonGrantRequest(principal_email=OTHER, expected_version=3)
        )
    assert not (await resolve(h, OTHER, second)).capabilities.comment
    assert not h.scheduler.calls


@pytest.mark.parametrize("invalid_recipient", ["self", "missing", "inactive"])
async def test_people_validate_recipient_and_preserve_cas(
    harness: Harness, invalid_recipient: str
) -> None:
    h = harness
    ref = await document(h, "artifact")
    email = OWNER if invalid_recipient == "self" else OTHER
    async with h.factory() as db:
        user = await db.get(User, OTHER)
        assert user is not None
        if invalid_recipient == "missing":
            await db.delete(user)
        elif invalid_recipient == "inactive":
            user.is_active = False
        await db.commit()
        with pytest.raises(FeedbackError, match="active Cognis user"):
            await set_person(
                db,
                OWNER,
                "artifact",
                ref,
                PersonGrantRequest(principal_email=email, expected_version=0),
            )
        assert list(await db.scalars(select(DocumentUserGrant))) == []


@pytest.mark.parametrize(
    "allow_comments,mode", [(False, "view"), (True, "view"), (False, "download")]
)
async def test_public_copy_is_owner_only_repeatable_pinned_and_does_not_revive(
    harness: Harness, allow_comments: bool, mode: Literal["view", "download"]
) -> None:
    h = harness
    source = await h.source()
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=source.source,
            expected_content_digest=source.content_digest,
            allow_comments=allow_comments,
            discussion="own_only",
            link_mode=mode,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    first = await h.service.public.copy_share(h.actor, share.share_id)
    second = await h.service.public.copy_share(h.actor, share.share_id)
    assert first.share_token == second.share_token == share.share_token
    assert first.version == share.version and first.expires_at == share.expires_at
    exchange = await h.service.public.exchange(share.share_token)
    assert exchange.link_mode == mode and exchange.allow_comments == allow_comments
    with pytest.raises(FeedbackError):
        await h.service.public.copy_share(Actor.user(OTHER), share.share_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/feedback/public-shares/{share.share_id}/link", headers={"x-test-user": OWNER}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["share_token"] == share.share_token
    async with h.factory() as db:
        row = await db.get(FeedbackShareRow, share.share_id)
        assert row is not None
        row.token_format = "random_v1"
        await db.commit()
    with pytest.raises(FeedbackError, match="cannot be recovered"):
        await h.service.public.copy_share(h.actor, share.share_id)
    # Older bearer links remain usable; recovery never silently rotates them.
    assert (await h.service.public.exchange(share.share_token)).share_id == share.share_id
    async with h.factory() as db:
        row = await db.get(FeedbackShareRow, share.share_id)
        row.token_format = "derived_v1"
        row.revoked = True
        await db.commit()
    with pytest.raises(FeedbackError, match="cannot be recovered"):
        await h.service.public.copy_share(h.actor, share.share_id)
    with pytest.raises(FeedbackError):
        await h.service.public.exchange(share.share_token)


@pytest.mark.parametrize("rich", [False, True])
async def test_managed_download_preserves_deliverable_export_format(
    harness: Harness, rich: bool
) -> None:
    h = harness
    ref = await document(h, "deliverable", rich=rich)
    source = await resolve(h, OWNER, ref)
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=source.source,
            expected_content_digest=source.content_digest,
            allow_comments=False,
            discussion="own_only",
            link_mode="download",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    access = await h.service.public.exchange(share.share_token)
    app = h.app()
    app.state.session_factory = h.factory
    app.state.artifact_store = h.store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/public/feedback/source/bytes?download=true",
            headers={"X-Cognis-Feedback-Capability": access.capability},
        )
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    if rich:
        assert response.headers["content-type"].startswith("text/html")
        assert 'id="cognis-deliverable-root"' in response.text
        assert "window.offlineRenderer = true;" in response.text
        assert '<script type="module" src=' not in response.text
    else:
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.text == "# Shared revision"
    assert access.capability not in response.text


@pytest.mark.parametrize("upgrade_path", ["alembic", "bootstrap"])
async def test_document_sharing_upgrade_preserves_legacy_tokens_and_is_idempotent(
    harness: Harness, upgrade_path: str
) -> None:
    h = harness
    source = await h.source()
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=source.source,
            expected_content_digest=source.content_digest,
            allow_comments=True,
            discussion="own_only",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    migration = importlib.import_module("cognis.store.migrations.versions.180_document_sharing")
    async with h.engine.begin() as connection:
        await connection.run_sync(lambda sync: DocumentUserGrant.__table__.drop(sync))
        await connection.execute(text("ALTER TABLE feedback_shares DROP COLUMN token_format"))
        await connection.execute(text("ALTER TABLE feedback_shares DROP COLUMN link_mode"))
        if upgrade_path == "alembic":

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()
                    migration.upgrade()

            await connection.run_sync(upgrade)
    await run_schema_bootstrap(h.engine)
    await run_schema_bootstrap(h.engine)
    async with h.engine.begin() as connection:
        primary_key = await connection.run_sync(
            lambda sync: inspect(sync).get_pk_constraint("document_user_grants")
        )
        assert primary_key["constrained_columns"] == ["kind", "lineage_id", "principal_email"]
        assert (
            await connection.execute(text("SELECT token_format, link_mode FROM feedback_shares"))
        ).one() == ("random_v1", "view")
    assert (await h.service.public.exchange(share.share_token)).share_id == share.share_id
    with pytest.raises(FeedbackError, match="cannot be recovered"):
        await h.service.public.copy_share(h.actor, share.share_id)
    async with h.engine.begin() as connection:

        def downgrade_then_upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()
                migration.upgrade()

        await connection.run_sync(downgrade_then_upgrade)


async def test_people_concurrent_changes_use_cas_and_inactive_recipients_can_be_revoked(
    harness: Harness,
) -> None:
    h = harness
    ref = await document(h, "artifact")

    async def grant(comment: bool):
        async with h.factory() as db:
            try:
                return await set_person(
                    db,
                    OWNER,
                    "artifact",
                    ref,
                    PersonGrantRequest(
                        principal_email=OTHER, expected_version=0, allow_comments=comment
                    ),
                )
            except FeedbackError as exc:
                return exc

    results = await asyncio.gather(grant(False), grant(True))
    assert sum(isinstance(result, FeedbackError) for result in results) == 1
    conflict = next(result for result in results if isinstance(result, FeedbackError))
    assert conflict.status == 409
    async with h.factory() as db:
        user = await db.get(User, OTHER)
        user.is_active = False
        await db.commit()
        row = await db.get(ArtifactRecordRow, ref)
        assert not await person_may_read_document(db, row, OTHER)
        page = await set_person(
            db,
            OWNER,
            "artifact",
            ref,
            PersonGrantRequest(principal_email=OTHER, expected_version=1, revoked=True),
        )
        assert page.items[0].version == 2 and page.items[0].revoked


async def test_people_grants_are_correlated_to_the_document_owner(harness: Harness) -> None:
    h = harness
    ref = await document(h, "artifact")
    async with h.factory() as db:
        await set_person(
            db,
            OWNER,
            "artifact",
            ref,
            PersonGrantRequest(principal_email=OTHER, expected_version=0),
        )
        row = await db.get(ArtifactRecordRow, ref)
        assert await person_may_read_document(db, row, OTHER)
        # A corrupt/imported row must not borrow grants from someone else's lineage.
        next_owner = f"{uuid4().hex}@cognis.test"
        await create_user(db, next_owner, name="Another owner", password_hash="!")
        row.owner_email = next_owner
        await db.commit()
        assert not await person_may_read_document(db, row, OTHER)
        assert await item(db, OTHER, "artifact", ref) is None


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
async def test_comment_link_follows_the_latest_revision(
    harness: Harness, kind: Literal["artifact", "deliverable"]
) -> None:
    """Outside viewers open the newest revision; the owner still lists the link and can revoke it."""
    h = harness
    first = await document(h, kind)
    source = await resolve(h, OWNER, first)
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=source.source,
            expected_content_digest=source.content_digest,
            allow_comments=True,
            discussion="own_only",
            link_mode="view",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    before = await h.service.public.exchange(share.share_token)
    assert before.source.content_ref == first

    second = await document(h, kind, first)
    after = await h.service.public.exchange(share.share_token)
    assert after.source.content_ref == second
    # The capability issued for the previous revision is retired.
    with pytest.raises(FeedbackError):
        await h.service.public.actor(before.capability)
    latest = await resolve(h, OWNER, second)
    listed = await h.service.public.shares(h.actor, latest.subject_id)
    assert [item.share_id for item in listed.items] == [share.share_id]
    listed_from_old = await h.service.public.shares(h.actor, source.subject_id)
    assert [item.share_id for item in listed_from_old.items] == [share.share_id]

    async with h.factory() as db:
        row = await db.get(FeedbackShareRow, share.share_id)
        assert row is not None
        row.revoked = True
        await db.commit()
    with pytest.raises(FeedbackError):
        await h.service.public.exchange(share.share_token)
