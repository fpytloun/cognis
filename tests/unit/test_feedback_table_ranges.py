from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cognis.core.feedback.primitives import Actor, FeedbackError, digest, encode
from cognis.core.feedback.rich_tables import table_membership, validate_table_range
from cognis.core.feedback.submissions import FrozenReview
from cognis.models import feedback as dto
from cognis.store import feedback_models as rows
from cognis.store.queries import create_deliverable
from tests.unit.test_feedback_runtime import OTHER, Harness
from tests.unit.test_feedback_runtime import harness as harness

TABLE = {
    "id": "release-table",
    "type": "table",
    "columns": ["name", "count"],
    "rows": [["Zulu", 3], {"values": ["Alpha", 4]}, {"name": "Beta", "count": 5}],
}


def locator(**changes: Any) -> dto.RichLocator:
    selection = {
        "representation": "rich-table-v1",
        "scope": "cells",
        "rows": [0, 2],
        "column_start": 0,
        "column_end": 2,
    }
    selection.update(changes)
    return dto.RichLocator(
        kind="rich",
        block_id="release-table",
        identity="semantic",
        field_path=[],
        table_range=dto.RichTableRange.model_validate(selection),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"rows": []},
        {"rows": [-1]},
        {"rows": [True]},
        {"rows": ["1"]},
        {"rows": [1.0]},
        {"rows": [2, 0]},
        {"rows": [0, 0]},
        {"rows": [1000]},
        {"column_start": -1},
        {"column_end": 0},
        {"column_start": 2, "column_end": 1},
        {"column_end": 129},
        {"rows": list(range(33)), "column_end": 128},
        {"scope": "table"},
        {"field_path": ["rows"]},
    ],
)
def test_rejects_invalid_or_oversized_range_before_product_expansion(changes: dict) -> None:
    with pytest.raises(ValidationError):
        locator(**changes)


@pytest.mark.parametrize(
    "mixed",
    [
        {"field_path": ["rows", "0"]},
        {"item_key": "row-key"},
        {
            "range": {
                "representation": "feedback-text-v1",
                "text_digest": "a" * 64,
                "start": 0,
                "end": 1,
                "exact": "A",
            }
        },
    ],
)
def test_rejects_mixed_structural_and_text_field_targets(mixed: dict) -> None:
    with pytest.raises(ValidationError):
        dto.RichLocator.model_validate({**locator().model_dump(), **mixed})


def test_legacy_locator_anchor_request_and_manifest_bytes_are_unchanged() -> None:
    legacy = {
        "kind": "rich",
        "block_id": "release-table",
        "identity": "semantic",
        "item_key": None,
        "field_path": ["rows", "0"],
        "range": None,
    }
    anchor = {
        "schema_version": 1,
        "source_token": "previously-issued-source-token",
        "content_digest": "a" * 64,
        "locator": legacy,
    }
    request = {
        "creation_key": "persisted-creation-key",
        "context": {"kind": "standalone"},
        "anchor": anchor,
        "body": "Existing review",
        "audience": "internal",
    }
    for model, value in (
        (dto.RichLocator, legacy),
        (dto.AnchorInput, anchor),
        (dto.ThreadCreateRequest, request),
    ):
        assert encode(model.model_validate(value).model_dump(mode="json")) == encode(value)
        assert digest(encode(model.model_validate(value).model_dump(mode="json"))) == digest(
            encode(value)
        )
    assert dto.RichLocator.model_validate({**legacy, "table_range": None}).model_dump() == legacy
    # A typed manifest embeds the same anchor; adding a default must not change its hash.
    saved_anchor = {
        "anchor_id": "persisted-anchor-id",
        "source": {"kind": "document", "content_ref": "persisted-document-ref"},
        "content_digest": "a" * 64,
        "schema_version": 1,
        "locator": legacy,
    }
    assert encode(dto.FeedbackAnchor.model_validate(saved_anchor).model_dump()) == encode(
        saved_anchor
    )


