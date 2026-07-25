from __future__ import annotations

import json
import time
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from PIL import Image
from starlette.testclient import TestClient

from cognis.api.routes import deliverables as deliverable_routes
from cognis.core.deliverable_links import _sign_share_token, signed_deliverable_view_link
from cognis.core.deliverable_media import _image_dimensions
from cognis.models.deliverable import (
    PULSE_DAILY_SKELETON,
    RichPayloadValidationError,
    normalize_rich_payload,
)
from cognis.store.models import Agent
from cognis.store.queries import (
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_managed_conversation_link,
    get_artifact_record,
)
from cognis.tools.builtin.workflow import WRITE_DELIVERABLE_TOOL
from cognis.tools.introspection import validate_available_tool_call_with_context
from cognis.tools.native_validation import (
    NativeValidationContext,
    write_deliverable_validation_state_fingerprint,
)
from tests.unit.test_task_continuation_tools import _seed_managed_rich_deliverable

pytest_plugins = ("tests.unit.test_task_continuation_tools",)


def _png(width: int = 2, height: int = 3) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), color="white").save(output, format="PNG")
    return output.getvalue()


async def _artifact(
    factory,
    *,
    artifact_id: str,
    owner: str,
    conversation_id: str | None,
    content: bytes | None = None,
) -> None:
    content = content or _png()
    await factory.artifact_store.async_save(
        "attachments",
        artifact_id,
        f"{artifact_id}.png",
        content,
        "image/png",
        owner,
    )
    async with factory() as session:
        await create_artifact_record(
            session,
            artifact_id=artifact_id,
            namespace="attachments",
            object_id=artifact_id,
            filename=f"{artifact_id}.png",
            owner_email=owner,
            conversation_id=conversation_id,
            purpose="chat_input",
            kind="image",
            mime_type="image/png",
            size_bytes=len(content),
            status="temporary",
        )
        await session.commit()


