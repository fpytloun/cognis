from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import (
    create_artifact_record,
    find_tool_artifact_record,
    find_tool_output_artifact_record,
    list_recent_artifact_records,
    search_artifact_records,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def test_artifact_query_helpers_filter_and_order(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _exercise() -> tuple[list[str], list[str], list[str], list[str]]:
            async with app.state.session_factory() as session:  # type: ignore[attr-defined]
                older = await create_artifact_record(
                    session,
                    artifact_id="doc_old",
                    namespace="documents",
                    object_id="doc_old",
                    filename="notes.txt",
                    owner_email="user@example.com",
                    purpose="chat_input",
                    kind="file",
                    mime_type="text/plain",
                    size_bytes=12,
                    status="attached",
                )
                older.created_at = datetime(2026, 4, 21, 8, 0, tzinfo=UTC)

                newest = await create_artifact_record(
                    session,
                    artifact_id="doc_new",
                    namespace="documents",
                    object_id="doc_new",
                    filename="weekly-report.pdf",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="pdf",
                    mime_type="application/pdf",
                    size_bytes=42,
                    status="attached",
                    conversation_id="conv-1",
                    session_id="sess-1",
                )
                newest.created_at = datetime(2026, 4, 23, 9, 0, tzinfo=UTC)

                deleted = await create_artifact_record(
                    session,
                    artifact_id="doc_deleted",
                    namespace="documents",
                    object_id="doc_deleted",
                    filename="deleted-report.pdf",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="pdf",
                    mime_type="application/pdf",
                    size_bytes=9,
                    status="deleted",
                )
                deleted.created_at = datetime(2026, 4, 24, 9, 0, tzinfo=UTC)

                expired = await create_artifact_record(
                    session,
                    artifact_id="doc_expired",
                    namespace="documents",
                    object_id="doc_expired",
                    filename="expired-report.pdf",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="pdf",
                    mime_type="application/pdf",
                    size_bytes=19,
                    status="temporary",
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                )
                expired.created_at = datetime(2026, 4, 26, 9, 0, tzinfo=UTC)

                other_owner = await create_artifact_record(
                    session,
                    artifact_id="doc_other",
                    namespace="documents",
                    object_id="doc_other",
                    filename="weekly-report-copy.pdf",
                    owner_email="other@example.com",
                    purpose="tool_output",
                    kind="pdf",
                    mime_type="application/pdf",
                    size_bytes=18,
                    status="attached",
                )
                other_owner.created_at = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)
                await session.commit()

            async with app.state.session_factory() as session:  # type: ignore[attr-defined]
                recent = await list_recent_artifact_records(
                    session,
                    owner_email="user@example.com",
                    limit=10,
                )
                filename_search = await search_artifact_records(
                    session,
                    owner_email="user@example.com",
                    query="weekly report",
                    limit=10,
                    kind="pdf",
                    purpose="tool_output",
                )
                purpose_search = await search_artifact_records(
                    session,
                    owner_email="user@example.com",
                    query="tool_output",
                    limit=10,
                )
                date_filtered = await search_artifact_records(
                    session,
                    owner_email="user@example.com",
                    created_after=datetime(2026, 4, 22, 0, 0, tzinfo=UTC),
                    limit=10,
                )
                return (
                    [row.artifact_id for row in recent],
                    [row.artifact_id for row in filename_search],
                    [row.artifact_id for row in purpose_search],
                    [row.artifact_id for row in date_filtered],
                )

        recent_ids, filename_ids, purpose_ids, date_ids = asyncio.run(_exercise())

    assert recent_ids == ["doc_new", "doc_old"]
    assert filename_ids == ["doc_new"]
    assert purpose_ids == ["doc_new"]
    assert date_ids == ["doc_new"]


def test_tool_artifact_source_identity_is_explicit(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _exercise() -> tuple[str | None, str | None]:
            async with app.state.session_factory() as session:  # type: ignore[attr-defined]
                await create_artifact_record(
                    session,
                    artifact_id="toolout_1",
                    namespace="tool-outputs",
                    object_id="toolout_1",
                    filename="renamed-output.txt",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/plain",
                    size_bytes=20,
                    status="temporary",
                    source_tool_call_id="call-web",
                )
                await create_artifact_record(
                    session,
                    artifact_id="att_1",
                    namespace="attachments",
                    object_id="att_1",
                    filename="image.jpg",
                    owner_email="user@example.com",
                    purpose="tool_artifact",
                    kind="image",
                    mime_type="image/jpeg",
                    size_bytes=30,
                    status="attached",
                    content_hash="bytes-hash",
                    source_tool_call_id="call-web",
                    source_anchor="media:1",
                )
                await session.commit()
            async with app.state.session_factory() as session:  # type: ignore[attr-defined]
                output = await find_tool_output_artifact_record(
                    session,
                    owner_email="user@example.com",
                    source_tool_call_id="call-web",
                )
                materialized = await find_tool_artifact_record(
                    session,
                    owner_email="user@example.com",
                    source_tool_call_id="call-web",
                    source_anchor="media:1",
                )
                return (
                    output.artifact_id if output else None,
                    materialized.artifact_id if materialized else None,
                )

        output_id, materialized_id = asyncio.run(_exercise())

    assert output_id == "toolout_1"
    assert materialized_id == "att_1"
