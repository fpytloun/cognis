"""Direct conversation projections preserve real session totals across upgrades."""

from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select

from cognis.store.usage.models import UsageSessionTotalRow
from cognis.store.usage.schema import ensure_usage_schema, rollback_usage_direct_sessions
from tests.usage_storage_harness import usage_storage as usage_storage


async def test_direct_upgrade_bootstrap_preserves_populated_totals(usage_storage):
    h = usage_storage
    initial = h.event().model_dump()
    initial["context"]["attribution"]["resources"] = {
        "session_id": "original-session",
        "conversation_id": "original-conversation",
        "provenance": "controller",
    }
    assert h.writer.try_emit(type(h.event()).model_validate(initial))
    await h.pump()
    async with h.sessions() as session:
        original = (await session.scalars(select(UsageSessionTotalRow))).one()
        key = original.dimension_key
        cost = original.historical_cost
    async with h.engine.begin() as connection:
        await connection.run_sync(rollback_usage_direct_sessions)

        def upgrade(sync):
            migration = import_module(
                "cognis.store.migrations.versions.189_usage_direct_conversations"
            )
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
                migration.upgrade()
            ensure_usage_schema(sync)
            ensure_usage_schema(sync)
            assert "ix_usage_direct_conversation" in {
                item["name"] for item in inspect(sync).get_indexes("usage_session_totals")
            }

        await connection.run_sync(upgrade)
    async with h.sessions() as session:
        retained = (await session.scalars(select(UsageSessionTotalRow))).one()
        assert (retained.dimension_key, retained.historical_cost) == (key, cost)
        assert retained.session_kind == "cognis"
    data = h.event().model_dump()
    data["context"]["trace"]["attempt_id"] = "direct-attempt"
    data["context"]["trace"]["request_id"] = "direct-request"
    data["observation_id"] = "direct-terminal"
    data["context"]["attribution"]["resources"] = {
        "conversation_id": "direct-conversation",
        "provenance": "controller",
    }
    assert h.writer.try_emit(type(h.event()).model_validate(data))
    await h.pump()
    async with h.sessions() as session:
        direct = (
            await session.scalars(
                select(UsageSessionTotalRow).where(UsageSessionTotalRow.session_kind == "direct")
            )
        ).one()
        assert direct.conversation_id == "direct-conversation"
        assert direct.session_id is None and direct.client_session_id is None
        assert direct.finalized_count == 1
    async with h.engine.begin() as connection:
        with pytest.raises(ValueError, match="Cannot downgrade"):
            await connection.run_sync(rollback_usage_direct_sessions)
