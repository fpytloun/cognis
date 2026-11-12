"""Agent identity, KB grants and work lineage must agree at every content boundary."""

from __future__ import annotations

import uuid

import pytest

from cognis.core.artifact_access import artifact_authorized_for_conversation, content_read_predicate
from cognis.core.artifact_sharing import set_artifact_share
from cognis.store.models import Agent, KnowledgebaseArtifactRow, KnowledgebaseRow, Session
from cognis.store.queries import (
    create_artifact_record,
    create_conversation,
    search_artifact_records,
)

OWNER = "owner@example.com"


async def test_shared_agent_reads_only_acting_users_content(task_continuation_db):
    from datetime import UTC, datetime

    from cognis.store.models import AgentGrantRow
    from cognis.store.queries import create_user, mark_artifacts_attached

    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        agent_owner = "agent-owner@example.com"
        await create_user(db, agent_owner, "Agent owner", password_hash="test-hash")
        agent = await db.get(Agent, "agent-owner")
        agent.owner_email = agent_owner
        grant = AgentGrantRow(
            grant_id=f"grant_{uuid.uuid4().hex}",
            agent_id=agent.agent_id,
            grantee_user_email=OWNER,
            granted_by=agent_owner,
        )
        db.add(grant)
        await db.flush()
        uploaded = await create_artifact_record(
            db,
            artifact_id=f"att_{uuid.uuid4().hex}",
            namespace="attachments",
            object_id=uuid.uuid4().hex,
            filename="upload.jpeg",
            owner_email=OWNER,
            purpose="chat_input",
            kind="image",
            mime_type="image/jpeg",
            size_bytes=12,
            status="temporary",
        )
        assert (
            await mark_artifacts_attached(
                db, [uploaded.artifact_id], owner_email=OWNER, conversation_id="conv-author"
            )
            == 1
        )
        await db.flush()
        assert await _allowed(
            db, uploaded.artifact_id, agent=agent.agent_id, conversation="conv-author"
        )
        assert await _allowed(db, ref, agent=agent.agent_id, conversation="conv-author-second")
        predicate = await content_read_predicate(
            db, owner_email=OWNER, agent_id=agent.agent_id, conversation_id="conv-author"
        )
        assert await search_artifact_records(
            db, owner_email=OWNER, query="upload", access_predicate=predicate
        )
        # Sharing an agent must not bridge either users or unrelated agents.
        uploaded.owner_email = agent_owner
        await db.flush()
        assert not await _allowed(
            db, uploaded.artifact_id, agent=agent.agent_id, conversation="conv-author"
        )
        uploaded.owner_email = OWNER
        assert not await _allowed(db, uploaded.artifact_id)
        assert not await _allowed(
            db, uploaded.artifact_id, agent=agent.agent_id, conversation="conv-reader"
        )
        grant.revoked_at = datetime.now(UTC)
        await db.flush()
        assert not await _allowed(db, ref, agent=agent.agent_id, conversation="conv-author")
        predicate = await content_read_predicate(
            db, owner_email=OWNER, agent_id=agent.agent_id, conversation_id="conv-author"
        )
        assert not await search_artifact_records(
            db, owner_email=OWNER, query="upload", access_predicate=predicate
        )


async def _seed(factory):
    async with factory() as db:
        db.add(Agent(agent_id="agent-reader", owner_email=OWNER, name="Reader"))
        await create_conversation(
            db, OWNER, "agent-owner", "agent_direct", conversation_id="conv-author"
        )
        await create_conversation(
            db, OWNER, "agent-owner", "agent_direct", conversation_id="conv-author-second"
        )
        await create_conversation(
            db, OWNER, "agent-reader", "agent_direct", conversation_id="conv-reader"
        )
        row = await create_artifact_record(
            db,
            artifact_id=f"att_{uuid.uuid4().hex}",
            namespace="attachments",
            object_id=uuid.uuid4().hex,
            filename="photo.jpeg",
            owner_email=OWNER,
            conversation_id="conv-author",
            purpose="chat_input",
            kind="image",
            mime_type="image/jpeg",
            size_bytes=12,
            status="attached",
        )
        await db.commit()
        return row.artifact_id


