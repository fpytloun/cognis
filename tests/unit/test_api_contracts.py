"""Contract tests for API response models.

Stage 20+ refactors occasionally drifted the shape between the DB
producers and the API response models (e.g. ``StepRunResponse.todos``
was typed ``dict`` while every producer wrote ``list[dict]``).

These tests pin the canonical shapes so future changes either update the
tests or immediately fail in CI.
"""

from __future__ import annotations

import types

import pytest
from pydantic import ValidationError

from cognis.api.models import (
    ActiveStreamSnapshotResponse,
    ActiveThinkingBlockResponse,
    ActiveThinkingSnapshotResponse,
    AgentGrantResponse,
    AgentResponse,
    CreateScheduleRequest,
    DeliverableResponse,
    EffectiveToolItemResponse,
    ExecutorTokenResponse,
    MessageHistoryResponse,
    ModelRoutingEntry,
    ModelRoutingResponse,
    PendingPauseResponse,
    SessionEventsResponse,
    SkillResponse,
    SkillVersionResponse,
    StepProfileResponse,
    StepRunResponse,
    TaskCreateRequest,
    TaskRerunResponse,
    TaskResponse,
    TaskUpdateRequest,
    ToolClassificationActionResponse,
    ToolClassificationOverrideRequest,
    ToolClassificationRequeueRequest,
    ToolResponse,
    UpdateScheduleRequest,
    WorkflowResponse,
)
from cognis.api.routes.push import PushSubscriptionStatusResponse
from cognis.api.serializers import llm_provider_to_response, step_run_to_response
from cognis.core.management import _normalize_pause_context, _normalize_pause_options
from cognis.models.search import (
    ConversationFlatSearchMatch,
    ConversationFlatSearchResponse,
    ConversationSearchMatch,
    ConversationSearchResponse,
    SearchMatch,
    SearchSessionMatch,
)


def test_task_requests_normalize_empty_optional_ids() -> None:
    create = TaskCreateRequest(
        agent_id="agent-1",
        title="Task",
        workflow_id="",
        project_id=" ",
        skill_id="",
    )
    update = TaskUpdateRequest(agent_id="", workflow_id="", project_id="", skill_id="")

    assert create.workflow_id is None
    assert create.project_id is None
    assert create.skill_id is None
    assert update.agent_id is None
    assert update.workflow_id is None
    assert update.project_id is None
    assert update.skill_id is None


class _FakeRow(types.SimpleNamespace):
    """Flexible stand-in for a SQLAlchemy ORM row in serializer tests."""


def _step_run_row(**overrides: object) -> _FakeRow:
    defaults = {
        "step_run_id": "sr-1",
        "task_id": "task-1",
        "step_name": "execute",
        "step_type": "run",
        "status": "completed",
        "attempt": 1,
        "agent_id": "agent-1",
        "workspace_root": None,
        "working_directory": None,
        "conversation_id": None,
        "session_id": None,
        "intaris_session_id": None,
        "output": None,
        "evaluation": None,
        "runtime_info": None,
        "todos": None,
        "started_at": None,
        "completed_at": None,
        "updated_at": None,
    }
    defaults.update(overrides)
    return _FakeRow(**defaults)


class TestStepRunTodosContract:
    """StepRunResponse.todos must always be list[dict]."""

    def test_empty_list_round_trip(self) -> None:
        response = step_run_to_response(_step_run_row(todos=[]))
        assert response.todos == []

    def test_populated_list_round_trip(self) -> None:
        payload = [
            {"content": "Ship release", "status": "in_progress"},
            {"content": "Write changelog", "status": "pending"},
        ]
        response = step_run_to_response(_step_run_row(todos=payload))
        assert response.todos == payload

    def test_none_is_normalized_to_empty_list(self) -> None:
        response = step_run_to_response(_step_run_row(todos=None))
        assert response.todos == []

    def test_unexpected_dict_is_coerced_to_empty_list(self) -> None:
        # Historical shape drift — StepRunResponse must not raise when a
        # legacy row stored a dict under ``todos``.
        response = step_run_to_response(_step_run_row(todos={"stray": "value"}))
        assert response.todos == []

    def test_non_dict_items_are_filtered(self) -> None:
        response = step_run_to_response(_step_run_row(todos=[{"ok": True}, "garbage"]))
        assert response.todos == [{"ok": True}]

    def test_runtime_info_round_trip(self) -> None:
        payload = {"executor_id": "exec-1", "environment": {"home": "/home/alice"}}
        response = step_run_to_response(_step_run_row(runtime_info=payload))
        assert response.runtime_info == payload

    def test_response_rejects_non_list_when_constructed_directly(self) -> None:
        with pytest.raises(ValidationError):
            StepRunResponse(
                step_run_id="sr",
                task_id="task",
                step_name="s",
                step_type="run",
                status="running",
                agent_id="agent",
                todos={"wrong": "shape"},  # type: ignore[arg-type]
            )


