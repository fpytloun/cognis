from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import cognis.core.agent_management as agent_management
from cognis.api.routes.agents import _validate_agent_definition_payload
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.agent_management import (
    AgentManagementDependencies,
    AgentManagementError,
    handle_agent_management_action,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    AuditLog,
    ChannelAccountRow,
    Conversation,
    Schedule,
    Session,
    Task,
)
from cognis.store.queries import (
    create_agent,
    create_channel_account,
    create_conversation,
    create_llm_provider,
    create_managed_conversation_link,
    create_schedule,
    create_session,
    create_task,
    create_user,
    get_agent,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL


class _EventBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


async def _deps(tmp_path: Path) -> tuple[AgentManagementDependencies, _EventBus]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/profiles.db")
    await run_schema_bootstrap(engine)
    event_bus = _EventBus()
    deps = AgentManagementDependencies(
        session_factory=create_session_factory(engine),
        event_bus=event_bus,
    )
    async with deps.session_factory() as session:
        await create_user(session, email="owner@example.com", name="Owner", password_hash="hash")
        await create_user(session, email="other@example.com", name="Other", password_hash="hash")
        await create_agent(
            session,
            agent_id="manager",
            owner_email="owner@example.com",
            name="Manager",
            status="active",
        )
        await create_agent(
            session,
            agent_id="target",
            owner_email="owner@example.com",
            name="Target",
            status="active",
            llm_config={"provider_id": "provider-a"},
        )
        await create_agent(
            session,
            agent_id="other",
            owner_email="other@example.com",
            name="Other",
            status="active",
        )
        await create_llm_provider(
            session,
            provider_id="provider-a",
            display_name="Provider A",
            location="controller",
            backend="litellm",
            config={"models": ["model-a"]},
        )
        await session.commit()
    return deps, event_bus


async def _action(
    deps: AgentManagementDependencies, arguments: dict[str, object]
) -> dict[str, object]:
    return await handle_agent_management_action(
        deps=deps,
        actor_email="owner@example.com",
        current_agent_id="manager",
        arguments=arguments,
    )


def test_runtime_profile_crud_clear_default_and_audit_sanitization(tmp_path: Path) -> None:
    async def scenario() -> None:
        deps, event_bus = await _deps(tmp_path)
        created = await _action(
            deps,
            {
                "action": "runtime_profiles_create",
                "agent_id": "target",
                "profile_id": "fast",
                "profile": {
                    "description": "Fast requests",
                    "provider_id": "provider-a",
                    "model": "model-a",
                    "system_prompt_extra": "never store this in audit",
                },
            },
        )
        assert created["status"] == "created"
        revision = created["updated_at"]
        assert isinstance(revision, str)

        updated = await _action(
            deps,
            {
                "action": "runtime_profiles_update",
                "agent_id": "target",
                "profile_id": "fast",
                "expected_updated_at": revision,
                "profile": {"system_prompt_extra": None, "reasoning_effort": "low"},
            },
        )
        assert updated["profile"]["system_prompt_extra"] is None  # type: ignore[index]
        assert updated["profile"]["model"] == "model-a"  # type: ignore[index]

        defaulted = await _action(
            deps,
            {
                "action": "runtime_profiles_default_set",
                "agent_id": "target",
                "default_profile_id": "fast",
            },
        )
        assert defaulted["configured_default_profile_id"] == "fast"
        cleared = await _action(
            deps,
            {
                "action": "runtime_profiles_default_set",
                "agent_id": "target",
                "default_profile_id": None,
            },
        )
        assert cleared["configured_default_profile_id"] is None

        deleted = await _action(
            deps,
            {
                "action": "runtime_profiles_delete",
                "agent_id": "target",
                "profile_id": "fast",
            },
        )
        assert deleted["status"] == "deleted"
        assert len(event_bus.events) == 5
        async with deps.session_factory() as session:
            audit_rows = (await session.execute(AuditLog.__table__.select())).all()
        assert all("never store this in audit" not in str(row) for row in audit_rows)

    asyncio.run(scenario())


def test_runtime_profile_validation_concurrency_and_authorization(tmp_path: Path) -> None:
    async def scenario() -> None:
        deps, _events = await _deps(tmp_path)
        with pytest.raises(AgentManagementError, match="Resource access denied"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_list",
                    "agent_id": "other",
                },
            )
        with pytest.raises(AgentManagementError, match="Invalid provider_id"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_create",
                    "agent_id": "target",
                    "profile_id": "bad-provider",
                    "profile": {"provider_id": "missing"},
                },
            )
        created = await _action(
            deps,
            {
                "action": "runtime_profiles_create",
                "agent_id": "target",
                "profile_id": "fast",
                "profile": {"provider_id": "provider-a", "model": "model-a"},
            },
        )
        with pytest.raises(AgentManagementError, match="conflict"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_update",
                    "agent_id": "target",
                    "profile_id": "fast",
                    "expected_updated_at": "2000-01-01T00:00:00+00:00",
                    "profile": {"enabled": False},
                },
            )
        with pytest.raises(AgentManagementError, match="Invalid model"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_update",
                    "agent_id": "target",
                    "profile_id": "fast",
                    "expected_updated_at": created["updated_at"],
                    "profile": {"model": "unknown"},
                },
            )

    asyncio.run(scenario())