async def _allowed(db, ref, *, agent="agent-reader", conversation="conv-reader", session_id=None):
    from cognis.store.queries import get_artifact_record

    return await artifact_authorized_for_conversation(
        db,
        artifact=await get_artifact_record(db, ref),
        owner_email=OWNER,
        conversation_id=conversation,
        agent_id=agent,
        session_id=session_id,
    )


async def test_agent_scope_and_explicit_grant_are_consistent(task_continuation_db):
    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        assert await _allowed(db, ref, agent="agent-owner", conversation="conv-author-second")
        assert not await _allowed(db, ref)
        predicate = await content_read_predicate(
            db, owner_email=OWNER, agent_id="agent-reader", conversation_id="conv-reader"
        )
        assert not await search_artifact_records(
            db, owner_email=OWNER, query="photo", limit=1, access_predicate=predicate
        )
        await set_artifact_share(
            db,
            owner_email=OWNER,
            owner_agent_id="agent-owner",
            content_ref=ref,
            grantee_agent_id="agent-reader",
        )
        await db.flush()
        assert await _allowed(db, ref)
        with pytest.raises(ValueError):
            await set_artifact_share(
                db,
                owner_email=OWNER,
                owner_agent_id="agent-reader",
                content_ref=ref,
                grantee_agent_id="agent-other",
            )
        await set_artifact_share(
            db,
            owner_email=OWNER,
            owner_agent_id="agent-owner",
            content_ref=ref,
            grantee_agent_id="agent-reader",
            revoke=True,
        )
        assert not await _allowed(db, ref)


async def test_exact_direct_and_granted_reads_do_not_construct_work_graph(
    task_continuation_db, monkeypatch
):
    from cognis.core import artifact_access

    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        await set_artifact_share(
            db,
            owner_email=OWNER,
            owner_agent_id="agent-owner",
            content_ref=ref,
            grantee_agent_id="agent-reader",
        )
        await db.commit()

    def forbidden_graph(**_kwargs):
        raise AssertionError("direct authorization constructed the recursive work graph")

    monkeypatch.setattr(artifact_access, "_work_nodes", forbidden_graph)
    async with factory() as db:
        assert await _allowed(db, ref, agent="agent-owner", conversation="conv-author-second")
        assert await _allowed(db, ref)


@pytest.mark.parametrize("agent", ["agent-owner", "agent-reader"])
async def test_batched_exact_authorization_matches_canonical_predicate(task_continuation_db, agent):
    from datetime import UTC, datetime, timedelta

    import sqlalchemy as sa

    from cognis.core.artifact_access import readable_artifact_ids
    from cognis.store.models import ArtifactGrantRow, ArtifactRecordRow

    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        row = await db.get(ArtifactRecordRow, ref)
        # Legacy content exercises provenance rather than the direct-owner branch.
        row.owner_agent_id = None
        await db.flush()
        await db.execute(sa.delete(ArtifactGrantRow).where(ArtifactGrantRow.content_ref == ref))
        conversation = "conv-author" if agent == "agent-owner" else "conv-reader"
        predicate = await content_read_predicate(
            db, owner_email=OWNER, agent_id=agent, conversation_id=conversation
        )
        expected = set(
            (
                await db.scalars(
                    sa.select(ArtifactRecordRow.artifact_id).where(
                        ArtifactRecordRow.artifact_id == ref, predicate
                    )
                )
            ).all()
        )
        assert (
            await readable_artifact_ids(
                db, [ref, ref], owner_email=OWNER, agent_id=agent, conversation_id=conversation
            )
            == expected
        )
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.flush()
        assert not await readable_artifact_ids(
            db, [ref], owner_email=OWNER, agent_id=agent, conversation_id=conversation
        )


async def test_kb_membership_grants_raw_content_and_revokes(task_continuation_db):
    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        kb_id = f"kb_{uuid.uuid4().hex}"
        db.add(KnowledgebaseRow(knowledgebase_id=kb_id, owner_email=OWNER, name="Pictures"))
        await db.flush()
        membership = KnowledgebaseArtifactRow(
            kb_artifact_id=f"kba_{uuid.uuid4().hex}",
            knowledgebase_id=kb_id,
            artifact_id=ref,
            status="ready",
        )
        db.add(membership)
        reader = await db.get(Agent, "agent-reader")
        reader.permissions = {"allowed_knowledgebases": [kb_id]}
        await db.flush()
        assert await _allowed(db, ref)
        from cognis.store.queries import detach_knowledgebase_artifact

        await detach_knowledgebase_artifact(
            db, owner_email=OWNER, knowledgebase_id=kb_id, artifact_id=ref
        )
        await db.flush()
        assert not await _allowed(db, ref)


