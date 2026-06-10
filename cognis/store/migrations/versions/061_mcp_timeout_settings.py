"""Replace OAuth MCP timeout setting with unified MCP timeout settings.

Revision ID: 061_mcp_timeout_settings
Revises: 060_mcp_oauth
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "061_mcp_timeout_settings"
down_revision = "060_mcp_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    settings = sa.table(
        "settings",
        sa.column("key", sa.String()),
        sa.column("category", sa.String()),
        sa.column("value", sa.JSON()),
        sa.column("updated_at", sa.TIMESTAMP(timezone=True)),
    )
    bind.execute(settings.delete().where(settings.c.key == "mcp.oauth_tool_timeout_seconds"))
    _insert_setting_if_missing(bind, settings, "mcp.tool_timeout_seconds", 300)
    _insert_setting_if_missing(bind, settings, "mcp.connect_timeout_seconds", 15)


def downgrade() -> None:
    bind = op.get_bind()
    settings = sa.table(
        "settings",
        sa.column("key", sa.String()),
        sa.column("category", sa.String()),
        sa.column("value", sa.JSON()),
        sa.column("updated_at", sa.TIMESTAMP(timezone=True)),
    )
    bind.execute(
        settings.delete().where(
            settings.c.key.in_(("mcp.tool_timeout_seconds", "mcp.connect_timeout_seconds"))
        )
    )
    _insert_setting_if_missing(bind, settings, "mcp.oauth_tool_timeout_seconds", 300)


def _insert_setting_if_missing(
    bind: sa.engine.Connection,
    settings: sa.TableClause,
    key: str,
    value: int,
) -> None:
    exists = bind.execute(
        sa.select(sa.literal(1)).select_from(settings).where(settings.c.key == key).limit(1)
    ).first()
    if exists is not None:
        return
    bind.execute(
        settings.insert().values(
            key=key,
            category="mcp",
            value=value,
            updated_at=datetime.now(UTC),
        )
    )