def test_normalized_membership_preserves_sorted_nonconsecutive_source_rows() -> None:
    assert table_membership(TABLE) == (
        ["name", "count"],
        {0: {"name", "count"}, 1: {"name", "count", "values"}, 2: {"name", "count"}},
    )
    validate_table_range({"blocks": [TABLE]}, locator())
    validate_table_range({"blocks": [TABLE]}, locator(scope="rows"))
    validate_table_range(
        {"blocks": [TABLE]}, locator(scope="columns", rows=[0, 1, 2], column_start=1)
    )
    for type_ in ("comparison_matrix", "decision_matrix"):
        validate_table_range({"blocks": [{**TABLE, "type": type_}]}, locator())
    validate_table_range({"blocks": [{"type": "section", "children": [TABLE]}]}, locator())
    validate_table_range({"blocks": [{**TABLE, "rows": None, "data": TABLE["rows"]}]}, locator())
    assert table_membership({"type": "table", "rows": [{"10": 1, "2": 2, "a": 3}]})[0] == [
        "2",
        "10",
        "a",
    ]


@pytest.mark.parametrize(
    "block,selection",
    [
        ({**TABLE, "columns": ["name", "name"]}, locator()),
        ({**TABLE, "columns": [{"key": 4}]}, locator()),
        ({**TABLE, "rows": [["A"]]}, locator(rows=[0])),
        ({**TABLE, "rows": [{"name": "A"}]}, locator(rows=[0])),
        ({**TABLE, "rows": [None, ["A", 1]]}, locator()),
        (TABLE, locator(rows=[3])),
        (TABLE, locator(column_end=3)),
        (TABLE, locator(scope="rows", column_start=1)),
        (TABLE, locator(scope="columns")),
        ({**TABLE, "type": "chart"}, locator()),
    ],
)
def test_missing_ragged_ambiguous_and_incomplete_scope_fail_closed(
    block: dict, selection: dto.RichLocator
) -> None:
    with pytest.raises(FeedbackError):
        validate_table_range({"blocks": [block]}, selection)


def test_not_arbitrary_object_paths_or_duplicate_semantic_ids() -> None:
    with pytest.raises(FeedbackError, match="ambiguous"):
        validate_table_range({"blocks": [TABLE, TABLE]}, locator())
    with pytest.raises(FeedbackError, match="rendered"):
        validate_table_range({"blocks": [], "assets": [TABLE]}, locator())
    local = locator().model_copy(update={"identity": "revision_local", "block_id": "/blocks/0"})
    validate_table_range({"blocks": [TABLE]}, local)


async def rich_source(
    h: Harness, supersedes: str | None = None, *, payload: dict | None = None
) -> dto.ResolvedFeedbackSource:
    async with h.factory() as db:
        document = await create_deliverable(
            db,
            conversation_id=h.conversation_id,
            content="Release data",
            format="rich",
            rich=payload if payload is not None else {"blocks": [TABLE]},
            artifact_store=h.store,
            supersedes_deliverable_id=supersedes,
        )
        await db.commit()
    return await h.service.sources.resolve(
        h.actor,
        dto.ResolveSourceRequest(
            source=dto.DocumentSourceRef(kind="document", content_ref=document.deliverable_id),
            context=dto.StandaloneFeedbackContext(kind="standalone"),
        ),
    )


def request(source: dto.ResolvedFeedbackSource) -> dto.ThreadCreateRequest:
    return dto.ThreadCreateRequest(
        creation_key=uuid4().hex,
        context=source.context,
        body="Review these exact cells",
        anchor=dto.AnchorInput(
            source_token=source.source_token,
            content_digest=source.content_digest,
            locator=locator(),
        ),
    )


@pytest.mark.asyncio
async def test_roundtrip_manifest_and_conservative_continuity(harness: Harness) -> None:
    h = harness
    source = await rich_source(h)
    created = await h.service.threads.create(h.actor, request(source))
    assert (await h.service.threads.thread(h.actor, created.thread_id)).anchor.locator == locator()
    preflight = await h.service.submissions.preflight(
        h.actor,
        dto.FeedbackPreflightRequest(
            context=source.context,
            selection=[
                dto.SelectedVersion(
                    entry_version_id=created.entries[0].entry_version_id, expected_entry_version=1
                )
            ],
            destination=dto.ConversationDestination(
                kind="producer_conversation", conversation_id=h.conversation_id
            ),
            update_mode="answer_only",
        ),
    )
    async with h.factory() as db:
        row = await db.get(rows.FeedbackPreflightRow, preflight.preflight_id)
        assert row is not None
        payload_id = row.payload_id
    frozen = FrozenReview.model_validate(await h.service.payloads.get(payload_id))
    assert frozen.manifest.entries[0].anchor.locator == locator()
    assert digest(encode(frozen.manifest.model_dump(mode="json"))) == preflight.manifest_hash
    legacy_manifest = frozen.manifest.model_dump(mode="json")
    legacy_locator = legacy_manifest["entries"][0]["anchor"]["locator"]
    del legacy_locator["table_range"]
    legacy_locator["field_path"] = ["rows", "0"]
    assert encode(
        dto.SubmissionManifest.model_validate(legacy_manifest).model_dump(mode="json")
    ) == encode(legacy_manifest)
    draft_key = uuid4().hex
    await h.service.threads.save_draft(
        h.actor,
        draft_key,
        dto.DraftRequest(
            expected_version=0,
            context=source.context,
            source=source.source,
            anchor=request(source).anchor,
            body="Recover this exact target",
        ),
    )
    draft = await h.service.threads.draft(h.actor, draft_key)
    assert draft.draft.anchor is not None
    assert draft.draft.anchor.locator == locator()
    target = await rich_source(h, source.source.content_ref)
    exact = await h.service.continuity.mapping(h.actor, created.anchor.anchor_id, source.subject_id)
    assert exact.category == "exact"
    mapped = await h.service.continuity.mapping(
        h.actor, created.anchor.anchor_id, target.subject_id
    )
    assert mapped.category == "not_located"