async def test_specialist_sessions_do_not_share_each_others_outputs(task_continuation_db):
    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        first = Session(
            session_id=f"sess_{uuid.uuid4().hex}",
            conversation_id="conv-reader",
            user_email=OWNER,
            agent_id="system:explore",
        )
        second = Session(
            session_id=f"sess_{uuid.uuid4().hex}",
            conversation_id="conv-reader",
            user_email=OWNER,
            agent_id="system:explore",
        )
        db.add_all([first, second])
        await db.flush()
        output = await create_artifact_record(
            db,
            artifact_id=f"art_{uuid.uuid4().hex}",
            namespace="artifacts",
            object_id=uuid.uuid4().hex,
            filename="result.txt",
            owner_email=OWNER,
            conversation_id="conv-reader",
            session_id=first.session_id,
            purpose="artifact_publish",
            kind="file",
            mime_type="text/plain",
            size_bytes=1,
            status="attached",
        )
        assert await _allowed(db, output.artifact_id)
        assert await _allowed(
            db, output.artifact_id, agent="system:explore", session_id=first.session_id
        )
        assert not await _allowed(
            db, output.artifact_id, agent="system:explore", session_id=second.session_id
        )
        assert not await _allowed(db, ref, agent="system:explore", session_id=first.session_id)


async def test_specialist_input_compaction_and_retry_are_scoped(task_continuation_db):
    from cognis.core.artifact_sharing import grant_referenced_inputs
    from cognis.store.queries import create_session

    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        parent = await create_session(db, "conv-author", OWNER, "agent-owner")
        first = await create_session(
            db,
            "conv-author",
            OWNER,
            "system:explore",
            parent_session_id=parent.session_id,
            activity_scope_id=parent.activity_scope_id,
        )
        sibling = await create_session(
            db,
            "conv-author",
            OWNER,
            "system:explore",
            parent_session_id=parent.session_id,
            activity_scope_id=parent.activity_scope_id,
        )
        await grant_referenced_inputs(
            db,
            owner_email=OWNER,
            source_agent_id="agent-owner",
            source_session_id=parent.session_id,
            source_conversation_id="conv-author",
            target_session_id=first.session_id,
            text=f"Inspect {ref}",
        )
        rotated = await create_session(
            db,
            "conv-author",
            OWNER,
            "system:explore",
            previous_session_id=first.session_id,
            parent_session_id=parent.session_id,
        )
        retry = await create_session(
            db,
            "conv-author",
            OWNER,
            "system:explore",
            source_session_id=rotated.session_id,
            parent_session_id=parent.session_id,
        )
        for child in (first, rotated, retry):
            assert await _allowed(
                db,
                ref,
                agent="system:explore",
                conversation="conv-author",
                session_id=child.session_id,
            )
        assert not await _allowed(
            db,
            ref,
            agent="system:explore",
            conversation="conv-author",
            session_id=sibling.session_id,
        )
        assert not await _allowed(db, ref, agent="system:explore", conversation="conv-author")


