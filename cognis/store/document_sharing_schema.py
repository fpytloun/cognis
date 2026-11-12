"""Idempotent schema installation for document people and recoverable public links."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from cognis.store.models import DocumentUserGrant


def ensure_document_sharing(connection: Connection) -> None:
    """Do not change the tokens of links created before recoverable links existed."""
    DocumentUserGrant.metadata.tables["document_user_grants"].create(connection, checkfirst=True)
    inspector = sa.inspect(connection)
    if inspector.has_table("feedback_shares"):
        columns = {column["name"] for column in inspector.get_columns("feedback_shares")}
        if "link_mode" not in columns:
            connection.execute(
                sa.text(
                    "ALTER TABLE feedback_shares ADD COLUMN link_mode VARCHAR NOT NULL DEFAULT 'view'"
                )
            )
        if "token_format" not in columns:
            connection.execute(
                sa.text(
                    "ALTER TABLE feedback_shares ADD COLUMN token_format VARCHAR NOT NULL DEFAULT 'random_v1'"
                )
            )
