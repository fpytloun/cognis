from __future__ import annotations

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from cognis.store import queries
from cognis.store.app_contact_schema import repair_app_contact_titles
from cognis.store.models import Conversation, ManagedChannelBinding
from tests.unit.test_app_contact import RECIPIENT, enqueue
from tests.unit.test_app_contact import contact_env as contact_env


@pytest.mark.parametrize("upgrade_path", ["bootstrap", "alembic"])
async def test_public_titles_and_legacy_objective_repair_preserve_recipient_renames(
    contact_env, upgrade_path
):
    env = contact_env
    outbox, binding = await enqueue(env, managed=True)
    async with env.factory() as db:
        link = await queries.get_managed_conversation_link(db, binding.link_id)
        source_id = link.target_conversation_id
        source = await db.get(Conversation, source_id)
        source.title = "Friday evening"
        row = await db.get(ManagedChannelBinding, binding.binding_id)
        row.objective = "PRIVATE objective and managing instructions"
        await db.commit()
    await env.service.deliver(outbox.delivery_id)
    public = await env.service.view(binding.binding_id, RECIPIENT)
    assert public.title == "Friday evening"
    assert public.conversation_id
    assert "PRIVATE" not in public.model_dump_json()
    async with env.factory() as db:
        recipient = await db.get(Conversation, public.conversation_id)
        assert recipient.title == "Friday evening"
        recipient.title = "PRIVATE objective and managing instructions"
        recipient.title_source = "user"  # The generated format persisted by v177.
        await db.commit()

    def upgrade(connection):
        if upgrade_path == "bootstrap":
            repair_app_contact_titles(connection)
        else:
            migration = importlib.import_module(
                "cognis.store.migrations.versions.178_app_contact_titles"
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.downgrade()

    async with env.engine.begin() as connection:
        await connection.run_sync(upgrade)
        await connection.run_sync(upgrade)
    assert (await env.service.view(binding.binding_id, RECIPIENT)).title == "Friday evening"
    async with env.factory() as db:
        recipient = await db.get(Conversation, public.conversation_id)
        assert recipient.title_source == "app_contact"
        recipient.title = "My own evening plan"
        recipient.title_source = "user"
        await db.commit()
    async with env.engine.begin() as connection:
        await connection.run_sync(upgrade)
        await connection.run_sync(upgrade)
    assert (await env.service.view(binding.binding_id, RECIPIENT)).title == "My own evening plan"
    async with env.factory() as db:
        assert (await db.get(Conversation, source_id)).title == "Friday evening"
