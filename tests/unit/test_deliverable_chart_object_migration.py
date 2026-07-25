from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import cognis.store.deliverable_chart_migration as migration_module
from cognis.api.app import create_app
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.rendering.deliverables import render_standalone_html
from cognis.store.database import create_engine, create_session_factory
from cognis.store.deliverable_chart_migration import DeliverableChartPayloadMigration
from cognis.store.deliverable_storage import (
    DELIVERABLE_CHART_V1_RICH_KEY_PREFIX,
    DELIVERABLE_LEGACY_RICH_FILENAME,
    DELIVERABLE_RICH_FILENAME,
    hydrate_deliverable_payload,
    store_deliverable_payload,
)
from cognis.store.models import Base, DeliverableRow

_LEGACY_CHART = {
    "blocks": [
        {
            "type": "chart",
            "chart_type": "bar",
            "data": [{"label": "Ready", "value": 3}],
            "description": "Readiness",
        }
    ]
}
_CANONICAL_CHART = {
    "blocks": [
        {
            "type": "chart",
            "spec_version": "cognis.chart.v1",
            "chart_type": "bar",
            "series": [
                {
                    "id": "ready",
                    "label": "Ready",
                    "points": [{"x": "Ready", "y": 3}],
                }
            ],
            "x_axis": {"type": "category"},
            "y_axis": {"type": "linear"},
            "description": "Readiness",
        }
    ]
}
_NO_CHART = {"blocks": [{"type": "markdown", "content": "Already compatible"}]}
_UNSUPPORTED_CHART = {
    "blocks": [
        {
            "type": "chart",
            "chart_type": "range",
            "data": [{"label": "Ambiguous", "value": 3}],
        }
    ]
}


@pytest.fixture
async def migration_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], ArtifactStore]]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/migration.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    try:
        yield session_factory, store
    finally:
        await engine.dispose()


def _payload_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    store: Any,
    *,
    deliverable_id: str,
    payload: object,
    rich_key: str = DELIVERABLE_LEGACY_RICH_FILENAME,
    stored_bytes: bytes | None = None,
    row_size: int | None = None,
    row_hash: str | None = None,
) -> None:
    content = f"Fallback for {deliverable_id}"
    content_bytes = content.encode()
    rich_bytes = stored_bytes if stored_bytes is not None else _payload_bytes(payload)
    await store.async_save(
        "deliverables",
        deliverable_id,
        "content.md",
        content_bytes,
        "text/markdown",
    )
    await store.async_save(
        "deliverables",
        deliverable_id,
        rich_key,
        rich_bytes,
        "application/json",
    )
    async with session_factory() as session:
        session.add(
            DeliverableRow(
                deliverable_id=deliverable_id,
                storage_namespace="deliverables",
                storage_object_id=deliverable_id,
                content_key="content.md",
                content_mime="text/markdown",
                content_size=len(content_bytes),
                content_hash=hashlib.sha256(content_bytes).hexdigest(),
                format="rich",
                rich_key=rich_key,
                rich_size=len(rich_bytes) if row_size is None else row_size,
                rich_hash=hashlib.sha256(rich_bytes).hexdigest() if row_hash is None else row_hash,
                status="approved",
            )
        )
        await session.commit()


async def _row(
    session_factory: async_sessionmaker[AsyncSession], deliverable_id: str
) -> DeliverableRow:
    async with session_factory() as session:
        row = await session.get(DeliverableRow, deliverable_id)
        assert row is not None
        session.expunge(row)
        return row


