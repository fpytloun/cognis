from __future__ import annotations

from dataclasses import dataclass

import pytest

from cognis.core.tool_arguments import validate_tool_arguments
from cognis.models.tool import ExecutorHandle
from cognis.tools.builtin.knowledgebase import (
    build_knowledgebase_tool_handlers,
    knowledgebase_tools,
)
from cognis.tools.registry import ToolExecutionContext


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.attach_calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, *, owner_email: str, knowledgebase_id: str, access_context=None):
        self.calls.append(("get", owner_email, getattr(access_context, "agent_id", None)))
        return None

    async def list(self, *, owner_email: str, access_context=None):
        self.calls.append(("list", owner_email, getattr(access_context, "agent_id", None)))
        return []

    async def artifacts(self, *, owner_email: str, knowledgebase_id: str, access_context=None):
        self.calls.append(("artifacts", owner_email, getattr(access_context, "agent_id", None)))
        return []

    async def jobs(self, *, owner_email: str, knowledgebase_id: str, access_context=None):
        self.calls.append(("jobs", owner_email, getattr(access_context, "agent_id", None)))
        return []

    async def diagnostics(self, *, owner_email: str, knowledgebase_id: str, access_context=None):
        self.calls.append(("diagnostics", owner_email, getattr(access_context, "agent_id", None)))
        return None

    async def search(
        self, *, owner_email: str, knowledgebase_id: str, payload, access_context=None
    ):
        self.calls.append(("search", owner_email, getattr(access_context, "agent_id", None)))
        return None

    async def attach(self, *, owner_email: str, knowledgebase_id: str, artifact_id: str, metadata):
        self.attach_calls.append((artifact_id, metadata))
        return None

    async def delete(self, *, owner_email: str, knowledgebase_id: str):
        self.calls.append(("delete", owner_email, None))
        return True

    async def update(self, *, owner_email: str, knowledgebase_id: str, payload):
        self.calls.append(("update", owner_email, None))
        return _CreatedKnowledgebase(knowledgebase_id=knowledgebase_id, name=payload.name)


@dataclass
class _CreatedKnowledgebase:
    knowledgebase_id: str
    name: str

    def model_dump(self, *, mode: str) -> dict[str, str]:
        return {"knowledgebase_id": self.knowledgebase_id, "name": self.name}


class _CreateService(_Service):
    async def create(
        self,
        *,
        owner_email: str,
        name: str,
        description,
        metadata_schema,
        settings,
        access_context=None,
    ):
        self.calls.append(("create", owner_email, getattr(access_context, "agent_id", None)))
        return _CreatedKnowledgebase(knowledgebase_id="kb_created", name=name)


def test_knowledgebase_tool_definitions_cover_agent_operations() -> None:
    tools = {tool.name: tool for tool in knowledgebase_tools()}

    expected = {
        "knowledgebase_create",
        "knowledgebase_list",
        "knowledgebase_get",
        "knowledgebase_update",
        "knowledgebase_attach_artifact",
        "knowledgebase_attach_artifacts",
        "knowledgebase_delete",
        "knowledgebase_detach_artifact",
        "knowledgebase_reindex_artifact",
        "knowledgebase_reindex",
        "knowledgebase_retry_job",
        "knowledgebase_search",
        "knowledgebase_read_source_context",
    }
    assert expected.issubset(tools)
    assert tools["knowledgebase_search"].read_only is True
    assert tools["knowledgebase_update"].read_only is False
    assert tools["knowledgebase_delete"].read_only is False
    assert tools["knowledgebase_detach_artifact"].read_only is False
    assert all(tool.category == "knowledgebase" for tool in tools.values())


def test_knowledgebase_create_accepts_string_array_metadata_schema() -> None:
    tool = {tool.name: tool for tool in knowledgebase_tools()}["knowledgebase_create"]

    error = validate_tool_arguments(
        "knowledgebase_create",
        {
            "name": "Feng Shui",
            "metadata_schema": {
                "fields": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lesson tags",
                        "filterable": True,
                    }
                }
            },
        },
        schema=tool.parameters,
    )

    assert error is None


