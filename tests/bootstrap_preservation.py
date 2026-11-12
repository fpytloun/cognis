"""Seed and compare release-era bootstrap data through a migration upgrade."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import sqlalchemy as sa

from cognis.store.models import Base

PRESERVED_TABLES = (
    "project_knowledgebases",
    "inference_routes",
    "agent_hooks",
    "feedback_subjects",
    "feedback_grants",
)


def seed_bootstrap_data(connection: sa.Connection) -> dict[str, list[dict[str, Any]]]:
    owner = "bootstrap-owner@cognis.test"
    subject_id = f"fbs_{uuid4().hex}"
    rows = {
        "users": {"email": owner},
        "projects": {"project_id": "bootstrap-project", "owner_email": owner, "name": "Preserved"},
        "knowledgebases": {
            "knowledgebase_id": "bootstrap-knowledge",
            "owner_email": owner,
            "name": "Preserved knowledge",
        },
        "project_knowledgebases": {
            "project_id": "bootstrap-project",
            "knowledgebase_id": "bootstrap-knowledge",
            "assigned_by": owner,
        },
        "inference_routes": {
            "route_id": "bootstrap-route",
            "owner_email": owner,
            "name": "Preserved route",
            "revision": 7,
            "config": {"targets": ["preserved-model"]},
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        "agent_hooks": {
            "hook_id": "bootstrap-hook",
            "owner_email": owner,
            "revision": 3,
            "definition": {"name": "Preserved hook", "trigger": "conversation_idle"},
        },
        "feedback_subjects": {
            "subject_id": subject_id,
            "source_key": f"bootstrap-source-{uuid4().hex}",
            "owner_email": owner,
            "source": {"kind": "document", "content_ref": f"art_{uuid4().hex}"},
            "content_digest": uuid4().hex,
            "version": 3,
        },
        "feedback_grants": {
            "subject_id": subject_id,
            "principal_email": owner,
            "version": 7,
            "capabilities": {"read": True, "comment": False},
        },
    }
    for name, values in rows.items():
        connection.execute(Base.metadata.tables[name].insert().values(**values))
    return read_bootstrap_data(connection)


def read_bootstrap_data(connection: sa.Connection) -> dict[str, list[dict[str, Any]]]:
    return {
        name: [
            dict(row)
            for row in connection.execute(sa.select(Base.metadata.tables[name])).mappings()
        ]
        for name in PRESERVED_TABLES
    }