def _token(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _daily_brief_arguments() -> tuple[dict, dict[str, str]]:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    source_urls = {source["id"]: source["url"] for source in payload["sources"]}
    evidence: dict[str, str] = {}
    article_number = 0
    for accordion in payload["blocks"][4]["blocks"]:
        for article in accordion["items"]:
            article_number += 1
            artifact_id = f"att_daily_article_{article_number}"
            source_url = source_urls[article["source_id"]]
            article["media"] = {
                "ref": artifact_id,
                "alt": f"Source image for {article['title']}",
                "source_url": source_url,
            }
            evidence[artifact_id] = source_url
    return (
        {
            "action": "rich:pulse",
            "content": "Accessible Daily Brief fallback.",
            "format": "rich",
            "rich": payload,
        },
        evidence,
    )


def _client(factory, monkeypatch: pytest.MonkeyPatch, *, email: str) -> TestClient:
    app = FastAPI()
    app.include_router(deliverable_routes.router)
    app.state.session_factory = factory
    app.state.artifact_store = factory.artifact_store
    app.state.config = SimpleNamespace(deliverable_share_link_ttl_seconds=60)
    monkeypatch.setattr(
        deliverable_routes,
        "require_current_user",
        lambda _request: SimpleNamespace(email=email),
    )
    return TestClient(app)


def test_generic_block_contract_accepts_visual_fields_and_rejects_embeds() -> None:
    payload, _ = normalize_rich_payload(
        {
            "blocks": [
                {
                    "type": "action",
                    "variant": "compact",
                    "dek": "Decision context",
                    "summary": "Act now",
                    "href": "https://example.test",
                    "source_ids": ["source-1"],
                    "citations": "source-1",
                    "icon": "arrow-up-right",
                    "tone": "positive",
                    "media": {
                        "ref": "att_image",
                        "alt": "Chart",
                        "focal_point": {"x": 0.4, "y": 0.6},
                    },
                }
            ]
        }
    )
    assert payload["blocks"][0]["icon"] == "arrow-up-right"

    with pytest.raises(RichPayloadValidationError, match="unsafe_rich_embed"):
        normalize_rich_payload({"blocks": [{"type": "card", "html": "<img>"}]})
    with pytest.raises(RichPayloadValidationError, match="unsafe_rich_url"):
        normalize_rich_payload({"blocks": [{"type": "action", "href": "javascript:alert(1)"}]})


def test_tool_v2_descriptor_exposes_generic_media_contract() -> None:
    operation = next(
        item
        for item in WRITE_DELIVERABLE_TOOL.native_operations or []
        if item.operation == "write_deliverable"
    )
    rich_schema = operation.input_schema["properties"]["rich"]
    media_schema = rich_schema["properties"]["blocks"]["items"]["properties"]["media"]
    assert {"ref", "artifact_id", "content_ref", "alt", "focal_point"} <= set(
        media_schema["properties"]
    )
    assert "rich_media_contract" in WRITE_DELIVERABLE_TOOL.descriptor_extensions


def test_image_decoder_bomb_degrades_to_invalid_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_bomb(_content: BytesIO):
        raise Image.DecompressionBombError("pixel limit")

    monkeypatch.setattr(Image, "open", raise_bomb)
    assert _image_dimensions(_png(), "image/png") is None


@pytest.mark.asyncio
async def test_media_authorization_denies_cross_tenant_before_loading_bytes(
    task_continuation_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_managed_rich_deliverable(task_continuation_db)
    await _artifact(
        task_continuation_db,
        artifact_id="att_foreign_media",
        owner="other@example.com",
        conversation_id=None,
    )
    loads = 0
    original_load = task_continuation_db.artifact_store.async_load

    async def counted_load(*args, **kwargs):
        nonlocal loads
        loads += 1
        return await original_load(*args, **kwargs)

    monkeypatch.setattr(task_continuation_db.artifact_store, "async_load", counted_load)
    async with task_continuation_db() as session:
        with pytest.raises(RichPayloadValidationError) as exc_info:
            await create_deliverable(
                session,
                conversation_id="conv-controller",
                content="Fallback",
                format="rich",
                rich={
                    "blocks": [{"type": "card", "media": {"ref": "att_foreign_media", "alt": "No"}}]
                },
                artifact_store=task_continuation_db.artifact_store,
            )
    assert exc_info.value.reason == "rich_media_not_accessible"
    assert loads == 0


@pytest.mark.asyncio
async def test_daily_brief_preflight_and_execution_share_provenance_and_media_validation(
    task_continuation_db,
) -> None:
    await _seed_managed_rich_deliverable(task_continuation_db)
    arguments, evidence = _daily_brief_arguments()
    for artifact_id in evidence:
        await _artifact(
            task_continuation_db,
            artifact_id=artifact_id,
            owner="owner@example.com",
            conversation_id="conv-controller",
        )

    context_kwargs = {
        "actor_email": "owner@example.com",
        "current_agent_id": "agent-owner",
        "session_factory": task_continuation_db,
        "artifact_store": task_continuation_db.artifact_store,
        "conversation_id": "conv-controller",
        "conversation_agent_id": "agent-owner",
        "task_description": "Produce daily_brief_v13.",
        "executed_tool_names": ("artifact_read",) * len(evidence),
    }
    missing_context = NativeValidationContext(**context_kwargs)
    preflight_missing = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        missing_context,
    )
    execution_missing = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        arguments,
        NativeValidationContext(
            **context_kwargs,
            write_deliverable_validation_phase="execution",
        ),
    )

    assert preflight_missing["valid"] is False
    assert preflight_missing["errors"] == execution_missing["errors"]
    assert any(
        error["code"] == "invalid_daily_brief"
        and "distinct artifact_read result" in error["message"]
        for error in preflight_missing["errors"]
    )

    valid_context = NativeValidationContext(
        **context_kwargs,
        materialized_artifact_evidence=tuple(sorted(evidence.items())),
    )
    assert (
        await validate_available_tool_call_with_context(
            [WRITE_DELIVERABLE_TOOL],
            "write_deliverable",
            arguments,
            valid_context,
        )
    )["valid"] is True
    assert (
        await validate_available_tool_call_with_context(
            [WRITE_DELIVERABLE_TOOL],
            "write_deliverable",
            arguments,
            NativeValidationContext(
                **context_kwargs,
                materialized_artifact_evidence=tuple(sorted(evidence.items())),
                write_deliverable_validation_phase="execution",
            ),
        )
    )["valid"] is True

    async with task_continuation_db() as session:
        for artifact_id in evidence:
            artifact = await get_artifact_record(session, artifact_id)
            assert artifact.status == "temporary"


