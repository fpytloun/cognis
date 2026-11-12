"""Alembic migration environment.

Supports both sync (``sqlite:///``) and async (``sqlite+aiosqlite:///``)
database URLs.  When the configured URL uses an async driver, migrations
run inside ``run_async_migrations()`` via ``asyncio.run()``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from cognis.store.migrations.compat import normalize_legacy_profile_override_revision
from cognis.store.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _is_async_url() -> bool:
    url = config.get_main_option("sqlalchemy.url") or ""
    return "+aiosqlite" in url or "+asyncpg" in url


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online_sync() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _do_run_migrations(connection)


async def run_migrations_online_async() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def _do_run_migrations(connection: Connection) -> None:
    normalize_legacy_profile_override_revision(connection)
    connection.commit()
    legacy_alter_table = None
    if connection.dialect.name == "sqlite":
        # Old stamps may coexist with bootstrap triggers referencing batch-recreated tables.
        legacy_alter_table = int(
            connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one()
        )
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.commit()
    try:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if legacy_alter_table is not None:
            connection.exec_driver_sql(f"PRAGMA legacy_alter_table={legacy_alter_table}")


provided_connection = config.attributes.get("connection")

if context.is_offline_mode():
    run_migrations_offline()
elif provided_connection is not None:
    _do_run_migrations(provided_connection)
elif _is_async_url():
    asyncio.run(run_migrations_online_async())
else:
    run_migrations_online_sync()
