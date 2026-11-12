"""Version-pinned asset preparation through controller and executor boundaries."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.commands import CommandDispatcher
from cognis.core.prompts import build_skill_guidance
from cognis.core.skill_context import deduplicate_skill_instructions
from cognis.core.tool_router import ToolRouter
from cognis.executor.runner import ExecutorRunner
from cognis.executor.skill_assets import prepare_skill_assets
from cognis.models.agent import AgentDefinition
from cognis.models.session import SessionModel
from cognis.models.skill import ResolvedSkill, SkillAssetRef
from cognis.models.tool import ExecutorCapabilities, ExecutorConfig, ExecutorHandle, ToolCall
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.executor.in_process import InProcessExecutorConnection
from cognis.providers.executor.websocket import ExecutorRPCError, WebSocketExecutorConnection
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import create_user, get_skill_scoped, get_skill_version
from cognis.tools.builtin.skill_management import SKILL_LOAD_TOOL, handle_skill_management_tool
from cognis.tools.registry import RegisteredTool, ToolRegistry
from cognis.tools.skill_service import prepare_skill_assets as prepare_controller_assets


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_scope", ["grantee_executor", "owner_executor"])
async def test_shared_agent_skill_read_use_assets_and_mutation_boundary(
    tmp_path, monkeypatch, executor_scope
):
    from cognis.api import runtime_support
    from cognis.api.serializers import agent_to_response
    from cognis.core.executor_policy import ExecutorPolicy
    from cognis.runtime_context import RuntimeAccessContext, current_runtime_access_context
    from cognis.store.queries import (
        create_agent,
        create_agent_grant,
        revoke_agent_grant,
        update_agent,
    )
    from cognis.tools.skills import resolve_skills_for_agent

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/shared.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    owner, guest = "owner@example.com", "guest@example.com"
    async with factory() as db:
        for email in (owner, guest):
            await create_user(db, email=email, name=email, password_hash="hashed")
        await db.commit()
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "recipient-executor"))
    executor = InProcessExecutorConnection(
        ExecutorHandle(executor_id="recipient-executor", executor_type="in_process"),
        ToolRegistry(),
        CircuitBreaker(),
    )

    async def call(actor, name, arguments):
        return await handle_skill_management_tool(
            name, arguments, factory, actor, artifact_store=store, executor=executor
        )

    try:
        assigned = await call(
            owner, "skill_write", {"name": "Shared procedure", "instructions": "Read shared data."}
        )
        assert not assigned.is_error, assigned.output
        skill_id = json.loads(assigned.output)["skill_id"]
        asset = await call(
            owner,
            "skill_asset_write",
            {"skill_id": skill_id, "filename": "reference.txt", "content": "shared asset bytes"},
        )
        assert not asset.is_error, asset.output
        private = await call(
            owner, "skill_write", {"name": "Private procedure", "instructions": "Never shared."}
        )
        private_id = json.loads(private.output)["skill_id"]
        async with factory() as db:
            row = await create_agent(
                db,
                agent_id="shared-reader",
                owner_email=owner,
                name="Shared reader",
                status="active",
                skills={"items": [{"skill_id": skill_id, "auto_load_instructions": True}]},
            )
            grant = await create_agent_grant(
                db,
                agent_id=row.agent_id,
                grantee_user_email=guest,
                granted_by=owner,
                executor_scope=executor_scope,
            )
            await db.commit()
            agent = AgentDefinition.model_validate(agent_to_response(row).model_dump())
        token = current_runtime_access_context.set(
            RuntimeAccessContext(user_email=guest, agent_id=agent.agent_id, agent_owner_email=owner)
        )
        try:
            async with factory() as db:
                resolved = await resolve_skills_for_agent(db, agent, owner_email=guest)
            shared = next(s for s in resolved.skills if s.skill_id == skill_id)
            assert shared.instructions == "Read shared data."
            assert shared.auto_load_instructions
            assert private_id not in {s.skill_id for s in resolved.skills}

            config = {
                "executor_id": "recipient-executor",
                "executor_type": "websocket",
                "owner_email": guest,
                "executor_owner_email": guest,
                "runtime_state": "active",
                "enabled_tools": [],
                "enabled_tool_groups": [],
                "config": {},
            }
            monkeypatch.setattr(
                runtime_support, "load_executor_policy", AsyncMock(return_value=ExecutorPolicy())
            )
            monkeypatch.setattr(
                runtime_support,
                "_resolve_eligible_executor_config",
                AsyncMock(return_value=config),
            )
            providers = SimpleNamespace(_session_factory=factory)
            runtime_factory = runtime_support.build_step_runtime_factory(
                providers=providers,
                shared_registry=ToolRegistry(),
                shared_connection=None,
                session_factory=factory,
            )
            await runtime_factory(agent=agent, user_email=guest, prepare_only=True)
            assert agent.skills["_auto_loaded_skill_ids"] == [skill_id]
            assert "Read shared data." in "\n".join(agent.skills["_auto_loaded_skill_contexts"])

            loaded = await call(
                guest, "skill_load", {"skill_id": skill_id, "require_assets_ready": True}
            )
            assert not loaded.is_error, loaded.output
            assert "Read shared data." in loaded.metadata["protected_context"]
            manifest = json.loads(loaded.output)["asset_manifest"]
            assert manifest[0]["executor_id"] == "recipient-executor"
            assert Path(manifest[0]["local_path"]).read_text() == "shared asset bytes"
            detail = await call(guest, "skill_get", {"skill_id": skill_id})
            assert not detail.is_error, detail.output
            search = await call(guest, "skill_search", {"query": "procedure"})
            assert {s["skill_id"] for s in json.loads(search.output)["skills"]} == {skill_id}
            listing = await call(guest, "skill_list", {})
            listed_ids = {s["skill_id"] for s in json.loads(listing.output)}
            assert skill_id in listed_ids
            assert private_id not in listed_ids

            for name, arguments in (
                ("skill_load", {"skill_id": private_id}),
                ("skill_get", {"skill_id": private_id}),
                ("skill_patch", {"skill_id": skill_id, "instructions": "Changed"}),
                ("skill_delete", {"skill_id": skill_id}),
                (
                    "skill_asset_write",
                    {"skill_id": skill_id, "filename": "reference.txt", "content": "Changed"},
                ),
            ):
                denied = await call(guest, name, arguments)
                assert denied.is_error, denied.output
            async with factory() as db:
                row = await get_skill_scoped(db, skill_id, owner_email=owner)
                version = await get_skill_version(db, row.current_version_id)
                assert version.instructions == "Read shared data."
                await update_agent(
                    db,
                    agent.agent_id,
                    updates={"skills": {"items": [{"skill_id": skill_id, "enabled": False}]}},
                )
                await db.commit()
            disabled = await call(guest, "skill_load", {"skill_id": skill_id})
            assert disabled.is_error
            async with factory() as db:
                await update_agent(
                    db, agent.agent_id, updates={"skills": {"items": [{"skill_id": skill_id}]}}
                )
                await revoke_agent_grant(db, grant.grant_id)
                await db.commit()
            revoked = await call(guest, "skill_load", {"skill_id": skill_id})
            assert revoked.is_error
        finally:
            current_runtime_access_context.reset(token)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_load_restores_assets_without_duplicating_instructions(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    email = "user@example.com"
    async with factory() as session:
        await create_user(session, email=email, name="User", password_hash="hashed")
        await session.commit()
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "executor"))
    executor = InProcessExecutorConnection(
        ExecutorHandle(executor_id="authorized", executor_type="in_process"),
        ToolRegistry(),
        CircuitBreaker(),
    )

    async def call(name, arguments):
        result = await handle_skill_management_tool(
            name, arguments, factory, email, artifact_store=store, executor=executor
        )
        assert not result.is_error, result.output
        return result

    try:
        created = await call("skill_write", {"name": "Portable", "instructions": "Read helper."})
        skill_id = json.loads(created.output)["skill_id"]
        await call(
            "skill_asset_write",
            {
                "skill_id": skill_id,
                "filename": "references/helper.txt",
                "content": "portable reference",
                "content_type": "text/plain",
            },
        )
        first = await call("skill_load", {"skill_id": skill_id, "require_assets_ready": True})
        messages = [{"role": "system", "content": first.metadata["protected_context"]}]
        path = Path(json.loads(first.output)["asset_manifest"][0]["local_path"])
        assert path.read_text() == "portable reference"
        path.unlink()

        repeated = deduplicate_skill_instructions(
            await call("skill_load", {"skill_id": skill_id, "require_assets_ready": True}),
            messages,
        )
        payload = json.loads(repeated.output)
        assert payload["status"] == "already_loaded"
        assert payload["instructions_loaded"] is False
        assert payload["asset_preparation"]["status"] == "ready"
        assert path.read_text() == "portable reference"
        assert "protected_context" not in repeated.metadata

        # A different executor still needs its own assets, not the first executor's paths.
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "other-executor"))
        executor = InProcessExecutorConnection(
            ExecutorHandle(executor_id="other-authorized", executor_type="in_process"),
            ToolRegistry(),
            CircuitBreaker(),
        )
        moved = deduplicate_skill_instructions(
            await call("skill_load", {"skill_id": skill_id, "require_assets_ready": True}),
            messages,
        )
        moved_payload = json.loads(moved.output)
        moved_path = Path(moved_payload["asset_manifest"][0]["local_path"])
        assert moved_path != path
        assert moved_path.read_text() == "portable reference"
        assert moved_payload["asset_manifest"][0]["executor_id"] == "other-authorized"
        assert moved_payload["instructions_loaded"] is False
        assert "protected_context" not in moved.metadata
    finally:
        await engine.dispose()


def _bundle(content: bytes = b"print('portable')\n") -> dict:
    return {
        "owner_email": "user@example.com",
        "skill_id": "portable-skill",
        "version_id": "sv-first",
        "assets": [
            {
                "asset_id": "sa-script",
                "filename": "scripts/helper.py",
                "content_hash": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "content_type": "text/x-python",
                "content_b64": base64.b64encode(content).decode(),
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["helper.py", "scripts/renamed.py"])
async def test_existing_asset_reuse_preserves_storage_and_history(
    tmp_path, monkeypatch, destination
):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    email = "user@example.com"
    async with factory() as session:
        await create_user(session, email=email, name="User", password_hash="hashed")
        await session.commit()
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "executor"))
    executor = InProcessExecutorConnection(
        ExecutorHandle(executor_id="authorized", executor_type="in_process"),
        ToolRegistry(),
        CircuitBreaker(),
    )

    async def call(name, arguments):
        result = await handle_skill_management_tool(
            name, arguments, factory, email, artifact_store=store, executor=executor
        )
        assert not result.is_error, result.output
        return json.JSONDecoder().raw_decode(result.output[result.output.index("{") :])[0]

    try:
        created = await call("skill_write", {"name": "Rename", "instructions": "Run helper."})
        skill_id = created["skill_id"]
        await call(
            "skill_asset_write",
            {
                "skill_id": skill_id,
                "filename": "helper.py",
                "content": "print('immutable')\n",
                "content_type": "text/x-python",
            },
        )
        async with factory() as session:
            skill = await get_skill_scoped(session, skill_id, owner_email=email)
            original = await get_skill_version(session, skill.current_version_id)
            original_ref = dict(original.asset_manifest[0])
        if destination != "helper.py":
            with monkeypatch.context() as patch:
                patch.setattr(
                    store,
                    "async_load",
                    AsyncMock(
                        return_value=(
                            b"x" * original_ref["size_bytes"],
                            original_ref["content_type"],
                        )
                    ),
                )
                rejected = await handle_skill_management_tool(
                    "skill_patch",
                    {
                        "skill_id": skill_id,
                        "assets": {
                            "upsert": [
                                {
                                    "filename": destination,
                                    "existing_asset_id": original_ref["asset_id"],
                                }
                            ]
                        },
                    },
                    factory,
                    email,
                    artifact_store=store,
                    executor=executor,
                )
                assert rejected.is_error
                assert "does not match its manifest" in rejected.output
            async with factory() as session:
                unchanged = await get_skill_scoped(session, skill_id, owner_email=email)
                assert unchanged.current_version_id == original.version_id
        patched = await call(
            "skill_patch",
            {
                "skill_id": skill_id,
                "expected_current_version_id": original.version_id,
                "instructions": "Run the reused helper.",
                "assets": {
                    "remove": ["helper.py"],
                    "upsert": [
                        {"filename": destination, "existing_asset_id": original_ref["asset_id"]}
                    ],
                },
            },
        )
        loaded = await call("skill_load", {"skill_id": skill_id})
        assert loaded["version_id"] == patched["version_id"]
        assert loaded.get("preparation_warning") is None
        assert loaded["python_environment"]["ready"]
        assert len(loaded["asset_manifest"]) == 1
        asset = loaded["asset_manifest"][0]
        assert asset["filename"] == destination
        assert Path(asset["local_path"]).read_text() == "print('immutable')\n"
        executed = subprocess.run(
            [loaded["python_environment"]["python_path"], asset["local_path"]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert executed.stdout.strip() == "immutable"
        async with factory() as session:
            current = await get_skill_version(session, loaded["version_id"])
            historical = await get_skill_version(session, original.version_id)
        assert historical.asset_manifest == original.asset_manifest
        current_ref = current.asset_manifest[0]
        assert current_ref["asset_id"] != original_ref["asset_id"]
        for key in ["content_hash", "size_bytes", "content_type"]:
            assert current_ref[key] == original_ref[key]
        assert (current_ref["artifact_object_id"] == original_ref["artifact_object_id"]) == (
            destination == "helper.py"
        )
        for ref in [original_ref, current_ref]:
            content, _ = await store.async_load(
                ref["artifact_namespace"], ref["artifact_object_id"], ref["filename"]
            )
            assert content == b"print('immutable')\n"
        await call(
            "skill_restore_version",
            {
                "skill_id": skill_id,
                "version_id": original.version_id,
            },
        )
        restored = await call("skill_load", {"skill_id": skill_id})
        assert restored["asset_manifest"][0]["filename"] == "helper.py"
        assert Path(restored["asset_manifest"][0]["local_path"]).read_bytes() == content
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reused_asset_rename_preserves_skill_scope_and_binary_policy(monkeypatch):
    content = b"\x89PNG\r\n\x1a\n"
    row = SimpleNamespace(
        filename="old.png",
        skill_version_id="stored-version",
        artifact_namespace="skills",
        artifact_object_id="stored-object",
        content_hash=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type="image/png",
    )
    monkeypatch.setattr("cognis.store.queries.get_skill_asset", AsyncMock(return_value=row))
    version = SimpleNamespace(skill_id="another-skill")
    monkeypatch.setattr(
        "cognis.tools.skill_service.get_skill_version",
        AsyncMock(return_value=version),
    )
    store = MagicMock()
    store.async_load = AsyncMock(return_value=(content, "image/png"))
    store.async_save = AsyncMock()
    assets = [{"filename": "new.png", "existing_asset_id": "stored-asset"}]
    with pytest.raises(ValueError, match="same skill"):
        await prepare_controller_assets(
            object(),
            store,
            owner_email="user@example.com",
            skill_id="owned-skill",
            assets=assets,
            allow_binary=False,
        )
    store.async_load.assert_not_awaited()
    version.skill_id = "owned-skill"
    with pytest.raises(ValueError, match="text or script files"):
        await prepare_controller_assets(
            object(),
            store,
            owner_email="user@example.com",
            skill_id="owned-skill",
            assets=assets,
            allow_binary=False,
        )
    store.async_save.assert_not_awaited()
    prepared = await prepare_controller_assets(
        object(),
        store,
        owner_email="user@example.com",
        skill_id="owned-skill",
        assets=assets,
        allow_binary=True,
    )
    assert prepared[0].filename == "new.png"
    assert prepared[0].content_type == "image/png"
    assert prepared[0].content == content


@pytest.mark.asyncio
async def test_prepare_reuses_repairs_and_isolates_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    bundle = _bundle()
    first, parallel = await asyncio.gather(
        prepare_skill_assets(bundle), prepare_skill_assets(bundle)
    )
    assert first == parallel
    path = Path(first["asset_manifest"][0]["local_path"])
    before = path.stat().st_mtime_ns
    assert await prepare_skill_assets(bundle) == first
    assert path.stat().st_mtime_ns == before
    path.write_text("corrupted")
    await prepare_skill_assets(bundle)
    assert path.read_text() == "print('portable')\n"
    replacement = _bundle(b"print('replacement')\n")
    replacement["version_id"] = "sv-second"
    updated = await prepare_skill_assets(replacement)
    updated_path = Path(updated["asset_manifest"][0]["local_path"])
    assert updated_path != path
    assert path.read_text() == "print('portable')\n"
    assert updated_path.read_text() == "print('replacement')\n"
    bundle["owner_email"] = "another@example.com"
    assert (await prepare_skill_assets(bundle))["asset_manifest"][0]["local_path"] != str(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        "../helper.py",
        "/helper.py",
        "scripts/../../helper.py",
        "scripts\\helper.py",
        "C:/helper.py",
        "scripts/./helper.py",
    ],
)
async def test_prepare_rejects_unsafe_paths(tmp_path, monkeypatch, filename):
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    bundle = _bundle()
    bundle["assets"][0]["filename"] = filename
    with pytest.raises(ValueError, match="filename"):
        await prepare_skill_assets(bundle)
    assert not (tmp_path / "skill_assets").exists()


@pytest.mark.asyncio
async def test_prepare_rejects_hash_and_symlink_escapes(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
    bundle = _bundle()
    bundle["assets"][0]["content_hash"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        await prepare_skill_assets(bundle)
    bundle = _bundle()
    result = await prepare_skill_assets(bundle)
    path = Path(result["asset_manifest"][0]["local_path"])
    outside = tmp_path / "outside.py"
    outside.write_text("protected")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(OSError):
        await prepare_skill_assets(bundle)
    assert outside.read_text() == "protected"
    path.unlink()
    path.parent.rmdir()
    path.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        await prepare_skill_assets(bundle)
    assert not (tmp_path / "helper.py").exists()


@pytest.mark.asyncio
async def test_publish_load_replace_and_clean_execution_without_restart(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    async with factory() as session:
        await create_user(session, email="user@example.com", name="User", password_hash="hashed")
        await create_user(
            session, email="another@example.com", name="Another", password_hash="hashed"
        )
        await session.commit()
    executor = InProcessExecutorConnection(
        ExecutorHandle(executor_id="authorized", executor_type="in_process"),
        ToolRegistry(),
        CircuitBreaker(),
        runtime_metadata={"skill_manifests": []},
    )
    agent = AgentDefinition(
        agent_id="portable-agent", name="Portable", owner_email="user@example.com"
    )
    session = SessionModel(
        session_id="portable-session",
        conversation_id="portable-conversation",
        user_email="user@example.com",
        agent_id=agent.agent_id,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=SKILL_LOAD_TOOL))
    guardrails = SimpleNamespace(
        evaluate=AsyncMock(
            return_value=SimpleNamespace(
                decision="approve",
                reasoning=None,
                risk=None,
                path=None,
                latency_ms=0,
                call_id="evaluation",
            )
        )
    )
    router = ToolRouter(guardrails=guardrails, session_factory=factory, artifact_store=store)

    async def call(name, arguments, *, email="user@example.com", connection=executor):
        if name == "skill_load" and email == "user@example.com":
            return await router.execute(
                ToolCall(call_id=uuid.uuid4().hex, name=name, arguments=arguments),
                session,
                agent,
                registry,
                connection,
            )
        return await handle_skill_management_tool(
            name,
            arguments,
            factory,
            email,
            artifact_store=store,
            executor=connection,
        )

    try:
        created = await call("skill_write", {"name": "Portable", "instructions": "Run helper."})
        assert not created.is_error
        skill_id = json.loads(created.output)["skill_id"]
        clean = tmp_path / "clean"
        clean.mkdir()
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "executor-one"))
        paths = []
        for text in ("first", "second"):
            published = await call(
                "skill_asset_write",
                {
                    "skill_id": skill_id,
                    "filename": "scripts/helper.py",
                    "content": f"print('{text}')\n",
                    "content_type": "text/x-python",
                },
            )
            assert not published.is_error, published.output
            loaded = await call("skill_load", {"skill_id": skill_id})
            assert not loaded.is_error, loaded.output
            payload, _ = json.JSONDecoder().raw_decode(loaded.output[loaded.output.index("{") :])
            asset = payload["asset_manifest"][0]
            paths.append(asset["local_path"])
            assert asset["executor_id"] == "authorized"
            assert asset["version_id"] == payload["version_id"]
            assert (
                hashlib.sha256(Path(asset["local_path"]).read_bytes()).hexdigest()
                == asset["content_hash"]
            )
            assert asset["local_path"] in loaded.metadata["protected_context"]
            result = subprocess.run(
                [sys.executable, asset["local_path"]],
                cwd=clean,
                check=True,
                capture_output=True,
                text=True,
            )
            assert result.stdout.strip() == text
        assert paths[0] != paths[1]
        assert executor.runtime_metadata["skill_manifests"] == []
        fresh = InProcessExecutorConnection(
            ExecutorHandle(executor_id="authorized-two", executor_type="in_process"),
            ToolRegistry(),
            CircuitBreaker(),
        )
        session = session.model_copy(update={"session_id": "fresh-portable-session"})
        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "executor-two"))
        reloaded = await call("skill_load", {"skill_id": skill_id}, connection=fresh)
        assert not reloaded.is_error
        payload, _ = json.JSONDecoder().raw_decode(reloaded.output[reloaded.output.index("{") :])
        current = payload["asset_manifest"][0]
        assert current["executor_id"] == "authorized-two"
        assert current["local_path"] not in paths
        assert (
            subprocess.run(
                [sys.executable, current["local_path"]],
                cwd=clean,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == "second"
        )
        denied_connection = AsyncMock()
        denied = await call(
            "skill_load",
            {"skill_id": skill_id},
            email="another@example.com",
            connection=denied_connection,
        )
        assert denied.is_error
        denied_connection.rpc_call.assert_not_called()
        failed = await call("skill_load", {"skill_id": skill_id}, connection=None)
        assert not failed.is_error
        assert "protected_context" in failed.metadata
        assert "Assets might not be runnable" in failed.output
        from cognis.executor import skill_python

        monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "broken-executor"))
        monkeypatch.setattr(
            skill_python, "_create_environment", AsyncMock(side_effect=OSError("missing Python"))
        )
        incomplete = await call("skill_load", {"skill_id": skill_id})
        assert not incomplete.is_error
        assert "missing Python" in incomplete.output
        assert "Python environment incomplete" in incomplete.metadata["protected_context"]
        incomplete_payload, _ = json.JSONDecoder().raw_decode(
            incomplete.output[incomplete.output.index("{") :]
        )
        assert Path(incomplete_payload["asset_manifest"][0]["local_path"]).exists()
        assert not incomplete_payload["python_environment"]["ready"]
    finally:
        await engine.dispose()


def test_portable_authoring_guidance_is_general():
    guidance = build_skill_guidance()
    assert "versioned skill assets" in guidance
    assert "never bundle secrets" in guidance
    assert "without the original workspace" in guidance


@pytest.mark.asyncio
async def test_bundle_layout_supports_neighboring_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
    script = (
        b"from pathlib import Path\nprint(Path(__file__).with_name('resource.txt').read_text())\n"
    )
    bundle = _bundle(script)
    resource = _bundle(b"bundled resource")["assets"][0]
    resource.update(
        asset_id="sa-resource", filename="scripts/resource.txt", content_type="text/plain"
    )
    bundle["assets"].append(resource)
    prepared = await prepare_skill_assets(bundle)
    clean = tmp_path / "clean"
    clean.mkdir()
    process = subprocess.run(
        [sys.executable, prepared["asset_manifest"][0]["local_path"]],
        cwd=clean,
        check=True,
        capture_output=True,
        text=True,
    )
    assert process.stdout.strip() == "bundled resource"


@pytest.mark.asyncio
async def test_websocket_prepare_dispatch_response_and_correlated_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
    requests = asyncio.Queue()
    responses = asyncio.Queue()

    class ControllerSocket:
        async def send_json(self, message):
            await requests.put(json.dumps(message))

        async def receive_json(self):
            return await responses.get()

        async def close(self, **kwargs):
            pass

    class RunnerSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return await requests.get()

        async def send(self, message):
            await responses.put(json.loads(message))

    runner = ExecutorRunner(ExecutorConfig(executor_id="authorized", controller_token="test"))
    connection = WebSocketExecutorConnection(
        ControllerSocket(), "authorized", ExecutorCapabilities()
    )
    connection.start_receiver()
    task = asyncio.create_task(runner._message_loop(RunnerSocket()))
    try:
        result = await connection.rpc_call("skill.assets.prepare", _bundle(), timeout=5)
        assert (
            Path(result["asset_manifest"][0]["local_path"]).read_bytes() == b"print('portable')\n"
        )
        corrupt = _bundle()
        corrupt["assets"][0]["content_hash"] = "f" * 64
        with pytest.raises(ExecutorRPCError, match="Skill asset preparation failed"):
            await connection.rpc_call("skill.assets.prepare", corrupt, timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await runner._cancel_connection_handler_tasks()
        await connection.close()


@pytest.mark.asyncio
async def test_slash_skill_executor_round_trip_records_current_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
    bundle = _bundle()
    ref = SkillAssetRef(
        **{
            key: bundle["assets"][0][key]
            for key in ("filename", "asset_id", "content_hash", "size_bytes", "content_type")
        },
        artifact_object_id="portable-object",
    )
    skill = ResolvedSkill(
        skill_id=bundle["skill_id"],
        name="Portable",
        instructions="Run helper",
        version_id=bundle["version_id"],
        version_number=1,
        content_hash="hash",
        asset_manifest=[ref],
        tools=[],
    )
    agent = AgentDefinition(
        agent_id="portable-agent", name="Portable", owner_email="user@example.com"
    )
    session = SessionModel(
        session_id="portable-session",
        conversation_id="portable-conversation",
        user_email="user@example.com",
        agent_id=agent.agent_id,
    )
    durable = []
    keys = {}
    warm = []

    async def record_events(_session_id, events, **kwargs):
        key = kwargs["idempotency_key"]
        if key not in keys:
            durable.extend(events)
            keys[key] = len(durable)
        return SimpleNamespace(ok=True, seq=keys[key])

    async def append_events(_session, events, result):
        if result.seq > len(warm):
            warm.extend(events)

    cache = SimpleNamespace(
        get_loaded_skill_context_hash=lambda *_: "hash",
        append_recorded_events=append_events,
    )
    active = ["executor-a"]
    connection = InProcessExecutorConnection(
        ExecutorHandle(executor_id=active[0], executor_type="in_process"),
        ToolRegistry(),
        CircuitBreaker(),
    )

    async def runtime_factory(*args, **kwargs):
        connection.handle = connection.handle.model_copy(update={"executor_id": active[0]})
        return SimpleNamespace(executor_connection=connection, cleanup=AsyncMock())

    dispatcher = CommandDispatcher(
        session_factory=None,
        session_manager=None,
        session_cache=cache,
        compaction_strategy=None,
        pause_waiter=None,
        notification_service=None,
        providers=SimpleNamespace(guardrails=SimpleNamespace(record_events=record_events)),
        skill_runtime_factory=runtime_factory,
        artifact_store=SimpleNamespace(
            async_load=AsyncMock(
                return_value=(base64.b64decode(bundle["assets"][0]["content_b64"]), "text/x-python")
            )
        ),
    )
    for executor_id in ("executor-a", "executor-b", "executor-a"):
        active[0] = executor_id
        result = await dispatcher._load_skill_for_session(
            session=session, agent=agent, user_email=session.user_email, skill=skill
        )
        assert result.type == "system_message"
        assert executor_id in warm[-1].data["content"]
    assert len(durable) == len(warm) == len(keys) == 3
    assert warm[-1].data["content"] == durable[-1].data["content"]
    dispatcher._skill_runtime_factory = None
    degraded = await dispatcher._load_skill_for_session(
        session=session, agent=agent, user_email=session.user_email, skill=skill
    )
    assert degraded.type == "system_message"
    assert "Warning:" in degraded.text
    assert "Assets might not be runnable" in warm[-1].data["content"]