def test_runtime_profile_delete_is_blocked_by_default_and_live_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        deps, _events = await _deps(tmp_path)
        await _action(
            deps,
            {
                "action": "runtime_profiles_create",
                "agent_id": "target",
                "profile_id": "fast",
                "profile": {},
            },
        )
        await _action(
            deps,
            {
                "action": "runtime_profiles_default_set",
                "agent_id": "target",
                "default_profile_id": "fast",
            },
        )
        with pytest.raises(AgentManagementError, match="configured default"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_delete",
                    "agent_id": "target",
                    "profile_id": "fast",
                },
            )
        await _action(
            deps,
            {
                "action": "runtime_profiles_default_set",
                "agent_id": "target",
                "default_profile_id": None,
            },
        )
        async with deps.session_factory() as session:
            await create_task(
                session,
                created_by="owner@example.com",
                agent_id="target",
                agent_profile_id="fast",
                title="Live",
                status="queued",
            )
            await session.commit()
        with pytest.raises(AgentManagementError, match="tasks=1"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_delete",
                    "agent_id": "target",
                    "profile_id": "fast",
                },
            )

    asyncio.run(scenario())


def test_runtime_profile_delete_migrates_every_live_reference_atomically(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        deps, _events = await _deps(tmp_path)
        await _action(
            deps,
            {
                "action": "runtime_profiles_create",
                "agent_id": "target",
                "profile_id": "legacy",
                "profile": {},
            },
        )
        replacement = await _action(
            deps,
            {
                "action": "runtime_profiles_create",
                "agent_id": "target",
                "profile_id": "final",
                "profile": {},
            },
        )
        async with deps.session_factory() as session:
            conversation = await create_conversation(
                session,
                user_email="owner@example.com",
                agent_id="target",
                agent_profile_id="legacy",
                context_type="agent_work",
            )
            await create_session(
                session,
                conversation.conversation_id,
                "owner@example.com",
                "target",
                agent_profile_id="legacy",
            )
            await create_task(
                session,
                created_by="owner@example.com",
                agent_id="target",
                agent_profile_id="legacy",
                title="Legacy task",
            )
            await create_schedule(
                session,
                name="Legacy schedule",
                agent_id="target",
                agent_profile_id="legacy",
                task_template={},
                created_by="owner@example.com",
            )
            await create_channel_account(
                session,
                channel_type="signal",
                display_name="Target channel",
                agent_id="target",
                default_agent_profile_id="legacy",
                user_email="owner@example.com",
            )
            other_conversation = await create_conversation(
                session,
                user_email="other@example.com",
                agent_id="other",
                agent_profile_id="legacy",
                context_type="agent_work",
            )
            await create_session(
                session,
                other_conversation.conversation_id,
                "other@example.com",
                "other",
                agent_profile_id="legacy",
            )
            await create_task(
                session,
                created_by="other@example.com",
                agent_id="other",
                agent_profile_id="legacy",
                title="Other task",
            )
            await create_schedule(
                session,
                name="Other schedule",
                agent_id="other",
                agent_profile_id="legacy",
                task_template={},
                created_by="other@example.com",
            )
            await create_channel_account(
                session,
                channel_type="slack",
                display_name="Other channel",
                agent_id="other",
                default_agent_profile_id="legacy",
                user_email="other@example.com",
            )
            await session.commit()

        deleted = await _action(
            deps,
            {
                "action": "runtime_profiles_delete",
                "agent_id": "target",
                "profile_id": "legacy",
                "replacement_profile_id": "final",
                "expected_updated_at": replacement["updated_at"],
            },
        )
        assert deleted["migrated_references"] == {
            "conversations": 1,
            "sessions": 1,
            "tasks": 1,
            "schedules": 1,
            "channel_accounts": 1,
        }
        assert deleted["replacement_profile_id"] == "final"
        assert "legacy" not in deleted["profiles"]

        async with deps.session_factory() as session:
            for model, profile_column in (
                (Conversation, Conversation.agent_profile_id),
                (Session, Session.agent_profile_id),
                (Task, Task.agent_profile_id),
                (Schedule, Schedule.agent_profile_id),
                (ChannelAccountRow, ChannelAccountRow.default_agent_profile_id),
            ):
                target_values = (
                    (
                        await session.execute(
                            select(profile_column).where(model.agent_id == "target")
                        )
                    )
                    .scalars()
                    .all()
                )
                other_values = (
                    (await session.execute(select(profile_column).where(model.agent_id == "other")))
                    .scalars()
                    .all()
                )
                assert target_values == ["final"]
                assert other_values == ["legacy"]
            audit = (
                (
                    await session.execute(
                        select(AuditLog)
                        .where(AuditLog.event_type == "agent_management_tool")
                        .order_by(AuditLog.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            assert audit is not None
            assert audit.details["profile_id"] == "legacy"
            assert audit.details["replacement_profile_id"] == "final"
            assert audit.details["migrated_references"]["tasks"] == 1

    asyncio.run(scenario())


def test_runtime_profile_delete_replacement_validation_and_default_protection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        deps, _events = await _deps(tmp_path)
        for profile_id, enabled in (
            ("legacy", True),
            ("final", True),
            ("disabled", False),
        ):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_create",
                    "agent_id": "target",
                    "profile_id": profile_id,
                    "profile": {"enabled": enabled},
                },
            )
        for replacement_profile_id, message in (
            ("legacy", "different"),
            ("missing", "same agent"),
            ("disabled", "enabled"),
        ):
            with pytest.raises(AgentManagementError, match=message):
                await _action(
                    deps,
                    {
                        "action": "runtime_profiles_delete",
                        "agent_id": "target",
                        "profile_id": "legacy",
                        "replacement_profile_id": replacement_profile_id,
                    },
                )

        await _action(
            deps,
            {
                "action": "runtime_profiles_default_set",
                "agent_id": "target",
                "default_profile_id": "legacy",
            },
        )
        with pytest.raises(AgentManagementError, match="configured default"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_delete",
                    "agent_id": "target",
                    "profile_id": "legacy",
                    "replacement_profile_id": "final",
                },
            )

    asyncio.run(scenario())


def test_runtime_profile_delete_rolls_back_migration_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        deps, _events = await _deps(tmp_path)
        for profile_id in ("legacy", "final"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_create",
                    "agent_id": "target",
                    "profile_id": profile_id,
                    "profile": {},
                },
            )
        async with deps.session_factory() as session:
            await create_task(
                session,
                created_by="owner@example.com",
                agent_id="target",
                agent_profile_id="legacy",
                title="Rollback",
            )
            await session.commit()

        async def fail_persist(*args: object, **kwargs: object) -> None:
            raise AgentManagementError("forced failure")

        monkeypatch.setattr(
            agent_management,
            "_persist_runtime_profile_updates",
            fail_persist,
        )
        with pytest.raises(AgentManagementError, match="forced failure"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_delete",
                    "agent_id": "target",
                    "profile_id": "legacy",
                    "replacement_profile_id": "final",
                },
            )

        async with deps.session_factory() as session:
            task_profile = await session.scalar(select(Task.agent_profile_id))
            row = await get_agent(session, "target")
            assert task_profile == "legacy"
            assert row is not None
            assert "legacy" in row.agent_profiles

    asyncio.run(scenario())


