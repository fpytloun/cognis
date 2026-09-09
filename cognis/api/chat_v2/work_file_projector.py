"""Transactional per-session Work current-file projection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import WorkCurrentFileRow, WorkRecordFileRow, WorkRecordRow

FILES_PROJECTOR_VERSION = "work-files-v3"
_DELETE_STATES = {"delete", "deleted", "removed"}


def _generation_id(owner_email: str, session_id: str, first_file_row_id: str) -> str:
    digest = hashlib.sha256(
        f"{owner_email}\0{session_id}\0{first_file_row_id}".encode()
    ).hexdigest()[:40]
    return f"wpg_{digest}"


def _current_file_id(
    owner_email: str, session_id: str, materializer_version: str, generation_id: str
) -> str:
    digest = hashlib.sha256(
        f"{owner_email}\0{session_id}\0{materializer_version}\0"
        f"{FILES_PROJECTOR_VERSION}\0{generation_id}".encode()
    ).hexdigest()[:40]
    return f"wcf_{digest}"


def _order(row: WorkRecordFileRow) -> tuple[Any, ...]:
    return (
        row.source_seq,
        row.item_ordinal,
        row.file_ordinal,
        row.work_record_file_id,
    )


@dataclass
class _Component:
    generation_id: str
    facts: list[WorkRecordFileRow] = field(default_factory=list)
    active_paths: set[str] = field(default_factory=set)
    rename_count: int = 0
    terminated: bool = False

    @property
    def latest(self) -> WorkRecordFileRow:
        return max(self.facts, key=_order)


async def rebuild_session_current_files(
    db: AsyncSession,
    *,
    owner_email: str,
    session_id: str,
    materializer_version: str,
) -> int:
    """Rebuild one physical session in authoritative source order."""

    fact_rows = list(
        (
            await db.execute(
                select(
                    WorkRecordFileRow,
                    WorkRecordRow.source_session_id,
                    WorkRecordRow.source_event_id,
                    WorkRecordRow.source_item_id,
                )
                .join(
                    WorkRecordRow,
                    WorkRecordRow.work_record_id == WorkRecordFileRow.work_record_id,
                )
                .where(
                    WorkRecordFileRow.owner_email == owner_email,
                    WorkRecordFileRow.session_id == session_id,
                    WorkRecordFileRow.materializer_version == materializer_version,
                    WorkRecordRow.owner_email == owner_email,
                    WorkRecordRow.session_id == session_id,
                    WorkRecordRow.materializer_version == materializer_version,
                    WorkRecordRow.is_evidence.is_(True),
                )
                .order_by(
                    WorkRecordFileRow.source_seq,
                    WorkRecordFileRow.item_ordinal,
                    WorkRecordFileRow.file_ordinal,
                    WorkRecordFileRow.work_record_file_id,
                )
            )
        ).all()
    )
    facts = [row[0] for row in fact_rows]
    source_by_file_id = {row[0].work_record_file_id: (row[1], row[2], row[3]) for row in fact_rows}
    active: dict[str, _Component] = {}
    components: list[_Component] = []
    for fact in facts:
        component = active.get(fact.old_path_id) if fact.old_path_id else None
        destination = active.get(fact.path_id)
        if component is None:
            component = destination
        if component is None:
            component = _Component(
                generation_id=_generation_id(owner_email, session_id, fact.work_record_file_id)
            )
            components.append(component)
        elif destination is not None and destination is not component:
            destination.terminated = True
            for path in tuple(destination.active_paths):
                if active.get(path) is destination:
                    del active[path]
            destination.active_paths.clear()
        component.facts.append(fact)
        component.active_paths.add(fact.path_id)
        active[fact.path_id] = component
        if fact.old_path_id and fact.old_path_id != fact.path_id:
            component.rename_count += 1
            component.active_paths.discard(fact.old_path_id)
            if active.get(fact.old_path_id) is component:
                del active[fact.old_path_id]
        fact.owner_email = owner_email
        fact.session_id = session_id
        fact.materializer_version = materializer_version
        fact.path_generation_id = component.generation_id
        if str(fact.status or "").lower() in _DELETE_STATES:
            component.active_paths.discard(fact.path_id)
            if active.get(fact.path_id) is component:
                del active[fact.path_id]

    await db.execute(
        delete(WorkCurrentFileRow).where(
            WorkCurrentFileRow.owner_email == owner_email,
            WorkCurrentFileRow.session_id == session_id,
            WorkCurrentFileRow.materializer_version == materializer_version,
            WorkCurrentFileRow.file_projector_version == FILES_PROJECTOR_VERSION,
        )
    )
    now = datetime.now(UTC)
    for recreate_ordinal, component in enumerate(components):
        latest = component.latest
        source_session_id, source_event_id, source_item_id = source_by_file_id[
            latest.work_record_file_id
        ]
        path_id = latest.path_id
        root_id, separator, relative_path = path_id.partition(":")
        db.add(
            WorkCurrentFileRow(
                current_file_id=_current_file_id(
                    owner_email,
                    session_id,
                    materializer_version,
                    component.generation_id,
                ),
                owner_email=owner_email,
                session_id=session_id,
                materializer_version=materializer_version,
                file_projector_version=FILES_PROJECTOR_VERSION,
                path_generation_id=component.generation_id,
                source_store="intaris",
                source_session_id=source_session_id,
                source_seq=latest.source_seq,
                source_event_id=source_event_id,
                source_item_id=source_item_id,
                path=latest.path,
                path_id=path_id,
                relative_path=relative_path if separator else None,
                root_id=root_id if separator else None,
                root_label=None,
                previous_path=latest.old_path,
                previous_path_id=latest.old_path_id,
                rename_ordinal=component.rename_count,
                recreate_ordinal=recreate_ordinal,
                state=("overwritten" if component.terminated else str(latest.status or "modified")),
                binary=latest.binary,
                generated=latest.generated,
                additions=sum(fact.additions for fact in component.facts),
                deletions=sum(fact.deletions for fact in component.facts),
                preview=None,
                preview_size=0,
                preview_truncated=latest.truncated,
                preview_omitted=latest.preview_omitted,
                content_hash=None,
                content_expires_at=None,
                content_scrubbed_at=now,
            )
        )
    await db.flush()
    return len(components)


async def scrub_expired_current_file_content(
    db: AsyncSession, *, now: datetime, limit: int = 500
) -> int:
    rows = list(
        (
            await db.scalars(
                select(WorkCurrentFileRow)
                .where(
                    WorkCurrentFileRow.content_expires_at.is_not(None),
                    WorkCurrentFileRow.content_expires_at <= now,
                    WorkCurrentFileRow.content_scrubbed_at.is_(None),
                )
                .order_by(WorkCurrentFileRow.content_expires_at)
                .limit(limit)
            )
        ).all()
    )
    for row in rows:
        row.preview = None
        row.preview_size = 0
        row.content_hash = None
        row.content_scrubbed_at = now
    return len(rows)
