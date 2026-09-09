#!/usr/bin/env python3
"""Seed deterministic Work Activity conformance fixtures for the e2e stack.

Creates real conversations/sessions and real Intaris tool_call/tool_result
events (no mock endpoints, no synthetic API) that exercise the live Work
projection contract described in docs/specs/42-work-live-projection.md:

- a completed root with real file-edit evidence (Files tab, retained content)
- a sessionless conversation (no session at all) that must render an empty
  Work view, never "Unable to load"
- five sibling conversations, one root session each, covering every
  server-owned execution_state label the UI must render verbatim
- one root with two physical sessions sharing the same `activity_scope_id`
  (session rotation) with an evidence event on each physical session, to
  prove the logical Work node collapses them without duplicating evidence

Idempotent — safe to re-run. Reuses the e2e admin/agent seeded by
scripts/seed_e2e.py (run `make e2e-seed` first).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.logging import setup_logging
from cognis.models.session import SessionEvent
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.runtime_context import current_user_email
from cognis.security import create_password_hasher
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import WorkSessionProjectionRow
from cognis.store.queries import (
    create_conversation,
    create_session,
    delete_setting,
    get_conversation,
    get_setting,
    update_conversation_active_session,
    upsert_setting,
)

E2E_AGENT_ID = "e2e-test-agent"
FIXTURE_VERSION = 2
PREVIEW_SETTING_KEY = "work.source_preview_max_lifetime_seconds"
PREVIEW_SETTING_BACKUP = Path("/data/work-conformance-preview-setting.json")

FILES_CONVERSATION_ID = "conv_work_conformance_files"
FILES_SESSION_ID = "sess_work_conformance_files_v2"

EMPTY_CONVERSATION_ID = "conv_work_conformance_empty"

LABEL_STATES = ("idle", "running", "waiting", "completed", "failed")

ROTATION_CONVERSATION_ID = "conv_work_conformance_rotation"
ROTATION_SCOPE_ID = "work-conformance-rotation-scope"
ROTATION_SESSION_A = "sess_work_conformance_rotation_a_v2"
ROTATION_SESSION_B = "sess_work_conformance_rotation_b_v2"
REFRESH_CONVERSATION_ID = "conv_work_conformance_refresh"
REFRESH_SESSION_ID = f"sess_work_conformance_refresh_v3_{uuid.uuid4().hex}"
REFRESH_OLD_COMMAND = "printf work-conformance-old"
REFRESH_NEW_COMMAND = "printf work-conformance-newest"
REFRESH_BACKLOG_SIZE = 8


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _apply_patch_events(
    *, turn_id: str, call_id: str, path: str, diff: str, message: str
) -> list[SessionEvent]:
    """Build a real tool_call/tool_result pair the Work materializer projects
    into a FileDiffRef, matching cognis/api/chat_v2/projector.py exactly."""

    return [
        SessionEvent(
            type="user_message",
            data={
                "role": "user",
                "content": message,
                "content_type": "text",
                "turn_id": turn_id,
            },
        ),
        SessionEvent(
            type="tool_call",
            data={
                "call_id": call_id,
                "name": "apply_patch",
                "turn_id": turn_id,
                "status": "started",
                "arguments": {"file_path": path},
            },
        ),
        SessionEvent(
            type="tool_result",
            data={
                "call_id": call_id,
                "name": "apply_patch",
                "turn_id": turn_id,
                "status": "complete",
                "result": f"Patched {path}",
                "file_diffs": [
                    {
                        "path": path,
                        "diff": diff,
                    }
                ],
                "duration_ms": 42,
            },
        ),
    ]


def _command_events(*, turn_id: str, call_id: str, command: str) -> list[SessionEvent]:
    return [
        SessionEvent(
            type="tool_call",
            data={
                "call_id": call_id,
                "name": "bash",
                "turn_id": turn_id,
                "status": "started",
                "arguments": {"command": command},
            },
        ),
        SessionEvent(
            type="tool_result",
            data={
                "call_id": call_id,
                "name": "bash",
                "turn_id": turn_id,
                "status": "complete",
                "result": command,
                "arguments": {"command": command},
                "duration_ms": 17,
            },
        ),
    ]


async def _ensure_conversation(
    session: AsyncSession,
    *,
    conversation_id: str,
    admin_email: str,
    title: str,
    scenario: str,
) -> bool:
    """Rebuild a fixed fixture and return True."""

    existing = await get_conversation(session, conversation_id)
    if existing is not None:
        raise RuntimeError(f"Fixture purge did not remove {conversation_id}")
    await create_conversation(
        session,
        admin_email,
        E2E_AGENT_ID,
        "web",
        conversation_id=conversation_id,
        title=title,
        title_source="manual",
        context_data={
            "seeded_by": "seed_work_conformance",
            "fixture_version": FIXTURE_VERSION,
            "scenario": scenario,
        },
    )
    return True


async def _seed_files_conversation(
    session: AsyncSession,
    guardrails_provider: IntarisProvider,
    admin_email: str,
) -> None:
    fresh = await _ensure_conversation(
        session,
        conversation_id=FILES_CONVERSATION_ID,
        admin_email=admin_email,
        title="Work conformance: completed root with file evidence",
        scenario="work-conformance-files",
    )
    if not fresh:
        return
    session_row = await create_session(
        session,
        FILES_CONVERSATION_ID,
        admin_email,
        E2E_AGENT_ID,
        session_id=FILES_SESSION_ID,
        intaris_session_id=FILES_SESSION_ID,
        mnemory_session_id=FILES_SESSION_ID,
        status="completed",
    )
    # A completed/null-active root: never mark this session active on the
    # conversation. Work must resolve and render retained evidence for a
    # completed root with no active session.
    await session.flush()

    intaris_id = session_row.intaris_session_id or session_row.session_id
    await guardrails_provider.create_session(
        intaris_id,
        "Work conformance completed root with real file evidence.",
        E2E_AGENT_ID,
        user_id=admin_email,
    )
    try:
        await guardrails_provider.record_events(
            intaris_id,
            _apply_patch_events(
                turn_id="turn_work_conformance_files",
                call_id="call_work_conformance_apply_patch",
                path="src/work_conformance_target.py",
                diff=(
                    "@@ -1,3 +1,4 @@\n"
                    " def handler():\n"
                    "-    return None\n"
                    "+    return {'status': 'ok'}\n"
                    " \n"
                ),
                message="Patch the conformance target module.",
            ),
            source="seed_work_conformance",
            idempotency_key=f"{intaris_id}:work-conformance-files-v1",
            user_email=admin_email,
            agent_id=E2E_AGENT_ID,
        )
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Intaris Work seed rejected events: {exc.response.text}") from exc
    print(f"Seeded work conformance files conversation: {FILES_CONVERSATION_ID}")


async def _seed_empty_conversation(session: AsyncSession, admin_email: str) -> None:
    fresh = await _ensure_conversation(
        session,
        conversation_id=EMPTY_CONVERSATION_ID,
        admin_email=admin_email,
        title="Work conformance: sessionless conversation",
        scenario="work-conformance-empty",
    )
    if fresh:
        print(f"Seeded work conformance empty conversation: {EMPTY_CONVERSATION_ID}")
    # Deliberately create no session and set no active_session_id: Work must
    # resolve the authorized (empty) session tree and render "No activity
    # yet", never "Unable to load".


async def _seed_label_conversations(
    session: AsyncSession, guardrails_provider: IntarisProvider, admin_email: str
) -> None:
    for state in LABEL_STATES:
        conversation_id = f"conv_work_conformance_label_{state}"
        session_id = f"sess_work_conformance_label_{state}_v{FIXTURE_VERSION}"
        fresh = await _ensure_conversation(
            session,
            conversation_id=conversation_id,
            admin_email=admin_email,
            title=f"Work conformance: {state} execution label",
            scenario=f"work-conformance-label-{state}",
        )
        # A controller session references an external stream even when it has
        # no events. Create it before exposing the controller row.
        await guardrails_provider.create_session(
            session_id,
            f"Work conformance {state} execution label.",
            E2E_AGENT_ID,
            user_id=admin_email,
        )
        from cognis.runtime_context import (
            current_agent_id,
            current_agent_owner_email,
            current_user_email,
        )

        user_token = current_user_email.set(admin_email)
        agent_token = current_agent_id.set(E2E_AGENT_ID)
        owner_token = current_agent_owner_email.set(admin_email)
        try:
            await guardrails_provider.get_session(session_id)
        finally:
            current_agent_owner_email.reset(owner_token)
            current_agent_id.reset(agent_token)
            current_user_email.reset(user_token)
        if not fresh:
            continue
        await create_session(
            session,
            conversation_id,
            admin_email,
            E2E_AGENT_ID,
            session_id=session_id,
            intaris_session_id=session_id,
            mnemory_session_id=session_id,
            status=state,
        )
        if state != "completed":
            await update_conversation_active_session(session, conversation_id, session_id)
        print(f"Seeded work conformance {state} label conversation: {conversation_id}")


async def _seed_rotation_conversation(
    session: AsyncSession,
    guardrails_provider: IntarisProvider,
    admin_email: str,
) -> None:
    fresh = await _ensure_conversation(
        session,
        conversation_id=ROTATION_CONVERSATION_ID,
        admin_email=admin_email,
        title="Work conformance: session rotation/recovery",
        scenario="work-conformance-rotation",
    )
    if not fresh:
        return
    session_a = await create_session(
        session,
        ROTATION_CONVERSATION_ID,
        admin_email,
        E2E_AGENT_ID,
        session_id=ROTATION_SESSION_A,
        intaris_session_id=ROTATION_SESSION_A,
        mnemory_session_id=ROTATION_SESSION_A,
        status="completed",
        activity_scope_id=ROTATION_SCOPE_ID,
    )
    session_b = await create_session(
        session,
        ROTATION_CONVERSATION_ID,
        admin_email,
        E2E_AGENT_ID,
        session_id=ROTATION_SESSION_B,
        intaris_session_id=ROTATION_SESSION_B,
        mnemory_session_id=ROTATION_SESSION_B,
        status="completed",
        previous_session_id=ROTATION_SESSION_A,
        activity_scope_id=ROTATION_SCOPE_ID,
    )
    await update_conversation_active_session(session, ROTATION_CONVERSATION_ID, ROTATION_SESSION_B)
    await session.flush()

    for row, path, call_id, turn_id in (
        (
            session_a,
            "src/work_conformance_rotation_a.py",
            "call_work_conformance_rotation_a",
            "turn_work_conformance_rotation_a",
        ),
        (
            session_b,
            "src/work_conformance_rotation_b.py",
            "call_work_conformance_rotation_b",
            "turn_work_conformance_rotation_b",
        ),
    ):
        intaris_id = row.intaris_session_id or row.session_id
        await guardrails_provider.create_session(
            intaris_id,
            "Work conformance rotated physical session.",
            E2E_AGENT_ID,
            user_id=admin_email,
        )
        await guardrails_provider.record_events(
            intaris_id,
            _apply_patch_events(
                turn_id=turn_id,
                call_id=call_id,
                path=path,
                diff=("@@ -1,2 +1,3 @@\n def rotated():\n-    pass\n+    return True\n"),
                message=f"Patch {path} on this physical session.",
            ),
            source="seed_work_conformance",
            idempotency_key=f"{intaris_id}:work-conformance-rotation-v1",
            user_email=admin_email,
            agent_id=E2E_AGENT_ID,
        )
    print(f"Seeded work conformance rotation conversation: {ROTATION_CONVERSATION_ID}")


async def _seed_refresh_conversation(
    session: AsyncSession,
    guardrails_provider: IntarisProvider,
    admin_email: str,
) -> None:
    await _ensure_conversation(
        session,
        conversation_id=REFRESH_CONVERSATION_ID,
        admin_email=admin_email,
        title="Work conformance: visible recovery beats backlog",
        scenario="work-conformance-refresh",
    )
    await create_session(
        session,
        REFRESH_CONVERSATION_ID,
        admin_email,
        E2E_AGENT_ID,
        session_id=REFRESH_SESSION_ID,
        intaris_session_id=REFRESH_SESSION_ID,
        mnemory_session_id=REFRESH_SESSION_ID,
        status="completed",
    )
    await session.flush()
    await guardrails_provider.create_session(
        REFRESH_SESSION_ID,
        "Work conformance visible recovery.",
        E2E_AGENT_ID,
        user_id=admin_email,
    )
    await guardrails_provider.record_events(
        REFRESH_SESSION_ID,
        _command_events(
            turn_id="turn_work_refresh_old",
            call_id="call_work_refresh_old",
            command=REFRESH_OLD_COMMAND,
        ),
        source="seed_work_conformance",
        idempotency_key=f"{REFRESH_SESSION_ID}:old-v3",
        user_email=admin_email,
        agent_id=E2E_AGENT_ID,
    )
    for index in range(REFRESH_BACKLOG_SIZE):
        backlog_conversation_id = f"conv_work_conformance_backlog_{index:02d}"
        backlog_session_id = f"sess_work_conformance_backlog_{index:02d}_v3"
        backlog_conversation = await create_conversation(
            session,
            admin_email,
            E2E_AGENT_ID,
            "web",
            conversation_id=backlog_conversation_id,
            title=f"Work conformance backlog {index:02d}",
            title_source="manual",
            context_data={"seeded_by": "seed_work_conformance", "fixture_version": 3},
        )
        backlog_conversation.status = "archived"
        backlog = await create_session(
            session,
            backlog_conversation_id,
            admin_email,
            E2E_AGENT_ID,
            session_id=backlog_session_id,
            intaris_session_id=backlog_session_id,
            mnemory_session_id=backlog_session_id,
            status="completed",
        )
        await guardrails_provider.create_session(
            backlog_session_id,
            "Work conformance lower-priority historical repair.",
            E2E_AGENT_ID,
            user_id=admin_email,
        )
        await session.flush()
        projection = await session.scalar(
            select(WorkSessionProjectionRow).where(
                WorkSessionProjectionRow.session_id == backlog.session_id,
                WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
            )
        )
        if projection is None:
            raise RuntimeError(f"Backlog projection state is missing: {backlog.session_id}")
        projection.state = "repair"
        projection.priority = 0
        projection.next_retry_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)


async def _finalize_delayed_refresh_fixture(
    session_factory: async_sessionmaker[AsyncSession],
    guardrails_provider: IntarisProvider,
    admin_email: str,
) -> None:
    async with httpx.AsyncClient(
        base_url=_env("COGNIS_URL", "http://cognis:8080"),
        timeout=20,
    ) as client:
        login = await client.post(
            "/api/auth/login",
            json={
                "email": admin_email,
                "password": _env("COGNIS_LOCAL_ADMIN_PASSWORD", "cognis-local-admin"),
                "mode": "native",
            },
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        scope = {
            "key": f"conversation:{REFRESH_CONVERSATION_ID}",
            "kind": "conversation",
            "conversation_id": REFRESH_CONVERSATION_ID,
        }
        refresh = await client.post(
            "/api/v1/work/refresh",
            headers=headers,
            json={"scope": scope},
        )
        refresh.raise_for_status()
        for _ in range(120):
            response = await client.get(
                f"/api/v1/chat/v2/conversations/{REFRESH_CONVERSATION_ID}/work",
                params={"category": "commands"},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("materialization", {}).get("state") == "live" and any(
                item.get("command") == REFRESH_OLD_COMMAND for item in payload.get("commands", [])
            ):
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Timed out materializing retained refresh command")

    append = await guardrails_provider.record_events(
        REFRESH_SESSION_ID,
        _command_events(
            turn_id="turn_work_refresh_new",
            call_id="call_work_refresh_new",
            command=REFRESH_NEW_COMMAND,
        ),
        source="seed_work_conformance",
        idempotency_key=f"{REFRESH_SESSION_ID}:new-v3",
        user_email=admin_email,
        agent_id=E2E_AGENT_ID,
    )
    if not append.ok:
        raise RuntimeError(f"Failed to append refresh source events: {append!r}")
    target_seq = 0
    user_token = current_user_email.set(admin_email)
    try:
        for _ in range(120):
            try:
                readable = await guardrails_provider.read_events(
                    REFRESH_SESSION_ID,
                    after_seq=0,
                    limit=100,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                await asyncio.sleep(0.25)
                continue
            matching_seqs = [
                int(event.get("seq", 0))
                for event in readable.events
                if "call_work_refresh_new" in json.dumps(event, sort_keys=True)
            ]
            if matching_seqs:
                target_seq = max(matching_seqs)
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError(
                "Timed out waiting for the appended refresh command to become readable"
            )
    finally:
        current_user_email.reset(user_token)
    async with session_factory() as session:
        state = await session.scalar(
            select(WorkSessionProjectionRow).where(
                WorkSessionProjectionRow.session_id == REFRESH_SESSION_ID,
                WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
            )
        )
        if state is None:
            raise RuntimeError("Refresh projection state is missing")
        state.target_seq = max(state.target_seq, target_seq)
        state.state = "repair"
        state.priority = 0
        state.retry_count = 0
        state.last_error = None
        state.next_retry_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
        state.lease_owner = None
        state.lease_expires_at = None
        await session.commit()
    print("Seeded future-delayed visible Work recovery fixture.")


async def _prioritize_file_projections(admin_email: str) -> None:
    async with httpx.AsyncClient(
        base_url=_env("COGNIS_URL", "http://cognis:8080"),
        timeout=20,
    ) as client:
        login = await client.post(
            "/api/auth/login",
            json={
                "email": admin_email,
                "password": _env("COGNIS_LOCAL_ADMIN_PASSWORD", "cognis-local-admin"),
                "mode": "native",
            },
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        expected_paths = {
            FILES_CONVERSATION_ID: {"src/work_conformance_target.py"},
            ROTATION_CONVERSATION_ID: {
                "src/work_conformance_rotation_a.py",
                "src/work_conformance_rotation_b.py",
            },
        }
        # Directly seeded Intaris events do not emit controller wake signals.
        # GET reads persisted Work only; explicitly request materialization.
        for conversation_id in expected_paths:
            refresh = await client.post(
                "/api/v1/work/refresh",
                json={
                    "scope": {
                        "kind": "conversation",
                        "key": f"conversation:{conversation_id}",
                        "conversation_id": conversation_id,
                    }
                },
                headers=headers,
            )
            refresh.raise_for_status()
        for _ in range(120):
            ready = True
            for conversation_id, paths in expected_paths.items():
                response = await client.get(
                    f"/api/v1/chat/v2/conversations/{conversation_id}/work",
                    params={"category": "files"},
                    headers=headers,
                )
                response.raise_for_status()
                retained = {
                    item["path"]
                    for mutation in response.json().get("mutations", [])
                    for item in mutation.get("file_stats", [])
                    if item.get("path_generation_id")
                }
                ready = ready and all(
                    any(retained_path.endswith(path) for retained_path in retained)
                    for path in paths
                )
            if ready:
                print("Work conformance file projections are live.")
                return
            await asyncio.sleep(0.5)
    raise RuntimeError("Timed out waiting for Work conformance file projections")


async def _purge_existing_fixtures(admin_email: str) -> None:
    async with httpx.AsyncClient(
        base_url=_env("COGNIS_URL", "http://cognis:8080"),
        timeout=20,
    ) as client:
        login = await client.post(
            "/api/auth/login",
            json={
                "email": admin_email,
                "password": _env("COGNIS_LOCAL_ADMIN_PASSWORD", "cognis-local-admin"),
                "mode": "native",
            },
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        conversation_ids = [
            FILES_CONVERSATION_ID,
            EMPTY_CONVERSATION_ID,
            *(f"conv_work_conformance_label_{state}" for state in LABEL_STATES),
            ROTATION_CONVERSATION_ID,
            REFRESH_CONVERSATION_ID,
            *(
                f"conv_work_conformance_backlog_{index:02d}"
                for index in range(REFRESH_BACKLOG_SIZE)
            ),
        ]
        for conversation_id in conversation_ids:
            response = await client.delete(
                f"/api/v1/conversations/{conversation_id}/purge",
                headers=headers,
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()
        print(f"Purged fixed Work conformance fixtures before v{FIXTURE_VERSION} rebuild.")


async def _seed() -> None:
    setup_logging(_env("COGNIS_LOG_LEVEL", "info"), _env("COGNIS_LOG_FORMAT", "text"))
    admin_email = _env("COGNIS_LOCAL_ADMIN_EMAIL", "admin@cognis-e2e.localdev.me")
    password_hasher = create_password_hasher()
    runtime_config, bootstrap_engine, _, _ = await bootstrap_runtime(
        load_config(),
        password_hasher,
    )
    await bootstrap_engine.dispose()

    engine: AsyncEngine = create_engine(runtime_config.database_url)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    auth_provider = JWTAuthProvider(
        runtime_config.jwt_private_key_path,
        runtime_config.jwt_public_key_path,
    )
    guardrails_provider = IntarisProvider(runtime_config.intaris_url, auth_provider)

    try:
        async with session_factory() as session:
            if _env("WORK_CONFORMANCE_CONFIGURE_ONLY").lower() in {"1", "true", "yes"}:
                existing = await get_setting(session, PREVIEW_SETTING_KEY)
                if not PREVIEW_SETTING_BACKUP.exists():
                    PREVIEW_SETTING_BACKUP.write_text(
                        json.dumps(
                            {
                                "exists": existing is not None,
                                "value": existing.value if existing is not None else None,
                                "category": existing.category if existing is not None else "work",
                            }
                        )
                    )
                await upsert_setting(
                    session,
                    PREVIEW_SETTING_KEY,
                    3600,
                    "work",
                    updated_by=admin_email,
                )
                await session.commit()
                print("Configured retained Work source previews for conformance tests.")
                return
            if _env("WORK_CONFORMANCE_RESTORE_ONLY").lower() in {"1", "true", "yes"}:
                if PREVIEW_SETTING_BACKUP.exists():
                    backup = json.loads(PREVIEW_SETTING_BACKUP.read_text())
                    if backup["exists"]:
                        await upsert_setting(
                            session,
                            PREVIEW_SETTING_KEY,
                            backup["value"],
                            backup["category"],
                            updated_by=admin_email,
                        )
                    else:
                        await delete_setting(session, PREVIEW_SETTING_KEY)
                    await session.commit()
                    PREVIEW_SETTING_BACKUP.unlink()
                    print("Restored the Work source preview setting.")
                return
        await _purge_existing_fixtures(admin_email)
        async with session_factory() as session:
            await _seed_files_conversation(session, guardrails_provider, admin_email)
            await _seed_empty_conversation(session, admin_email)
            await _seed_label_conversations(session, guardrails_provider, admin_email)
            await _seed_rotation_conversation(session, guardrails_provider, admin_email)
            await _seed_refresh_conversation(session, guardrails_provider, admin_email)
            await session.commit()
        await _prioritize_file_projections(admin_email)
        await _finalize_delayed_refresh_fixture(
            session_factory,
            guardrails_provider,
            admin_email,
        )

        print(
            json.dumps(
                {
                    "files_conversation_id": FILES_CONVERSATION_ID,
                    "empty_conversation_id": EMPTY_CONVERSATION_ID,
                    "label_conversation_ids": {
                        state: f"conv_work_conformance_label_{state}" for state in LABEL_STATES
                    },
                    "rotation_conversation_id": ROTATION_CONVERSATION_ID,
                    "refresh_conversation_id": REFRESH_CONVERSATION_ID,
                },
                indent=2,
            )
        )
    finally:
        await guardrails_provider.client.aclose()
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(_seed())
    except Exception as exc:  # noqa: BLE001 - top-level seed script error path
        print(f"Work conformance seed failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