def test_deliverable_response_round_trip() -> None:
    response = DeliverableResponse(
        deliverable_id="dlv-1",
        step_run_id="sr-1",
        version=2,
        content="# Result",
        format="markdown",
        title="Implementation summary",
        target="channel",
        outputs={"tests": "passed"},
        status="approved",
    )

    assert response.outputs == {"tests": "passed"}
    assert response.status == "approved"


def test_executor_token_response_allows_non_expiring_tokens() -> None:
    response = ExecutorTokenResponse(executor_id="exec-1", token="jwt", expires_in=None)

    assert response.expires_in is None


def test_message_history_response_round_trips_active_streams() -> None:
    response = MessageHistoryResponse(
        items=[],
        has_active_turn=True,
        active_streams=[
            ActiveStreamSnapshotResponse(
                conversation_id="conv-1",
                session_id="sess-1",
                message_id="turn-1",
                turn_id="turn-1",
                content="partial assistant text",
                chunk_count=3,
                content_offset=22,
                updated_at="2026-04-20T00:00:00Z",
            )
        ],
    )

    assert response.has_active_turn is True
    assert response.active_streams[0].content == "partial assistant text"
    assert response.active_streams[0].chunk_count == 3


def test_session_events_response_round_trips_active_thinking() -> None:
    response = SessionEventsResponse(
        session_id="sess-1",
        items=[],
        active_thinking=[
            ActiveThinkingSnapshotResponse(
                session_id="sess-1",
                message_id="sr-1",
                turn_id="sr-1",
                blocks=[
                    ActiveThinkingBlockResponse(
                        block_id="thk-1",
                        title="Considering options",
                        content="Considering options for the task",
                        started_at="2026-04-20T00:00:00Z",
                        duration_ms=1200,
                    )
                ],
                updated_at="2026-04-20T00:00:00Z",
            )
        ],
    )

    assert response.active_thinking[0].message_id == "sr-1"
    assert response.active_thinking[0].blocks[0].content == "Considering options for the task"
    assert response.active_thinking[0].blocks[0].duration_ms == 1200


def test_search_responses_round_trip_current_intaris_shape() -> None:
    match = SearchMatch(
        session_id="intaris-1",
        kind="reasoning",
        ref_id="audit-1",
        snippet="matched <mark>reasoning</mark>",
        score=0.9,
    )
    session_match = SearchSessionMatch(
        session_id="intaris-1",
        match_count=1,
        top_match=match,
    )
    conversation_match = ConversationSearchMatch(
        conversation_id="conv-1",
        agent_id="agent-1",
        status="active",
        session_id="sess-1",
        intaris_session_id="intaris-1",
        match_count=session_match.match_count,
        top_match=session_match.top_match,
        extra_matches=[
            SearchMatch(
                session_id="intaris-1",
                kind="intention",
                ref_id="audit-2",
                snippet="related <mark>intention</mark>",
                score=0.7,
            )
        ],
        kind_rank=0,
    )
    flat_match = ConversationFlatSearchMatch(
        conversation_id="conv-1",
        agent_id="agent-1",
        status="active",
        session_id="sess-1",
        intaris_session_id="intaris-1",
        match=match,
        kind_rank=0,
    )

    assert (
        ConversationSearchResponse(matches=[conversation_match]).matches[0].top_match.ref_id
        == "audit-1"
    )
    assert (
        ConversationSearchResponse(matches=[conversation_match]).matches[0].extra_matches[0].ref_id
        == "audit-2"
    )
    assert ConversationFlatSearchResponse(matches=[flat_match]).matches[0].match.kind == "reasoning"


