from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from cognis.api.app import create_app
from cognis.api.chat_v2.event_store import (
    RawSessionEvent,
    SessionEventPage,
    SessionHistoryAvailability,
)
from cognis.api.chat_v2.schemas import ToolCallTimelineItem
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.api.chat_v2.work_repository import _session_runtime_metadata
from cognis.core.execution_metadata import persist_execution_metadata
from cognis.store import queries
from cognis.store.models import (
    Agent,
    DirectTurnRequestRow,
    ManagedConversationLink,
    Session,
    User,
    WorkCurrentFileRow,
    WorkRecordFileRow,
    WorkRecordRow,
    WorkSessionProjectionRow,
)
from cognis.store.work_live_invalidation import bump_live_work_revision


@pytest.fixture
def work_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    monkeypatch.setenv("COGNIS_INTARIS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("COGNIS_MNEMORY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("COGNIS_RUNTIME_MODE", "simple")
    with TestClient(create_app()) as client:
        assert client.portal is not None
        client.portal.call(client.app.state.work_materializer.stop)
        guardrails_read = AsyncMock(
            side_effect=AssertionError("foreground Work route called Intaris")
        )
        client.app.state.intaris_event_store._guardrails.read_events = guardrails_read  # noqa: SLF001
        yield client
        guardrails_read.assert_not_awaited()


def _auth_headers(client: TestClient, email: str = "owner@example.com") -> dict[str, str]:
    token = client.app.state.auth_provider.sign_access_token(email, "Owner", "user")
    return {"Authorization": f"Bearer {token}"}


def _seed_identity(client: TestClient, email: str = "owner@example.com") -> None:
    async def seed() -> None:
        async with client.app.state.session_factory() as db:
            db.add(User(email=email, name="Owner", role="user"))
            await db.flush()
            db.add(Agent(agent_id=f"agent-{email}", owner_email=email, name="Agent"))
            await db.commit()

    asyncio.run(seed())


def _seed_conversation(
    client: TestClient,
    *,
    conversation_id: str,
    email: str = "owner@example.com",
    status: str = "active",
    session_id: str | None = None,
) -> None:
    async def seed() -> None:
        async with client.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id=conversation_id,
                user_email=email,
                agent_id=f"agent-{email}",
                context_type="web",
            )
            conversation.status = status
            if session_id is not None:
                session = await queries.create_session(
                    db,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    user_email=email,
                    agent_id=f"agent-{email}",
                    intaris_session_id=f"source-{session_id}",
                )
                if status == "active":
                    await queries.update_conversation_active_session(
                        db,
                        conversation_id,
                        session.session_id,
                    )
            await db.commit()

    asyncio.run(seed())


