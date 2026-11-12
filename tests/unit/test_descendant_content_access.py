"""Characterize physical-session ancestry at every persisted-content read boundary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, update

from cognis.core.agent_loop import AgentLoop
from cognis.store.models import (
    Agent,
    ArtifactGrantRow,
    ArtifactRecordRow,
    Conversation,
    DeliverableRow,
    KnowledgebaseArtifactRow,
    KnowledgebaseRow,
    ManagedConversationLink,
    ProjectAgentRow,
    ProjectGrantRow,
    ProjectRow,
    Session,
    StepRun,
    Task,
)
from cognis.store.queries import (
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_managed_conversation_link,
    create_session,
    create_step_run,
    create_task,
    create_user,
)
from cognis.tools.builtin.artifact_tools import handle_artifact_tool

OWNER = "owner@example.com"


def _metadata(session):
    return {
        "conversation_id": session.conversation_id,
        "agent_id": session.agent_id,
        "session_id": session.session_id,
    }


async def _tool(factory, actor, name, arguments, *, user_email=OWNER):
    return await handle_artifact_tool(
        name,
        arguments,
        llm=None,
        artifact_store=factory.artifact_store,
        session_factory=factory,
        user_email=user_email,
        runtime_metadata=_metadata(actor),
    )


async def _outputs(factory, producer, *, step_run_id=None, project_id=None):
    artifacts, deliverables = [], []
    for revision in (1, 2):
        ref = f"art_{uuid.uuid4().hex}"
        content = f"Child result {revision}".encode()
        await factory.artifact_store.async_save(
            "artifacts", ref, "child-result.txt", content, "text/plain", OWNER
        )
        async with factory() as db:
            revision_step_id = step_run_id
            if step_run_id is not None and revision > 1:
                original_step = await db.get(StepRun, step_run_id)
                attempt = await create_step_run(
                    db,
                    task_id=original_step.task_id,
                    step_name=original_step.step_name,
                    step_type="run",
                    agent_id=producer.agent_id,
                    conversation_id=producer.conversation_id,
                    attempt=revision,
                    attempt_number=revision,
                )
                attempt.session_id = producer.session_id
                revision_step_id = attempt.step_run_id
            await create_artifact_record(
                db,
                artifact_id=ref,
                namespace="artifacts",
                object_id=ref,
                filename="child-result.txt",
                owner_email=OWNER,
                conversation_id=producer.conversation_id,
                session_id=producer.session_id,
                purpose="artifact_publish",
                kind="file",
                mime_type="text/plain",
                size_bytes=len(content),
                status="attached",
                lineage_id=artifacts[0] if artifacts else ref,
                revision=revision,
                supersedes_artifact_id=artifacts[-1] if artifacts else None,
                project_id=project_id,
            )
            artifacts.append(ref)
            row = await create_deliverable(
                db,
                step_run_id=revision_step_id,
                conversation_id=producer.conversation_id if step_run_id is None else None,
                session_id=producer.session_id if step_run_id is None else None,
                content=f"Child result {revision}",
                supersedes_deliverable_id=deliverables[-1] if deliverables else None,
                artifact_store=factory.artifact_store,
                project_id=project_id,
            )
            if step_run_id is not None:
                row.status = "approved"
            deliverables.append(row.deliverable_id)
            await db.commit()
    return artifacts, deliverables


async def _assert_surfaces(factory, actor, ref, *, allowed, user_email=OWNER):
    for surface in (
        "artifact_get_content",
        "artifact_read",
        "artifact_get_metadata",
        "artifact_get_url",
    ):
        result = await _tool(factory, actor, surface, {"artifact_id": ref}, user_email=user_email)
        assert result.is_error is not allowed, (surface, ref, result.output)
        if not allowed:
            assert result.output == f"Artifact not found: {ref}"
            assert not result.attachments
            assert not result.metadata

    loop = object.__new__(AgentLoop)
    loop.session_manager = SimpleNamespace(session_factory=factory)
    loop.artifact_store = factory.artifact_store
    context = SimpleNamespace(
        session=actor,
        conversation=SimpleNamespace(conversation_id=actor.conversation_id, user_email=user_email),
    )
    if allowed:
        attachment, deliverable = await loop._resolve_attachable_content_ref(context, ref)
        assert (attachment.artifact_id if attachment else deliverable.deliverable_id) == ref
    else:
        with pytest.raises(ValueError, match="not found or is unavailable"):
            await loop._resolve_attachable_content_ref(context, ref)


async def _work(factory, kind):
    async with factory() as db:
        db.add(Agent(agent_id="agent-worker", owner_email=OWNER, name="Worker"))
        db.add(Agent(agent_id="agent-observer", owner_email=OWNER, name="Observer"))
        await db.flush()
        await create_conversation(
            db, OWNER, "agent-observer", "agent_direct", conversation_id="conv-observer"
        )
        observer = await create_session(db, "conv-observer", OWNER, "agent-observer")
        root = await create_session(db, "conv-owner", OWNER, "agent-owner")
        if kind == "managed":
            await create_conversation(
                db, OWNER, "agent-worker", "agent_work", conversation_id="conv-worker"
            )
            parent = root
            producer = await create_session(db, "conv-worker", OWNER, "agent-worker")
            link = await create_managed_conversation_link(
                db,
                user_email=OWNER,
                controller_agent_id=parent.agent_id,
                controller_conversation_id=parent.conversation_id,
                controller_session_id=parent.session_id,
                target_agent_id=producer.agent_id,
                target_conversation_id=producer.conversation_id,
                target_session_id=producer.session_id,
                title="Worker",
            )
            # Reading output must not depend on whether managed work is open.
            link.conversation_state = "closed"
            sibling = observer
        else:
            parent = await create_session(
                db, "conv-owner", OWNER, "system:explore", parent_session_id=root.session_id
            )
            producer = await create_session(
                db,
                "conv-owner",
                OWNER,
                "system:research",
                parent_session_id=parent.session_id,
            )
            sibling = await create_session(
                db, "conv-owner", OWNER, "system:explore", parent_session_id=root.session_id
            )
        await db.commit()
    return parent, producer, sibling, observer


async def _task_work(factory):
    parent, producer, sibling, observer = await _work(factory, "managed")
    async with factory() as db:
        # Only task ownership, not a managed link or a source session, may grant access.
        await db.execute(delete(ManagedConversationLink))
        task = await create_task(
            db, created_by=OWNER, agent_id=parent.agent_id, title="Cross-agent step"
        )
        step = await create_step_run(
            db,
            task_id=task.task_id,
            step_name="report",
            step_type="run",
            agent_id=producer.agent_id,
            conversation_id=producer.conversation_id,
        )
        step.session_id = producer.session_id
        await db.commit()
    return parent, producer, sibling, observer, step


async def _worker_project(factory, *, owner_email=OWNER):
    async with factory() as db:
        project = ProjectRow(
            project_id="project-worker", owner_email=owner_email, name="Worker project"
        )
        db.add(project)
        await db.flush()
        db.add(
            ProjectAgentRow(
                project_id=project.project_id,
                agent_id="agent-worker",
                is_default=True,
                added_by=OWNER,
            )
        )
        await db.commit()
    return project.project_id


@pytest.mark.parametrize("kind", ["managed", "delegate", "task"])
@pytest.mark.parametrize("snapshots", [True, False], ids=["output-grants", "live-provenance"])
async def test_project_descendant_outputs_do_not_require_parent_project_assignment(
    task_continuation_db, kind, snapshots
):
    factory = task_continuation_db
    step = None
    if kind == "task":
        parent, producer, sibling, observer, step = await _task_work(factory)
    else:
        parent, producer, sibling, observer = await _work(factory, kind)
    project_id = await _worker_project(factory)
    if step:
        async with factory() as db:
            task = await db.get(Task, step.task_id)
            task.project_id = project_id
            await db.commit()
    artifacts, deliverables = await _outputs(
        factory,
        producer,
        step_run_id=step.step_run_id if step else None,
        project_id=project_id,
    )
    async with factory() as db:
        rotated_parent = await create_session(
            db,
            parent.conversation_id,
            OWNER,
            parent.agent_id,
            parent_session_id=parent.parent_session_id,
            previous_session_id=parent.session_id,
        )
        rotated_producer = await create_session(
            db,
            producer.conversation_id,
            OWNER,
            producer.agent_id,
            parent_session_id=producer.parent_session_id,
            previous_session_id=producer.session_id,
        )
        if step and snapshots:
            await db.execute(
                update(StepRun)
                .where(StepRun.task_id == step.task_id)
                .values(session_id=rotated_producer.session_id)
            )
        if not snapshots:
            await db.execute(
                delete(ArtifactGrantRow).where(
                    ArtifactGrantRow.content_ref.in_(artifacts + deliverables)
                )
            )
        await db.commit()
    for revisions in (artifacts, deliverables):
        for ref in revisions:
            await _assert_surfaces(factory, rotated_parent, ref, allowed=True)
        latest = await _tool(
            factory,
            rotated_parent,
            "artifact_get_content",
            {"artifact_id": revisions[0], "latest": True},
        )
        assert not latest.is_error, latest.output
        assert latest.metadata["content"] == "Child result 2"
        assert latest.metadata["revision"] == 2
        for denied in (sibling, observer):
            await _assert_surfaces(factory, denied, revisions[-1], allowed=False)
        await _assert_surfaces(
            factory, rotated_parent, revisions[-1], allowed=False, user_email="other@example.com"
        )
    fresh_artifacts, fresh_deliverables = await _outputs(
        factory,
        rotated_producer,
        # A new task publication uses a new step run, not a rewrite of the
        # already-revised canonical output from the previous attempts.
        step_run_id=await _next_step(factory, step, rotated_producer) if step else None,
        project_id=project_id,
    )
    for ref in (fresh_artifacts[-1], fresh_deliverables[-1]):
        await _assert_surfaces(factory, rotated_parent, ref, allowed=True)
    private_artifacts, private_deliverables = await _outputs(factory, rotated_parent)
    for ref in (private_artifacts[-1], private_deliverables[-1]):
        await _assert_surfaces(factory, rotated_producer, ref, allowed=False)


async def _next_step(factory, previous, producer):
    async with factory() as db:
        step = await create_step_run(
            db,
            task_id=previous.task_id,
            step_name="follow-up",
            step_type="run",
            agent_id=producer.agent_id,
            conversation_id=producer.conversation_id,
        )
        step.session_id = producer.session_id
        await db.commit()
    return step.step_run_id


@pytest.mark.parametrize("kind", ["managed", "delegate"])
@pytest.mark.parametrize("snapshots", [True, False], ids=["output-grants", "live-provenance"])
async def test_descendant_revisions_survive_physical_session_rollover(
    task_continuation_db, kind, snapshots
):
    factory = task_continuation_db
    parent, producer, sibling, observer = await _work(factory, kind)
    artifacts, deliverables = await _outputs(factory, producer)
    async with factory() as db:
        rotated_parent = await create_session(
            db,
            parent.conversation_id,
            OWNER,
            parent.agent_id,
            parent_session_id=parent.parent_session_id,
            previous_session_id=parent.session_id,
        )
        rotated_producer = await create_session(
            db,
            producer.conversation_id,
            OWNER,
            producer.agent_id,
            parent_session_id=producer.parent_session_id,
            previous_session_id=producer.session_id,
        )
        if not snapshots:
            # Force the shared resolver to prove ancestry instead of using the
            # output-specific grants normally persisted at publication.
            await db.execute(
                delete(ArtifactGrantRow).where(
                    ArtifactGrantRow.content_ref.in_(artifacts + deliverables)
                )
            )
        await db.commit()

    for revisions in (artifacts, deliverables):
        for revision, ref in enumerate(revisions, 1):
            await _assert_surfaces(factory, rotated_parent, ref, allowed=True)
            exact = await _tool(
                factory, rotated_parent, "artifact_get_content", {"artifact_id": ref}
            )
            assert exact.metadata["content"] == f"Child result {revision}"
            assert exact.metadata["revision"] == revision
            assert exact.metadata["is_latest"] is (revision == 2)
        latest = await _tool(
            factory,
            rotated_parent,
            "artifact_get_content",
            {"artifact_id": revisions[0], "latest": True},
        )
        assert not latest.is_error, latest.output
        assert latest.metadata["content"] == "Child result 2"
        assert latest.metadata["revision"] == 2
        for denied in (sibling, observer):
            await _assert_surfaces(factory, denied, revisions[-1], allowed=False)
        await _assert_surfaces(
            factory, rotated_parent, revisions[-1], allowed=False, user_email="other@example.com"
        )

    # New publications after both rotations must still reach the original
    # controller even though the managed link points at older physical sessions.
    new_artifacts, new_deliverables = await _outputs(factory, rotated_producer)
    for ref in (new_artifacts[-1], new_deliverables[-1]):
        await _assert_surfaces(factory, rotated_parent, ref, allowed=True)


async def test_managed_ancestry_does_not_grant_worker_storage_or_reverse_access(
    task_continuation_db,
):
    factory = task_continuation_db
    parent, producer, _sibling, _observer = await _work(factory, "managed")
    async with factory() as db:
        await create_conversation(
            db, OWNER, producer.agent_id, "agent_direct", conversation_id="conv-worker-private"
        )
        unrelated = await create_session(db, "conv-worker-private", OWNER, producer.agent_id)
        await db.commit()
    for actor, reader in ((unrelated, parent), (parent, producer)):
        artifacts, deliverables = await _outputs(factory, actor)
        for ref in (artifacts[-1], deliverables[-1]):
            await _assert_surfaces(factory, reader, ref, allowed=False)


@pytest.mark.parametrize("project_scoped", [False, True])
async def test_latest_revision_requires_its_own_provenance(task_continuation_db, project_scoped):
    factory = task_continuation_db
    parent, producer, _sibling, _observer = await _work(factory, "managed")
    project_id = await _worker_project(factory) if project_scoped else None
    artifacts, deliverables = await _outputs(factory, producer, project_id=project_id)
    async with factory() as db:
        unrelated = await create_session(db, "conv-observer", OWNER, "agent-observer")
        # Model a same-owner revision whose producer is outside the parent tree.
        # The first revision's output grant cannot authorize a different revision.
        for model, ref in ((ArtifactRecordRow, artifacts[-1]), (DeliverableRow, deliverables[-1])):
            row = await db.get(model, ref)
            row.session_id = unrelated.session_id
            row.conversation_id = unrelated.conversation_id
            row.owner_agent_id = unrelated.agent_id
        await db.execute(
            delete(ArtifactGrantRow).where(
                ArtifactGrantRow.content_ref.in_([artifacts[-1], deliverables[-1]])
            )
        )
        await db.commit()
    for revisions in (artifacts, deliverables):
        await _assert_surfaces(factory, parent, revisions[0], allowed=True)
        result = await _tool(
            factory, parent, "artifact_get_content", {"artifact_id": revisions[0], "latest": True}
        )
        assert result.is_error
        assert result.output == f"Artifact not found: {revisions[0]}"
        assert not result.metadata
        await _assert_surfaces(factory, parent, revisions[-1], allowed=False)


async def test_descendant_output_grants_do_not_bypass_project_scope(task_continuation_db):
    factory = task_continuation_db
    parent, producer, _sibling, _observer = await _work(factory, "managed")
    artifacts, deliverables = await _outputs(factory, producer)
    async with factory() as db:
        for project_id in ("project-controller", "project-worker"):
            db.add(ProjectRow(project_id=project_id, owner_email=OWNER, name=project_id))
        await db.flush()
        for project_id in ("project-controller", "project-worker"):
            db.add(ProjectAgentRow(project_id=project_id, agent_id=parent.agent_id, added_by=OWNER))
        conversation = await db.get(Conversation, parent.conversation_id)
        conversation.project_id = "project-controller"
        for model, ref in ((ArtifactRecordRow, artifacts[-1]), (DeliverableRow, deliverables[-1])):
            row = await db.get(model, ref)
            row.project_id = "project-worker"
        await db.commit()

    # The user owns both projects and the parent agent is assigned to both,
    # but work in one project's context cannot read the other project's output.
    for ref in (artifacts[-1], deliverables[-1]):
        await _assert_surfaces(factory, parent, ref, allowed=False)
    async with factory() as db:
        conversation = await db.get(Conversation, parent.conversation_id)
        conversation.project_id = "project-worker"
        await db.commit()
    for ref in (artifacts[-1], deliverables[-1]):
        await _assert_surfaces(factory, parent, ref, allowed=True)
    async with factory() as db:
        await db.execute(
            delete(ProjectAgentRow).where(
                ProjectAgentRow.project_id == "project-worker",
                ProjectAgentRow.agent_id == parent.agent_id,
            )
        )
        await db.commit()
    # Removing assignment must not remove access to specific retained outputs.
    for ref in (artifacts[-1], deliverables[-1]):
        await _assert_surfaces(factory, parent, ref, allowed=True)
    async with factory() as db:
        project = await db.get(ProjectRow, "project-worker")
        project.status = "archived"
        await db.commit()
    for ref in (artifacts[-1], deliverables[-1]):
        await _assert_surfaces(factory, parent, ref, allowed=False)


@pytest.mark.parametrize(
    ("grant_kind", "document_access"),
    [
        pytest.param("explicit", True, id="explicit"),
        pytest.param("input", False, id="input"),
        pytest.param("knowledgebase", False, id="knowledgebase"),
    ],
)
async def test_non_output_grants_do_not_grant_unassigned_project_access(
    task_continuation_db, grant_kind, document_access
):
    factory = task_continuation_db
    parent, producer, _sibling, _observer = await _work(factory, "managed")
    project_id = await _worker_project(factory)
    async with factory() as db:
        await create_conversation(
            db, OWNER, producer.agent_id, "agent_direct", conversation_id="conv-private-worker"
        )
        unrelated = await create_session(db, "conv-private-worker", OWNER, producer.agent_id)
        await db.commit()
    artifacts, deliverables = await _outputs(factory, unrelated, project_id=project_id)
    refs = [artifacts[-1], deliverables[-1]]
    async with factory() as db:
        if grant_kind == "knowledgebase":
            kb = KnowledgebaseRow(
                knowledgebase_id="kb-worker", owner_email=OWNER, name="Worker knowledge"
            )
            db.add(kb)
            await db.flush()
            db.add(
                KnowledgebaseArtifactRow(
                    kb_artifact_id=f"kba_{uuid.uuid4().hex}",
                    knowledgebase_id=kb.knowledgebase_id,
                    artifact_id=artifacts[-1],
                    status="ready",
                )
            )
            reader = await db.get(Agent, parent.agent_id)
            reader.permissions = {"allowed_knowledgebases": [kb.knowledgebase_id]}
            refs = [artifacts[-1]]
        else:
            for ref in refs:
                db.add(
                    ArtifactGrantRow(
                        owner_email=OWNER,
                        content_ref=ref,
                        agent_id=parent.agent_id,
                        source=grant_kind,
                    )
                )
        await db.commit()
    for ref in refs:
        await _assert_surfaces(factory, parent, ref, allowed=document_access)
    ungranted_artifacts, ungranted_deliverables = await _outputs(
        factory, unrelated, project_id=project_id
    )
    for ref in (ungranted_artifacts[-1], ungranted_deliverables[-1]):
        await _assert_surfaces(factory, parent, ref, allowed=False)


async def test_retained_project_outputs_require_current_human_access(task_continuation_db):
    factory = task_continuation_db
    parent, producer, _sibling, _observer = await _work(factory, "managed")
    project_owner = "project-owner@example.com"
    async with factory() as db:
        await create_user(db, project_owner, "Project owner", password_hash="test-hash")
        await db.commit()
    project_id = await _worker_project(factory, owner_email=project_owner)
    async with factory() as db:
        grant = ProjectGrantRow(
            grant_id=f"pgrant_{uuid.uuid4().hex}",
            project_id=project_id,
            grantee_user_email=OWNER,
            permission="member",
            granted_by=project_owner,
        )
        db.add(grant)
        await db.commit()
    artifacts, deliverables = await _outputs(factory, producer, project_id=project_id)
    async with factory() as db:
        grant = await db.get(ProjectGrantRow, grant.grant_id)
        grant.permission = "viewer"
        await db.commit()
    for ref in (artifacts[-1], deliverables[-1]):
        await _assert_surfaces(factory, parent, ref, allowed=True)
    async with factory() as db:
        grant = await db.get(ProjectGrantRow, grant.grant_id)
        grant.revoked_at = datetime.now(UTC)
        await db.commit()
    for ref in (artifacts[-1], deliverables[-1]):
        await _assert_surfaces(factory, parent, ref, allowed=False)


async def test_default_project_publication_survives_provenance_purge(task_continuation_db):
    factory = task_continuation_db
    parent, producer, _sibling, _observer = await _work(factory, "managed")
    project_id = await _worker_project(factory)
    published = await _tool(
        factory, producer, "artifact_write", {"filename": "result.txt", "content": "Child result"}
    )
    assert not published.is_error, published.output
    assert published.metadata["project_id"] == project_id
    ref = published.metadata["artifact_id"]
    async with factory() as db:
        report = await create_deliverable(
            db,
            conversation_id=producer.conversation_id,
            session_id=producer.session_id,
            content="Child report",
            project_id=project_id,
            artifact_store=factory.artifact_store,
        )
        await create_conversation(
            db, OWNER, producer.agent_id, "agent_direct", conversation_id="conv-unrelated-worker"
        )
        unrelated = await create_session(db, "conv-unrelated-worker", OWNER, producer.agent_id)
        await db.execute(delete(ManagedConversationLink))
        await db.execute(delete(Session).where(Session.session_id == producer.session_id))
        await db.commit()
    unrelated_result = await _tool(
        factory, unrelated, "artifact_write", {"filename": "private.txt", "content": "Unrelated"}
    )
    assert not unrelated_result.is_error, unrelated_result.output
    unrelated_ref = unrelated_result.metadata["artifact_id"]
    for output in (ref, report.deliverable_id):
        await _assert_surfaces(factory, parent, output, allowed=True)
    await _assert_surfaces(factory, parent, unrelated_ref, allowed=False)
    listed = await _tool(factory, parent, "artifact_list_recent", {})
    assert not listed.is_error, listed.output
    listed_ids = {item["artifact_id"] for item in listed.metadata["items"]}
    assert ref in listed_ids
    assert unrelated_ref not in listed_ids


async def test_output_read_access_does_not_authorize_project_revisions(task_continuation_db):
    from cognis.core.content_refs import authorize_supersedes_target
    from cognis.models.deliverable import DeliverableRevisionError

    factory = task_continuation_db
    parent, producer, _sibling, _observer = await _work(factory, "managed")
    project_id = await _worker_project(factory)
    artifacts, deliverables = await _outputs(factory, producer, project_id=project_id)
    for ref in (artifacts[-1], deliverables[-1]):
        await _assert_surfaces(factory, parent, ref, allowed=True)
    revised = await _tool(
        factory,
        parent,
        "artifact_write",
        {"filename": "result.txt", "content": "Not authorized", "supersedes": artifacts[-1]},
    )
    assert revised.is_error
    assert revised.metadata["reason"] == "supersedes_not_found"
    async with factory() as db:
        with pytest.raises(DeliverableRevisionError, match="not found or is not accessible"):
            await authorize_supersedes_target(
                db,
                deliverables[-1],
                user_email=OWNER,
                accessor_conversation_id=parent.conversation_id,
                accessor_agent_id=parent.agent_id,
                accessor_session_id=parent.session_id,
            )
