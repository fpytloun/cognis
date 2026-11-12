"""Real Cognis clients against an isolated peer Intaris HTTP process.

Opt in with COGNIS_INTARIS_SOURCE and optionally COGNIS_INTARIS_PYTHON and
COGNIS_INTARIS_REVISION. The peer's existing interpreter is never synchronized.
No production credentials, session/API mocks, or outbound inference are used.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cognis.core.managed_conversations import inherited_managed_session_policy
from cognis.core.session import SessionManager
from cognis.core.tool_router import ToolRouter
from cognis.core.workflow_step_scope import WorkflowStepDeclarationError, WorkflowStepScope
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.session import ConversationContext, SessionEvent
from cognis.models.task import TaskModel
from cognis.models.tool import (
    NativeToolDefinition,
    Permission,
    ToolCall,
    ToolResult,
    ToolSource,
)
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState, pin_effective_workflow
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.backends.guardrails.null import NoGuardrailsProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.runtime_context import RuntimeAccessContext, scoped_runtime_context
from cognis.store.models import Agent
from cognis.store.queries import get_agent
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.unit.test_session_manager import _Cache, _session_factory

USER = "user@example.com"


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@dataclass
class PeerService:
    source: Path
    python: Path
    data: Path
    auth: JWTAuthProvider
    public_key: Path
    process: subprocess.Popen | None = None
    url: str = ""
    generation: int = 0
    log: Any = None
    reports: list[dict[str, Any]] = field(default_factory=list)

    def start(self) -> None:
        """Reserve a random loopback socket and launch only the peer interpreter."""
        self.generation += 1
        safety_path = self.data / f"safety-{self.generation}.json"
        self.log = (self.data / f"server-{self.generation}.log").open("w")
        env = {
            "PATH": os.defpath,
            "HOME": str(self.data),
            "PYTHONPATH": str(self.source),
            "PYTHONUNBUFFERED": "1",
            "DATA_DIR": str(self.data),
            "DB_PATH": str(self.data / "intaris.db"),
            "DB_BACKEND": "sqlite",
            "LLM_API_KEY": "synthetic-wire-inference-disabled",
            "LLM_BASE_URL": "http://127.0.0.1:1/v1",
            "EVALUATOR_BACKEND": "llm",
            "ANALYSIS_ENABLED": "false",
            "JUDGE_MODE": "disabled",
            "METRICS_ENABLED": "false",
            "INTARIS_SEARCH_ENABLED": "false",
            "EVENT_STORE_ENABLED": "true",
            "EVENT_STORE_BACKEND": "filesystem",
            "EVENT_STORE_PATH": str(self.data / "events"),
            "INTARIS_API_KEY": "synthetic-wire-local-key",
            "INTARIS_JWT_PUBLIC_KEY": str(self.public_key),
            "RATE_LIMIT": "0",
            "INTARIS_WIRE_SAFETY": str(safety_path),
        }
        launcher = Path(__file__).parent / "fixtures/intaris_session_modes_server.py"
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            self.url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            self.process = subprocess.Popen(
                [str(self.python), str(launcher), str(listener.fileno())],
                cwd=self.source,
                env=env,
                pass_fds=(listener.fileno(),),
                stdout=self.log,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 25
            with httpx.Client(trust_env=False, timeout=0.5) as probe:
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        break
                    try:
                        if probe.get(f"{self.url}/health").is_success:
                            return
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
        self.stop()
        pytest.fail((self.data / f"server-{self.generation}.log").read_text())

    def stop(self) -> None:
        """Stop only our child process and verify the no-inference fences."""
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                pytest.fail("Disposable Intaris failed graceful shutdown")
            finally:
                self.process = None
                self.log.close()
            report = json.loads((self.data / f"safety-{self.generation}.json").read_text())
            self.reports.append(report)
            print("WIRE_SAFETY", json.dumps(report, sort_keys=True))
            assert report["outbound_attempts"] == report["model_calls"] == 0
            assert Path(report["intaris_module"]).resolve().is_relative_to(self.source)
            assert Path(report["python"]) == self.python


@pytest.fixture(scope="module")
def peer(tmp_path_factory):
    source_env = os.environ.get("COGNIS_INTARIS_SOURCE")
    if not source_env:
        pytest.skip("COGNIS_INTARIS_SOURCE must name a disposable peer checkout")
    source = Path(source_env).resolve()
    python = Path(os.environ.get("COGNIS_INTARIS_PYTHON", str(source / ".venv/bin/python")))
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    expected = os.environ.get("COGNIS_INTARIS_REVISION")
    assert not expected or revision == expected
    data = tmp_path_factory.mktemp("intaris-wire")
    key = ec.generate_private_key(ec.SECP256R1())
    private, public = data / "private.pem", data / "public.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    service = PeerService(source, python, data, JWTAuthProvider(private, public), public)
    print("WIRE_PEER", revision, source, python)
    service.start()
    try:
        yield service
    finally:
        service.stop()


def provider(peer: PeerService, trace: list | None = None) -> IntarisProvider:
    result = IntarisProvider(peer.url, peer.auth, user_email=USER)
    if trace is not None:

        async def observe(response: httpx.Response) -> None:
            await response.aread()
            trace.append(
                (
                    response.request.method,
                    response.request.url.path,
                    json.loads(response.request.content) if response.request.content else None,
                    response.status_code,
                    response.json(),
                )
            )

        result.client.event_hooks["response"] = [observe]
    return result


async def flags(client: IntarisProvider, sid: str, agent: str = "wire-agent") -> tuple[bool, bool]:
    value = await client.get_session(sid, user_email=USER, agent_id=agent)
    return value.analytics_enabled, value.guardrails_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("analytics", [False, True])
@pytest.mark.parametrize("guardrails", [False, True])
async def test_real_create_patch_rehydrate_and_record(peer, analytics, guardrails):
    client = provider(peer)
    sid = f"wire-{uuid.uuid4().hex}"
    selected = client if guardrails else NoGuardrailsProvider(client)
    try:
        await selected.create_session(
            sid, "Read synthetic inputs", "wire-agent", analytics_enabled=analytics
        )
        assert await flags(client, sid) == (analytics, guardrails)
        initial = await selected.record_events(
            sid,
            [SessionEvent(type="lifecycle", data={"kind": "before_toggle"})],
            user_email=USER,
            agent_id="wire-agent",
        )
        assert initial.ok and initial.count == 1
        # A real 409 create retry must verify the stored flags, not infer success.
        await selected.create_session(
            sid, "Read synthetic inputs", "wire-agent", analytics_enabled=analytics
        )
        headers = client._headers("wire-agent", USER)
        for body in ({}, {"analytics_enabled": None, "guardrails_enabled": None}):
            response = await client.client.patch(
                f"/api/v1/session/{sid}", headers=headers, json=body
            )
            response.raise_for_status()
            assert await flags(client, sid) == (analytics, guardrails)
        await client.update_session_policy(
            sid, agent_id="wire-agent", analytics_enabled=False, guardrails_enabled=False
        )
        assert await flags(client, sid) == (False, False)
        await client.update_session_policy(
            sid, agent_id="wire-agent", analytics_enabled=None, guardrails_enabled=None
        )
        assert await flags(client, sid) == (False, False)
        appended = await client.record_events(
            sid,
            [
                SessionEvent(
                    type="user_message", data={"role": "user", "content": "Synthetic input"}
                )
            ],
            user_email=USER,
            agent_id="wire-agent",
        )
        assert appended.ok and appended.count == 1
        cold = provider(peer)
        try:
            assert await flags(cold, sid) == (False, False)
            with scoped_runtime_context(
                access_context=RuntimeAccessContext(user_email=USER, agent_id="wire-agent")
            ):
                history = await cold.read_events(sid)
            assert any(
                event["data"].get("content") == "Synthetic input" for event in history.events
            )
            assert any(event["data"].get("kind") == "before_toggle" for event in history.events)
        finally:
            await cold.client.aclose()
    finally:
        await client.client.aclose()


@pytest.mark.asyncio
async def test_real_false_ack_rejection_and_restart(peer):
    client = provider(peer)
    sid = f"wire-ack-{uuid.uuid4().hex}"
    try:
        await client.create_session(sid, "Synthetic read", "wire-agent")
        with pytest.raises(ValueError, match="analytics_enabled"):
            await client.create_session(
                sid, "Synthetic read", "wire-agent", analytics_enabled=False
            )
        await client.update_session_policy(sid, agent_id="wire-agent", analytics_enabled=False)
        with pytest.raises(ValueError, match="guardrails_enabled"):
            await NoGuardrailsProvider(client).create_session(
                sid,
                "Synthetic read",
                "wire-agent",
                analytics_enabled=False,
            )
        await client.update_session_policy(sid, agent_id="wire-agent", guardrails_enabled=False)
        await client.record_events(
            sid,
            [SessionEvent(type="lifecycle", data={"kind": "wire_restart"})],
            user_email=USER,
            agent_id="wire-agent",
        )
    finally:
        await client.client.aclose()
    peer.stop()
    peer.start()
    cold = provider(peer)
    try:
        await NoGuardrailsProvider(cold).create_session(
            sid,
            "Synthetic read",
            "wire-agent",
            analytics_enabled=False,
        )
        assert await flags(cold, sid) == (False, False)
        with scoped_runtime_context(
            access_context=RuntimeAccessContext(user_email=USER, agent_id="wire-agent")
        ):
            history = await cold.read_events(sid)
        assert any(event["data"].get("kind") == "wire_restart" for event in history.events)
    finally:
        await cold.client.aclose()


@pytest.mark.asyncio
async def test_delegation_inherits_activated_allowances_not_ambient_task_grants(peer, tmp_path):
    engine, factory = await _session_factory(
        tmp_path,
        capabilities={
            "memory_backend": "none",
            "guardrails_backend": "none",
            "analytics_enabled": False,
            "intaris_evaluation": "balanced",
        },
    )
    client = provider(peer)
    manager = SessionManager(factory, SimpleNamespace(guardrails=client, memory=object()), _Cache())
    try:
        with scoped_runtime_context(
            access_context=RuntimeAccessContext(
                user_email=USER,
                agent_id="agent-1",
                session_policy={"allow_policies": ["Inspect the application source"]},
            )
        ):
            _, parent = await manager.create_conversation_with_root_session(
                user_email=USER,
                agent_id="agent-1",
                context=ConversationContext(type="web"),
            )
        # Prove that Intaris still refuses independently added child authority.
        with pytest.raises(httpx.HTTPStatusError) as rejected:
            await client.create_session(
                f"wire-refused-child-{uuid.uuid4().hex}",
                "Review synthetic source",
                "agent-1",
                parent_session_id=parent.session_id,
                policy={
                    "evaluation_policy_version": "hybrid_v1",
                    "profile": "balanced",
                    "allow_policies": ["Inspect another repository"],
                },
                guardrails_enabled=False,
            )
        assert rejected.value.response.status_code == 422
        assert rejected.value.response.json()["detail"]["code"] == "invalid_delegated_policy"
        with scoped_runtime_context(
            access_context=RuntimeAccessContext(
                user_email=USER,
                agent_id="agent-1",
                session_policy={
                    "allow_policies": ["Inspect another repository"],
                    "deny_policies": ["Do not deploy"],
                },
            )
        ):
            child = await manager.create_child_session(
                parent,
                mode="delegate_sync",
                task_description="Review synthetic source",
                agent_id="agent-1",
                effective_agent_id="agent-1",
            )
            for refresh in (False, True):
                if refresh:
                    await manager.refresh_intaris_session_policy(child)
                stored = await client.get_session(
                    child.session_id, user_email=USER, agent_id="agent-1"
                )
                assert stored.policy["allow_policies"] == ["Inspect the application source"]
                assert stored.policy["deny_policies"] == ["Do not deploy"]
        stored_parent = await client.get_session(
            parent.session_id, user_email=USER, agent_id="agent-1"
        )
        assert stored_parent.policy["allow_policies"] == ["Inspect the application source"]
        assert "deny_policies" not in stored_parent.policy
    finally:
        await client.client.aclose()
        await engine.dispose()


def workflow_scope(sid: str, agent_id: str) -> WorkflowStepScope:
    args = {"file_path": "/workspace/notes.txt"}
    step = StepDefinition(
        name="inspect", type="tool_call", tool_call={"tool": "read", "args": args}
    )
    workflow = Workflow(workflow_id=f"wf-{sid}", name="Wire", steps=[step])
    state = WorkflowState()
    pin_effective_workflow(state, workflow)
    return WorkflowStepScope.build(
        task=TaskModel(
            task_id=f"task-{sid}",
            title="Synthetic wire task",
            created_by=USER,
            agent_id=agent_id,
            creation_origin="owner_configured",
        ),
        workflow=workflow,
        state=state,
        step=step,
        session_id=sid,
        step_run_id=f"run-{sid}",
        logical_call_id=f"call-{sid}",
        rendered_args=args,
    )


@pytest.mark.asyncio
async def test_real_defaults_strictness_and_declaration_requires_existing_session(peer):
    client = provider(peer)
    sid = f"wire-defaults-{uuid.uuid4().hex}"
    try:
        binding = workflow_scope(sid, "wire-agent")
        item = binding.declaration(
            session_id=sid,
            call_id=binding.logical_call_id,
            tool="read",
            args={"file_path": "/workspace/notes.txt"},
            executor_id="contract-executor",
        )
        with pytest.raises(WorkflowStepDeclarationError) as rejected:
            await client.declare_workflow_step(item, user_email=USER, agent_id="wire-agent")
        assert rejected.value.code == "workflow_step_conflict"
        absent = await client.client.get(
            f"/api/v1/session/{sid}", headers=client._headers("wire-agent", USER)
        )
        assert absent.status_code == 404
        headers = client._headers("wire-agent", USER)
        created = await client.client.post(
            "/api/v1/intention",
            headers=headers,
            json={
                "session_id": sid,
                "intention": "Read synthetic inputs",
            },
        )
        created.raise_for_status()
        assert created.json()["analytics_enabled"] is True
        assert created.json()["guardrails_enabled"] is True
        for value in ("false", 0, None):
            invalid = await client.client.post(
                "/api/v1/intention",
                headers=headers,
                json={
                    "session_id": f"invalid-type-{uuid.uuid4().hex}",
                    "intention": "Read synthetic inputs",
                    "analytics_enabled": value,
                },
            )
            assert invalid.status_code == 422
        await client.declare_workflow_step(item, user_email=USER, agent_id="wire-agent")
        assert await flags(client, sid) == (True, True)
    finally:
        await client.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("analytics", [False, True])
@pytest.mark.parametrize("guardrails", [False, True])
@pytest.mark.parametrize("same_agent", [False, True])
async def test_real_delegate_managed_and_workflow_order(
    peer, tmp_path, analytics, guardrails, same_agent
):
    engine, factory = await _session_factory(
        tmp_path,
        capabilities={
            "memory_backend": "none",
            "guardrails_backend": "intaris" if guardrails else "none",
            "analytics_enabled": analytics,
            "intaris_evaluation": "balanced",
        },
    )
    trace = []
    client = provider(peer, trace)
    manager = SessionManager(factory, SimpleNamespace(guardrails=client, memory=object()), _Cache())
    try:
        _, parent = await manager.create_conversation_with_root_session(
            user_email=USER,
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        )
        child = await manager.create_child_session(
            parent,
            mode="delegate_sync",
            task_description="Read synthetic inputs",
            agent_id="system:research",
            effective_agent_id="system:research",
        )
        assert await flags(client, child.session_id, child.agent_id) == (analytics, guardrails)
        # A changed target config must win even when target identity equals parent.
        target_id = "agent-1" if same_agent else f"target-{uuid.uuid4().hex}"
        target_caps = {
            "memory_backend": "none",
            "analytics_enabled": not analytics,
            "guardrails_backend": "none" if guardrails else "intaris",
            "intaris_evaluation": "balanced",
        }
        async with factory() as db:
            if same_agent:
                (await get_agent(db, target_id)).capabilities = target_caps
            else:
                db.add(
                    Agent(
                        agent_id=target_id,
                        owner_email=USER,
                        name="Wire target",
                        capabilities=target_caps,
                    )
                )
            await db.commit()
        parent_policy = {"intaris_evaluation": "strict", "deny_policies": ["Do not publish"]}
        inherited = inherited_managed_session_policy({}, parent_policy)
        with scoped_runtime_context(
            access_context=RuntimeAccessContext(
                user_email=USER,
                agent_id="agent-1",
                session_policy=parent_policy,
            )
        ):
            _, target = await manager.create_conversation_with_root_session(
                user_email=USER,
                agent_id=target_id,
                context=ConversationContext(
                    type="agent_work",
                    platform_data={
                        "managed_session_policy": inherited,
                    },
                ),
            )
        assert await flags(client, target.session_id, target_id) == (not analytics, not guardrails)
        # Exercise the real mandatory ToolRouter declaration/evaluation boundary.
        await manager.refresh_intaris_session_policy(child)
        binding = workflow_scope(child.session_id, child.agent_id)
        registry = ToolRegistry()
        registry.register(
            RegisteredTool(
                definition=NativeToolDefinition(
                    name="read",
                    description="Synthetic read",
                    parameters={"type": "object"},
                    read_only=True,
                    source=ToolSource(type="builtin"),
                )
            )
        )
        executor = SimpleNamespace(
            executor_id="contract-executor",
            tool_execute=AsyncMock(return_value=ToolResult(output="synthetic result")),
        )
        agent = AgentDefinition(
            agent_id=child.agent_id,
            owner_email=USER,
            name="Wire child",
            permissions=AgentPermissions(default=Permission.ALLOW),
        )
        agent = await manager.effective_session_agent(child, agent)
        dispatch = ToolRouter(client).execute(
            ToolCall(
                call_id=binding.logical_call_id,
                name="read",
                arguments={"file_path": "/workspace/notes.txt"},
            ),
            agent=agent,
            session=child,
            registry=registry,
            executor=executor,
            workflow_step_scope=binding,
            workflow_executor_id=executor.executor_id,
        )
        if guardrails:
            # Analysis is deliberately disabled, so no alignment model may run.
            # The declared child must fail closed, not silently bypass alignment.
            with pytest.raises(httpx.HTTPStatusError) as refused:
                await dispatch
            assert refused.value.response.status_code == 503
            assert refused.value.response.json()["detail"]["code"] == "alignment_unavailable"
            executor.tool_execute.assert_not_awaited()
        else:
            outcome = await dispatch
            assert not outcome.is_error, outcome
            executor.tool_execute.assert_awaited_once()
        declarations = [
            i
            for i, row in enumerate(trace)
            if row[0] == "POST" and row[2] and "workflow_step" in row[2]
        ]
        assert len(declarations) == 1
        assert trace[declarations[0]][3] == 200
        before = trace[: declarations[0]]
        canonical = [
            row
            for row in before
            if row[0] == "GET" and row[1] == f"/api/v1/session/{child.session_id}"
        ]
        assert canonical
        assert canonical[-1][4]["analytics_enabled"] is analytics
        assert canonical[-1][4]["guardrails_enabled"] is guardrails
        assert await flags(client, child.session_id, child.agent_id) == (analytics, guardrails)
    finally:
        await client.client.aclose()
        await engine.dispose()