def test_current_managed_rotation_survives_history_cap(work_api: TestClient) -> None:
    _seed_identity(work_api)
    _seed_conversation(work_api, conversation_id="cap-parent", session_id="cap-root")
    _seed_conversation(work_api, conversation_id="cap-child", session_id="cap-child-current")

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            root = await db.get(Session, "cap-root")
            current = await db.get(Session, "cap-child-current")
            root.activity_scope_id = "cap-parent-scope"
            current.activity_scope_id = "cap-child-scope"
            for index in range(270):
                old = await queries.create_session(
                    db,
                    session_id=f"cap-history-{index:03}",
                    conversation_id="cap-parent",
                    user_email="owner@example.com",
                    agent_id="agent-owner@example.com",
                    intaris_session_id=f"cap-history-source-{index:03}",
                    activity_scope_id="cap-parent-scope",
                )
                old.status = "completed"
                old.previous_session_id = "cap-root"
            old_child = await queries.create_session(
                db,
                session_id="cap-child-old",
                conversation_id="cap-child",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id="cap-child-source-old",
                activity_scope_id="cap-child-scope",
            )
            old_child.status = "completed"
            current.previous_session_id = old_child.session_id
            db.add(
                ManagedConversationLink(
                    link_id="cap-managed-link",
                    user_email="owner@example.com",
                    controller_agent_id=root.agent_id,
                    controller_conversation_id="cap-parent",
                    controller_session_id=root.session_id,
                    target_agent_id=current.agent_id,
                    target_conversation_id="cap-child",
                    target_session_id=old_child.session_id,
                    conversation_state="open",
                    turn_state="running",
                    active_turn_id="cap-running-turn",
                )
            )
            db.add(
                DirectTurnRequestRow(
                    request_id="cap-running-request",
                    turn_id="cap-running-turn",
                    conversation_id="cap-child",
                    session_id=current.session_id,
                    agent_id=current.agent_id,
                    user_id="owner@example.com",
                    idempotency_scope="cap",
                    idempotency_key="cap-running",
                    admission_hash="cap",
                    payload_hash="cap",
                    payload={},
                    status="running",
                )
            )
            await db.commit()

    asyncio.run(seed())
    for conversation_id in ("cap-parent", "cap-child"):
        for endpoint in ("work", "activity-overview"):
            response = work_api.get(
                f"/api/v1/chat/v2/conversations/{conversation_id}/{endpoint}",
                headers=_auth_headers(work_api),
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            child = next(
                node for node in payload["workstreams"] if node["conversation_id"] == "cap-child"
            )
            assert child["session_id"] == "cap-child-current"
            assert child["execution_state"] == "running"
            assert child["active_turn_id"] == "cap-running-turn"
            if conversation_id == "cap-parent":
                assert payload["graph_truncated"] is True
                assert child["parent_key"] is not None


def test_active_delegate_from_prior_managed_generation_remains_in_overview(
    work_api: TestClient,
) -> None:
    _seed_identity(work_api)
    _seed_conversation(work_api, conversation_id="nested-parent", session_id="nested-root")
    _seed_conversation(
        work_api,
        conversation_id="nested-managed",
        session_id="nested-managed-current",
    )

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            root = await db.get(Session, "nested-root")
            current = await db.get(Session, "nested-managed-current")
            root.activity_scope_id = "root-scope"
            current.activity_scope_id = "managed-current-scope"
            old = await queries.create_session(
                db,
                session_id="nested-managed-old",
                conversation_id="nested-managed",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id="source-nested-managed-old",
                activity_scope_id="managed-old-scope",
            )
            old.status = "completed"
            current.previous_session_id = old.session_id
            terminal = await queries.create_session(
                db,
                session_id="nested-terminal-delegate",
                conversation_id="nested-managed",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id="source-nested-terminal-delegate",
                parent_session_id=old.session_id,
                activity_scope_id="managed-old-scope",
                delegation_mode="delegate",
                delegation_task="Terminal nested review",
            )
            terminal.status = "completed"
            db.add(
                ManagedConversationLink(
                    link_id="nested-link",
                    user_email="owner@example.com",
                    controller_agent_id=root.agent_id,
                    controller_conversation_id=root.conversation_id,
                    controller_session_id=root.session_id,
                    target_agent_id=current.agent_id,
                    target_conversation_id=current.conversation_id,
                    target_session_id=old.session_id,
                    conversation_state="open",
                    turn_state="running",
                )
            )
            await db.commit()

    asyncio.run(seed())
    initial = work_api.get(
        "/api/v1/chat/v2/conversations/nested-parent/activity-overview",
        headers=_auth_headers(work_api),
    )
    assert initial.status_code == 200, initial.text
    assert "nested-active-delegate" not in {
        node["session_id"] for node in initial.json()["workstreams"]
    }

    async def add_active_delegate() -> None:
        async with work_api.app.state.session_factory() as db:
            active = await queries.create_session(
                db,
                session_id="nested-active-delegate",
                conversation_id="nested-managed",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id="source-nested-active-delegate",
                parent_session_id="nested-managed-old",
                activity_scope_id="managed-old-scope",
                delegation_mode="delegate",
                delegation_task="Active nested review",
            )
            await db.commit()
            assert active.parent_session_id == "nested-managed-old"

    asyncio.run(add_active_delegate())
    response = work_api.get(
        "/api/v1/chat/v2/conversations/nested-parent/activity-overview",
        headers=_auth_headers(work_api),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert int(body["work_revision"]) > int(initial.json()["work_revision"])
    by_session = {node["session_id"]: node for node in body["workstreams"]}
    assert "nested-active-delegate" in by_session
    assert "nested-terminal-delegate" not in by_session
    assert (
        by_session["nested-active-delegate"]["parent_key"]
        == by_session["nested-managed-current"]["key"]
    )


def test_execution_runtime_snapshot_survives_cold_reads(work_api: TestClient) -> None:
    _seed_identity(work_api)
    _seed_conversation(work_api, conversation_id="runtime-owner", session_id="runtime-session")

    async def exercise() -> None:
        factory = work_api.app.state.session_factory
        async with factory() as db:
            row = await db.get(Session, "runtime-session")
            row.delegation_metadata = {"retry_source": "prior-session"}
            row.delegation_mode = "sync"
            await db.commit()
        for _ in range(2):
            await persist_execution_metadata(
                factory,
                session_id="runtime-session",
                user_email="owner@example.com",
                model="resolved-model",
                provider_id="resolved-provider",
                profile_id="resolved-profile",
                reasoning_effort="medium",
                reasoning_mode="adaptive",
                turn_id="turn-current",
                runtime_selection_revision=0,
            )
        async with factory() as db:
            row = await db.get(Session, "runtime-session")
            assert row.delegation_metadata["retry_source"] == "prior-session"
            assert (
                row.delegation_metadata["execution_runtime"]["provider_id"] == "resolved-provider"
            )
            values = await _session_runtime_metadata(
                db, owner_email="owner@example.com", session_rows=[row], session_cache=None
            )
            assert values[row.session_id][:3] == ("resolved-model", "medium", "resolved-profile")
            original_metadata = dict(row.delegation_metadata)
            agent = await db.get(Agent, row.agent_id)
            agent.llm_config = {"model": "changed-default", "reasoning_effort": "high"}
            for lineage in ("retry", "fork", "rotation"):
                successor = await queries.create_session(
                    db,
                    session_id=f"runtime-{lineage}",
                    conversation_id="runtime-owner",
                    user_email=row.user_email,
                    agent_id=row.agent_id,
                    intaris_session_id=f"runtime-source-{lineage}",
                )
                successor.delegation_metadata = original_metadata
                successor.status = "completed"
                if lineage == "rotation":
                    successor.previous_session_id = row.session_id
            await db.commit()
        for lineage in ("retry", "fork", "rotation"):
            async with factory() as db:
                successor = await db.get(Session, f"runtime-{lineage}")
                values = await _session_runtime_metadata(
                    db,
                    owner_email="owner@example.com",
                    session_rows=[successor],
                    session_cache=None,
                )
                assert values[successor.session_id][0] is None
            await persist_execution_metadata(
                factory,
                session_id=f"runtime-{lineage}",
                user_email="owner@example.com",
                model=f"resolved-{lineage}",
                provider_id="resolved-provider",
                profile_id="resolved-profile",
                reasoning_effort=None,
                reasoning_mode=None,
            )
            async with factory() as db:
                successor = await db.get(Session, f"runtime-{lineage}")
                values = await _session_runtime_metadata(
                    db,
                    owner_email="owner@example.com",
                    session_rows=[successor],
                    session_cache=None,
                )
                assert values[successor.session_id][:2] == (f"resolved-{lineage}", None)
        with pytest.raises(ValueError, match="owner was not found"):
            await persist_execution_metadata(
                factory,
                session_id="runtime-session",
                user_email="other@example.com",
                model="wrong-owner",
                provider_id=None,
                profile_id=None,
                reasoning_effort=None,
                reasoning_mode=None,
            )

    asyncio.run(exercise())
    response = work_api.get(
        "/api/v1/chat/v2/sessions/runtime-session/activity-overview",
        headers=_auth_headers(work_api),
    )
    assert response.status_code == 200, response.text
    node = next(
        node for node in response.json()["workstreams"] if node["session_id"] == "runtime-session"
    )
    assert node["model"] == "resolved-model"
    assert node["reasoning_effort"] == "medium"
    assert node["agent_profile_id"] == "resolved-profile"
    assert node["runtime_selection_revision"] == 0
    assert isinstance(node["runtime_recorded_at"], str)


def test_active_runtime_identity_requires_session_matching_execution_evidence(
    work_api: TestClient,
) -> None:
    _seed_identity(work_api)
    _seed_conversation(
        work_api,
        conversation_id="runtime-evidence",
        session_id="runtime-source",
    )

    async def exercise() -> None:
        factory = work_api.app.state.session_factory
        await persist_execution_metadata(
            factory,
            session_id="runtime-source",
            user_email="owner@example.com",
            model="executed-model",
            provider_id="executed-provider",
            profile_id="executed-profile",
            reasoning_effort="low",
            reasoning_mode=None,
            turn_id="executed-turn",
            runtime_selection_revision=0,
        )
        async with factory() as db:
            source = await db.get(Session, "runtime-source")
            agent = await db.get(Agent, source.agent_id)
            agent.default_agent_profile_id = "changed-profile"
            agent.agent_profiles = {
                "changed-profile": {
                    "model": "changed-model",
                    "reasoning_effort": "high",
                }
            }
            idle_unproven = await queries.create_session(
                db,
                session_id="runtime-idle-unproven",
                conversation_id="runtime-evidence",
                user_email=source.user_email,
                agent_id=source.agent_id,
                agent_profile_id="changed-profile",
                model_override="prospective-idle-model",
                reasoning_effort_override="high",
                intaris_session_id="runtime-idle-unproven",
            )
            delegated_unproven = await queries.create_session(
                db,
                session_id="runtime-delegated-unproven",
                conversation_id="runtime-evidence",
                user_email=source.user_email,
                agent_id=source.agent_id,
                agent_profile_id="changed-profile",
                model_override="prospective-delegated-model",
                reasoning_effort_override="high",
                parent_session_id=source.session_id,
                delegation_mode="delegate",
                delegation_metadata={
                    "model": "delegation-request-model",
                    "reasoning_effort": "high",
                },
                intaris_session_id="runtime-delegated-unproven",
            )
            copied = await queries.create_session(
                db,
                session_id="runtime-copied",
                conversation_id="runtime-evidence",
                user_email=source.user_email,
                agent_id=source.agent_id,
                intaris_session_id="runtime-copied",
            )
            copied.delegation_metadata = dict(source.delegation_metadata)
            source.status = "completed"
            await db.commit()

            values = await _session_runtime_metadata(
                db,
                owner_email="owner@example.com",
                session_rows=[source, idle_unproven, delegated_unproven, copied],
                session_cache=None,
            )
            assert values[source.session_id][:3] == (
                "executed-model",
                "low",
                "executed-profile",
            )
            for unproven in (idle_unproven, delegated_unproven, copied):
                value = values.get(unproven.session_id)
                assert value is None or value[:3] == (None, None, None)

    asyncio.run(exercise())


def test_work_and_overview_return_durable_owner_revision(work_api: TestClient) -> None:
    _seed_identity(work_api)
    _seed_conversation(
        work_api,
        conversation_id="revision-contract",
        session_id="revision-session",
    )

    async def bump() -> int:
        async with work_api.app.state.session_factory() as db:
            await bump_live_work_revision(db, "owner@example.com")
            revision = await bump_live_work_revision(db, "owner@example.com")
            await db.commit()
            return revision

    revision = asyncio.run(bump())
    headers = _auth_headers(work_api)
    work = work_api.get(
        "/api/v1/chat/v2/conversations/revision-contract/work",
        headers=headers,
    )
    overview = work_api.get(
        "/api/v1/chat/v2/conversations/revision-contract/activity-overview",
        headers=headers,
    )
    assert work.status_code == 200
    assert overview.status_code == 200
    assert work.json()["work_revision"] == revision
    assert overview.json()["work_revision"] == revision


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/chat/v2/conversations/sessionless/activity-overview",
        "/api/v1/chat/v2/conversations/sessionless/work",
    ],
)
def test_sessionless_conversation_is_empty_live(
    work_api: TestClient,
    path: str,
) -> None:
    _seed_identity(work_api)
    _seed_conversation(work_api, conversation_id="sessionless")

    response = work_api.get(path, headers=_auth_headers(work_api))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["materialization"]["state"] == "live"
    assert payload["workstreams"] == []
    if path.endswith("activity-overview"):
        assert payload["summary"] == {
            "mutations": 0,
            "commands": 0,
            "changed_files": 0,
            "artifacts": 0,
            "deliverables": 0,
            "additions": 0,
            "deletions": 0,
            "omitted_files": 0,
        }


