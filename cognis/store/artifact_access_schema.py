"""Idempotent agent ownership migration shared by bootstrap and Alembic."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, cast

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Connection

from cognis.store.models import ArtifactGrantRow

logger = logging.getLogger(__name__)


def ensure_artifact_access_schema(connection: Connection) -> None:
    """Preserve provenance while making content ownership independent of its source."""
    from cognis.store.document_write_schema import ensure_document_write_schema

    # Current ORM grant reads also run during older bootstrap/migration paths.
    ensure_document_write_schema(connection)
    tables = set(sa.inspect(connection).get_table_names())
    # Null is initialized ownership too; infer only while adding owner columns.
    initialize_owners: set[str] = set()
    for table, columns in (
        ("sessions", ("artifact_scope_id",)),
        ("artifacts", ("owner_agent_id",)),
        ("deliverables", ("owner_email", "owner_agent_id")),
    ):
        if table not in tables:
            continue
        existing = {c["name"] for c in sa.inspect(connection).get_columns(table)}
        for column in columns:
            if column not in existing:
                connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR"))
                if column in {"owner_email", "owner_agent_id"}:
                    initialize_owners.add(table)
    cast(sa.Table, ArtifactGrantRow.__table__).create(connection, checkfirst=True)
    if (
        not {"artifacts", "deliverables", "conversations", "sessions", "tasks", "step_runs"}
        <= tables
    ):
        return
    if "deliverables" in initialize_owners:
        connection.execute(
            sa.text("""
        UPDATE deliverables SET owner_email = COALESCE(
            owner_email,
            (SELECT t.created_by FROM tasks t JOIN step_runs r ON r.task_id = t.task_id
             WHERE r.step_run_id = deliverables.step_run_id),
            (SELECT c.user_email FROM conversations c
             WHERE c.conversation_id = deliverables.conversation_id)),
        owner_agent_id = COALESCE(
            owner_agent_id,
            (SELECT t.agent_id FROM tasks t JOIN step_runs r ON r.task_id = t.task_id
             WHERE r.step_run_id = deliverables.step_run_id
             AND (deliverables.owner_email IS NULL OR t.created_by = deliverables.owner_email)),
            (SELECT s.agent_id FROM sessions s WHERE s.session_id = deliverables.session_id
             AND (deliverables.owner_email IS NULL OR s.user_email = deliverables.owner_email)),
            (SELECT c.agent_id FROM conversations c
             WHERE c.conversation_id = deliverables.conversation_id
             AND (deliverables.owner_email IS NULL OR c.user_email = deliverables.owner_email)))
        WHERE owner_email IS NULL OR owner_agent_id IS NULL
    """)
        )
    if "artifacts" in initialize_owners:
        connection.execute(
            sa.text("""
        UPDATE artifacts SET owner_agent_id = COALESCE(
            (SELECT d.owner_agent_id FROM deliverables d
             WHERE d.deliverable_id = artifacts.artifact_id AND d.owner_email = artifacts.owner_email),
            (SELECT s.agent_id FROM sessions s
             WHERE s.session_id = artifacts.session_id AND s.user_email = artifacts.owner_email),
            (SELECT c.agent_id FROM conversations c
             WHERE c.conversation_id = artifacts.conversation_id AND c.user_email = artifacts.owner_email))
        WHERE owner_agent_id IS NULL
    """)
        )
    connection.execute(
        sa.text("""
        UPDATE artifacts SET conversation_id = (
            SELECT d.conversation_id FROM deliverables d WHERE d.deliverable_id = artifacts.artifact_id),
        session_id = (
            SELECT d.session_id FROM deliverables d WHERE d.deliverable_id = artifacts.artifact_id)
        WHERE purpose = 'conversation_deliverable' AND EXISTS (
            SELECT 1 FROM deliverables d WHERE d.deliverable_id = artifacts.artifact_id
            AND d.owner_email = artifacts.owner_email)
    """)
    )
    for table in ("artifacts", "deliverables"):
        connection.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_agent_owner "
                f"ON {table} (owner_email, owner_agent_id)"
            )
        )
    foreign_keys = [
        fk
        for fk in sa.inspect(connection).get_foreign_keys("deliverables")
        if fk["constrained_columns"] in (["conversation_id"], ["session_id"], ["step_run_id"])
        and fk.get("options", {}).get("ondelete", "").upper() == "CASCADE"
    ]
    if foreign_keys:
        ops = Operations(MigrationContext.configure(connection))
        naming = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
        with ops.batch_alter_table("deliverables", naming_convention=naming) as batch:
            for fk in foreign_keys:
                column = fk["constrained_columns"][0]
                name = fk["name"] or f"fk_deliverables_{column}_{fk['referred_table']}"
                batch.drop_constraint(name, type_="foreignkey")
                batch.create_foreign_key(
                    name,
                    fk["referred_table"],
                    [column],
                    fk["referred_columns"],
                    ondelete="SET NULL",
                )
    if {"agents", "managed_conversation_links"} <= tables:
        _backfill_entitlements(connection, tables, initialize_owners)


def _backfill_entitlements(
    connection: Connection, tables: set[str], initialize_owners: set[str]
) -> None:
    from cognis.store.models import (
        Agent,
        ArtifactRecordRow,
        Conversation,
        DeliverableRow,
        DirectTurnRequestRow,
        ManagedConversationLink,
        Session,
        StepRun,
        Task,
    )

    batch_size = 100

    def load(model: Any, *names: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                sa.select(*(getattr(model, name) for name in names))
            ).mappings()
        ]

    def write_batches(statement: Any, rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(rows), batch_size):
            connection.execute(statement, rows[start : start + batch_size])

    # All writes stay on the caller's connection: batches never commit or release
    # the outer Alembic/bootstrap transaction, so a failure rolls back the repair.
    sessions = {
        row["session_id"]: row
        for row in load(
            Session,
            "session_id",
            "user_email",
            "agent_id",
            "conversation_id",
            "parent_session_id",
            "previous_session_id",
            "source_session_id",
            "artifact_scope_id",
        )
    }
    scope_updates = []
    scope_roots: dict[str, dict[str, Any]] = {}
    for row in sessions.values():
        if row["artifact_scope_id"]:
            continue
        root, seen = row, {row["session_id"]}
        cyclic = False
        while root["previous_session_id"]:
            cached = scope_roots.get(root["session_id"])
            if cached is not None:
                root = cached
                break
            previous = sessions.get(root["previous_session_id"])
            if previous is not None and previous["session_id"] in seen:
                cyclic = True
                break
            if previous is None or any(
                previous[key] != row[key]
                for key in ("user_email", "agent_id", "conversation_id", "parent_session_id")
            ):
                break
            root = previous
            seen.add(root["session_id"])
        # Cache only terminating chains; corrupt cycles retain the original
        # start-dependent root selection rather than acquiring a new scope.
        if not cyclic:
            for session_id in seen:
                scope_roots[session_id] = root
        row["artifact_scope_id"] = root["artifact_scope_id"] or root["session_id"]
        scope_updates.append(dict(key_id=row["session_id"], scope=row["artifact_scope_id"]))
    write_batches(
        sa.update(Session)
        .where(Session.session_id == sa.bindparam("key_id"))
        .values(artifact_scope_id=sa.bindparam("scope")),
        scope_updates,
    )
    conversations = {
        row["conversation_id"]: row
        for row in load(Conversation, "conversation_id", "user_email", "agent_id")
    }
    tasks = {
        row["task_id"]: row
        for row in load(
            Task, "task_id", "created_by", "agent_id", "created_by_agent_id", "source_session_id"
        )
    }
    runs = {
        row["step_run_id"]: row for row in load(StepRun, "step_run_id", "task_id", "session_id")
    }
    # scalar(select(Task).join(StepRun)) uses the first matching row, not all
    # tasks for a session. Preserve that distinction for ownership and ancestry.
    session_tasks: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs.values():
        task = tasks.get(run["task_id"])
        if task is not None:
            session_tasks.setdefault((task["created_by"], run["session_id"]), task)

    edges: dict[tuple[str, str], set[str]] = defaultdict(set)
    seeds: dict[tuple[str, str], set[str]] = defaultdict(set)

    def edge(owner: str, parent: str | None, child: str) -> None:
        if parent is not None:
            edges[owner, parent].add(child)

    for row in sessions.values():
        owner, child = row["user_email"], f"s:{row['session_id']}"
        edge(owner, f"c:{row['conversation_id']}", child)
        for key in ("parent_session_id", "previous_session_id", "source_session_id"):
            if row[key] is not None:
                edge(owner, f"s:{row[key]}", child)
    for row in conversations.values():
        seeds[row["user_email"], row["agent_id"]].add(f"c:{row['conversation_id']}")
    for task in tasks.values():
        owner, child = task["created_by"], f"t:{task['task_id']}"
        if task["source_session_id"] is not None:
            edge(owner, f"s:{task['source_session_id']}", child)
        for key in ("agent_id", "created_by_agent_id"):
            if task[key] is not None:
                seeds[owner, task[key]].add(child)
    for run in runs.values():
        task = tasks.get(run["task_id"])
        if task is not None and run["session_id"] is not None:
            edge(task["created_by"], f"t:{task['task_id']}", f"s:{run['session_id']}")

    links = load(
        ManagedConversationLink,
        "link_id",
        "user_email",
        "parent_link_id",
        "root_link_id",
        "depth",
        "controller_conversation_id",
        "controller_agent_id",
        "controller_session_id",
        "target_conversation_id",
        "target_agent_id",
    )
    valid_links = {}
    for link in links:
        controller = conversations.get(link["controller_conversation_id"])
        target = conversations.get(link["target_conversation_id"])
        if (
            controller is not None
            and target is not None
            and controller["user_email"] == target["user_email"] == link["user_email"]
            and controller["agent_id"] == link["controller_agent_id"]
            and target["agent_id"] == link["target_agent_id"]
        ):
            valid_links[link["link_id"]] = link
    roots = {
        key: link
        for key, link in valid_links.items()
        if link["parent_link_id"] is None and link["root_link_id"] == key and link["depth"] == 1
    }
    for link in valid_links.values():
        parent = roots.get(link["parent_link_id"])
        if link["link_id"] not in roots and not (
            parent is not None
            and link["user_email"] == parent["user_email"]
            and link["root_link_id"] == parent["root_link_id"]
            and link["depth"] == parent["depth"] + 1
            and link["depth"] <= 2
            and link["controller_conversation_id"] == parent["target_conversation_id"]
            and link["controller_agent_id"] == parent["target_agent_id"]
        ):
            continue
        owner, child = link["user_email"], f"c:{link['target_conversation_id']}"
        edge(owner, f"c:{link['controller_conversation_id']}", child)
        if link["controller_session_id"] is not None:
            edge(owner, f"s:{link['controller_session_id']}", child)

    recipients: dict[tuple[str, str], set[str]] = defaultdict(set)
    agents = load(Agent, "agent_id", "owner_email")
    for agent in agents:
        owner, agent_id = agent["owner_email"], agent["agent_id"]
        if agent_id.startswith("system:"):
            continue
        pending = list(seeds[owner, agent_id])
        reachable_nodes: set[str] = set()
        while pending:
            node = pending.pop()
            if node in reachable_nodes:
                continue
            reachable_nodes.add(node)
            recipients[owner, node].add(agent_id)
            pending.extend(edges.get((owner, node), ()))

    ancestor_scopes: dict[str, set[str]] = {}

    def scopes(producer: dict[str, Any]) -> set[str]:
        key = producer["session_id"]
        if key not in ancestor_scopes:
            pending, seen, result = [producer], set(), set()
            while pending:
                item = pending.pop()
                if item["session_id"] in seen or item["user_email"] != producer["user_email"]:
                    continue
                seen.add(item["session_id"])
                result.add(f"scope:{item['artifact_scope_id'] or item['session_id']}")
                task = session_tasks.get((producer["user_email"], item["session_id"]))
                for source in (
                    item["parent_session_id"],
                    item["previous_session_id"],
                    item["source_session_id"],
                    task["source_session_id"] if task else None,
                ):
                    parent = sessions.get(source) if source else None
                    if parent is not None:
                        pending.append(parent)
            ancestor_scopes[key] = result
        return ancestor_scopes[key]

    existing = set(
        connection.execute(
            sa.select(
                ArtifactGrantRow.owner_email,
                ArtifactGrantRow.content_ref,
                ArtifactGrantRow.agent_id,
                ArtifactGrantRow.source,
            )
        ).tuples()
    )
    output_refs = {(owner, ref) for owner, ref, _, source in existing if source == "output"}
    grants: list[dict[str, Any]] = []
    grant_counts: dict[str, int] = defaultdict(int)

    def grant(owner: str, ref: str, recipient: str, source: str) -> None:
        key = (owner, ref, recipient, source)
        if key in existing:
            return
        existing.add(key)
        if source == "output":
            output_refs.add((owner, ref))
        grants.append(dict(owner_email=owner, content_ref=ref, agent_id=recipient, source=source))
        grant_counts[source] += 1
        if len(grants) == batch_size:
            connection.execute(sa.insert(ArtifactGrantRow), grants)
            grants.clear()

    artifacts = {}
    content_count = 0
    for model, id_column in (
        (DeliverableRow, "deliverable_id"),
        (ArtifactRecordRow, "artifact_id"),
    ):
        names = [id_column, "owner_email", "owner_agent_id", "session_id", "conversation_id"]
        if model is DeliverableRow:
            names.append("step_run_id")
        rows = load(model, *names)
        if model is ArtifactRecordRow:
            artifacts = {row[id_column]: row for row in rows}
        # Snapshot candidates per table, as the old backfill did. Deliverable
        # output grants suppress the same reference in the later artifact pass.
        candidates = [
            row for row in rows if (row["owner_email"], row[id_column]) not in output_refs
        ]
        owner_updates = []
        for row in candidates:
            owner, ref = row["owner_email"], row[id_column]
            if not owner:
                continue
            producer = sessions.get(row["session_id"]) if row["session_id"] else None
            if producer is not None and producer["user_email"] != owner:
                continue
            if model.__tablename__ in initialize_owners and (
                not row["owner_agent_id"] or row["owner_agent_id"].startswith("system:")
            ):
                task = session_tasks.get((owner, row["session_id"])) if row["session_id"] else None
                conversation = (
                    conversations.get(row["conversation_id"]) if row["conversation_id"] else None
                )
                identities = [
                    task["agent_id"] if task else None,
                    producer["agent_id"] if producer else None,
                    conversation["agent_id"]
                    if conversation and conversation["user_email"] == owner
                    else None,
                ]
                cursor, seen = producer, set()
                while cursor is not None and cursor["session_id"] not in seen:
                    seen.add(cursor["session_id"])
                    identities.append(cursor["agent_id"])
                    cursor = (
                        sessions.get(cursor["parent_session_id"])
                        if cursor["parent_session_id"]
                        else None
                    )
                    if cursor is not None and cursor["user_email"] != owner:
                        break
                row["owner_agent_id"] = next(
                    (item for item in identities if item and not item.startswith("system:")),
                    None,
                )
                owner_updates.append(dict(key_id=ref, agent_owner=row["owner_agent_id"]))
            targets = set(scopes(producer)) if producer is not None else set()
            if row["owner_agent_id"]:
                targets.add(row["owner_agent_id"])
            if row["session_id"] is not None:
                targets.update(recipients.get((owner, f"s:{row['session_id']}"), ()))
            elif row["conversation_id"] is not None:
                targets.update(recipients.get((owner, f"c:{row['conversation_id']}"), ()))
            if model is DeliverableRow:
                content_run = runs.get(row["step_run_id"])
                if content_run is not None:
                    targets.update(recipients.get((owner, f"t:{content_run['task_id']}"), ()))
            for recipient in sorted(targets):
                grant(owner, ref, recipient, "output")
            content_count += 1
        write_batches(
            sa.update(model)
            .where(getattr(model, id_column) == sa.bindparam("key_id"))
            .values(owner_agent_id=sa.bindparam("agent_owner")),
            owner_updates,
        )

    if "direct_turn_requests" in tables:
        for turn in connection.execute(
            sa.select(
                DirectTurnRequestRow.conversation_id,
                DirectTurnRequestRow.user_id,
                DirectTurnRequestRow.agent_id,
                DirectTurnRequestRow.payload,
            ).where(DirectTurnRequestRow.started_at.is_not(None))
        ).mappings():
            conversation = conversations.get(turn["conversation_id"])
            if (
                conversation is None
                or conversation["user_email"] != turn["user_id"]
                or conversation["agent_id"] != turn["agent_id"]
            ):
                continue
            for item in (turn["payload"] or {}).get("attachments") or []:
                ref = item.get("artifact_id") if isinstance(item, dict) else None
                artifact = artifacts.get(ref) if ref else None
                if (
                    ref is not None
                    and artifact is not None
                    and artifact["owner_email"] == turn["user_id"]
                ):
                    grant(turn["user_id"], ref, turn["agent_id"], "input")
    if grants:
        connection.execute(sa.insert(ArtifactGrantRow), grants)
    logger.info(
        "Artifact entitlement backfill: sessions=%d contents=%d output_grants=%d input_grants=%d",
        len(scope_updates),
        content_count,
        grant_counts["output"],
        grant_counts["input"],
    )