def test_runtime_profile_delete_expected_updated_at_conflict_preserves_references(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        deps, _events = await _deps(tmp_path)
        for profile_id in ("legacy", "final"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_create",
                    "agent_id": "target",
                    "profile_id": profile_id,
                    "profile": {},
                },
            )
        async with deps.session_factory() as session:
            await create_task(
                session,
                created_by="owner@example.com",
                agent_id="target",
                agent_profile_id="legacy",
                title="Conflict",
            )
            await session.commit()
        with pytest.raises(AgentManagementError, match="conflict"):
            await _action(
                deps,
                {
                    "action": "runtime_profiles_delete",
                    "agent_id": "target",
                    "profile_id": "legacy",
                    "replacement_profile_id": "final",
                    "expected_updated_at": "2000-01-01T00:00:00+00:00",
                },
            )
        async with deps.session_factory() as session:
            assert await session.scalar(select(Task.agent_profile_id)) == "legacy"

    asyncio.run(scenario())


def test_runtime_profile_disable_and_delete_are_blocked_by_channel_default(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        deps, _events = await _deps(tmp_path)
        await _action(
            deps,
            {
                "action": "runtime_profiles_create",
                "agent_id": "target",
                "profile_id": "chat",
                "profile": {},
            },
        )
        async with deps.session_factory() as session:
            await create_channel_account(
                session,
                channel_type="signal",
                display_name="Signal",
                agent_id="target",
                default_agent_profile_id="chat",
                user_email="owner@example.com",
            )
            await session.commit()

        for action, extra in (
            ("runtime_profiles_update", {"profile": {"enabled": False}}),
            ("runtime_profiles_delete", {}),
        ):
            with pytest.raises(AgentManagementError, match="channel_accounts=1"):
                await _action(
                    deps,
                    {
                        "action": action,
                        "agent_id": "target",
                        "profile_id": "chat",
                        **extra,
                    },
                )

    asyncio.run(scenario())


def test_runtime_profile_delete_ignores_managed_link_profile_provenance(tmp_path: Path) -> None:
    async def scenario() -> None:
        deps, _events = await _deps(tmp_path)
        await _action(
            deps,
            {
                "action": "runtime_profiles_create",
                "agent_id": "target",
                "profile_id": "initial",
                "profile": {},
            },
        )
        async with deps.session_factory() as session:
            conversation = await create_conversation(
                session,
                user_email="owner@example.com",
                agent_id="target",
                context_type="agent_work",
            )
            target_session = await create_session(
                session,
                conversation.conversation_id,
                "owner@example.com",
                "target",
            )
            await create_managed_conversation_link(
                session,
                user_email="owner@example.com",
                controller_agent_id="manager",
                controller_conversation_id=conversation.conversation_id,
                controller_session_id=target_session.session_id,
                target_agent_id="target",
                target_agent_profile_id="initial",
                target_conversation_id=conversation.conversation_id,
                target_session_id=target_session.session_id,
                title="Profile provenance",
            )
            await session.commit()

        deleted = await _action(
            deps,
            {
                "action": "runtime_profiles_delete",
                "agent_id": "target",
                "profile_id": "initial",
            },
        )
        assert deleted["status"] == "deleted"

    asyncio.run(scenario())


def test_api_rejects_dangling_or_disabled_default_profile() -> None:
    payload = {
        "agent_id": "target",
        "owner_email": "owner@example.com",
        "name": "Target",
        "agent_profiles": {
            "fast": {"profile_id": "fast", "enabled": False},
        },
        "default_agent_profile_id": "fast",
    }
    with pytest.raises(HTTPException) as exc_info:
        _validate_agent_definition_payload(payload)
    assert exc_info.value.status_code == 400


def test_runtime_profile_operations_are_introspectable() -> None:
    operations = {operation.operation for operation in MANAGE_AGENTS_TOOL.native_operations}
    assert {
        "runtime_profiles_list",
        "runtime_profiles_get",
        "runtime_profiles_create",
        "runtime_profiles_update",
        "runtime_profiles_delete",
        "runtime_profiles_default_set",
    } <= operations
    delete_operation = next(
        operation
        for operation in MANAGE_AGENTS_TOOL.native_operations
        if operation.operation == "runtime_profiles_delete"
    )
    assert "replacement_profile_id" in delete_operation.input_schema["properties"]


def test_settings_update_schema_exposes_supported_memory_fields() -> None:
    operation = next(
        operation
        for operation in MANAGE_AGENTS_TOOL.native_operations
        if operation.operation == "settings_update"
    )
    settings_schema = operation.input_schema["properties"]["settings"]
    settings_properties = settings_schema["properties"]

    assert settings_properties["memory_backend"] == {"type": "string"}
    assert settings_properties["memory_backend_options"] == {"type": "object"}