def test_completed_conversation_with_null_active_pointer_uses_historical_root(
    work_api: TestClient,
) -> None:
    _seed_identity(work_api)
    _seed_conversation(
        work_api,
        conversation_id="completed",
        status="completed",
        session_id="historical",
    )

    for suffix in ("activity-overview", "work"):
        response = work_api.get(
            f"/api/v1/chat/v2/conversations/completed/{suffix}",
            headers=_auth_headers(work_api),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["materialization"]["state"] == "live"
        assert len(payload["workstreams"]) == 1
        assert payload["workstreams"][0]["session_id"] == "historical"


def test_active_session_precedence_maps_idle_and_terminal_states(
    work_api: TestClient,
) -> None:
    _seed_identity(work_api)

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id="states",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            terminal = await queries.create_session(
                db,
                session_id="state-terminal",
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id="source-terminal",
            )
            terminal.status = "completed"
            active = await queries.create_session(
                db,
                session_id="state-active",
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id="source-active",
            )
            await queries.update_conversation_active_session(
                db,
                conversation.conversation_id,
                active.session_id,
            )
            await db.commit()

    asyncio.run(seed())
    response = work_api.get(
        "/api/v1/chat/v2/conversations/states/activity-overview",
        headers=_auth_headers(work_api),
    )

    assert response.status_code == 200, response.text
    nodes = {node["session_id"]: node for node in response.json()["workstreams"]}
    assert nodes["state-active"]["current"] is True
    assert nodes["state-active"]["execution_state"] == "idle"
    assert "state-terminal" not in nodes
    terminal_response = work_api.get(
        "/api/v1/chat/v2/sessions/state-terminal/activity-overview",
        headers=_auth_headers(work_api),
    )
    assert terminal_response.status_code == 200, terminal_response.text
    terminal_node = terminal_response.json()["workstreams"][0]
    assert terminal_node["current"] is False
    assert terminal_node["execution_state"] == "completed"


@pytest.mark.parametrize(
    ("evidence_kind", "status", "expected"),
    [
        ("direct", "running", "running"),
        ("direct", "recoverable", "recovering"),
        ("direct", "completed", "completed"),
        ("task", "running", "running"),
        ("task", "paused", "waiting"),
        ("task", "completed", "completed"),
        ("managed", "running", "running"),
        ("managed", "paused", "waiting"),
        ("managed", "failed", "failed"),
        ("managed", "interrupted", "failed"),
        ("managed", "completed", "completed"),
    ],
)
def test_public_overview_maps_persisted_execution_evidence(
    work_api: TestClient,
    evidence_kind: str,
    status: str,
    expected: str,
) -> None:
    _seed_identity(work_api)

    async def seed() -> str:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id=f"evidence-{evidence_kind}-{status}",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            root = await queries.create_session(
                db,
                session_id=f"session-{evidence_kind}-{status}",
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id=f"source-{evidence_kind}-{status}",
            )
            await queries.update_conversation_active_session(
                db, conversation.conversation_id, root.session_id
            )
            if evidence_kind == "direct":
                db.add(
                    DirectTurnRequestRow(
                        request_id=f"request-{status}",
                        turn_id=f"turn-{status}",
                        conversation_id=conversation.conversation_id,
                        session_id=root.session_id,
                        agent_id=root.agent_id,
                        user_id=root.user_email,
                        idempotency_scope="work-conformance",
                        idempotency_key=f"direct-{status}",
                        admission_hash="admission",
                        payload_hash="payload",
                        payload={},
                        status=status,
                    )
                )
                unrelated = await queries.create_conversation(
                    db,
                    conversation_id=f"unrelated-{status}",
                    user_email=root.user_email,
                    agent_id=root.agent_id,
                    context_type="web",
                )
                unrelated_session = await queries.create_session(
                    db,
                    session_id=f"unrelated-session-{status}",
                    conversation_id=unrelated.conversation_id,
                    user_email=root.user_email,
                    agent_id=root.agent_id,
                    intaris_session_id=f"unrelated-source-{status}",
                )
                db.add(
                    DirectTurnRequestRow(
                        request_id=f"unrelated-request-{status}",
                        turn_id=f"unrelated-turn-{status}",
                        conversation_id=unrelated.conversation_id,
                        session_id=unrelated_session.session_id,
                        agent_id=root.agent_id,
                        user_id=root.user_email,
                        idempotency_scope="work-conformance",
                        idempotency_key=f"unrelated-{status}",
                        admission_hash="admission",
                        payload_hash="payload",
                        payload={},
                        status="running",
                    )
                )
            elif evidence_kind == "task":
                task = await queries.create_task(
                    db,
                    created_by=root.user_email,
                    agent_id=root.agent_id,
                    title="Evidence task",
                    status="completed" if status == "completed" else "running",
                    source_session_id=root.session_id,
                )
                step = await queries.create_step_run(
                    db,
                    task_id=task.task_id,
                    step_name="run",
                    step_type="run",
                    agent_id=root.agent_id,
                    conversation_id=conversation.conversation_id,
                    status=status,
                )
                step.session_id = root.session_id
            else:
                active_turn_id = None
                if status == "running":
                    active_turn_id = f"managed-turn-{status}"
                    db.add(
                        DirectTurnRequestRow(
                            request_id=f"managed-request-{status}",
                            turn_id=active_turn_id,
                            conversation_id=conversation.conversation_id,
                            session_id=root.session_id,
                            agent_id=root.agent_id,
                            user_id=root.user_email,
                            idempotency_scope="work-conformance",
                            idempotency_key=f"managed-{status}",
                            admission_hash="admission",
                            payload_hash="payload",
                            payload={},
                            status=status,
                        )
                    )
                elif status == "paused":
                    db.add(
                        DirectTurnRequestRow(
                            request_id="managed-request-settled",
                            turn_id="managed-turn-settled",
                            conversation_id=conversation.conversation_id,
                            session_id=root.session_id,
                            agent_id=root.agent_id,
                            user_id=root.user_email,
                            idempotency_scope="work-conformance",
                            idempotency_key="managed-settled",
                            admission_hash="admission",
                            payload_hash="payload",
                            payload={},
                            status="completed",
                        )
                    )
                db.add(
                    ManagedConversationLink(
                        link_id=f"link-{status}",
                        user_email=root.user_email,
                        controller_agent_id=root.agent_id,
                        controller_conversation_id=conversation.conversation_id,
                        controller_session_id=root.session_id,
                        target_agent_id=root.agent_id,
                        target_conversation_id=conversation.conversation_id,
                        target_session_id=root.session_id,
                        title="Managed evidence",
                        conversation_state=("completed" if status == "completed" else "open"),
                        turn_state=status,
                        active_turn_id=active_turn_id,
                    )
                )
            await db.commit()
            return conversation.conversation_id

    conversation_id = asyncio.run(seed())
    response = work_api.get(
        f"/api/v1/chat/v2/conversations/{conversation_id}/activity-overview",
        headers=_auth_headers(work_api),
    )
    work_response = work_api.get(
        f"/api/v1/chat/v2/conversations/{conversation_id}/work",
        headers=_auth_headers(work_api),
    )
    assert response.status_code == 200, response.text
    assert work_response.status_code == 200, work_response.text
    assert response.json()["workstreams"][0]["execution_state"] == expected
    assert work_response.json()["workstreams"][0]["execution_state"] == expected


def test_active_delegate_without_direct_admission_remains_running(work_api: TestClient) -> None:
    _seed_identity(work_api)

    async def seed() -> str:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id="delegate-lifecycle",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            parent = await queries.create_session(
                db,
                session_id="delegate-parent",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="delegate-parent-source",
            )
            child = await queries.create_session(
                db,
                session_id="delegate-child",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="delegate-child-source",
                parent_session_id=parent.session_id,
                activity_scope_id=parent.activity_scope_id,
            )
            child.delegation_mode = "sync"
            child.status = "active"
            await queries.update_conversation_active_session(
                db, conversation.conversation_id, parent.session_id
            )
            await db.commit()
            return conversation.conversation_id

    conversation_id = asyncio.run(seed())
    for endpoint in ("activity-overview", "work"):
        response = work_api.get(
            f"/api/v1/chat/v2/conversations/{conversation_id}/{endpoint}",
            headers=_auth_headers(work_api),
        )
        assert response.status_code == 200, response.text
        nodes = {node["session_id"]: node for node in response.json()["workstreams"]}
        assert nodes["delegate-parent"]["execution_state"] == "idle"
        assert "delegate-child" in nodes, nodes
        assert nodes["delegate-child"]["execution_state"] == "running"


def test_rotation_uses_current_conversation_turn_and_keeps_queued_successor_distinct(
    work_api: TestClient,
) -> None:
    _seed_identity(work_api)

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id="lifecycle-rotation",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            previous = await queries.create_session(
                db,
                session_id="lifecycle-old",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="source-lifecycle-old",
                activity_scope_id="lifecycle-scope",
            )
            previous.status = "completed"
            current = await queries.create_session(
                db,
                session_id="lifecycle-current",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="source-lifecycle-current",
                activity_scope_id="lifecycle-scope",
                previous_session_id=previous.session_id,
            )
            await queries.update_conversation_active_session(
                db, conversation.conversation_id, current.session_id
            )
            for suffix, session_id, status in (
                ("previous", previous.session_id, "completed"),
                ("current", previous.session_id, "running"),
                ("successor", current.session_id, "queued"),
            ):
                db.add(
                    DirectTurnRequestRow(
                        request_id=f"lifecycle-request-{suffix}",
                        turn_id=f"lifecycle-turn-{suffix}",
                        conversation_id=conversation.conversation_id,
                        session_id=session_id,
                        agent_id=conversation.agent_id,
                        user_id=conversation.user_email,
                        idempotency_scope="work-conformance",
                        idempotency_key=f"lifecycle-{suffix}",
                        admission_hash="admission",
                        payload_hash="payload",
                        payload={},
                        status=status,
                    )
                )
            await db.commit()

    asyncio.run(seed())
    response = work_api.get(
        "/api/v1/chat/v2/conversations/lifecycle-rotation/activity-overview",
        headers=_auth_headers(work_api),
    )

    assert response.status_code == 200, response.text
    workstreams = response.json()["workstreams"]
    assert len(workstreams) == 1
    assert workstreams[0]["backing_session_ids"] == ["lifecycle-current", "lifecycle-old"]
    assert workstreams[0]["execution_state"] == "running"


def test_stale_running_managed_link_without_durable_turn_is_idle(work_api: TestClient) -> None:
    _seed_identity(work_api)

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id="stale-managed",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            session = await queries.create_session(
                db,
                session_id="stale-managed-session",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="source-stale-managed",
            )
            await queries.update_conversation_active_session(
                db, conversation.conversation_id, session.session_id
            )
            db.add(
                DirectTurnRequestRow(
                    request_id="stale-managed-settled-request",
                    turn_id="stale-managed-settled-turn",
                    conversation_id=conversation.conversation_id,
                    session_id=session.session_id,
                    agent_id=conversation.agent_id,
                    user_id=conversation.user_email,
                    idempotency_scope="work-conformance",
                    idempotency_key="stale-managed-settled",
                    admission_hash="admission",
                    payload_hash="payload",
                    payload={},
                    status="completed",
                )
            )
            db.add(
                ManagedConversationLink(
                    link_id="stale-managed-link",
                    user_email=conversation.user_email,
                    controller_agent_id=conversation.agent_id,
                    controller_conversation_id=conversation.conversation_id,
                    controller_session_id=session.session_id,
                    target_agent_id=conversation.agent_id,
                    target_conversation_id=conversation.conversation_id,
                    target_session_id=session.session_id,
                    title="Stale managed evidence",
                    conversation_state="open",
                    turn_state="running",
                    active_turn_id="missing-turn",
                )
            )
            await db.commit()

    asyncio.run(seed())
    response = work_api.get(
        "/api/v1/chat/v2/conversations/stale-managed/activity-overview",
        headers=_auth_headers(work_api),
    )
    work_response = work_api.get(
        "/api/v1/chat/v2/conversations/stale-managed/work",
        headers=_auth_headers(work_api),
    )

    assert response.status_code == 200, response.text
    assert work_response.status_code == 200, work_response.text
    assert response.json()["workstreams"][0]["execution_state"] == "idle"
    assert work_response.json()["workstreams"][0]["execution_state"] == "idle"


def test_current_turn_does_not_leak_into_old_activity_scope(work_api: TestClient) -> None:
    _seed_identity(work_api)

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id="isolated-scopes",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            old = await queries.create_session(
                db,
                session_id="isolated-old",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="source-isolated-old",
                activity_scope_id="old-scope",
            )
            old.status = "completed"
            current = await queries.create_session(
                db,
                session_id="isolated-current",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="source-isolated-current",
                activity_scope_id="current-scope",
            )
            await queries.update_conversation_active_session(
                db, conversation.conversation_id, current.session_id
            )
            db.add(
                DirectTurnRequestRow(
                    request_id="isolated-request",
                    turn_id="isolated-turn",
                    conversation_id=conversation.conversation_id,
                    session_id=current.session_id,
                    agent_id=conversation.agent_id,
                    user_id=conversation.user_email,
                    idempotency_scope="work-conformance",
                    idempotency_key="isolated-current",
                    admission_hash="admission",
                    payload_hash="payload",
                    payload={},
                    status="running",
                )
            )
            await db.commit()

    asyncio.run(seed())
    headers = _auth_headers(work_api)
    for suffix in ("activity-overview", "work"):
        response = work_api.get(
            f"/api/v1/chat/v2/sessions/isolated-old/{suffix}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["workstreams"][0]["execution_state"] == "completed"


def test_null_session_recovery_belongs_to_current_scope(work_api: TestClient) -> None:
    _seed_identity(work_api)

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id="null-session-recovery",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            current = await queries.create_session(
                db,
                session_id="null-session-current",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="source-null-session-current",
                activity_scope_id="current-scope",
            )
            await queries.update_conversation_active_session(
                db, conversation.conversation_id, current.session_id
            )
            db.add(
                DirectTurnRequestRow(
                    request_id="null-session-request",
                    turn_id="null-session-turn",
                    conversation_id=conversation.conversation_id,
                    session_id=None,
                    agent_id=conversation.agent_id,
                    user_id=conversation.user_email,
                    idempotency_scope="work-conformance",
                    idempotency_key="null-session-current",
                    admission_hash="admission",
                    payload_hash="payload",
                    payload={},
                    status="recoverable",
                )
            )
            await db.commit()

    asyncio.run(seed())
    headers = _auth_headers(work_api)
    for suffix in ("activity-overview", "work"):
        response = work_api.get(
            f"/api/v1/chat/v2/conversations/null-session-recovery/{suffix}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["workstreams"][0]["execution_state"] == "recovering"


def test_newer_rotation_owns_lifecycle_over_terminal_predecessor(work_api: TestClient) -> None:
    _seed_identity(work_api)

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id="terminal-predecessor",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            old = await queries.create_session(
                db,
                session_id="terminal-predecessor-old",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="source-terminal-predecessor-old",
                activity_scope_id="shared-scope",
            )
            old.status = "completed"
            current = await queries.create_session(
                db,
                session_id="terminal-predecessor-current",
                conversation_id=conversation.conversation_id,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                intaris_session_id="source-terminal-predecessor-current",
                activity_scope_id="shared-scope",
                previous_session_id=old.session_id,
            )
            await queries.update_conversation_active_session(
                db, conversation.conversation_id, current.session_id
            )
            await db.commit()

    asyncio.run(seed())
    headers = _auth_headers(work_api)
    for suffix in ("activity-overview", "work"):
        response = work_api.get(
            f"/api/v1/chat/v2/conversations/terminal-predecessor/{suffix}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        workstream = response.json()["workstreams"][0]
        assert workstream["session_id"] == "terminal-predecessor-current"
        assert workstream["execution_state"] == "idle"


def test_rotations_collapse_and_retain_deduped_files_during_partial_recovery(
    work_api: TestClient,
) -> None:
    _seed_identity(work_api)

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            conversation = await queries.create_conversation(
                db,
                conversation_id="rotations",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                context_type="web",
            )
            first = await queries.create_session(
                db,
                session_id="rotation-1",
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id="source-rotation-1",
                activity_scope_id="activity-1",
            )
            second = await queries.create_session(
                db,
                session_id="rotation-2",
                conversation_id=conversation.conversation_id,
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                intaris_session_id="source-rotation-2",
                activity_scope_id="activity-1",
                previous_session_id=first.session_id,
            )
            await queries.update_conversation_active_session(
                db,
                conversation.conversation_id,
                second.session_id,
            )
            projections = list(
                (
                    await db.scalars(
                        select(WorkSessionProjectionRow).where(
                            WorkSessionProjectionRow.session_id.in_(
                                [first.session_id, second.session_id]
                            )
                        )
                    )
                ).all()
            )
            assert len(projections) == 2
            projections[0].state = "failed"
            projections[0].last_error = "retained history gap"
            for index, session in enumerate((first, second), start=1):
                db.add(
                    WorkCurrentFileRow(
                        current_file_id=f"current-{index}",
                        owner_email="owner@example.com",
                        session_id=session.session_id,
                        materializer_version=WORK_MATERIALIZER_VERSION,
                        file_projector_version="work-files-v3",
                        path_generation_id="path-generation-shared",
                        source_session_id=f"source-{session.session_id}",
                        source_seq=index,
                        source_item_id="tool:shared",
                        path="src/shared.py",
                        path_id="repo:src/shared.py",
                        root_id="repo",
                        state="modified",
                    )
                )
            await db.commit()

    asyncio.run(seed())
    overview = work_api.get(
        "/api/v1/chat/v2/conversations/rotations/activity-overview",
        headers=_auth_headers(work_api),
    )
    files = work_api.get(
        "/api/v1/chat/v2/conversations/rotations/work?category=files",
        headers=_auth_headers(work_api),
    )

    assert overview.status_code == files.status_code == 200
    overview_payload = overview.json()
    files_payload = files.json()
    assert overview_payload["materialization"]["state"] == "partial"
    assert len(overview_payload["workstreams"]) == 1
    assert overview_payload["workstreams"][0]["backing_session_ids"] == [
        "rotation-1",
        "rotation-2",
    ]
    assert files_payload["materialization"]["state"] == "partial"
    assert len(files_payload["mutations"]) == 1
    assert len(files_payload["mutations"][0]["file_diffs"]) == 1
    assert files_payload["mutations"][0]["file_diffs"][0]["path_id"] == "repo:src/shared.py"
    assert files_payload["mutations"][0]["file_stats"][0]["path_id"] == "repo:src/shared.py"
    assert files_payload["mutations"][0]["file_stats"][0]["preview_available"] is True


def test_conversation_work_routes_have_bounded_query_count(work_api: TestClient) -> None:
    _seed_identity(work_api)
    _seed_conversation(
        work_api,
        conversation_id="budget",
        session_id="budget-session",
    )
    engine = client_engine = work_api.app.state.engine
    counts: list[str] = []

    def count_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        counts.append(statement)

    event.listen(client_engine.sync_engine, "before_cursor_execute", count_query)
    try:
        results: dict[str, int] = {}
        for suffix in ("activity-overview", "work"):
            counts.clear()
            response = work_api.get(
                f"/api/v1/chat/v2/conversations/budget/{suffix}",
                headers=_auth_headers(work_api),
            )
            assert response.status_code == 200, response.text
            results[suffix] = len(counts)
        assert results["activity-overview"] <= 50, results
        assert results["work"] <= 50, results

        async def add_descendants() -> None:
            async with work_api.app.state.session_factory() as db:
                for index in range(12):
                    await queries.create_session(
                        db,
                        session_id=f"budget-child-{index}",
                        conversation_id="budget",
                        user_email="owner@example.com",
                        agent_id="agent-owner@example.com",
                        intaris_session_id=f"source-budget-child-{index}",
                        parent_session_id="budget-session",
                    )
                await db.commit()

        asyncio.run(add_descendants())
        scaled: dict[str, int] = {}
        for suffix in ("activity-overview", "work"):
            counts.clear()
            response = work_api.get(
                f"/api/v1/chat/v2/conversations/budget/{suffix}",
                headers=_auth_headers(work_api),
            )
            assert response.status_code == 200, response.text
            scaled[suffix] = len(counts)
        assert scaled["activity-overview"] <= results["activity-overview"] + 8, (
            results,
            scaled,
        )
        assert scaled["work"] <= results["work"] + 8, (results, scaled)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_query)


def test_file_history_preserves_exact_source_and_path_identity(
    work_api: TestClient,
) -> None:
    _seed_identity(work_api)
    _seed_conversation(
        work_api,
        conversation_id="history",
        session_id="history-session",
    )

    async def seed() -> None:
        async with work_api.app.state.session_factory() as db:
            projection = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == "history-session"
                )
            )
            assert projection is not None
            projection.covered_through_seq = 7
            projection.target_seq = 7
            item = ToolCallTimelineItem(
                id="tool:exact-call",
                sort_key="7",
                call_id="exact-call",
                tool_name="write",
                status="complete",
            )
            db.add(
                WorkRecordRow(
                    work_record_id="history-record",
                    owner_email="owner@example.com",
                    session_id="history-session",
                    source_store="intaris",
                    source_session_id="retained-source",
                    source_seq=7,
                    source_item_id=item.id,
                    item_ordinal=0,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    occurred_at=datetime.now(UTC),
                    record_type=item.kind,
                    timeline_item=item.model_dump(mode="json"),
                )
            )
            await db.flush()
            db.add(
                WorkRecordFileRow(
                    work_record_file_id="history-file",
                    owner_email="owner@example.com",
                    session_id="history-session",
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    work_record_id="history-record",
                    file_ordinal=0,
                    path="src/exact.py",
                    path_id="repo:src/exact.py",
                    path_generation_id="path-generation-exact",
                    source_seq=7,
                    item_ordinal=0,
                    additions=1,
                    deletions=0,
                    status="modified",
                )
            )
            await db.commit()

    asyncio.run(seed())
    event_page = SessionEventPage(
        store_id="intaris",
        session_id="retained-source",
        events=[
            RawSessionEvent(
                store_id="intaris",
                session_id="retained-source",
                seq=7,
                type="tool_result",
                data={
                    "call_id": "exact-call",
                    "name": "write",
                    "file_diffs": [{"path": "src/exact.py", "diff": "+line"}],
                    "status": "complete",
                },
            )
        ],
        first_seq=7,
        last_seq=7,
        availability=SessionHistoryAvailability(
            durable_last_seq=7,
            first_available_seq=1,
        ),
    )
    reader = SimpleNamespace(read_session_events=AsyncMock(return_value=event_page))
    work_api.app.state.cached_event_store.bind = Mock(return_value=reader)
    payload = {
        "scope": {
            "key": "conversation:history",
            "kind": "conversation",
            "conversation_id": "history",
        },
        "path_generation_id": "path-generation-exact",
        "limit": 20,
    }

    response = work_api.post(
        "/api/v1/work/file-history",
        headers=_auth_headers(work_api),
        json=payload,
    )

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["path_id"] == "repo:src/exact.py"
    assert item["path_generation_id"] == "path-generation-exact"
    assert item["source_item_id"] == "tool:exact-call"
    reader.read_session_events.assert_awaited_once()
    assert reader.read_session_events.await_args.kwargs["session_id"] == "retained-source"

    bad_cursor = work_api.post(
        "/api/v1/work/file-history",
        headers=_auth_headers(work_api),
        json={**payload, "before": "tampered"},
    )
    assert bad_cursor.status_code == 409
    assert "cursor_invalid" in bad_cursor.text


def test_refresh_prioritizes_authorized_graph_without_foreground_intaris(
    work_api: TestClient,
) -> None:
    _seed_identity(work_api)
    _seed_conversation(
        work_api,
        conversation_id="refresh",
        session_id="refresh-session",
    )
    prioritized = AsyncMock()
    work_api.app.state.work_materializer.prioritize_sessions = prioritized

    response = work_api.post(
        "/api/v1/work/refresh",
        headers=_auth_headers(work_api),
        json={
            "scope": {
                "key": "conversation:refresh",
                "kind": "conversation",
                "conversation_id": "refresh",
            }
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] is True
    assert response.json()["session_count"] == 1
    prioritized.assert_awaited_once()
    assert [row.session_id for row in prioritized.await_args.args[0]] == ["refresh-session"]

    _seed_identity(work_api, "foreign@example.com")
    denied = work_api.post(
        "/api/v1/work/refresh",
        headers=_auth_headers(work_api, "foreign@example.com"),
        json={
            "scope": {
                "key": "conversation:refresh",
                "kind": "conversation",
                "conversation_id": "refresh",
            }
        },
    )
    assert denied.status_code == 400