@pytest.mark.parametrize("specialist", [False, True])
@pytest.mark.parametrize("revocation", ["assignment", "detach"])
async def test_kb_image_discovery_inference_and_hydration_agree(
    task_continuation_db, specialist, revocation
):
    import io

    from PIL import Image

    from cognis.core.attachment_utils import hydrate_attachment_refs
    from cognis.providers.llm.codex_artifacts import materialize_codex_artifact_images
    from cognis.tools.builtin.artifact_tools import handle_artifact_tool

    factory = task_continuation_db
    ref = await _seed(factory)
    image = io.BytesIO()
    Image.new("RGB", (3, 3), "red").save(image, "JPEG")

    class Store:
        async def async_load(self, *args):
            return image.getvalue(), "image/jpeg"

        async def async_get_public_url(self, *args, **kwargs):
            return "https://cognis.fpy.cz/authorized-image"

    class VisionModel:
        async def get_model_info(self, *args, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

    store = Store()
    attachment = dict(
        artifact_id=ref, filename="photo.jpeg", kind="image", mime_type="image/jpeg", size_bytes=12
    )
    metadata = {"conversation_id": "conv-reader", "agent_id": "agent-reader"}
    async with factory() as db:
        if specialist:
            from cognis.store.queries import create_session

            worker = await create_session(db, "conv-reader", OWNER, "system:explore")
            metadata.update(agent_id=worker.agent_id, session_id=worker.session_id)
        kb_id = f"kb_{uuid.uuid4().hex}"
        db.add(KnowledgebaseRow(knowledgebase_id=kb_id, owner_email=OWNER, name="Pictures"))
        await db.flush()
        db.add(
            KnowledgebaseArtifactRow(
                kb_artifact_id=f"kba_{uuid.uuid4().hex}",
                knowledgebase_id=kb_id,
                artifact_id=ref,
                status="ready",
            )
        )
        reader = await db.get(Agent, "agent-reader")
        reader.permissions = {"allowed_knowledgebases": [kb_id]}
        await db.commit()
    for allowed in (True, False):
        search = await handle_artifact_tool(
            "artifact_search",
            {"query": "photo"},
            llm=None,
            artifact_store=store,
            session_factory=factory,
            user_email=OWNER,
            runtime_metadata=metadata,
        )
        assert bool(search.metadata["items"]) == allowed
        for tool in ("artifact_get_metadata", "artifact_get_url"):
            result = await handle_artifact_tool(
                tool,
                {"artifact_id": ref},
                llm=None,
                artifact_store=store,
                session_factory=factory,
                user_email=OWNER,
                runtime_metadata=metadata,
            )
            assert result.is_error != allowed
        result = await handle_artifact_tool(
            "artifact_read",
            {"artifact_id": ref},
            llm=VisionModel(),
            artifact_store=store,
            session_factory=factory,
            user_email=OWNER,
            runtime_metadata=metadata,
            current_model="gpt-4o",
        )
        assert result.is_error != allowed
        assert bool(result.attachments) == allowed
        from cognis.tools.builtin.image import ImageSourceError, _resolve_image_artifact_to_b64

        if allowed:
            assert await _resolve_image_artifact_to_b64(
                ref,
                artifact_store=store,
                session_factory=factory,
                user_email=OWNER,
                argument_name="reference_images",
                runtime_metadata=metadata,
            )
        else:
            with pytest.raises(ImageSourceError):
                await _resolve_image_artifact_to_b64(
                    ref,
                    artifact_store=store,
                    session_factory=factory,
                    user_email=OWNER,
                    argument_name="reference_images",
                    runtime_metadata=metadata,
                )
        hydrated = await hydrate_attachment_refs(
            factory, store, [attachment], owner_email=OWNER, conversation_id="conv-reader"
        )
        assert bool(hydrated) == allowed
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://cognis.fpy.cz/authorized-image",
                            "cognis_artifact": attachment,
                        },
                    }
                ],
            }
        ]
        projected = await materialize_codex_artifact_images(
            messages,
            session_factory=factory,
            artifact_store=store,
            owner_email=OWNER,
            conversation_id="conv-reader",
            agent_id=metadata["agent_id"],
            session_id=metadata.get("session_id"),
        )
        assert (projected[0]["content"][0]["type"] == "image_url") == allowed
        async with factory() as db:
            if revocation == "assignment":
                reader = await db.get(Agent, "agent-reader")
                reader.permissions = {"allowed_knowledgebases": []}
            elif allowed:
                from cognis.store.queries import detach_knowledgebase_artifact

                await detach_knowledgebase_artifact(
                    db, owner_email=OWNER, knowledgebase_id=kb_id, artifact_id=ref
                )
            await db.commit()