def test_push_subscription_status_response_round_trip() -> None:
    response = PushSubscriptionStatusResponse(
        configured=True,
        enabled_subscriptions=1,
        last_error="push endpoint rejected request",
    )

    assert response.configured is True
    assert response.enabled_subscriptions == 1
    assert response.last_error == "push endpoint rejected request"


def test_agent_response_round_trips_sharing_fields() -> None:
    response = AgentResponse(
        agent_id="agent-1",
        owner_email="owner@example.com",
        name="Shared Agent",
        status="active",
        is_shared_with_me=True,
        shared_by_email="owner@example.com",
        granted_permission="use",
        executor_scope="owner_executor",
        is_readonly_for_caller=True,
    )

    assert response.is_shared_with_me is True
    assert response.shared_by_email == "owner@example.com"
    assert response.executor_scope == "owner_executor"


def test_agent_grant_response_round_trip() -> None:
    response = AgentGrantResponse(
        grant_id="grant-1",
        agent_id="agent-1",
        grantee_type="user",
        grantee_user_email="guest@example.com",
        permission="use",
        executor_scope="grantee_executor",
        granted_by="owner@example.com",
    )

    assert response.grantee_user_email == "guest@example.com"
    assert response.executor_scope == "grantee_executor"


class TestPendingPauseShapeContract:
    """PendingPauseResponse must accept canonical shapes and
    normalization helpers must cover legacy shapes."""

    def test_options_as_list_of_dicts(self) -> None:
        response = PendingPauseResponse(
            pause_id="p-1",
            pause_type="step_input",
            options=[{"label": "Approve", "action": "approve"}],
        )
        assert response.options == [{"label": "Approve", "action": "approve"}]

    def test_options_none_is_allowed(self) -> None:
        response = PendingPauseResponse(pause_id="p-1", pause_type="step_input")
        assert response.options is None

    def test_normalize_options_from_list_of_strings(self) -> None:
        normalized = _normalize_pause_options(["Yes", "No"])
        assert normalized == [
            {"label": "Yes", "action": "Yes"},
            {"label": "No", "action": "No"},
        ]


class TestSkillResponseContracts:
    def test_skill_version_accepts_asset_manifest_entries(self) -> None:
        response = SkillVersionResponse(
            version_id="sv-1",
            skill_id="skill-1",
            version_number=1,
            content_hash="a" * 64,
            instructions="hello",
            asset_manifest=[
                {
                    "filename": "scripts/tool.py",
                    "asset_id": "sa-1",
                    "artifact_namespace": "skills",
                    "artifact_object_id": "ska-1",
                    "content_hash": "b" * 64,
                    "size_bytes": 10,
                    "content_type": "text/x-python",
                    "url": "https://example.test/tool.py",
                }
            ],
        )
        assert response.asset_manifest is not None
        assert response.asset_manifest[0].artifact_object_id == "ska-1"

    def test_skill_response_round_trips_current_version_shape(self) -> None:
        response = SkillResponse(
            skill_id="skill-1",
            name="Skill One",
            instructions="hello",
            attach_to_all_agents=False,
            current_version=SkillVersionResponse(
                version_id="sv-1",
                skill_id="skill-1",
                version_number=1,
                content_hash="a" * 64,
                instructions="hello",
            ),
        )
        assert response.current_version is not None
        assert response.current_version.version_id == "sv-1"

    def test_skill_version_round_trips_decomposition_fields(self) -> None:
        response = SkillVersionResponse(
            version_id="sv-1",
            skill_id="skill-1",
            version_number=2,
            content_hash="a" * 64,
            instructions="hello",
            steps=[{"name": "plan", "type": "run", "prompt": "Plan it"}],
            decomposition_source_hash="b" * 64,
            decomposition_stale=True,
        )

        assert response.steps is not None
        assert response.steps[0]["name"] == "plan"
        assert response.decomposition_stale is True

    def test_normalize_options_from_mixed_shape_drops_junk(self) -> None:
        normalized = _normalize_pause_options([{"label": "A"}, 42, "B"])
        assert normalized == [{"label": "A"}, {"label": "B", "action": "B"}]

    def test_normalize_options_returns_none_for_empty_or_invalid(self) -> None:
        assert _normalize_pause_options(None) is None
        assert _normalize_pause_options("not a list") is None
        assert _normalize_pause_options([]) is None

    def test_normalize_context_accepts_dict(self) -> None:
        assert _normalize_pause_context({"key": "value"}) == {"key": "value"}

    def test_normalize_context_wraps_string(self) -> None:
        assert _normalize_pause_context("background") == {"note": "background"}

    def test_normalize_context_none_and_empty_return_none(self) -> None:
        assert _normalize_pause_context(None) is None
        assert _normalize_pause_context("") is None