def test_write_validation_state_fingerprint_detects_evidence_and_skill_changes() -> None:
    original = NativeValidationContext(
        task_description="Produce daily_brief_v13.",
        materialized_artifact_evidence=(("att_source", "https://example.test/article"),),
        loaded_skill_snapshots=(
            (
                "skill_daily",
                (
                    ("skill_id", "skill_daily"),
                    ("name", "daily-brief"),
                    ("version_id", "sv_13"),
                    ("version_number", 13),
                    ("content_hash", "hash-13"),
                    ("contract_version", 13),
                ),
            ),
        ),
    )
    changed_evidence = NativeValidationContext(
        task_description=original.task_description,
        materialized_artifact_evidence=(("att_source", "https://example.test/different"),),
        loaded_skill_snapshots=original.loaded_skill_snapshots,
    )

    assert write_deliverable_validation_state_fingerprint(
        original, schema_hash="schema"
    ) != write_deliverable_validation_state_fingerprint(changed_evidence, schema_hash="schema")


@pytest.mark.asyncio
async def test_managed_descendant_media_allowed_and_sibling_denied(
    task_continuation_db,
) -> None:
    await _seed_managed_rich_deliverable(task_continuation_db)
    await _artifact(
        task_continuation_db,
        artifact_id="att_descendant_media",
        owner="owner@example.com",
        conversation_id="conv-grandchild",
    )
    async with task_continuation_db() as session:
        session.add(
            Agent(agent_id="agent-sibling-media", owner_email="owner@example.com", name="Sibling")
        )
        await session.flush()
        await create_conversation(
            session,
            "owner@example.com",
            "agent-sibling-media",
            "agent_work",
            conversation_id="conv-sibling-media",
        )
        await create_managed_conversation_link(
            session,
            user_email="owner@example.com",
            controller_agent_id="agent-owner",
            controller_conversation_id="conv-controller",
            controller_session_id="sess-controller",
            target_agent_id="agent-sibling-media",
            target_conversation_id="conv-sibling-media",
            target_session_id="sess-sibling-media",
            title="Sibling",
        )
        await session.commit()

    async with task_continuation_db() as session:
        allowed = await create_deliverable(
            session,
            deliverable_id="dlv_media_allowed",
            conversation_id="conv-controller",
            content="Fallback",
            format="rich",
            rich={
                "blocks": [
                    {
                        "type": "card",
                        "media": {
                            "ref": "att_descendant_media",
                            "alt": "Descendant image",
                            "credit": "Owner",
                            "source_url": "https://example.test/source",
                        },
                    }
                ]
            },
            artifact_store=task_continuation_db.artifact_store,
        )
        await session.commit()
        local_ref = allowed.rich_payload["blocks"][0]["media"]
        manifest = allowed.rich_payload["media_manifest"]
        item = manifest[local_ref["key"]]
        artifact = await get_artifact_record(session, "att_descendant_media")
        assert artifact.status == "attached"
        assert artifact.expires_at is None

    assert local_ref == {
        "key": next(iter(manifest)),
        "alt": "Descendant image",
        "credit": "Owner",
        "source_url": "https://example.test/source",
    }
    assert item == {
        "artifact_ref": "att_descendant_media",
        "mime_type": "image/png",
        "filename": "att_descendant_media.png",
        "size_bytes": len(_png()),
        "width": 2,
        "height": 3,
        "sha256": item["sha256"],
        "provenance": {
            "credit": "Owner",
            "source_url": "https://example.test/source",
        },
    }
    assert "signed" not in json.dumps(allowed.rich_payload).lower()
    assert "http://testserver/api/" not in json.dumps(allowed.rich_payload)

    await _artifact(
        task_continuation_db,
        artifact_id="att_sibling_media",
        owner="owner@example.com",
        conversation_id="conv-sibling-media",
    )
    async with task_continuation_db() as session:
        with pytest.raises(RichPayloadValidationError) as exc_info:
            await create_deliverable(
                session,
                conversation_id="conv-child",
                content="Fallback",
                format="rich",
                rich={"blocks": [{"type": "card", "media": {"ref": "att_sibling_media"}}]},
                artifact_store=task_continuation_db.artifact_store,
            )
    assert exc_info.value.reason == "rich_media_not_accessible"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime_type", "content", "reason"),
    [
        ("image/svg+xml", b"<svg/>", "unsupported_rich_media_type"),
        ("image/png", b"\x89PNG\r\n\x1a\nbroken", "invalid_rich_media_dimensions"),
    ],
)
async def test_media_rejects_unsafe_mime_and_missing_dimensions(
    task_continuation_db,
    mime_type: str,
    content: bytes,
    reason: str,
) -> None:
    await _seed_managed_rich_deliverable(task_continuation_db)
    artifact_id = f"att_invalid_{reason}"
    await task_continuation_db.artifact_store.async_save(
        "attachments",
        artifact_id,
        "invalid.bin",
        content,
        mime_type,
        "owner@example.com",
    )
    async with task_continuation_db() as session:
        await create_artifact_record(
            session,
            artifact_id=artifact_id,
            namespace="attachments",
            object_id=artifact_id,
            filename="invalid.bin",
            owner_email="owner@example.com",
            conversation_id="conv-controller",
            purpose="chat_input",
            kind="image",
            mime_type=mime_type,
            size_bytes=len(content),
            status="attached",
        )
        await session.commit()
    async with task_continuation_db() as session:
        with pytest.raises(RichPayloadValidationError) as exc_info:
            await create_deliverable(
                session,
                conversation_id="conv-controller",
                content="Fallback",
                format="rich",
                rich={"blocks": [{"type": "figure", "media": {"ref": artifact_id}}]},
                artifact_store=task_continuation_db.artifact_store,
            )
    assert exc_info.value.reason == reason