async def test_owned_content_survives_source_purge_and_migration_is_idempotent(
    task_continuation_db,
):
    import sqlalchemy as sa

    from cognis.core.content_refs import get_accessible_deliverable_ref
    from cognis.store.artifact_access_schema import ensure_artifact_access_schema
    from cognis.store.models import ArtifactRecordRow, Conversation, DeliverableRow
    from cognis.store.queries import create_deliverable, create_session

    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        source = await create_session(db, "conv-author", OWNER, "agent-owner")
        deliverable = await create_deliverable(
            db,
            step_run_id=None,
            conversation_id="conv-author",
            session_id=source.session_id,
            content="Preserved content",
            published_owner_email=OWNER,
            artifact_store=factory.artifact_store,
        )
        await db.commit()
        dlv = deliverable.deliverable_id
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(ensure_artifact_access_schema)
        await connection.run_sync(ensure_artifact_access_schema)
    async with factory() as db:
        await db.execute(sa.delete(Session).where(Session.conversation_id == "conv-author"))
        await db.execute(
            sa.delete(Conversation).where(Conversation.conversation_id == "conv-author")
        )
        await db.commit()
        assert await _allowed(db, ref, agent="agent-owner", conversation="conv-author-second")
        readable = await get_accessible_deliverable_ref(
            db,
            factory.artifact_store,
            dlv,
            OWNER,
            accessor_agent_id="agent-owner",
            accessor_conversation_id="conv-author-second",
        )
        assert readable is not None
        assert (await db.get(ArtifactRecordRow, ref)).owner_agent_id == "agent-owner"
        assert (await db.get(DeliverableRow, dlv)).owner_agent_id == "agent-owner"


async def test_nested_specialist_output_survives_producer_deletion(task_continuation_db):
    import sqlalchemy as sa

    from cognis.core.content_refs import get_accessible_deliverable_ref
    from cognis.store.queries import create_deliverable, create_session

    factory = task_continuation_db
    await _seed(factory)
    async with factory() as db:
        root = await create_session(db, "conv-author", OWNER, "agent-owner")
        parent = await create_session(
            db, "conv-author", OWNER, "system:explore", parent_session_id=root.session_id
        )
        sibling = await create_session(
            db, "conv-author", OWNER, "system:explore", parent_session_id=root.session_id
        )
        child = await create_session(
            db, "conv-author", OWNER, "system:research", parent_session_id=parent.session_id
        )
        ref = f"art_{uuid.uuid4().hex}"
        output = await create_artifact_record(
            db,
            artifact_id=ref,
            namespace="artifacts",
            object_id=ref,
            filename="result.txt",
            owner_email=OWNER,
            conversation_id="conv-author",
            session_id=child.session_id,
            purpose="artifact_publish",
            kind="file",
            mime_type="text/plain",
            size_bytes=1,
            status="attached",
        )
        dlv = await create_deliverable(
            db,
            conversation_id="conv-author",
            session_id=child.session_id,
            step_run_id=None,
            content="Child result",
            artifact_store=factory.artifact_store,
        )
        assert output.owner_agent_id == "agent-owner"
        await db.execute(sa.delete(Session).where(Session.session_id == child.session_id))
        await db.flush()
        for accessor, permitted in ((parent, True), (sibling, False)):
            assert (
                await _allowed(
                    db,
                    ref,
                    agent=accessor.agent_id,
                    conversation="conv-author",
                    session_id=accessor.session_id,
                )
                == permitted
            )
            readable = await get_accessible_deliverable_ref(
                db,
                factory.artifact_store,
                dlv.deliverable_id,
                OWNER,
                accessor_agent_id=accessor.agent_id,
                accessor_session_id=accessor.session_id,
                accessor_conversation_id="conv-author",
            )
            assert (readable is not None) == permitted


