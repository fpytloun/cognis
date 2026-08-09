"""Read-only schema compatibility checks and explicit Alembic upgrades."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

_ALEMBIC_INI = Path(__file__).parent / "migrations" / "alembic.ini"


def alembic_config(database_url: str) -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_INI.parent))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def expected_schema_heads() -> frozenset[str]:
    return frozenset(ScriptDirectory.from_config(alembic_config("sqlite:///unused")).get_heads())


@dataclass(frozen=True)
class SchemaStatus:
    compatible: bool
    current_heads: frozenset[str]
    expected_heads: frozenset[str]
    error: str | None = None


async def validate_schema(
    engine: AsyncEngine,
    *,
    expected_heads: frozenset[str] | None = None,
) -> SchemaStatus:
    expected = expected_heads or expected_schema_heads()
    try:
        async with engine.connect() as connection:
            current = frozenset(
                await connection.run_sync(
                    lambda sync_connection: MigrationContext.configure(
                        sync_connection
                    ).get_current_heads()
                )
            )
    except Exception as exc:
        return SchemaStatus(False, frozenset(), expected, f"Database unavailable: {exc}")
    if current != expected:
        return SchemaStatus(
            False,
            current,
            expected,
            f"Schema revision mismatch: current={sorted(current)}, expected={sorted(expected)}",
        )
    return SchemaStatus(True, current, expected)


def upgrade_schema(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