def test_knowledgebase_attach_accepts_numeric_metadata() -> None:
    tool = {tool.name: tool for tool in knowledgebase_tools()}["knowledgebase_attach_artifacts"]

    error = validate_tool_arguments(
        "knowledgebase_attach_artifacts",
        {
            "knowledgebase_id": "kb_1",
            "artifact_ids": ["att_1"],
            "metadata": {
                "dataset": "fengshui_kb_smoke",
                "document_count": 5,
                "tags": ["ming-kua", "feng-shui"],
            },
        },
        schema=tool.parameters,
    )

    assert error is None


def test_knowledgebase_bulk_attach_accepts_per_document_metadata() -> None:
    tool = {tool.name: tool for tool in knowledgebase_tools()}["knowledgebase_attach_artifacts"]

    error = validate_tool_arguments(
        "knowledgebase_attach_artifacts",
        {
            "knowledgebase_id": "kb_1",
            "items": [
                {
                    "artifact_id": "att_62",
                    "metadata": {
                        "lesson_no": 62,
                        "title": "Ložnice",
                        "folder": "mistnosti",
                        "category": "mistnosti-domova",
                        "tags": ["ložnice", "kuchyň"],
                        "youtube_id": "abc123",
                        "source_paths": ["lessons/62-loznice.md"],
                    },
                }
            ],
        },
        schema=tool.parameters,
    )

    assert error is None


def test_knowledgebase_bulk_attach_uses_provider_compatible_schema() -> None:
    tool = {tool.name: tool for tool in knowledgebase_tools()}["knowledgebase_attach_artifacts"]

    assert "oneOf" not in tool.parameters
    assert (
        validate_tool_arguments(
            "knowledgebase_attach_artifacts", {"knowledgebase_id": "kb_1"}, schema=tool.parameters
        )
        is None
    )


@pytest.mark.asyncio
async def test_knowledgebase_bulk_attach_returns_structured_error_for_invalid_contract() -> None:
    service = _Service()
    handler = build_knowledgebase_tool_handlers(service)["knowledgebase_attach_artifacts"]
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={"user_email": "owner@example.com"},
    )

    result = await handler({"knowledgebase_id": "kb_1"}, context)

    assert result["error"] == "invalid_tool_arguments"
    assert result["operation"] == "attach_artifacts"
    assert service.attach_calls == []


@pytest.mark.asyncio
async def test_knowledgebase_bulk_attach_forwards_per_document_metadata() -> None:
    service = _Service()
    handler = build_knowledgebase_tool_handlers(service)["knowledgebase_attach_artifacts"]
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={"user_email": "owner@example.com"},
    )

    await handler(
        {
            "knowledgebase_id": "kb_1",
            "items": [
                {"artifact_id": "att_1", "metadata": {"lesson_no": 62}},
                {"artifact_id": "att_2", "metadata": {"category": "mistnosti-domova"}},
            ],
        },
        context,
    )

    assert service.attach_calls == [
        ("att_1", {"lesson_no": 62}),
        ("att_2", {"category": "mistnosti-domova"}),
    ]


@pytest.mark.asyncio
async def test_knowledgebase_delete_handler_returns_deleted_status() -> None:
    service = _Service()
    handler = build_knowledgebase_tool_handlers(service)["knowledgebase_delete"]
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={"user_email": "owner@example.com"},
    )

    result = await handler({"knowledgebase_id": "kb_1"}, context)

    assert result == {"knowledgebase_id": "kb_1", "deleted": True}
    assert service.calls == [("delete", "owner@example.com", None)]


@pytest.mark.asyncio
async def test_knowledgebase_update_handler_forwards_payload() -> None:
    service = _Service()
    handler = build_knowledgebase_tool_handlers(service)["knowledgebase_update"]
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={"user_email": "owner@example.com"},
    )

    result = await handler({"knowledgebase_id": "kb_1", "name": "Updated KB"}, context)

    assert result == {"knowledgebase_id": "kb_1", "name": "Updated KB"}
    assert service.calls == [("update", "owner@example.com", None)]


