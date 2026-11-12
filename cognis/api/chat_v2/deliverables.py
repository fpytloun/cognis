"""Request-time deliverable rendering data, separate from canonical chat events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import SQLAlchemyError

from cognis.api.chat_v2.schemas import (
    AssistantDeliverableTimelineItem,
    ChatSnapshot,
    ChatSyncResponse,
    TimelineBackfillResponse,
    TimelineItem,
    UpsertTimelineItemOp,
)
from cognis.api.common import api_exception
from cognis.api.models import DeliverableResponse
from cognis.api.serializers import deliverable_to_response
from cognis.core.deliverable_reads import readable_deliverables
from cognis.core.task_output_presentation import load_task_outputs
from cognis.models.deliverable import TaskDeliverableFreshness
from cognis.store.deliverable_storage import (
    DeliverableStoreUnavailable,
    hydrate_deliverable_payload,
)
from cognis.store.queries import get_deliverable_lineage_summaries, get_step_run, get_task

# Keep the previous maximum payload envelope, not an arbitrary document count.
MAX_INLINE_TOTAL_BYTES = 2 * 1024 * 1024
INLINE_CONCURRENCY = 4
INLINE_TIMEOUT_SECONDS = 1.0


class HistoryPageTooLarge(ValueError):
    """The intact source page needs a smaller history read window."""


class HistoryPayloadUnavailable(Exception):
    """An authorized body could not be prepared for history admission."""

    def __init__(self, errors: list[Exception]) -> None:
        super().__init__("A document in this history page is unavailable")
        self.retryable = any(
            not isinstance(error, (FileNotFoundError, ValueError)) for error in errors
        )


async def hydrate_history_page[ResponseT: (ChatSnapshot, TimelineBackfillResponse)](
    app: Any,
    response: ResponseT,
    *,
    caller_email: str,
    conversation_id: str,
    limit: int,
    read_page: Callable[[int], Awaitable[ResponseT]],
) -> ResponseT:
    """Prepare an intact canonical source window, reducing it before admission.

    The canonical builder, not a rendered-item slice, owns every retry's cursors
    and composed source references. A raw limit of one is the indivisible page
    exception to the byte target; it may project more than one document.
    """
    reduced = False
    while True:
        try:
            prepared = await hydrate_chat_deliverables(
                app,
                response,
                caller_email=caller_email,
                conversation_id=conversation_id,
                require_complete=True,
                allow_oversize=limit == 1,
            )
            if reduced:
                if isinstance(prepared, ChatSnapshot):
                    return prepared.model_copy(
                        update={
                            "timeline": prepared.timeline.model_copy(
                                update={"history_window_reduced": True}
                            )
                        }
                    )
                return prepared.model_copy(update={"history_window_reduced": True})
            return prepared
        except (HistoryPageTooLarge, HistoryPayloadUnavailable) as exc:
            if limit <= 1:
                retryable = isinstance(exc, HistoryPayloadUnavailable) and exc.retryable
                raise api_exception(
                    503 if retryable else 409,
                    "history_document_unavailable",
                    "A document in this history page is unavailable. "
                    + ("Try again." if retryable else "Its stored payload needs repair."),
                ) from exc
            limit = max(1, limit // 2)
            response = await read_page(limit)
            reduced = True


async def hydrate_chat_deliverables[
    ResponseT: (ChatSnapshot, ChatSyncResponse, TimelineBackfillResponse)
](
    app: Any,
    response: ResponseT,
    *,
    caller_email: str,
    conversation_id: str,
    require_complete: bool = False,
    allow_oversize: bool = False,
) -> ResponseT:
    """Co-deliver authorized document bodies within a bounded page envelope.

    Run outside the immutable snapshot cache, using the detail route's policy.
    Normal multi-document pages must not depend on card-mounted HTTP requests.
    Unavailable and out-of-envelope bodies retain the exceptional detail loader.
    """
    if isinstance(response, ChatSnapshot):
        items = response.timeline.items
    elif isinstance(response, ChatSyncResponse):
        items = [op.item for op in response.ops if isinstance(op, UpsertTimelineItemOp)]
    else:
        items = response.items
    ids = list(
        dict.fromkeys(
            item.deliverable_id
            for item in items
            if isinstance(item, AssistantDeliverableTimelineItem)
        )
    )
    if not ids:
        return response

    documents: dict[str, DeliverableResponse] = {}
    freshness: dict[str, TaskDeliverableFreshness] = {}
    errors: list[Exception] = []
    slots = asyncio.Semaphore(INLINE_CONCURRENCY)

    async def load(row: Any, lineage: Any) -> None:
        try:
            async with slots:
                await hydrate_deliverable_payload(row, app.state.artifact_store)
                document = deliverable_to_response(
                    row, lineage=lineage, task_freshness=freshness.get(row.deliverable_id)
                )
                if require_complete or (
                    len(document.model_dump_json().encode("utf-8")) <= MAX_INLINE_TOTAL_BYTES
                ):
                    documents[row.deliverable_id] = document
        except (
            OSError,
            ValueError,
            BotoCoreError,
            ClientError,
            DeliverableStoreUnavailable,
        ) as exc:
            errors.append(exc)
            # Detail loading owns errors for unavailable/corrupt payloads.
            return

    async def prepare() -> None:
        async with app.state.session_factory() as session:
            rows = await readable_deliverables(
                session, ids, caller_email, accessor_conversation_id=conversation_id
            )
            selected = []
            remaining = MAX_INLINE_TOTAL_BYTES
            for id in reversed(ids):
                read = rows.get(id)
                if read is None:
                    continue
                row = read.row
                if require_complete:
                    selected.append(row)
                    continue
                fields = ["content"] + [
                    field for field in ("rich", "outputs") if getattr(row, f"{field}_key")
                ]
                if any(
                    not isinstance(getattr(row, f"{field}_size"), int)
                    or getattr(row, f"{field}_size") < 0
                    for field in fields
                ):
                    continue
                size = sum(
                    size or 0 for size in (row.content_size, row.rich_size, row.outputs_size)
                )
                if size > remaining:
                    continue
                selected.append(row)
                remaining -= size
            if require_complete and not allow_oversize:
                estimated = sum(
                    size or 0
                    for row in selected
                    for size in (row.content_size, row.rich_size, row.outputs_size)
                )
                if estimated > MAX_INLINE_TOTAL_BYTES:
                    raise HistoryPageTooLarge()
            lineage = await get_deliverable_lineage_summaries(session, selected)
            loaded_tasks: set[str] = set()
            for row in selected:
                if row.step_run_id is None:
                    continue
                run = await get_step_run(session, row.step_run_id)
                if run is None or run.task_id in loaded_tasks:
                    continue
                task = await get_task(session, run.task_id)
                if task is not None and task.created_by == caller_email:
                    freshness.update((await load_task_outputs(session, task)).freshness)
                    loaded_tasks.add(task.task_id)
        # All DB leases end before concurrent object-store reads.
        async with asyncio.TaskGroup() as group:
            for row in selected:
                group.create_task(load(row, lineage[row.deliverable_id]))

    if require_complete:
        # History admission waits for its selected page, using normal request
        # cancellation and storage timeouts, never a deadline-to-card fallback.
        await prepare()
        if errors:
            raise HistoryPayloadUnavailable(errors)
        if (
            not allow_oversize
            and sum(
                len(document.model_dump_json().encode("utf-8")) for document in documents.values()
            )
            > MAX_INLINE_TOTAL_BYTES
        ):
            raise HistoryPageTooLarge()
    else:
        try:
            async with asyncio.timeout(INLINE_TIMEOUT_SECONDS):
                await prepare()
        except (TimeoutError, OSError, ValueError, SQLAlchemyError, BotoCoreError, ClientError):
            # Live sync keeps its existing best-effort overlay and cursor semantics.
            pass

    remaining = MAX_INLINE_TOTAL_BYTES
    for id in reversed(ids):
        document = documents.get(id)
        if document is not None:
            size = len(document.model_dump_json().encode("utf-8"))
            if not require_complete and size > remaining:
                documents.pop(id)
            else:
                remaining -= size

    def enrich(item: TimelineItem) -> TimelineItem:
        if isinstance(item, AssistantDeliverableTimelineItem):
            document = documents.get(item.deliverable_id)
            if document is not None:
                return item.model_copy(update={"deliverable": document})
        return item

    if isinstance(response, ChatSnapshot):
        return response.model_copy(
            update={
                "timeline": response.timeline.model_copy(
                    update={"items": [enrich(item) for item in items]}
                )
            }
        )
    if isinstance(response, ChatSyncResponse):
        return response.model_copy(
            update={
                "ops": [
                    op.model_copy(update={"item": enrich(op.item)})
                    if isinstance(op, UpsertTimelineItemOp)
                    else op
                    for op in response.ops
                ]
            }
        )
    return response.model_copy(update={"items": [enrich(item) for item in items]})