class TestTaskResponseRoundTrip:
    """TaskResponse round trip preserves delivery default and status."""

    def test_defaults(self) -> None:
        response = TaskResponse(
            task_id="task-1",
            title="Task",
            status="running",
            created_by="user@example.com",
            agent_id="agent-1",
            source_type="api",
        )
        assert response.description == ""
        assert response.workflow_state is None
        assert response.completion_mode_family == "default"
        assert response.created_by_agent_id is None


class TestTaskCreateRequest:
    """TaskCreateRequest preserves optional creator-agent marker."""

    def test_created_by_agent_id_defaults_to_none(self) -> None:
        create = TaskCreateRequest(agent_id="agent-1", title="Task")
        assert create.created_by_agent_id is None


class TestScheduleRequests:
    """Schedule requests strip reserved task template fields."""

    def test_create_strips_task_creator_agent_marker(self) -> None:
        request = CreateScheduleRequest(
            name="Daily",
            agent_id="agent-1",
            schedule_type="interval",
            interval_seconds=60,
            task_template={"title": "Task", "created_by_agent_id": "agent-2"},
        )

        assert request.task_template == {"title": "Task"}

    def test_update_strips_task_creator_agent_marker(self) -> None:
        request = UpdateScheduleRequest(
            task_template={"title": "Task", "created_by_agent_id": "agent-2"}
        )

        assert request.task_template == {"title": "Task"}


def test_task_rerun_response_round_trip() -> None:
    response = TaskRerunResponse(
        ok=True,
        source_task_id="task-old",
        task_id="task-new",
        status="queued",
        created_new=True,
    )

    assert response.source_task_id == "task-old"
    assert response.task_id == "task-new"
    assert response.created_new is True


def test_workflow_response_round_trips_lifecycle_and_lineage() -> None:
    response = WorkflowResponse(
        workflow_id="wf-1",
        name="Workflow",
        lifecycle="ephemeral",
        lineage={"base_workflow_id": "system:software-development"},
    )

    assert response.lifecycle == "ephemeral"
    assert response.lineage == {"base_workflow_id": "system:software-development"}


def test_workflow_response_preserves_step_profile_shape() -> None:
    response = WorkflowResponse(
        workflow_id="wf-1",
        name="Workflow",
        steps=[
            {
                "name": "execute",
                "type": "run",
                "step_profile_id": "system:coding",
                "step_profile_mode": "hard",
                "step_profile": {
                    "matrix": {"filesystem": ["read", "write"]},
                    "tool_overrides": {"include": ["read"], "exclude": ["bash"]},
                    "allow_tool_search": False,
                },
            }
        ],
    )

    assert response.steps[0]["step_profile_id"] == "system:coding"
    assert response.steps[0]["step_profile_mode"] == "hard"
    assert response.steps[0]["step_profile"]["matrix"]["filesystem"] == ["read", "write"]


def test_tool_response_round_trips_classification_fields() -> None:
    response = ToolResponse(
        name="read",
        description="Read a file",
        category="filesystem",
        profile_group="filesystem",
        read_only=True,
        capabilities=["read"],
        classification_status="ready",
        classification_source="declared",
        classification_confidence=1.0,
    )

    assert response.capabilities == ["read"]
    assert response.profile_group == "filesystem"
    assert response.classification_status == "ready"
    assert response.classification_source == "declared"
    assert response.classification_confidence == 1.0


