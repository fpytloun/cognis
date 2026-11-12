"""Execution and descendant membership; provenance never substitutes for authority."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import and_, false, literal, or_, select, union_all
from sqlalchemy.orm import aliased

from cognis.store.managed_lineage import managed_lineage
from cognis.store.models import Conversation, ManagedConversationLink, Session

MAX_MEMBERS = 10000
MAX_LANE_MEMBERS = 1000
LANE_FIELDS = (
    "user_email",
    "conversation_id",
    "agent_id",
    "parent_session_id",
    "delegation_mode",
    "delegation_task",
)


def same_lane(left: Any, right: Any, *, activity: bool = True) -> Any:
    """SQL equivalent of the canonical continuation lane identity."""
    names = (*LANE_FIELDS, "activity_scope_id") if activity else LANE_FIELDS
    # The non-null owner equality exposes the existing owner/previous-session index.
    return and_(
        left.user_email == right.user_email,
        *(
            getattr(left, name).is_not_distinct_from(getattr(right, name))
            for name in names
            if name != "user_email"
        ),
    )


def own_execution(physical: Any, conversation: Any) -> Any:
    """Creation paths distinguish own historical roots from delegates/hooks.

    Compaction preserves parent/delegation identity; renew/reset/undo may change
    activity and previous links but remain own root executions. Fork origins use
    source_session_id, which deliberately does not disqualify a conversation root.
    """
    return and_(
        physical.user_email == conversation.user_email,
        physical.agent_id == conversation.agent_id,
        physical.parent_session_id.is_(None),
        physical.delegation_mode.is_(None),
        physical.delegation_task.is_(None),
    )


def membership(selectors: list[Any], inclusion: str, scope: Any) -> Any:
    """Bounded consumers use UNION identity, never path/depth in the dedup key."""
    result = _seeds(selectors, inclusion, scope).cte("usage_members", recursive=True)
    if inclusion == "self":
        return result
    edges = _lineage_edges(inclusion, scope)
    # Equality on the edge source lets PostgreSQL push each reached member into
    # every indexed UNION ALL arm. A correlated IN probe was evaluated once per
    # sessions row instead and exceeded the statement budget on real histories.
    return result.union(
        select(result.c.selector_kind, result.c.selector_id, edges.c.dst)
        .select_from(result)
        .join(edges, edges.c.src == result.c.session_id)
    )


FRONTIER_CHUNK = 1000


def direct_conversation_membership(selectors: list[Any], inclusion: str, scope: Any) -> Any:
    """Conversation-only work follows validated managed lineage, not fake sessions."""
    from cognis.store.usage_queries import authorized_selector

    seeds = [
        select(
            literal(item.kind).label("selector_kind"),
            literal(item.id).label("selector_id"),
            literal(item.id).label("conversation_id"),
        ).where(authorized_selector(literal(item.kind), literal(item.id), scope))
        for item in selectors
        if item.kind == "conversation"
    ]
    if not seeds:
        seeds = [
            select(
                literal("").label("selector_kind"),
                literal("").label("selector_id"),
                literal("").label("conversation_id"),
            ).where(false())
        ]
    seed = union_all(*seeds).subquery("usage_direct_roots")
    result = select(seed).cte("usage_direct_members", recursive=True)
    if inclusion != "inclusive" or scope.mode == "project":
        return result
    edges = managed_lineage(
        owner_email=ManagedConversationLink.user_email if scope.mode == "admin" else scope.actor,
        name="usage_direct_lineage",
    )
    return result.union(
        select(result.c.selector_kind, result.c.selector_id, edges.c.target_conversation_id)
        .select_from(result)
        .join(edges, edges.c.controller_conversation_id == result.c.conversation_id)
    )


async def materialized_membership(
    session: Any, selectors: list[Any], inclusion: str, scope: Any
) -> Any:
    """Resolve the same membership as :func:`membership` once, as a literal CTE.

    The recursive CTE rebuilds every lineage edge over all sessions on each
    recursion step, and each consuming statement re-evaluates it. Expanding the
    frontier with explicit source IDs lets every edge arm use its indexes, and
    consumers then read a bounded literal set instead of repeating the recursion.
    """
    from cognis.core.usage.query_service_errors import UsageQueryError

    seeds = (await session.execute(select(_seeds(selectors, inclusion, scope).subquery()))).all()
    rows = {(kind, identifier, physical) for kind, identifier, physical in seeds}
    if inclusion != "self" and rows:
        edges = _lineage_edges(inclusion, scope)
        adjacency: dict[str, set[str]] = defaultdict(set)
        seen = {physical for _, _, physical in rows}
        frontier = set(seen)
        while frontier:
            ordered = sorted(frontier)
            for start in range(0, len(ordered), FRONTIER_CHUNK):
                chunk = ordered[start : start + FRONTIER_CHUNK]
                for src, dst in (
                    await session.execute(
                        select(edges.c.src, edges.c.dst).where(edges.c.src.in_(chunk))
                    )
                ).all():
                    adjacency[src].add(dst)
            frontier = {dst for src in frontier for dst in adjacency[src]} - seen
            seen |= frontier
            if len(seen) > MAX_MEMBERS:
                raise UsageQueryError("query_budget_exceeded", "Sidebar membership exceeds 10000")
        by_selector: dict[tuple[str, str], set[str]] = defaultdict(set)
        for kind, identifier, physical in rows:
            by_selector[kind, identifier].add(physical)
        rows = set()
        for (kind, identifier), reached in by_selector.items():
            pending = list(reached)
            while pending:
                for dst in adjacency[pending.pop()]:
                    if dst not in reached:
                        reached.add(dst)
                        pending.append(dst)
            rows.update((kind, identifier, physical) for physical in reached)
    if len(rows) > MAX_MEMBERS:
        raise UsageQueryError("query_budget_exceeded", "Sidebar membership exceeds 10000")
    if not rows:
        return (
            select(
                literal("").label("selector_kind"),
                literal("").label("selector_id"),
                literal("").label("session_id"),
            )
            .where(false())
            .cte("usage_members")
        )
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for kind, identifier, physical in sorted(rows):
        grouped[kind, identifier].append(physical)
    # Portable literal set (SQLite has no aliased VALUES): an indexed primary-key
    # IN list per selector, never the recursion again.
    return union_all(
        *(
            select(
                literal(kind).label("selector_kind"),
                literal(identifier).label("selector_id"),
                Session.session_id,
            ).where(Session.session_id.in_(sessions))
            for (kind, identifier), sessions in grouped.items()
        )
    ).cte("usage_members")


def _seeds(selectors: list[Any], inclusion: str, scope: Any) -> Any:
    """Distinct directly selected executions for each selector."""
    from cognis.store.usage_queries import authorized_selector

    s, c = Session, Conversation
    seeds = []
    for item in {(item.kind, item.id): item for item in selectors}.values():
        predicate = (
            s.conversation_id == item.id
            if item.kind == "conversation"
            else or_(s.session_id == item.id, s.activity_scope_id == item.id)
            if item.kind == "activity"
            else s.session_id == item.id
        )
        if item.kind == "conversation" and inclusion != "self":
            predicate = and_(predicate, own_execution(s, c))
        seeds.append(
            select(
                literal(item.kind).label("selector_kind"),
                literal(item.id).label("selector_id"),
                s.session_id,
            )
            .join(c, c.conversation_id == s.conversation_id)
            .where(predicate, authorized_selector(literal(item.kind), literal(item.id), scope))
        )
    seed = union_all(*seeds).subquery("usage_seeds")
    return select(seed).distinct()


def _lineage_edges(inclusion: str, scope: Any) -> Any:
    """Directed session edges `src -> dst` admitted by the inclusion mode."""
    s, c = Session, Conversation
    parent, candidate = aliased(s), aliased(s)

    def edge(*conditions: Any) -> Any:
        return select(parent.session_id.label("src"), candidate.session_id.label("dst")).where(
            *conditions
        )

    arms = [
        edge(candidate.previous_session_id == parent.session_id, same_lane(parent, candidate)),
        edge(candidate.session_id == parent.previous_session_id, same_lane(parent, candidate)),
    ]
    if inclusion == "inclusive":
        arms.append(
            edge(
                candidate.parent_session_id == parent.session_id,
                candidate.user_email == parent.user_email,
                candidate.conversation_id == parent.conversation_id,
            )
        )
        owner = ManagedConversationLink.user_email if scope.mode == "admin" else scope.actor
        managed = managed_lineage(owner_email=owner, name="usage_managed_lineage")
        controller_conversation = aliased(c)
        arms.append(
            select(parent.session_id.label("src"), candidate.session_id.label("dst"))
            .select_from(managed)
            .join(
                controller_conversation,
                controller_conversation.conversation_id == managed.c.controller_conversation_id,
            )
            .join(parent, parent.conversation_id == managed.c.controller_conversation_id)
            .join(
                candidate,
                and_(
                    candidate.conversation_id == managed.c.target_conversation_id,
                    candidate.user_email == parent.user_email,
                ),
            )
            .where(
                managed.c.kind == "agent",
                parent.user_email == controller_conversation.user_email,
                or_(
                    parent.session_id == managed.c.controller_session_id,
                    and_(
                        managed.c.controller_session_id.is_(None),
                        own_execution(parent, controller_conversation),
                    ),
                ),
            )
        )
    return union_all(*arms).subquery("usage_lineage_edges")


async def invalid_memberships(
    session: Any, members: Any, scope: Any, *, inclusive: bool
) -> set[tuple[str, str]]:
    """One bounded metadata proof for the page, never one read per selector.

    Validate both ends of previous links, branching and cycles before accounting.
    No delegation task text or transcript data is returned to callers or logged.
    """
    from cognis.core.usage.query_service_errors import UsageQueryError
    from cognis.store.usage_queries import authorized_selector

    # Bound authorized selector identities before the more expensive metadata proof.
    identities = (
        await session.execute(
            select(members.c.selector_kind, members.c.selector_id, members.c.session_id)
            .where(authorized_selector(members.c.selector_kind, members.c.selector_id, scope))
            .limit(MAX_MEMBERS + 1)
        )
    ).all()
    if len(identities) > MAX_MEMBERS:
        raise UsageQueryError("query_budget_exceeded", "Sidebar membership exceeds 10000")
    del identities

    s = Session
    previous, successor = aliased(s), aliased(s)
    bounded = select(members).limit(MAX_MEMBERS + 1).cte("usage_proof_members")
    rows = list(
        (
            await session.execute(
                select(
                    bounded.c.selector_kind,
                    bounded.c.selector_id,
                    s.session_id,
                    s.conversation_id,
                    s.parent_session_id,
                    s.previous_session_id,
                    s.activity_scope_id,
                    previous.session_id.label("previous_exists"),
                    previous.activity_scope_id.label("previous_activity"),
                    same_lane(s, previous, activity=False).label("compatible_previous"),
                    select(successor.session_id)
                    .where(
                        successor.previous_session_id == s.session_id,
                        same_lane(s, successor),
                    )
                    .order_by(successor.session_id)
                    .offset(1)
                    .limit(1)
                    .correlate(s)
                    .scalar_subquery()
                    .label("branch"),
                )
                .select_from(bounded)
                .join(s, s.session_id == bounded.c.session_id)
                .outerjoin(previous, previous.session_id == s.previous_session_id)
                .where(authorized_selector(bounded.c.selector_kind, bounded.c.selector_id, scope))
                .limit(MAX_MEMBERS + 1)
            )
        ).mappings()
    )
    if len(rows) > MAX_MEMBERS:
        raise UsageQueryError("query_budget_exceeded", "Sidebar membership exceeds 10000")
    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
    for row in rows:
        groups[row["selector_kind"], row["selector_id"]][row["session_id"]] = row
    managed_edges = []
    if inclusive and rows:
        owner = ManagedConversationLink.user_email if scope.mode == "admin" else scope.actor
        links = managed_lineage(owner_email=owner, name="usage_proof_managed_lineage")
        conversations = sorted({row["conversation_id"] for row in rows})
        m = ManagedConversationLink
        controller = aliased(Conversation)
        controller_session = aliased(Session)
        managed_edges = list(
            (
                await session.execute(
                    select(
                        m.controller_conversation_id,
                        m.target_conversation_id,
                        m.controller_session_id,
                        links.c.link_id.label("validated"),
                        select(controller_session.session_id)
                        .where(
                            controller_session.session_id == m.controller_session_id,
                            controller_session.conversation_id == m.controller_conversation_id,
                            controller_session.agent_id == m.controller_agent_id,
                            controller_session.user_email == m.user_email,
                        )
                        .correlate(m)
                        .exists()
                        .label("controller_valid"),
                    )
                    .join(controller, controller.conversation_id == m.controller_conversation_id)
                    .outerjoin(links, links.c.link_id == m.link_id)
                    .where(
                        m.kind == "agent",
                        m.user_email == owner,
                        controller.user_email == m.user_email,
                        m.controller_conversation_id.in_(conversations),
                    )
                    .limit(MAX_MEMBERS + 1)
                )
            ).all()
        )
        if len(managed_edges) > MAX_MEMBERS:
            raise UsageQueryError("query_budget_exceeded", "Managed edge budget exceeds 10000")
    invalid = set()
    for key, values in groups.items():
        if any(
            not row["activity_scope_id"]
            or row["branch"] is not None
            or (
                row["previous_session_id"] is not None
                and (
                    row["previous_exists"] is None
                    or not row["compatible_previous"]
                    or not row["previous_activity"]
                )
            )
            for row in values.values()
        ):
            invalid.add(key)
            continue
        for edge_name in ("previous_session_id", "parent_session_id"):
            done = set()
            for identifier in values:
                path = set()
                current: str | None = identifier
                while current is not None and current in values and current not in done:
                    if current in path:
                        invalid.add(key)
                        break
                    path.add(current)
                    current = values[current][edge_name]
                done.update(path)
        if key in invalid:
            continue
        lengths: dict[str, int] = {}
        for identifier in values:
            chain: list[str] = []
            current = identifier
            while current is not None and current in values and current not in lengths:
                chain.append(current)
                predecessor = values[current]["previous_session_id"]
                if (
                    predecessor not in values
                    or values[predecessor]["activity_scope_id"]
                    != values[current]["activity_scope_id"]
                ):
                    current = None
                    break
                current = predecessor
            length = lengths.get(current, 0) if current is not None else 0
            for physical in reversed(chain):
                length += 1
                lengths[physical] = length
                if length > MAX_LANE_MEMBERS:
                    invalid.add(key)
        reached_conversations = {row["conversation_id"] for row in values.values()}
        adjacency: dict[str, set[str]] = {identifier: set() for identifier in reached_conversations}
        incoming = {identifier: 0 for identifier in reached_conversations}
        for parent, child, anchor, validated, controller_valid in managed_edges:
            if parent not in reached_conversations:
                continue
            if anchor is None and key[0] in {"execution", "session"}:
                invalid.add(key)
            if not validated or (anchor is not None and not controller_valid):
                invalid.add(key)
            if (
                parent in reached_conversations
                and child in reached_conversations
                and (anchor is None or anchor in values)
                and child not in adjacency[parent]
            ):
                adjacency[parent].add(child)
                incoming[child] += 1
        roots = [identifier for identifier, degree in incoming.items() if degree == 0]
        visited = 0
        while roots:
            identifier = roots.pop()
            visited += 1
            for child in adjacency[identifier]:
                incoming[child] -= 1
                if incoming[child] == 0:
                    roots.append(child)
        if visited != len(reached_conversations):
            invalid.add(key)
    return invalid
