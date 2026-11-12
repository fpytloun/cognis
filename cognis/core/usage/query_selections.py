"""Encrypted, expiring selections narrow fresh SQL authority; they never grant it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, Field
from sqlalchemy import func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from cognis.core.usage.query_metadata import utc
from cognis.core.usage.query_models import UsageModel, UsageScope
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.core.usage.sessions import UsageSessionCorrelation, session_correlation
from cognis.models.usage import UsageAttemptContext
from cognis.store.usage.models import UsageAttemptRow


class UsageSelection(UsageModel):
    """Private token payload, never an exposed namespace or persistent registry."""

    kind: Literal["session", "request"]
    subject_key: str | None = None
    correlation: UsageSessionCorrelation | None = None
    request_id: str | None = None
    owner_key: str | None = None
    start: AwareDatetime
    end: AwareDatetime
    max_row_id: int
    recorded_before: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


def selection_fingerprint(cursors: Any, scope: Any) -> str:
    """Bind refs to actor, mode, project and immutable trusted invocation."""
    return str(cursors.fingerprint(UsageScope(), scope, "selection-v2"))


def mint_selection(cursors: Any, scope: Any, selection: UsageSelection) -> str:
    """Hide even low-entropy labels and request IDs from restricted consumers."""
    return str(
        cursors.encode(selection_fingerprint(cursors, scope), [selection.model_dump(mode="json")])
    )


def read_selection(cursors: Any, scope: Any, token: str) -> UsageSelection:
    """Decode only after current actor/resource admission has succeeded."""
    values = cursors.decode(token, selection_fingerprint(cursors, scope))
    if values is None or len(values) != 1:
        raise UsageQueryError("invalid_usage_cursor", "Invalid usage selection")
    return UsageSelection.model_validate(values[0])


def allocator_tail() -> ColumnElement[int]:
    """Seek the global allocator tail independently of an enclosing page's filters."""
    return func.coalesce(
        select(UsageAttemptRow.row_id)
        .order_by(UsageAttemptRow.row_id.desc())
        .limit(1)
        .correlate(None)
        .scalar_subquery(),
        0,
    )


async def selection_boundary(session: Any, query: Any, scope: Any, cursors: Any) -> int:
    """Read the physical allocator tail by primary-key seek, never count/group raw data."""
    if query.selection_ref:
        return read_selection(cursors, scope, query.selection_ref).max_row_id
    return int(await session.scalar(select(allocator_tail())) or 0)


def request_selection(row: Any, query: Any, scope: Any, cursors: Any, fence: int) -> str:
    """Carry the parent registration fence through a namespace-bound request trail."""
    context = UsageAttemptContext.model_validate(row.context_payload)
    return mint_selection(
        cursors,
        scope,
        UsageSelection(
            kind="request",
            subject_key=row.usage_subject_key,
            request_id=row.request_id,
            correlation=session_correlation(context.attribution),
            start=utc(query.start),
            end=utc(query.end),
            max_row_id=fence,
            recorded_before=(
                read_selection(cursors, scope, query.selection_ref).recorded_before
                if query.selection_ref
                else datetime.now(UTC)
            ),
        ),
    )


def proxy_predicates(row: Any, correlation: UsageSessionCorrelation) -> list[Any]:
    """Exactly match the indexed namespace, including an explicit null client."""
    return [
        row.session_id.is_(None),
        row.client_id.is_(None)
        if correlation.client_id is None
        else row.client_id == correlation.client_id,
        row.client_session_id == correlation.client_session_id,
        or_(row.service_id.is_(None), row.service_id.not_in(["intaris", "mnemory"])),
    ]


def selection_predicates(row: Any, query: Any, scope: Any, cursors: Any) -> list[Any]:
    """Intersect a ref with the caller's normal filters and fresh resource SQL."""
    if query.selection_ref is None:
        return []
    selection = read_selection(cursors, scope, query.selection_ref)
    if utc(query.start) < selection.start or utc(query.end) > selection.end:
        raise UsageQueryError(
            "usage_selection_window", "Selection cannot expand its retained window"
        )
    terms = [
        row.started_at_observed >= selection.start,
        row.started_at_observed < selection.end,
        row.row_id <= selection.max_row_id,
        row.registered_at <= selection.recorded_before,
    ]
    if selection.subject_key is not None:
        terms.append(row.usage_subject_key == selection.subject_key)
    if selection.owner_key is not None:
        terms.append(row.owner_key == selection.owner_key)
    if selection.kind == "request":
        if selection.request_id is None:
            raise UsageQueryError("invalid_usage_cursor", "Invalid request selection")
        terms.append(row.request_id == selection.request_id)
    if selection.correlation is None and selection.kind == "session":
        raise UsageQueryError("invalid_usage_cursor", "Missing session namespace")
    if selection.correlation is not None and selection.correlation.session_kind == "proxy":
        if selection.subject_key is None:
            raise UsageQueryError("invalid_usage_cursor", "Missing proxy subject")
        terms.extend(proxy_predicates(row, selection.correlation))
    elif selection.correlation is not None:
        terms.append(row.session_id == selection.correlation.session_id)
    return terms