@pytest.mark.parametrize("runtime_ref", [False, True])
async def test_lazy_ref_cannot_bypass_specialist_isolation(task_continuation_db, runtime_ref):
    from types import SimpleNamespace

    from cognis.core.artifact_sharing import grant_referenced_inputs
    from cognis.store.queries import create_session
    from cognis.tools.builtin.artifact_tools import materialize_tool_artifact_ref

    factory = task_continuation_db
    await _seed(factory)
    async with factory() as db:
        parent = await create_session(db, "conv-author", OWNER, "agent-owner")
        producer = await create_session(
            db, "conv-author", OWNER, "system:explore", parent_session_id=parent.session_id
        )
        sibling = await create_session(
            db, "conv-author", OWNER, "system:explore", parent_session_id=parent.session_id
        )
        ref, call = f"art_{uuid.uuid4().hex}", f"call_{uuid.uuid4().hex}"
        await create_artifact_record(
            db,
            artifact_id=ref,
            namespace="tool_outputs",
            object_id=ref,
            filename="result.txt",
            owner_email=OWNER,
            conversation_id="conv-author",
            session_id=producer.session_id,
            purpose="tool_output",
            kind="file",
            mime_type="text/plain",
            size_bytes=10,
            status="attached",
            source_tool_call_id=call,
        )
        await db.commit()

    class OutputStore:
        async def list_anchors(self, call_id):
            assert call_id == call
            return [
                SimpleNamespace(
                    anchor="result:1",
                    artifact_candidate={"source_type": "artifact_id", "artifact_id": ref},
                )
            ]

    lazy = f"tool_artifact:{call}:result:1"
    metadata = {
        "conversation_id": "conv-author",
        "agent_id": sibling.agent_id,
        "session_id": sibling.session_id,
        "tool_output_store": OutputStore(),
    }
    if runtime_ref:
        metadata["authorized_lazy_artifact_refs"] = [lazy]
    for allowed in (False, True):
        result = await materialize_tool_artifact_ref(
            lazy,
            artifact_store=factory.artifact_store,
            session_factory=factory,
            user_email=OWNER,
            runtime_metadata=metadata,
        )
        assert result.is_error != allowed
        async with factory() as db:
            await grant_referenced_inputs(
                db,
                owner_email=OWNER,
                source_agent_id="agent-owner",
                source_session_id=parent.session_id,
                source_conversation_id="conv-author",
                target_session_id=sibling.session_id,
                text=ref,
            )
            await db.commit()


async def test_task_requester_gets_outputs_not_worker_storage(task_continuation_db):
    from cognis.core.content_refs import get_accessible_deliverable_ref
    from cognis.store.queries import (
        create_deliverable,
        create_session,
        create_step_run,
        create_task,
    )

    factory = task_continuation_db
    private = await _seed(factory)
    async with factory() as db:
        requester = await create_session(db, "conv-reader", OWNER, "agent-reader")
        task = await create_task(
            db,
            created_by=OWNER,
            agent_id="agent-owner",
            title="Delegated task",
            created_by_agent_id="agent-reader",
            source_session_id=requester.session_id,
        )
        producer = await create_session(db, "conv-author", OWNER, "system:research")
        step = await create_step_run(
            db,
            task_id=task.task_id,
            step_name="produce",
            step_type="run",
            agent_id="system:research",
        )
        step.session_id = producer.session_id
        step.conversation_id = "conv-author"
        await db.flush()
        ref = f"art_{uuid.uuid4().hex}"
        await create_artifact_record(
            db,
            artifact_id=ref,
            namespace="artifacts",
            object_id=ref,
            filename="result.txt",
            owner_email=OWNER,
            conversation_id="conv-author",
            session_id=producer.session_id,
            purpose="artifact_publish",
            kind="file",
            mime_type="text/plain",
            size_bytes=1,
            status="attached",
        )
        dlv = await create_deliverable(
            db,
            step_run_id=step.step_run_id,
            content="Task output",
            artifact_store=factory.artifact_store,
        )
        assert await _allowed(db, ref)
        assert not await _allowed(db, private)
        assert await get_accessible_deliverable_ref(
            db,
            factory.artifact_store,
            dlv.deliverable_id,
            OWNER,
            accessor_agent_id="agent-reader",
            accessor_conversation_id="conv-reader",
        )


async def test_human_owner_can_share_legacy_artifact_but_reader_cannot_reshare(
    task_continuation_db,
):
    from cognis.store.models import ArtifactRecordRow

    factory = task_continuation_db
    ref = await _seed(factory)
    async with factory() as db:
        row = await db.get(ArtifactRecordRow, ref)
        row.owner_agent_id = None
        row.conversation_id = None
        await db.flush()
        assert not await _allowed(db, ref)
        await set_artifact_share(
            db,
            owner_email=OWNER,
            owner_agent_id=None,
            content_ref=ref,
            grantee_agent_id="agent-reader",
        )
        await db.flush()
        assert await _allowed(db, ref)
        with pytest.raises(ValueError):
            await set_artifact_share(
                db,
                owner_email=OWNER,
                owner_agent_id="agent-reader",
                content_ref=ref,
                grantee_agent_id="agent-owner",
            )
