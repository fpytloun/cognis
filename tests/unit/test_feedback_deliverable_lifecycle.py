from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from cognis.core.feedback.authoring import prepare_result_write
from cognis.core.feedback.comment_links import create_comment_link
from cognis.core.feedback.primitives import Actor, FeedbackError
from cognis.core.feedback.submissions import DispatchCommand
from cognis.core.library import item
from cognis.models import feedback as dto
from cognis.models.deliverable import DeliverableStatus
from cognis.store.models import DeliverableRow, StepRun, Task
from cognis.store.queries import (
    create_deliverable,
    create_project,
    create_project_grant,
    create_step_run,
    create_task,
    get_deliverable_lineage_head,
    revoke_project_grant,
    update_deliverable_status,
    update_step_run,
    update_task_status,
)
from tests.unit.test_feedback_runtime import OTHER, OWNER, Harness, _running_packet
from tests.unit.test_feedback_runtime import harness as harness


async def workflow_output(
    h: Harness,
    *,
    content: str = "Approved workflow output",
    project_id: str | None = None,
    supersedes: str | None = None,
    status: DeliverableStatus = DeliverableStatus.APPROVED,
) -> DeliverableRow:
    """Use the workflow writer and lifecycle transitions, not invented statuses."""
    async with h.factory() as db:
        task = await create_task(
            db,
            created_by=OWNER,
            agent_id=h.agent_id,
            title="Workflow source",
            status="running",
            project_id=project_id,
        )
        step = await create_step_run(
            db,
            task_id=task.task_id,
            step_name="write",
            step_type="run",
            agent_id=h.agent_id,
            status="running",
        )
        document = await create_deliverable(
            db,
            step_run_id=step.step_run_id,
            content=content,
            artifact_store=h.store,
            supersedes_deliverable_id=supersedes,
            project_id=project_id,
        )
        assert document.status == DeliverableStatus.BUFFERED
        if status != DeliverableStatus.BUFFERED:
            approval = (
                DeliverableStatus.REJECTED
                if status == DeliverableStatus.REJECTED
                else DeliverableStatus.APPROVED
            )
            await update_deliverable_status(db, document.deliverable_id, status=approval)
            await update_step_run(
                db, step.step_run_id, status=approval, deliverable_id=document.deliverable_id
            )
            await update_task_status(
                db,
                task.task_id,
                "completed" if approval == DeliverableStatus.APPROVED else "failed",
            )
            if status == DeliverableStatus.DELIVERED:
                await update_deliverable_status(db, document.deliverable_id, status=status)
        await db.commit()
        return document


def request_for(
    h: Harness, document: DeliverableRow, *, chat: bool = False
) -> dto.ResolveSourceRequest:
    return dto.ResolveSourceRequest(
        source=dto.DocumentSourceRef(kind="document", content_ref=document.deliverable_id),
        context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
        if chat
        else dto.StandaloneFeedbackContext(kind="standalone"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("published_by_call", [False, True])
@pytest.mark.parametrize("status", [DeliverableStatus.BUFFERED, DeliverableStatus.APPROVED])
async def test_agent_comment_link_for_exact_deliverable_revision(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    published_by_call: bool,
    status: DeliverableStatus,
) -> None:
    h = harness
    monkeypatch.setattr(type(h.store), "base_url", property(lambda _: "https://feedback.test"))
    document = await workflow_output(h, status=status)
    link = await create_comment_link(
        h.service.public,
        content_ref=document.deliverable_id,
        user_email=OWNER,
        agent_id=h.agent_id,
        published_by_call=published_by_call,
    )
    assert link["allow_comments"] is True
    assert link["comment_link_status"] == "ready"
    source = await h.service.sources.resolve(h.actor, request_for(h, document))
    assert source.source.content_ref == document.deliverable_id
    assert not h.scheduler.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [False, True])
async def test_feedback_resolves_real_approved_workflow_output(
    harness: Harness, chat: bool
) -> None:
    h = harness
    document = await workflow_output(h)
    async with h.factory() as db:
        step = await db.get(StepRun, document.step_run_id)
        task = await db.get(Task, step.task_id)
        library_item = await item(db, OWNER, "deliverable", document.deliverable_id)
        assert task.status == "completed" and step.status == "approved"
        assert library_item is not None and library_item.status == "approved"
        assert await h.service.sources.document_access(db, OWNER, document.deliverable_id)
        assert not await h.service.sources.document_access(db, OTHER, document.deliverable_id)
    request = request_for(h, document, chat=chat)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()), base_url="http://feedback.test"
    ) as client:
        response = await client.post(
            "/api/v1/feedback/sources/resolve",
            headers={"x-test-user": OWNER},
            json=request.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        source = dto.ResolvedFeedbackSource.model_validate(response.json())
        assert source.source == request.source
        assert source.revision == 1 and source.latest_content_ref == document.deliverable_id
        assert source.capabilities.dispatch and source.capabilities.disclose
        denied = await client.post(
            "/api/v1/feedback/sources/resolve",
            headers={"x-test-user": OTHER, "x-test-role": "admin"},
            json=request.model_dump(mode="json"),
        )
        assert denied.status_code == 404
        assert denied.json()["detail"]["code"] == "not_found"
    evidence = await h.service.sources.load(OWNER, request.source)
    assert evidence.text == "Approved workflow output"
    assert evidence.task_id == task.task_id and evidence.step_name == "write"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DeliverableStatus.BUFFERED, DeliverableStatus.DELIVERED])