@pytest.mark.asyncio
async def test_range_save_preserves_actor_context_digest_and_guest_fences(harness: Harness) -> None:
    h = harness
    source = await rich_source(h)
    value = request(source)
    for actor, changed in (
        (Actor.user(OTHER), value),
        (
            h.actor,
            value.model_copy(
                update={
                    "context": dto.ChatFeedbackContext(
                        kind="chat", conversation_id=h.conversation_id
                    )
                }
            ),
        ),
        (
            h.actor,
            value.model_copy(
                update={"anchor": value.anchor.model_copy(update={"content_digest": "b" * 64})}
            ),
        ),
    ):
        with pytest.raises(FeedbackError):
            await h.service.threads.create(actor, changed)
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=source.source,
            expected_content_digest=source.content_digest,
            allow_comments=True,
            discussion="own_only",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    exchanged = await h.service.public.exchange(share.share_token)
    guest = await h.service.public.guest(
        exchanged.capability, dto.GuestSessionRequest(display_name="Reviewer")
    )
    actor, _ = await h.service.public.actor(exchanged.capability, guest.guest_capability)
    public_source = await h.service.sources.resolve(
        actor, dto.ResolveSourceRequest(source=source.source, context=source.context)
    )
    public_request = request(public_source)
    saved = await h.service.threads.create(actor, public_request)
    assert saved.entries[0].author.kind == "guest_unverified"
    assert saved.anchor.locator == locator()
    await h.service.public.update_share(
        h.actor, share.share_id, dto.ShareUpdateRequest(expected_version=1, allow_comments=False)
    )
    with pytest.raises(FeedbackError):
        await h.service.threads.create(actor, request(public_source))


def test_empty_columns_numeric_object_keys_and_table_dimension_budgets() -> None:
    with pytest.raises(FeedbackError):
        validate_table_range({"blocks": [{**TABLE, "columns": []}]}, locator())
    for changes in ({"rows": [["A", 1]] * 1001}, {"columns": [str(i) for i in range(129)]}):
        with pytest.raises(FeedbackError):
            validate_table_range({"blocks": [{**TABLE, **changes}]}, locator())
    from cognis.core.feedback.rich_tables import object_keys

    assert object_keys({"10": "ten", "2": "two", "1" * 5000: "large", "name": "last"}) == [
        "2",
        "10",
        "1" * 5000,
        "name",
    ]


@pytest.mark.parametrize("parent", ["section", "grid", "modal"])
def test_nested_authored_blocks_remain_supported(parent: str) -> None:
    validate_table_range({"blocks": [{"type": parent, "blocks": [TABLE]}]}, locator())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent,key", [("gallery", "blocks"), ("gallery", "items"), ("divider", "children")]
)
async def test_save_table_ranges_in_all_rendered_container_positions(
    harness: Harness, parent: str, key: str
) -> None:
    payload = {"blocks": [{"type": parent, key: [TABLE]}]}
    validate_table_range(payload, locator())
    source = await rich_source(harness, payload=payload)
    thread = await harness.service.threads.create(harness.actor, request(source))
    assert thread.anchor.locator == locator()
    with pytest.raises(FeedbackError, match="rendered"):
        validate_table_range({"blocks": [{"type": parent, "metadata": TABLE}]}, locator())
