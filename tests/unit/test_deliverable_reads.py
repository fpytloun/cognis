from __future__ import annotations

import pytest

from cognis.core.deliverable_reads import readable_deliverables
from cognis.store.models import DeliverableRow
from cognis.store.queries import get_deliverable_lineage_summaries, update_deliverable_status
from tests.unit.test_artifact_virtual_deliverable_refs import _seed_managed_deliverables
from tests.unit.test_deliverable_revisions import _chat_conversation, _write_chat
from tests.unit.test_managed_descendant_deliverable_security import _DENIED_CASES, _prepare_case


@pytest.mark.parametrize(
    "case",
    [
        case
        for case in _DENIED_CASES
        # HTTP derives the agent from the verified accessor, not caller input.
        if case.name not in {"mismatched_controller_agent", "mismatched_controller_conversation"}
    ],
    ids=lambda case: case.name,
)
async def test_grouped_read_preserves_managed_denials(task_continuation_db, case):
    await _prepare_case(task_continuation_db, case)
    async with task_continuation_db() as session:
        assert (
            await readable_deliverables(
                session,
                [case.deliverable_id],
                case.user_email,
                accessor_conversation_id=case.conversation_id,
            )
            == {}
        )


async def test_grouped_read_requires_managed_accessor_and_does_not_leak_unknown_ids(
    task_continuation_db,
):
    await _seed_managed_deliverables(task_continuation_db)
    async with task_continuation_db() as session:
        assert (
            await readable_deliverables(
                session, ["dlv_child"], "owner@example.com", allow_unscoped_ref=True
            )
            == {}
        )
        readable = await readable_deliverables(
            session,
            ["dlv_child", "dlv_child", "dlv_grandchild", "dlv_other"],
            "owner@example.com",
            accessor_conversation_id="conv-controller",
        )
        assert set(readable) == {"dlv_child", "dlv_grandchild"}


async def test_batch_lineages_keep_distinct_heads_counts_and_legacy_fallback(task_continuation_db):
    factory = task_continuation_db
    await _chat_conversation(factory)
    a1 = await _write_chat(factory, content="A1")
    a2 = await _write_chat(factory, content="A2", supersedes=a1.deliverable_id)
    a3 = await _write_chat(factory, content="A3", supersedes=a2.deliverable_id)
    b1 = await _write_chat(factory, content="B1")
    b2 = await _write_chat(factory, content="B2", supersedes=b1.deliverable_id)
    legacy = await _write_chat(factory, content="Legacy")
    no_live_head = await _write_chat(factory, content="Rejected")
    async with factory() as session:
        await update_deliverable_status(session, a3.deliverable_id, status="rejected")
        await update_deliverable_status(session, no_live_head.deliverable_id, status="rejected")
        row = await session.get(DeliverableRow, legacy.deliverable_id)
        row.lineage_id = None
        await session.commit()
    ids = [row.deliverable_id for row in (a1, a2, a3, b1, b2, legacy, no_live_head)]
    async with factory() as session:
        rows = [await session.get(DeliverableRow, id) for id in ids]
        summaries = await get_deliverable_lineage_summaries(session, rows)
    for revision in (a1, a2, a3):
        summary = summaries[revision.deliverable_id]
        assert summary.lineage_id == a1.deliverable_id
        assert summary.latest_deliverable_id == a2.deliverable_id
        assert (summary.latest_revision, summary.revision_count) == (2, 2)
    for revision in (b1, b2):
        summary = summaries[revision.deliverable_id]
        assert summary.lineage_id == b1.deliverable_id
        assert summary.latest_deliverable_id == b2.deliverable_id
        assert (summary.latest_revision, summary.revision_count) == (2, 2)
    for revision in (legacy, no_live_head):
        summary = summaries[revision.deliverable_id]
        assert summary.lineage_id == summary.latest_deliverable_id == revision.deliverable_id
        assert (summary.latest_revision, summary.revision_count) == (1, 1)