async def test_other_live_workflow_states_follow_canonical_policy(
    harness: Harness, status: DeliverableStatus
) -> None:
    h = harness
    document = await workflow_output(h, status=status)
    source = await h.service.sources.resolve(h.actor, request_for(h, document))
    assert source.latest_content_ref == document.deliverable_id
    async with h.factory() as db:
        head = await get_deliverable_lineage_head(db, document.lineage_id)
        assert head is not None and head.deliverable_id == source.source.content_ref


@pytest.mark.asyncio
async def test_public_comment_link_follows_live_head_and_fails_closed(
    harness: Harness,
) -> None:
    h = harness
    original = await workflow_output(h)
    request = request_for(h, original)
    source = await h.service.sources.resolve(h.actor, request)
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=request.source,
            expected_content_digest=source.content_digest,
            allow_comments=True,
            discussion="shared",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    assert share.share_token is not None
    second = await workflow_output(
        h,
        content="Approved revision two",
        supersedes=original.deliverable_id,
    )
    async with h.factory() as db:
        old = await db.get(DeliverableRow, original.deliverable_id)
        assert old.status == DeliverableStatus.APPROVED
    # A later rejected candidate must not displace the approved live head.
    rejected = await workflow_output(
        h,
        content="Rejected candidate",
        supersedes=second.deliverable_id,
        status=DeliverableStatus.REJECTED,
    )
    resolved = await h.service.sources.resolve(h.actor, request)
    assert resolved.source == source.source and resolved.content_digest == source.content_digest
    assert resolved.latest_content_ref == second.deliverable_id
    rejected_source = await h.service.sources.resolve(h.actor, request_for(h, rejected))
    assert rejected_source.latest_content_ref == second.deliverable_id
    async with h.factory() as db:
        head = await get_deliverable_lineage_head(db, original.lineage_id)
        assert head is not None and head.deliverable_id == resolved.latest_content_ref
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()), base_url="http://feedback.test"
    ) as client:
        exchange = await client.post(
            "/api/v1/public/feedback/exchange", json={"share_token": share.share_token}
        )
        assert exchange.status_code == 200, exchange.text
        # Outside viewers see the live head, never the rejected candidate or the old revision.
        assert exchange.json()["source"]["content_ref"] == second.deliverable_id
        headers = {"X-Cognis-Feedback-Capability": exchange.json()["capability"]}
        content = await client.get("/api/v1/public/feedback/source", headers=headers)
        assert content.status_code == 200 and content.json()["content"] == "Approved revision two"
        public_actor, _ = await h.service.public.actor(
            headers["X-Cognis-Feedback-Capability"], None
        )
        for other in (original, rejected):
            with pytest.raises(FeedbackError) as wrong_revision:
                await h.service.sources.resolve(public_actor, request_for(h, other))
            assert wrong_revision.value.status == 404
        # Following the head bumped the share version; revoke against the current one.
        current = await h.service.public.copy_share(h.actor, share.share_id)
        await h.service.public.update_share(
            h.actor,
            share.share_id,
            dto.ShareUpdateRequest(expected_version=current.version, revoked=True),
        )
        assert (
            await client.get("/api/v1/public/feedback/source", headers=headers)
        ).status_code == 404
        # A valid unrevoked capability must still lose access when the exact revision it
        # was issued for is removed; it must not fall through to another revision.
        replacement_share = await h.service.public.create_share(
            h.actor,
            source.subject_id,
            dto.ShareRequest(
                source=request.source,
                expected_content_digest=source.content_digest,
                allow_comments=True,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
        exchange = await client.post(
            "/api/v1/public/feedback/exchange",
            json={"share_token": replacement_share.share_token},
        )
        assert exchange.status_code == 200
        assert exchange.json()["source"]["content_ref"] == second.deliverable_id
        headers = {"X-Cognis-Feedback-Capability": exchange.json()["capability"]}
        async with h.factory() as db:
            served = await db.get(DeliverableRow, second.deliverable_id)
            assert served is not None
            await db.delete(served)
            await db.commit()
        for path in ["/source", "/source/bytes"]:
            denied = await client.get(f"/api/v1/public/feedback{path}", headers=headers)
            assert denied.status_code == 404
            assert denied.json()["detail"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_approved_revision_requires_reconfirmation_but_unchanged_head_can_dispatch(
    harness: Harness,
) -> None:
    h = harness
    original = await workflow_output(h)
    source = await h.service.sources.resolve(h.actor, request_for(h, original))
    thread = await h.thread(source)
    request = dto.FeedbackPreflightRequest(
        context=source.context,
        destination=dto.ConversationDestination(
            kind="existing_followup", conversation_id=h.conversation_id
        ),
        selection=[
            dto.SelectedVersion(
                entry_version_id=thread.entries[0].entry_version_id, expected_entry_version=1
            )
        ],
        update_mode="answer_only",
    )
    preflight = await h.service.submissions.preflight(h.actor, request)
    second = await workflow_output(
        h,
        content="Approved revision two",
        supersedes=original.deliverable_id,
    )
    command = DispatchCommand(content="Discuss the pinned source", chat_mode="build")
    with pytest.raises(FeedbackError) as changed:
        await h.service.submissions.prepare(
            h.actor,
            dto.IncludedFeedback(
                creation_key=uuid4().hex,
                preflight_id=preflight.preflight_id,
                manifest_hash=preflight.manifest_hash,
            ),
            command,
            standalone=True,
        )
    assert changed.value.code == "source_head_changed" and not h.scheduler.calls
    refreshed = await h.service.submissions.preflight(h.actor, request)
    assert refreshed.source_summaries[0].latest_content_ref == second.deliverable_id
    pending = await h.service.submissions.prepare(
        h.actor,
        dto.IncludedFeedback(
            creation_key=uuid4().hex,
            preflight_id=refreshed.preflight_id,
            manifest_hash=refreshed.manifest_hash,
        ),
        command,
        standalone=True,
    )
    receipt = await h.service.submissions.dispatch(h.actor, pending.submission_id)
    assert receipt.delivery == "accepted" and len(h.scheduler.calls) == 1
    detail = await h.service.submissions.detail(h.actor, receipt.submission_id)
    assert detail.manifest.sources[0].source == source.source
    assert detail.manifest.sources[0].content_digest == source.content_digest


@pytest.mark.asyncio
async def test_approved_workflow_project_reads_recheck_grants(harness: Harness) -> None:
    h = harness
    async with h.factory() as db:
        project = await create_project(db, owner_email=OWNER, name="Workflow feedback")
        grant = await create_project_grant(
            db,
            project_id=project.project_id,
            granted_by=OWNER,
            grantee_user_email=OTHER,
            permission="viewer",
        )
        await db.commit()
    document = await workflow_output(h, project_id=project.project_id)
    request = request_for(h, document)
    reader = await h.service.sources.resolve(Actor.user(OTHER), request)
    assert reader.capabilities.read and not reader.capabilities.dispatch
    async with h.factory() as db:
        await revoke_project_grant(db, grant.grant_id)
        await db.commit()
    with pytest.raises(FeedbackError) as revoked:
        await h.service.sources.resolve(Actor.user(OTHER), request)
    assert revoked.value.status == 404
    assert (await h.service.sources.resolve(h.actor, request)).source == request.source


@pytest.mark.asyncio
async def test_approved_results_remain_visible_in_results_and_continuity(harness: Harness) -> None:
    h = harness
    document = await workflow_output(h)
    source = await h.service.sources.resolve(h.actor, request_for(h, document, chat=True))
    receipt, scope, _ = await _running_packet(h, source)
    participant = await prepare_result_write(h.service.agents, scope)
    assert participant is not None
    async with h.factory() as db:
        result = await create_deliverable(
            db,
            conversation_id=h.conversation_id,
            session_id=scope.session_id,
            turn_id=scope.turn_id,
            content="Response to approved workflow feedback",
            artifact_store=h.store,
        )
        await participant(db, result.deliverable_id, True)
        await update_deliverable_status(
            db, result.deliverable_id, status=DeliverableStatus.APPROVED
        )
        await db.commit()
    results = await h.service.agents.results(h.actor, receipt.submission_id)
    assert [value.content_ref for value in results.items] == [result.deliverable_id]
    history = await h.service.continuity.history(h.actor, source.subject_id, None, 100)
    assert any(
        value.kind == "result" and value.result_ref == result.deliverable_id
        for value in history.items
    )