@pytest.mark.asyncio
async def test_filesystem_backfill_upgrades_current_schema_legacy_payload(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, store = migration_db
    await _seed(session_factory, store, deliverable_id="dlv_legacy", payload=_LEGACY_CHART)

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    row = await _row(session_factory, "dlv_legacy")
    assert stats.migrated == 1
    assert row.rich_key is not None
    assert row.rich_key.startswith(f"{DELIVERABLE_CHART_V1_RICH_KEY_PREFIX}.")
    migrated, _mime = await store.async_load(
        row.storage_namespace, row.storage_object_id, row.rich_key
    )
    assert len(migrated) == row.rich_size
    assert hashlib.sha256(migrated).hexdigest() == row.rich_hash
    assert json.loads(migrated)["blocks"][0]["spec_version"] == "cognis.chart.v1"
    assert not await store.async_exists(
        "deliverables", "dlv_legacy", DELIVERABLE_LEGACY_RICH_FILENAME
    )


@pytest.mark.asyncio
async def test_canonical_and_no_chart_payloads_are_promoted_and_double_run_is_idempotent(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, store = migration_db
    await _seed(session_factory, store, deliverable_id="dlv_canonical", payload=_CANONICAL_CHART)
    await _seed(session_factory, store, deliverable_id="dlv_no_chart", payload=_NO_CHART)
    service = DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    )

    first = await service.run_once()
    first_keys = {
        row.deliverable_id: row.rich_key
        for row in [
            await _row(session_factory, "dlv_canonical"),
            await _row(session_factory, "dlv_no_chart"),
        ]
    }
    second = await service.run_once()

    assert first.promoted == 2
    assert second.scanned == 0
    assert {
        row.deliverable_id: row.rich_key
        for row in [
            await _row(session_factory, "dlv_canonical"),
            await _row(session_factory, "dlv_no_chart"),
        ]
    } == first_keys


@pytest.mark.asyncio
async def test_backfill_uses_bounded_stable_keyset_pages(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, store = migration_db
    for index in range(5):
        await _seed(
            session_factory,
            store,
            deliverable_id=f"dlv_page_{index}",
            payload=_NO_CHART,
        )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
        batch_size=2,
    ).run_once()

    assert stats.scanned == 5
    assert stats.batches == 3
    assert stats.promoted == 5


@pytest.mark.asyncio
async def test_missing_corrupt_and_integrity_mismatch_payloads_remain_unchanged(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, store = migration_db
    await _seed(
        session_factory,
        store,
        deliverable_id="dlv_corrupt",
        payload={},
        stored_bytes=b"{not-json",
    )
    await _seed(
        session_factory,
        store,
        deliverable_id="dlv_hash",
        payload=_LEGACY_CHART,
        row_hash="0" * 64,
    )
    await _seed(
        session_factory,
        store,
        deliverable_id="dlv_size",
        payload=_LEGACY_CHART,
        row_size=1,
    )
    await _seed(session_factory, store, deliverable_id="dlv_missing", payload=_LEGACY_CHART)
    await store.async_delete("deliverables", "dlv_missing", DELIVERABLE_LEGACY_RICH_FILENAME)

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    assert stats.corrupt == 1
    assert stats.integrity_mismatch == 2
    assert stats.missing == 1
    for deliverable_id in ("dlv_corrupt", "dlv_hash", "dlv_size", "dlv_missing"):
        assert (await _row(session_factory, deliverable_id)).rich_key == "rich.json"


@pytest.mark.asyncio
async def test_deep_json_is_skipped_without_aborting_later_rows(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, store = migration_db
    deeply_nested_json = b"[" * 1_100 + b"0" + b"]" * 1_100
    await _seed(
        session_factory,
        store,
        deliverable_id="dlv_deep",
        payload={},
        stored_bytes=deeply_nested_json,
    )
    await _seed(
        session_factory,
        store,
        deliverable_id="dlv_later",
        payload=_LEGACY_CHART,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    assert stats.corrupt == 1
    assert stats.migrated == 1
    assert (await _row(session_factory, "dlv_deep")).rich_key == "rich.json"
    assert (await _row(session_factory, "dlv_later")).rich_key != "rich.json"


class _ContractStore:
    """Fake S3-like contract exposing only exact-key async operations."""

    def __init__(
        self,
        objects: dict[tuple[str, str, str], bytes],
        *,
        fail_upload: bool = False,
        load_failures: int = 0,
        upload_failures: int = 0,
        truncate_upload: bool = False,
        on_save: Callable[[str], Awaitable[None]] | None = None,
        on_delete: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.objects = objects
        self.fail_upload = fail_upload
        self.load_failures = load_failures
        self.upload_failures = upload_failures
        self.truncate_upload = truncate_upload
        self.on_save = on_save
        self.on_delete = on_delete
        self.saved_keys: list[str] = []
        self.deleted_keys: list[str] = []

    async def async_load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        if self.load_failures:
            self.load_failures -= 1
            raise OSError("transient load failure")
        try:
            return self.objects[(namespace, object_id, filename)], "application/json"
        except KeyError:
            raise FileNotFoundError(filename) from None

    async def async_save(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        owner_email: str | None = None,
    ) -> None:
        del content_type, owner_email
        self.saved_keys.append(filename)
        if self.fail_upload or self.upload_failures:
            if self.upload_failures:
                self.upload_failures -= 1
            raise OSError("upload failed")
        self.objects[(namespace, object_id, filename)] = (
            content[:-1] if self.truncate_upload else content
        )
        if self.on_save is not None:
            await self.on_save(filename)

    async def async_delete(self, namespace: str, object_id: str, filename: str) -> None:
        if self.on_delete is not None:
            await self.on_delete(filename)
        self.deleted_keys.append(filename)
        self.objects.pop((namespace, object_id, filename), None)


@pytest.mark.asyncio
async def test_fake_s3_contract_uses_no_listing_or_backend_specific_apis(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_contract",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_contract", "rich.json"
    )
    store = _ContractStore({("deliverables", "dlv_contract", "rich.json"): legacy_bytes})

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    assert stats.migrated == 1
    assert store.saved_keys[0].startswith(f"{DELIVERABLE_CHART_V1_RICH_KEY_PREFIX}.")
    assert store.deleted_keys == ["rich.json"]


@pytest.mark.asyncio
async def test_upload_failure_leaves_row_and_old_key_unchanged(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_upload",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_upload", "rich.json"
    )
    store = _ContractStore(
        {("deliverables", "dlv_upload", "rich.json"): legacy_bytes},
        fail_upload=True,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    assert stats.upload_failed == 1
    assert (await _row(session_factory, "dlv_upload")).rich_key == "rich.json"
    assert ("deliverables", "dlv_upload", "rich.json") in store.objects
    assert not any(
        filename.startswith(DELIVERABLE_CHART_V1_RICH_KEY_PREFIX)
        for _namespace, _object_id, filename in store.objects
    )


@pytest.mark.asyncio
async def test_transient_load_and_upload_failures_retry_within_one_pass(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_retry",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_retry", "rich.json"
    )
    store = _ContractStore(
        {("deliverables", "dlv_retry", "rich.json"): legacy_bytes},
        load_failures=1,
        upload_failures=1,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
        storage_retry_delay_seconds=0,
    ).run_once()

    assert stats.migrated == 1
    assert stats.skipped == 0
    assert len(store.saved_keys) == 2
    assert len(set(store.saved_keys)) == 1


@pytest.mark.asyncio
async def test_persistent_load_failure_is_distinct_from_missing(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_load_failure",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_load_failure", "rich.json"
    )
    store = _ContractStore(
        {("deliverables", "dlv_load_failure", "rich.json"): legacy_bytes},
        load_failures=3,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
        storage_retry_delay_seconds=0,
    ).run_once()

    assert stats.load_failed == 1
    assert stats.missing == 0
    assert (await _row(session_factory, "dlv_load_failure")).rich_key == "rich.json"


@pytest.mark.asyncio
async def test_silently_corrupt_staged_write_is_verified_before_cas(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_truncated_stage",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_truncated_stage", "rich.json"
    )
    store = _ContractStore(
        {("deliverables", "dlv_truncated_stage", "rich.json"): legacy_bytes},
        truncate_upload=True,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    assert stats.upload_failed == 1
    assert (await _row(session_factory, "dlv_truncated_stage")).rich_key == "rich.json"
    assert ("deliverables", "dlv_truncated_stage", "rich.json") in store.objects
    assert store.deleted_keys == [store.saved_keys[0]]


@pytest.mark.asyncio
async def test_canonical_expansion_over_size_limit_is_not_staged(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, store = migration_db
    await _seed(
        session_factory,
        store,
        deliverable_id="dlv_expands",
        payload=_LEGACY_CHART,
    )
    monkeypatch.setattr(
        migration_module,
        "_json_bytes",
        lambda _payload: b"x" * 256_001,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    assert stats.unsupported == 1
    assert (await _row(session_factory, "dlv_expands")).rich_key == "rich.json"


@pytest.mark.asyncio
async def test_cas_conflict_deletes_only_staged_key(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_conflict",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_conflict", "rich.json"
    )

    async def conflict(_staged_key: str) -> None:
        async with session_factory() as session:
            await session.execute(
                sa.update(DeliverableRow)
                .where(DeliverableRow.deliverable_id == "dlv_conflict")
                .values(rich_hash="f" * 64)
            )
            await session.commit()

    store = _ContractStore(
        {("deliverables", "dlv_conflict", "rich.json"): legacy_bytes},
        on_save=conflict,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    assert stats.cas_conflict == 1
    assert (await _row(session_factory, "dlv_conflict")).rich_key == "rich.json"
    assert store.deleted_keys == store.saved_keys
    assert ("deliverables", "dlv_conflict", "rich.json") in store.objects


@pytest.mark.asyncio
async def test_status_change_conflicts_before_superseding_cleanup_can_break_pointer(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_superseded",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_superseded", "rich.json"
    )

    async def supersede(_staged_key: str) -> None:
        async with session_factory() as session:
            await session.execute(
                sa.update(DeliverableRow)
                .where(DeliverableRow.deliverable_id == "dlv_superseded")
                .values(status="superseded")
            )
            await session.commit()

    store = _ContractStore(
        {("deliverables", "dlv_superseded", "rich.json"): legacy_bytes},
        on_save=supersede,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    row = await _row(session_factory, "dlv_superseded")
    assert stats.cas_conflict == 1
    assert row.status == "superseded"
    assert row.rich_key == "rich.json"
    assert store.deleted_keys == store.saved_keys
    assert ("deliverables", "dlv_superseded", "rich.json") in store.objects


@pytest.mark.asyncio
async def test_benign_status_transition_can_still_migrate_in_same_pass(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_approved",
        payload=_LEGACY_CHART,
    )
    async with session_factory() as session:
        await session.execute(
            sa.update(DeliverableRow)
            .where(DeliverableRow.deliverable_id == "dlv_approved")
            .values(status="buffered")
        )
        await session.commit()
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_approved", "rich.json"
    )

    async def approve(_staged_key: str) -> None:
        async with session_factory() as session:
            await session.execute(
                sa.update(DeliverableRow)
                .where(DeliverableRow.deliverable_id == "dlv_approved")
                .values(status="approved")
            )
            await session.commit()

    store = _ContractStore(
        {("deliverables", "dlv_approved", "rich.json"): legacy_bytes},
        on_save=approve,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    row = await _row(session_factory, "dlv_approved")
    assert stats.migrated == 1
    assert row.status == "approved"
    assert row.rich_key is not None
    assert row.rich_key.startswith(DELIVERABLE_CHART_V1_RICH_KEY_PREFIX)


@pytest.mark.asyncio
async def test_ambiguous_commit_preserves_staged_key_when_db_pointer_was_committed(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_ambiguous_commit",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_ambiguous_commit", "rich.json"
    )
    store = _ContractStore({("deliverables", "dlv_ambiguous_commit", "rich.json"): legacy_bytes})

    class _AmbiguousCommitMigration(DeliverableChartPayloadMigration):
        async def _commit(self, session: AsyncSession) -> None:
            await session.commit()
            raise OSError("commit result lost")

    stats = await _AmbiguousCommitMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    row = await _row(session_factory, "dlv_ambiguous_commit")
    assert stats.migrated == 1
    assert row.rich_key is not None
    assert row.rich_key in store.saved_keys
    assert ("deliverables", "dlv_ambiguous_commit", row.rich_key) in store.objects
    assert store.deleted_keys == ["rich.json"]


@pytest.mark.asyncio
async def test_db_pointer_commits_before_old_key_delete(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_order",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_order", "rich.json"
    )

    async def assert_committed(filename: str) -> None:
        if filename == "rich.json":
            assert (await _row(session_factory, "dlv_order")).rich_key != "rich.json"

    store = _ContractStore(
        {("deliverables", "dlv_order", "rich.json"): legacy_bytes},
        on_delete=assert_committed,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()

    assert stats.migrated == 1
    assert store.deleted_keys == ["rich.json"]


@pytest.mark.asyncio
async def test_object_migration_preserves_chart_shaped_auxiliary_records(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, store = migration_db
    auxiliary = {
        "type": "chart",
        "chart_type": "range",
        "data": [{"label": "Opaque dataset", "value": 2}],
    }
    payload = {
        **_LEGACY_CHART,
        "datasets": [auxiliary],
        "metadata": {"analytics": auxiliary},
    }
    await _seed(
        session_factory,
        store,
        deliverable_id="dlv_auxiliary_chart",
        payload=payload,
    )

    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()
    row = await _row(session_factory, "dlv_auxiliary_chart")
    assert row.rich_key is not None
    migrated_bytes, _mime = await store.async_load(
        "deliverables",
        "dlv_auxiliary_chart",
        row.rich_key,
    )
    migrated = json.loads(migrated_bytes)

    assert stats.migrated == 1
    assert migrated["datasets"] == [auxiliary]
    assert migrated["metadata"] == {"analytics": auxiliary}


@pytest.mark.asyncio
async def test_skipped_legacy_payload_hydrates_and_renders_safe_content_fallback(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, store = migration_db
    await _seed(
        session_factory,
        store,
        deliverable_id="dlv_unsupported",
        payload=_UNSUPPORTED_CHART,
    )
    stats = await DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    ).run_once()
    row = await _row(session_factory, "dlv_unsupported")

    await hydrate_deliverable_payload(row, store)
    rendered = render_standalone_html(row)

    assert stats.unsupported == 1
    assert row.rich_key == "rich.json"
    assert "Fallback for dlv_unsupported" in rendered
    assert "Chart data is unavailable" not in rendered


@pytest.mark.asyncio
async def test_new_rich_writes_use_chart_v1_filename(tmp_path: Path) -> None:
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))

    stored = await store_deliverable_payload(
        store,
        deliverable_id="dlv_new",
        content="Fallback",
        format="rich",
        rich_payload=_NO_CHART,
        outputs=None,
    )

    assert stored.rich.key == DELIVERABLE_RICH_FILENAME
    assert stored.rich.key == "rich.chart-v1.json"
    assert await store.async_exists("deliverables", "dlv_new", DELIVERABLE_RICH_FILENAME)


def test_app_lifecycle_starts_and_stops_one_shot_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    app = create_app()

    with TestClient(app) as client:
        service = client.app.state.deliverable_chart_migration  # type: ignore[attr-defined]
        assert service._task is not None  # noqa: SLF001

    assert service._task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_service_stop_awaits_cancellation_safe_inflight_row(
    migration_db: tuple[async_sessionmaker[AsyncSession], ArtifactStore],
) -> None:
    session_factory, filesystem_store = migration_db
    await _seed(
        session_factory,
        filesystem_store,
        deliverable_id="dlv_cancel",
        payload=_LEGACY_CHART,
    )
    legacy_bytes, _mime = await filesystem_store.async_load(
        "deliverables", "dlv_cancel", "rich.json"
    )
    saved = asyncio.Event()
    release = asyncio.Event()

    async def pause_after_save(_filename: str) -> None:
        saved.set()
        await release.wait()

    store = _ContractStore(
        {("deliverables", "dlv_cancel", "rich.json"): legacy_bytes},
        on_save=pause_after_save,
    )
    service = DeliverableChartPayloadMigration(
        session_factory=session_factory,
        artifact_store=store,
    )
    await service.start()
    await saved.wait()
    stop_task = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()

    release.set()
    await stop_task

    row = await _row(session_factory, "dlv_cancel")
    assert row.rich_key is not None
    assert row.rich_key.startswith(DELIVERABLE_CHART_V1_RICH_KEY_PREFIX)