@pytest.mark.asyncio
async def test_knowledgebase_handlers_use_runtime_user() -> None:
    service = _Service()
    handler = build_knowledgebase_tool_handlers(service)["knowledgebase_list"]
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={"user_email": "owner@example.com"},
    )

    assert await handler({}, context) == []
    assert service.calls == [("list", "owner@example.com", None)]


@pytest.mark.asyncio
async def test_knowledgebase_create_forwards_agent_runtime_context() -> None:
    service = _CreateService()
    handler = build_knowledgebase_tool_handlers(service)["knowledgebase_create"]
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={
            "runtime_access": {
                "user_email": "owner@example.com",
                "agent_id": "agent_owner",
                "agent_owner_email": "owner@example.com",
            }
        },
    )

    result = await handler({"name": "Agent KB"}, context)

    assert result == {"knowledgebase_id": "kb_created", "name": "Agent KB"}
    assert service.calls == [("create", "owner@example.com", "agent_owner")]


@pytest.mark.asyncio
async def test_knowledgebase_handlers_forward_agent_runtime_context() -> None:
    service = _Service()
    handler = build_knowledgebase_tool_handlers(service)["knowledgebase_list"]
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={
            "runtime_access": {
                "user_email": "grantee@example.com",
                "agent_id": "agent_owner",
                "agent_owner_email": "owner@example.com",
            }
        },
    )

    assert await handler({}, context) == []
    assert service.calls == [("list", "grantee@example.com", "agent_owner")]


@pytest.mark.asyncio
async def test_read_handlers_forward_agent_runtime_context() -> None:
    service = _Service()
    handlers = build_knowledgebase_tool_handlers(service)
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={
            "runtime_access": {
                "user_email": "grantee@example.com",
                "agent_id": "agent_owner",
                "agent_owner_email": "owner@example.com",
            }
        },
    )

    args = {"knowledgebase_id": "kb_owner"}
    assert await handlers["knowledgebase_list_artifacts"](args, context) == []
    assert await handlers["knowledgebase_list_jobs"](args, context) == []
    status = await handlers["knowledgebase_status"](args, context)
    assert status["error"] == "knowledgebase_not_found_or_unavailable"
    assert status["knowledgebase_id"] == "kb_owner"
    assert service.calls == [
        ("artifacts", "grantee@example.com", "agent_owner"),
        ("jobs", "grantee@example.com", "agent_owner"),
        ("diagnostics", "grantee@example.com", "agent_owner"),
    ]


@pytest.mark.asyncio
async def test_read_handlers_return_structured_errors_instead_of_none() -> None:
    service = _Service()
    handlers = build_knowledgebase_tool_handlers(service)
    context = ToolExecutionContext(
        executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
        runtime_metadata={"user_email": "owner@example.com"},
    )

    get_result = await handlers["knowledgebase_get"]({"knowledgebase_id": "kb_missing"}, context)
    status_result = await handlers["knowledgebase_status"](
        {"knowledgebase_id": "kb_missing"}, context
    )
    search_result = await handlers["knowledgebase_search"](
        {"knowledgebase_id": "kb_missing", "query": "Ming Kua"}, context
    )

    for result in (get_result, status_result, search_result):
        assert result["error"] == "knowledgebase_not_found_or_unavailable"
        assert result["knowledgebase_id"] == "kb_missing"


@pytest.mark.asyncio
async def test_knowledgebase_handlers_reject_missing_user() -> None:
    handler = build_knowledgebase_tool_handlers(_Service())["knowledgebase_list"]

    with pytest.raises(ValueError, match="User context"):
        await handler(
            {},
            ToolExecutionContext(
                executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process"),
                runtime_metadata={},
            ),
        )
