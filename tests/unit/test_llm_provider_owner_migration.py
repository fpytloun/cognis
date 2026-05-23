from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _load_migration() -> ModuleType:
    migration_path = (
        Path(__file__).parents[2]
        / "cognis"
        / "store"
        / "migrations"
        / "versions"
        / "056_llm_provider_owner.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_056_llm_provider_owner", migration_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_llm_provider_owner_migration_rebuilds_sqlite_model_routing_pk(
    monkeypatch: object,
) -> None:
    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE llm_providers (
                    provider_id VARCHAR PRIMARY KEY,
                    display_name VARCHAR NOT NULL,
                    location VARCHAR NOT NULL,
                    backend VARCHAR NOT NULL,
                    config TEXT NOT NULL,
                    is_default BOOLEAN NOT NULL DEFAULT 0,
                    status VARCHAR NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE model_routing (
                    task_type VARCHAR PRIMARY KEY,
                    provider_id VARCHAR,
                    model VARCHAR NOT NULL,
                    config TEXT,
                    updated_at TIMESTAMP NOT NULL,
                    FOREIGN KEY(provider_id) REFERENCES llm_providers(provider_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO llm_providers (
                    provider_id, display_name, location, backend, config, status, created_at, updated_at
                ) VALUES (
                    'openai', 'OpenAI', 'controller', 'litellm', '{}', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO model_routing (task_type, provider_id, model, config, updated_at)
                VALUES ('default', 'openai', 'gpt-4o-mini', NULL, CURRENT_TIMESTAMP)
                """
            )
        )

        ops = Operations(MigrationContext.configure(conn))
        monkeypatch.setattr(migration, "op", ops)  # type: ignore[attr-defined]

        migration.upgrade()
        upgraded_columns = conn.execute(text("PRAGMA table_info(model_routing)")).mappings().all()
        upgraded_pk = [row["name"] for row in upgraded_columns if row["pk"]]
        assert upgraded_pk == ["route_id"]
        assert "owner_email" in {row["name"] for row in upgraded_columns}
        assert conn.execute(text("SELECT model FROM model_routing")).scalar_one() == "gpt-4o-mini"

        conn.execute(
            text(
                """
                INSERT INTO model_routing (route_id, task_type, owner_email, provider_id, model, config, updated_at)
                VALUES ('route_user_default', 'default', 'user@example.com', 'openai', 'gpt-5.4', NULL, CURRENT_TIMESTAMP)
                """
            )
        )
        migration.downgrade()
        downgraded_columns = conn.execute(text("PRAGMA table_info(model_routing)")).mappings().all()
        downgraded_pk = [row["name"] for row in downgraded_columns if row["pk"]]
        assert downgraded_pk == ["task_type"]
        assert "owner_email" not in {row["name"] for row in downgraded_columns}
        assert conn.execute(text("SELECT model FROM model_routing")).scalar_one() == "gpt-4o-mini"