def test_effective_tool_item_round_trips_classification_fields() -> None:
    response = EffectiveToolItemResponse(
        tool_id="builtin:read",
        name="read",
        description="Read a file",
        category="filesystem",
        profile_group="filesystem",
        read_only=True,
        capabilities=["read"],
        classification_status="pending",
        classification_source="llm",
        classification_confidence=0.83,
        permission="allow",
    )

    assert response.capabilities == ["read"]
    assert response.profile_group == "filesystem"
    assert response.classification_status == "pending"
    assert response.classification_source == "llm"
    assert response.classification_confidence == pytest.approx(0.83)


def test_step_profile_response_round_trips_matrix_shape() -> None:
    response = StepProfileResponse(
        profile_id="system:coding",
        name="Coding",
        mode="soft",
        has_override=True,
        is_custom=False,
        config={
            "matrix": {"filesystem": ["read", "write"], "shell": ["write", "privileged"]},
            "allow_tool_search": True,
        },
    )

    assert response.profile_id == "system:coding"
    assert response.has_override is True
    assert response.is_custom is False
    assert response.config["matrix"]["filesystem"] == ["read", "write"]


def test_tool_classification_action_models_round_trip() -> None:
    requeue = ToolClassificationRequeueRequest(
        tool_id="mcp:github:search/issues", pending_only=False
    )
    override = ToolClassificationOverrideRequest(
        tool_id="mcp:github:search/issues",
        profile_group="development",
        capabilities=["read"],
    )
    response = ToolClassificationActionResponse(updated=1, status="queued")

    assert requeue.tool_id == "mcp:github:search/issues"
    assert override.profile_group == "development"
    assert override.capabilities == ["read"]
    assert response.updated == 1
    assert response.status == "queued"


class TestModelRoutingContracts:
    def test_model_routing_defaults_to_empty_route_entries(self) -> None:
        response = ModelRoutingResponse()

        assert response.default == ModelRoutingEntry(model=None, reasoning_effort=None)
        assert response.image_generation == ModelRoutingEntry(model=None, reasoning_effort=None)
        assert response.attachment_analysis == ModelRoutingEntry(model=None, reasoning_effort=None)

    def test_model_routing_preserves_nested_entry_shape(self) -> None:
        response = ModelRoutingResponse(
            default={"model": "gpt-5.4", "reasoning_effort": "xhigh"},
            speech_to_text={"model": "gpt-4o-transcribe", "reasoning_effort": None},
        )

        assert response.default.model == "gpt-5.4"
        assert response.default.reasoning_effort == "xhigh"
        assert response.speech_to_text.model == "gpt-4o-transcribe"


class TestLLMProviderSerializer:
    """Provider list enriches stored models with derived capability fields."""

    def _provider_row(self, **config_overrides: object) -> _FakeRow:
        config = {
            "preset": "openai",
            "default_model": "gpt-5.4",
            "models": [
                {
                    "model_id": "gpt-5.4",
                    "supports_reasoning": True,
                },
                {
                    "model_id": "gpt-4o-mini",
                    "supports_reasoning": False,
                },
            ],
            **config_overrides,
        }
        return _FakeRow(
            provider_id="openai",
            display_name="OpenAI",
            location="controller",
            backend="litellm",
            config=config,
            is_default=True,
            status="active",
            created_at=None,
            updated_at=None,
            last_test=None,
        )

    def test_reasoning_model_gets_reasoning_efforts_populated(self) -> None:
        response = llm_provider_to_response(self._provider_row())

        reasoning_model = next(m for m in response.models if m["model_id"] == "gpt-5.4")
        assert reasoning_model["reasoning_efforts"] == [
            "default",
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
        ]

    def test_non_reasoning_model_keeps_empty_reasoning_efforts(self) -> None:
        response = llm_provider_to_response(self._provider_row())

        standard_model = next(m for m in response.models if m["model_id"] == "gpt-4o-mini")
        assert standard_model.get("reasoning_efforts", []) == []

    def test_explicitly_configured_reasoning_efforts_are_preserved(self) -> None:
        response = llm_provider_to_response(
            self._provider_row(
                models=[
                    {
                        "model_id": "gpt-5.4",
                        "supports_reasoning": True,
                        "reasoning_efforts": ["default", "low", "high"],
                    }
                ]
            )
        )

        reasoning_model = next(m for m in response.models if m["model_id"] == "gpt-5.4")
        assert reasoning_model["reasoning_efforts"] == ["default", "low", "high"]
