"""Database migration commands."""

from __future__ import annotations

import typer

from cognis.config import load_config
from cognis.store.schema import upgrade_schema

database_app = typer.Typer(help="Database schema management")


@database_app.command("upgrade")  # type: ignore[untyped-decorator]
def upgrade() -> None:
    """Upgrade the configured database to the latest Alembic revision."""
    config = load_config()
    upgrade_schema(config.database_url)
    typer.echo("Database upgraded to the latest schema revision.")
