"""Exact scope, provenance, transport, and no-dispatch workflow invariants."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, inspect, text

from cognis.bootstrap import _ensure_workflow_creation_origin
from cognis.core.tool_router import ToolRouter
from cognis.core.workflow_rendering import WorkflowRenderer
from cognis.core.workflow_step_scope import (
    WorkflowEvaluationBinding,
    WorkflowStepDeclarationError,
    WorkflowStepScope,
    canonical_digest,
)
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.session import SessionModel
from cognis.models.task import TaskModel
from cognis.models.tool import (
    EvaluationResult,
    NativeToolDefinition,
    Permission,
    ToolCall,
    ToolResult,
    ToolSource,
)
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState, pin_effective_workflow
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.tools.registry import RegisteredTool, ToolRegistry


def scope(*, origin="owner_configured", creator=None, rendered=None):
    args = rendered or {"command": "printf %s café", "target_executor": "sample-desktop"}
    step = StepDefinition(
        name="inspect",
        type="tool_call",
        tool_call={"tool": "bash", "args": args},
    )
    workflow = Workflow(workflow_id="wf_scope", name="Scope", steps=[step])
    state = WorkflowState()
    pin_effective_workflow(state, workflow)
    task = TaskModel(
        task_id="task_scope",
        title="Never authority",
        created_by="owner@test.local",
        agent_id="agent_scope",
        creation_origin=origin,
        created_by_agent_id=creator,
    )
    return WorkflowStepScope.build(
        task=task,
        workflow=workflow,
        state=state,
        step=step,
        session_id="sess_scope",
        step_run_id="sr_scope",
        logical_call_id="call_scope",
        rendered_args=args,
    )


def declaration(binding=None, **overrides):
    args = {
        "session_id": "sess_scope",
        "call_id": "call_scope",
        "tool": "bash",
        "args": {"command": "printf %s café"},
        "executor_id": "sample-desktop",
    }
    return (binding or scope()).declaration(**(args | overrides))


def test_canonical_vectors():
    vectors = json.loads(
        (Path(__file__).parents[1] / "fixtures/workflow_step_digest_vectors.json").read_text()
    )
    for vector in vectors:
        assert canonical_digest(vector["value"]) == vector["sha256"]
        assert (
            json.dumps(
                vector["value"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            == vector["canonical_json"]
        )
    with pytest.raises(ValueError):
        canonical_digest({"value": float("nan")})


@pytest.mark.parametrize(
    ("origin", "creator", "expected"),
    [
        ("owner_configured", None, "owner_configured"),
        ("owner_configured", "agent_scope", "agent_composed"),
        ("agent_composed", None, "agent_composed"),
        (None, None, "agent_composed"),
    ],
)
def test_authority_is_persisted_origin_not_owner(origin, creator, expected):
    assert declaration(scope(origin=origin, creator=creator)).authorized_by == expected


def test_exact_projection_and_binding():
    receipt = declaration()
    assert receipt.projection == "exact"
    assert receipt.rendered_args_digest != receipt.evaluated_args_digest
    assert receipt.declaration_digest == canonical_digest(receipt.model_dump())
    for change in [{"session_id": "sess_rotated"}, {"call_id": "call_retry"}, {"tool": "write"}]:
        with pytest.raises(WorkflowStepDeclarationError, match="declaration_mismatch"):
            declaration(**change)
    assert declaration(args={"command": "<resolved-at-execution>"}).projection == "unsupported"
    assert (
        declaration(args={"command": "printf %s café", "artifact": {}}).projection == "unsupported"
    )
    assert declaration(replace(scope(), session_id="sess_rotated"), session_id="sess_rotated")


def test_remote_output_is_not_a_second_template_or_tool():
    renderer = WorkflowRenderer()
    result = renderer.render_native(
        {"command": "{{ steps.previous.value }}"},
        {"steps": {"previous": {"value": "{{ dangerous() }}"}}},
    )
    assert result == {"command": "{{ dangerous() }}"}
    assert declaration(scope(rendered=result), args=result).tool == "bash"


def test_unpinned_and_replaced_definition_cannot_declare():
    workflow = Workflow(
        workflow_id="wf_scope",
        name="Scope",
        steps=[
            StepDefinition(
                name="inspect",
                type="tool_call",
                tool_call={"tool": "bash", "args": {"command": "true"}},
            )
        ],
    )
    task = TaskModel(
        task_id="task_scope", title="Scope", created_by="owner@test.local", agent_id="agent_scope"
    )
    state = WorkflowState()
    kwargs = dict(
        task=task,
        workflow=workflow,
        state=state,
        step=workflow.steps[0],
        session_id="sess_scope",
        step_run_id="sr_scope",
        logical_call_id="call_scope",
        rendered_args={"command": "true"},
    )
    with pytest.raises(WorkflowStepDeclarationError, match="definition_unverified"):
        WorkflowStepScope.build(**kwargs)
    pin_effective_workflow(state, workflow)
    workflow.steps[0].tool_call.tool = "write"
    with pytest.raises(WorkflowStepDeclarationError, match="definition_unverified"):
        WorkflowStepScope.build(**kwargs)


def test_bootstrap_is_idempotent_and_never_backfills_authority():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for table in ("tasks", "schedules"):
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
            connection.execute(text(f"INSERT INTO {table} (id) VALUES (1)"))
        _ensure_workflow_creation_origin(connection)
        _ensure_workflow_creation_origin(connection)
        for table in ("tasks", "schedules"):
            assert "creation_origin" in {
                col["name"] for col in inspect(connection).get_columns(table)
            }
            assert connection.execute(text(f"SELECT creation_origin FROM {table}")).scalar() is None


def test_controller_jwt_is_bound_and_short_lived(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    private = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
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
    provider = JWTAuthProvider(private, public)
    item = declaration()
    token = provider.sign_workflow_step_jwt(
        "owner@test.local",
        "agent_scope",
        session_id=item.session_id,
        governing_execution_id=item.step_run_id,
        governing_snapshot_id=item.workflow_definition_digest,
        declaration_digest=item.declaration_digest,
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == "owner@test.local"
    assert claims["agent_id"] == "agent_scope"
    assert claims["typ"] == "controller"
    assert claims["aud"] == ["intaris"]
    assert claims["session_id"] == item.session_id
    assert claims["governing_execution_id"] == item.step_run_id
    assert claims["governing_snapshot_id"] == item.workflow_definition_digest
    assert claims["declaration_digest"] == item.declaration_digest
    assert claims["exp"] - claims["iat"] == 60
    assert claims["nbf"] <= claims["exp"]
    assert claims["jti"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401, 403, 409, 422, 503])
async def test_transport_is_declaration_only_and_fail_closed(status):
    auth = MagicMock()
    auth.sign_workflow_step_jwt.return_value = "signed-controller-capability"
    provider = IntarisProvider("http://intaris.test", auth)
    await provider.client.aclose()
    item = declaration()
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={
                "ok": True,
                "declaration_digest": item.declaration_digest,
                "admission_revision": 4,
            }
            if status == 200
            else {"code": "workflow_step_conflict"},
        )

    provider.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="http://intaris.test",
    )
    try:
        if status == 200:
            result = await provider.declare_workflow_step(
                item, user_email="owner@test.local", agent_id="agent_scope"
            )
            assert result.admission_revision == 4
        else:
            with pytest.raises(WorkflowStepDeclarationError):
                await provider.declare_workflow_step(
                    item, user_email="owner@test.local", agent_id="agent_scope"
                )
        assert len(requests) == 1
        assert requests[0].url.path == "/api/v1/intention"
        body = json.loads(requests[0].content)
        assert set(body) == {"session_id", "workflow_step"}
        assert body["session_id"] == body["workflow_step"]["session_id"] == item.session_id
        assert body == {
            "session_id": item.session_id,
            "workflow_step": item.model_dump(mode="json"),
        }
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["auth", "transport", "schema", "conflict", "deny", "projection", None]
)
async def test_router_declares_final_arguments_and_never_dispatches_on_failure(failure):
    guardrails = AsyncMock()
    if failure in {"auth", "transport", "schema", "conflict"}:
        guardrails.declare_workflow_step.side_effect = WorkflowStepDeclarationError(
            "workflow_step_conflict"
            if failure == "conflict"
            else f"workflow_step.declaration_{failure}_failed"
        )
    guardrails.evaluate.return_value = EvaluationResult(
        decision="deny"
        if failure == "deny"
        else "escalate"
        if failure == "projection"
        else "approve",
        call_id="eval_scope",
        reasoning="Matched scope",
        session_status="terminated" if failure == "deny" else None,
        status_reason="human_denial" if failure == "deny" else None,
    )
    router = ToolRouter(guardrails)
    if failure == "projection":
        router._prepare_guardrails_tool_call = AsyncMock(
            return_value=ToolCall(
                call_id="call_scope",
                name="bash",
                arguments={"command": "<resolved-at-execution>"},
            )
        )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="bash",
                description="Execute",
                parameters={"type": "object"},
                read_only=False,
                source=ToolSource(type="builtin"),
            )
        )
    )
    executor = MagicMock()
    executor.tool_execute = AsyncMock(return_value=ToolResult(output="ok"))
    executor.executor_id = "sample-desktop"
    agent = AgentDefinition(
        agent_id="agent_scope",
        name="Scope",
        owner_email="owner@test.local",
        permissions=AgentPermissions(default=Permission.ALLOW),
    )
    session = SessionModel(
        session_id="sess_scope",
        agent_id="agent_scope",
        user_email="owner@test.local",
        conversation_id="conv_scope",
    )
    result = await router.execute(
        ToolCall(call_id="call_scope", name="bash", arguments={"command": "printf %s café"}),
        agent=agent,
        session=session,
        registry=registry,
        executor=executor,
        workflow_step_scope=scope(),
        workflow_executor_id="sample-desktop",
    )
    guardrails.declare_workflow_step.assert_awaited_once()
    sent = guardrails.declare_workflow_step.call_args.args[0]
    expected_args = {
        "command": "<resolved-at-execution>" if failure == "projection" else "printf %s café"
    }
    assert sent.evaluated_args_digest == canonical_digest(expected_args)
    assert sent.projection == ("unsupported" if failure == "projection" else "exact")
    if failure == "conflict":
        assert result.is_error
        assert result.metadata["execution_state"] == "not_dispatched"
        guardrails.evaluate.assert_not_awaited()
        executor.tool_execute.assert_not_awaited()
    elif failure in {"auth", "transport", "schema"}:
        # Intaris without declaration support: evaluate undeclared, no binding,
        # so no configured-scope grant can apply.
        guardrails.evaluate.assert_awaited_once()
        assert guardrails.evaluate.call_args.kwargs.get("workflow_binding") is None
        assert guardrails.evaluate.call_args.kwargs["arguments"] == expected_args
    else:
        guardrails.evaluate.assert_awaited_once()
        binding = guardrails.evaluate.call_args.kwargs["workflow_binding"]
        assert binding.declaration is sent
        assert binding.user_email == session.user_email
        assert binding.agent_id == agent.agent_id
        assert guardrails.evaluate.call_args.kwargs["arguments"] == expected_args
        if failure:
            assert result.is_error
            if failure == "deny":
                assert result.metadata["evaluation"]["session_status"] == "terminated"
                assert result.metadata["evaluation"]["status_reason"] == "human_denial"
            executor.tool_execute.assert_not_awaited()
        else:
            executor.tool_execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["transport", "wrong_digest", "old_server"])
async def test_transport_failure_or_unrecognized_receipt_is_never_success(reply):
    provider = IntarisProvider("http://intaris.test", MagicMock())
    await provider.client.aclose()

    async def handle(request):
        if reply == "transport":
            raise httpx.ReadTimeout("unavailable", request=request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "declaration_digest": canonical_digest({}),
                "admission_revision": 1,
            }
            if reply == "wrong_digest"
            else {"session_id": "sess_scope", "intention": "old"},
        )

    provider.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="http://intaris.test",
    )
    provider.auth_provider.sign_workflow_step_jwt.return_value = "signed-controller-capability"
    try:
        with pytest.raises(WorkflowStepDeclarationError):
            await provider.declare_workflow_step(
                declaration(),
                user_email="owner@test.local",
                agent_id="agent_scope",
            )
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
async def test_scoped_oauth_failure_never_replays_without_new_admission():
    oauth = AsyncMock()
    executor = AsyncMock()
    router = ToolRouter(AsyncMock(), mcp_oauth_service=oauth)
    failed = ToolResult(output="Unauthorized", is_error=True, metadata={"status_code": 401})
    result = await router._recover_remote_mcp_oauth_once(
        result=failed,
        allow_replay=False,
        tool_call=ToolCall(call_id="call_scope", name="bash", arguments={}),
        registered_tool=MagicMock(),
        session=MagicMock(),
        executor=executor,
        timeout_seconds=30,
        outer_timeout=31,
        output_chunk_callback=None,
    )
    assert result is failed
    oauth.refresh_token_for_server_id.assert_not_awaited()
    executor.tool_execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [False, True])
async def test_evaluate_wire_binding_and_auth_are_controller_owned(declared):
    item = declaration()
    binding = WorkflowEvaluationBinding(
        declaration=item,
        user_email="owner@test.local",
        agent_id="agent_scope",
    )
    auth = MagicMock()
    auth.sign_service_jwt.return_value = "service-token"
    auth.sign_workflow_step_jwt.return_value = "controller-token"
    provider = IntarisProvider("http://intaris.test", auth)
    await provider.client.aclose()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"decision": "approve", "call_id": "eval_scope"})

    provider.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="http://intaris.test",
    )
    # Neither ambient context nor tool arguments are a declaration capability.
    args = (
        {"command": "printf %s café"}
        if declared
        else {
            "workflow_binding": binding.model_dump(),
            "logical_call_id": item.logical_call_id,
            "controller_context": binding.controller_context,
        }
    )
    context = {"approval_call_id": "approved-receipt", "controller_context": {"forged": True}}
    try:
        await provider.evaluate(
            item.session_id,
            "bash",
            args,
            context,
            **({"workflow_binding": binding} if declared else {}),
        )
        body = json.loads(requests[0].content)
        expected = {
            "session_id": item.session_id,
            "tool": "bash",
            "args": args,
            "context": context,
            "approval_call_id": "approved-receipt",
        }
        if declared:
            expected.update(
                logical_call_id=item.logical_call_id,
                controller_context={
                    "governing_execution_id": item.step_run_id,
                    "governing_snapshot_id": item.workflow_definition_digest,
                    "executor_id": item.executor_id,
                },
            )
            auth.sign_service_jwt.assert_not_called()
            auth.sign_workflow_step_jwt.assert_called_once_with(
                binding.user_email,
                binding.agent_id,
                session_id=item.session_id,
                governing_execution_id=item.step_run_id,
                governing_snapshot_id=item.workflow_definition_digest,
                declaration_digest=item.declaration_digest,
            )
        else:
            auth.sign_service_jwt.assert_called_once()
            auth.sign_workflow_step_jwt.assert_not_called()
        assert body == expected
        assert requests[0].headers["Authorization"] == (
            "Bearer controller-token" if declared else "Bearer service-token"
        )
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["stale_admission", "stale_approval"])
async def test_declared_router_retry_preserves_binding_and_approval_receipt(conflict):
    from cognis.core.workflow_step_scope import DeclaredWorkflowCall

    item = declaration()
    binding = WorkflowEvaluationBinding(
        declaration=item,
        user_email="owner@test.local",
        agent_id="agent_scope",
    )
    auth = MagicMock()
    auth.sign_workflow_step_jwt.return_value = "controller-token"
    provider = IntarisProvider("http://intaris.test", auth)
    await provider.client.aclose()
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(409, json={"detail": {"code": conflict, "message": "Changed"}})
        return httpx.Response(
            200,
            json={
                "decision": "escalate" if conflict == "stale_approval" else "approve",
                "call_id": "eval_scope",
                "minimum_outcome": "escalate",
            },
        )

    provider.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="http://intaris.test",
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="bash",
                description="Execute",
                parameters={"type": "object"},
                source=ToolSource(type="builtin"),
            )
        )
    )
    call = DeclaredWorkflowCall(
        call_id=item.logical_call_id,
        name="bash",
        arguments={"command": "printf %s café"},
        runtime_metadata={"approval_call_id": "approved-receipt"},
        workflow_binding=binding,
    )
    router = ToolRouter(provider)
    agent = AgentDefinition(agent_id="agent_scope", name="Scope", owner_email="owner@test.local")
    session = SessionModel(
        session_id=item.session_id,
        conversation_id="conv_scope",
        agent_id="agent_scope",
        user_email="owner@test.local",
    )
    try:
        # Exercise the public containment path and its stale-receipt fresh admission.
        result = await router.evaluate_tool_call(call, agent, session, registry)
        assert result.decision == ("escalate" if conflict == "stale_approval" else "approve")
        assert len(requests) == 2
        bodies = [json.loads(request.content) for request in requests]
        for body in bodies:
            assert body["logical_call_id"] == item.logical_call_id
            assert body["controller_context"] == binding.controller_context
        assert bodies[0]["approval_call_id"] == "approved-receipt"
        if conflict == "stale_admission":
            assert bodies[0] == bodies[1]
        else:
            assert "approval_call_id" not in bodies[1]
            assert bodies[1]["minimum_outcome"] == "escalate"
        assert all(
            request.headers["Authorization"] == "Bearer controller-token" for request in requests
        )
        assert "workflow_binding" not in call.model_dump()
    finally:
        await provider.client.aclose()