@pytest.mark.asyncio
async def test_private_and_public_media_proxy_security(
    task_continuation_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_managed_rich_deliverable(task_continuation_db)
    await _artifact(
        task_continuation_db,
        artifact_id="att_proxy_media",
        owner="owner@example.com",
        conversation_id="conv-controller",
    )
    async with task_continuation_db() as session:
        row = await create_deliverable(
            session,
            deliverable_id="dlv_media_proxy",
            conversation_id="conv-controller",
            content="Fallback",
            format="rich",
            rich={
                "blocks": [{"type": "figure", "media": {"ref": "att_proxy_media", "alt": "Proxy"}}]
            },
            artifact_store=task_continuation_db.artifact_store,
        )
        await session.commit()
        media_key = row.rich_payload["blocks"][0]["media"]["key"]

    share = signed_deliverable_view_link(
        task_continuation_db.artifact_store,
        "dlv_media_proxy",
        base_url="http://testserver",
        ttl_seconds=60,
    )
    token = _token(share.url)
    with _client(task_continuation_db, monkeypatch, email="owner@example.com") as client:
        private = client.get(f"/api/v1/deliverables/dlv_media_proxy/media/{media_key}")
        missing = client.get(
            "/api/v1/deliverables/dlv_media_proxy/media/media_000000000000000000000000"
        )
        traversal = client.get(
            f"/api/v1/deliverables/dlv_media_proxy/media/{media_key}%2F..%2Fcontent.md"
        )
        public = client.get(f"/api/v1/deliverables/share/{token}/media/{media_key}")
        tamper_index = len(token) // 2
        replacement = "A" if token[tamper_index] != "A" else "B"
        tampered_token = f"{token[:tamper_index]}{replacement}{token[tamper_index + 1 :]}"
        tampered = client.get(f"/api/v1/deliverables/share/{tampered_token}/media/{media_key}")
        expired_token = _sign_share_token("test-secret", "dlv_media_proxy", int(time.time()) - 1)
        expired = client.get(f"/api/v1/deliverables/share/{expired_token}/media/{media_key}")
        public_traversal = client.get(
            f"/api/v1/deliverables/share/{token}/media/{media_key}%2F..%2Fcontent.md"
        )

    assert private.status_code == 200
    assert private.content == _png()
    assert private.headers["x-content-type-options"] == "nosniff"
    assert private.headers["cache-control"] == "private, max-age=60"
    assert missing.status_code == 404
    assert traversal.status_code == 404
    assert public.status_code == 200
    assert public.headers["cache-control"].startswith("public, max-age=")
    assert int(public.headers["cache-control"].rsplit("=", 1)[1]) <= 60
    assert tampered.status_code == 404
    assert expired.status_code == 404
    assert public_traversal.status_code == 404

    with _client(task_continuation_db, monkeypatch, email="other@example.com") as client:
        denied = client.get(f"/api/v1/deliverables/dlv_media_proxy/media/{media_key}")
    assert denied.status_code == 404

    async with task_continuation_db() as session:
        artifact = await get_artifact_record(session, "att_proxy_media")
        artifact.status = "deleted"
        await session.commit()
    with _client(task_continuation_db, monkeypatch, email="owner@example.com") as client:
        deleted = client.get(f"/api/v1/deliverables/dlv_media_proxy/media/{media_key}")
    assert deleted.status_code == 404


@pytest.mark.asyncio
async def test_artifact_editorial_card_renders_private_public_and_pdf(
    task_continuation_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_managed_rich_deliverable(task_continuation_db)
    await _artifact(
        task_continuation_db,
        artifact_id="att_editorial_media",
        owner="owner@example.com",
        conversation_id="conv-controller",
    )
    async with task_continuation_db() as session:
        row = await create_deliverable(
            session,
            deliverable_id="dlv_editorial_media",
            conversation_id="conv-controller",
            title="Editorial media integration",
            content="Accessible editorial fallback.",
            format="rich",
            rich={
                "blocks": [
                    {
                        "type": "card",
                        "variant": "editorial",
                        "title": "Artifact-backed lead",
                        "dek": "Rendered consistently across every publication surface.",
                        "content": "Evidence-backed editorial summary.",
                        "citations": ["source-editorial"],
                        "media": {
                            "ref": "att_editorial_media",
                            "alt": "Editorial integration image",
                            "credit": "Cognis integration fixture",
                            "source_url": "https://source.example.org/editorial",
                        },
                    },
                    {
                        "type": "chart",
                        "title": "Meaningful trend",
                        "description": "Three observations establish direction.",
                        "spec_version": "cognis.chart.v1",
                        "chart_type": "line",
                        "series": [
                            {
                                "id": "trend",
                                "label": "Trend",
                                "points": [
                                    {"x": "T-2", "y": 12},
                                    {"x": "T-1", "y": 17},
                                    {"x": "Now", "y": 23},
                                ],
                            }
                        ],
                        "x_axis": {"type": "category"},
                        "y_axis": {"type": "linear"},
                        "source": "Editorial source",
                        "source_url": "https://source.example.org/editorial",
                        "observed_at": "2026-07-14T08:00:00+02:00",
                    },
                    {"type": "source_list", "title": "Sources", "numbered": True},
                ],
                "sources": [
                    {
                        "id": "source-editorial",
                        "title": "Editorial source",
                        "url": "https://source.example.org/editorial",
                    }
                ],
                "metadata": {"toc": True},
            },
            artifact_store=task_continuation_db.artifact_store,
        )
        await session.commit()
        media_key = row.rich_payload["blocks"][0]["media"]["key"]
        persisted = json.dumps(row.rich_payload, sort_keys=True)

    assert "att_editorial_media" in persisted
    assert "signed" not in persisted
    assert "token=" not in persisted

    share = signed_deliverable_view_link(
        task_continuation_db.artifact_store,
        "dlv_editorial_media",
        base_url="http://testserver",
        ttl_seconds=60,
    )
    token = _token(share.url)
    with _client(task_continuation_db, monkeypatch, email="owner@example.com") as client:
        private_html = client.get("/api/v1/deliverables/dlv_editorial_media/view")
        public_html = client.get(f"/api/v1/deliverables/share/{token}/view")
        private_media = client.get(f"/api/v1/deliverables/dlv_editorial_media/media/{media_key}")
        public_media = client.get(f"/api/v1/deliverables/share/{token}/media/{media_key}")
        pdf = client.get("/api/v1/deliverables/dlv_editorial_media/download.pdf")

    assert private_html.status_code == 200
    assert f"/api/v1/deliverables/dlv_editorial_media/media/{media_key}" in private_html.text
    assert "card-variant-editorial" in private_html.text
    assert 'class="chart-svg' in private_html.text
    assert "chart-line" in private_html.text
    assert 'href="https://source.example.org/editorial"' in private_html.text
    assert public_html.status_code == 200
    assert f"/api/v1/deliverables/share/{token}/media/{media_key}" in public_html.text
    assert private_media.content == public_media.content == _png()
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
