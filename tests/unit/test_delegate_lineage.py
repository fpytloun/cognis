from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.bootstrap import _ensure_delegate_lineage_column
from cognis.core.agent_loop import AgentLoop
from cognis.models.tool import ToolCall, ToolResult


class _SessionFactory:
    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


def _source(**updates: Any) -> SimpleNamespace:
    values = {
        "session_id": "child-source",
        "parent_session_id": "parent",
        "status": "failed",
        "agent_id": "system:code-review",
        "agent_profile_id": "system:review",
        "delegation_task": "Review the change",
        "delegation_metadata": {},
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        session=SimpleNamespace(session_id="parent", user_email="user@example.com"),
        agent=SimpleNamespace(agent_id="controller"),
    )


async def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    arguments: dict[str, Any],
    source: SimpleNamespace | None,
    siblings: list[SimpleNamespace] | None = None,
) -> tuple[ToolResult, ToolCall | None]:
    agent_loop = object.__new__(AgentLoop)
    agent_loop.session_manager = SimpleNamespace(session_factory=_SessionFactory())
    captured: dict[str, ToolCall] = {}

    async def _get_session_row(_db: object, _session_id: str) -> SimpleNamespace | None:
        return source

    async def _list_child_sessions(_db: object, _parent_id: str) -> list[SimpleNamespace]:
        return ([source] if source is not None else []) + list(siblings or [])

    async def _handle_delegate(tc: ToolCall, **_kwargs: Any) -> ToolResult:
        captured["call"] = tc
        return ToolResult(output=json.dumps({"status": "completed"}))

    async def _require_orchestration_target(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("cognis.store.queries.get_session_row", _get_session_row)
    monkeypatch.setattr("cognis.store.queries.list_child_sessions", _list_child_sessions)
    monkeypatch.setattr(agent_loop, "_handle_delegate", _handle_delegate)
    monkeypatch.setattr(
        agent_loop,
        "_require_orchestration_target",
        _require_orchestration_target,
    )

    result = await agent_loop._handle_delegate_lineage(
        ToolCall(call_id="call-lineage", name=name, arguments=arguments),
        ctx=_ctx(),  # type: ignore[arg-type]
        events_to_record=[],
        on_token=None,
        on_tool_call=None,
        on_tool_result=None,
    )
    return result, captured.get("call")


@pytest.mark.asyncio
async def test_retry_reruns_original_task_and_preserves_specialist_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, delegated = await _invoke(
        monkeypatch,
        name="retry_subsession",
        arguments={"session_id": "child-source"},
        source=_source(),
    )

    assert not result.is_error
    assert delegated is not None
    assert delegated.arguments["task"] == "Review the change"
    assert delegated.arguments["agent_id"] == "system:code-review"
    assert delegated.arguments["agent_profile_id"] == "system:review"
    assert delegated.runtime_metadata["delegate_lineage"] == {
        "operation": "retry",
        "source_session_id": "child-source",
        "root_session_id": "child-source",
        "lineage_depth": 1,
    }


@pytest.mark.asyncio
async def test_follow_up_uses_new_instruction_and_existing_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, delegated = await _invoke(
        monkeypatch,
        name="follow_up_subsession",
        arguments={"session_id": "child-source", "instruction": "Review the fixes"},
        source=_source(
            status="completed",
            delegation_metadata={
                "root_session_id": "child-root",
                "lineage_depth": 2,
            },
        ),
        siblings=[_source(session_id="child-root")],
    )

    assert not result.is_error
    assert delegated is not None
    assert delegated.arguments["task"] == "Review the fixes"
    assert delegated.runtime_metadata["delegate_lineage"]["operation"] == "follow_up"
    assert delegated.runtime_metadata["delegate_lineage"]["root_session_id"] == "child-root"
    assert delegated.runtime_metadata["delegate_lineage"]["lineage_depth"] == 3


@pytest.mark.asyncio
async def test_fork_preserves_source_identity_and_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, delegated = await _invoke(
        monkeypatch,
        name="fork_subsession",
        arguments={
            "session_id": "child-source",
            "instruction": "Take an independent branch",
        },
        source=_source(status="completed"),
    )

    assert not result.is_error
    assert delegated is not None
    assert delegated.arguments["agent_id"] == "system:code-review"
    assert delegated.arguments["agent_profile_id"] == "system:review"
    assert delegated.runtime_metadata["delegate_lineage"]["operation"] == "fork"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        (_source(parent_session_id="other"), "subsession_not_direct_child"),
        (_source(status="active"), "subsession_not_terminal"),
        (
            _source(
                delegation_metadata={
                    "lineage_depth": 8,
                    "root_session_id": "child-source",
                }
            ),
            "delegate_lineage_depth_exceeded",
        ),
    ],
)
async def test_lineage_rejects_unauthorized_active_or_too_deep_sources(
    monkeypatch: pytest.MonkeyPatch,
    source: SimpleNamespace,
    expected_code: str,
) -> None:
    result, delegated = await _invoke(
        monkeypatch,
        name="follow_up_subsession",
        arguments={"session_id": "child-source", "instruction": "Continue"},
        source=source,
    )

    assert result.is_error
    assert json.loads(result.output)["code"] == expected_code
    assert delegated is None


@pytest.mark.asyncio
async def test_retry_does_not_duplicate_active_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_retry = _source(
        session_id="child-retry",
        status="active",
        delegation_metadata={
            "operation": "retry",
            "source_session_id": "child-source",
        },
    )
    result, delegated = await _invoke(
        monkeypatch,
        name="retry_subsession",
        arguments={"session_id": "child-source"},
        source=_source(),
        siblings=[active_retry],
    )

    assert result.is_error
    assert json.loads(result.output)["code"] == "subsession_retry_already_active"
    assert delegated is None


@pytest.mark.asyncio
async def test_empty_follow_up_instruction_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, delegated = await _invoke(
        monkeypatch,
        name="follow_up_subsession",
        arguments={"session_id": "child-source", "instruction": "   "},
        source=_source(status="completed"),
    )

    assert result.is_error
    assert json.loads(result.output)["code"] == "subsession_instruction_required"
    assert delegated is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"lineage_depth": "not-an-integer"},
        {"lineage_depth": 1.5},
        {"lineage_depth": -1},
        {"lineage_depth": 1},
        {"lineage_depth": 1, "root_session_id": ""},
        {"lineage_depth": 0, "root_session_id": "unowned-root"},
        {"lineage_depth": 1, "root_session_id": "unowned-root"},
        "scalar-metadata",
        ["list-metadata"],
    ],
)
async def test_malformed_delegate_lineage_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    metadata: Any,
) -> None:
    result, delegated = await _invoke(
        monkeypatch,
        name="follow_up_subsession",
        arguments={"session_id": "child-source", "instruction": "Continue"},
        source=_source(status="completed", delegation_metadata=metadata),
    )

    assert result.is_error
    assert json.loads(result.output)["code"] == "delegate_lineage_invalid"
    assert delegated is None


@pytest.mark.asyncio
async def test_malformed_active_sibling_lineage_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, delegated = await _invoke(
        monkeypatch,
        name="retry_subsession",
        arguments={"session_id": "child-source"},
        source=_source(status="failed"),
        siblings=[
            _source(
                session_id="child-active",
                status="active",
                delegation_metadata=["malformed"],
            )
        ],
    )

    assert result.is_error
    assert json.loads(result.output)["code"] == "delegate_lineage_invalid"
    assert delegated is None


@pytest.mark.asyncio
async def test_delegate_lineage_bootstrap_backfills_legacy_sqlite_rows() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE sessions (session_id VARCHAR PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO sessions (session_id) VALUES ('legacy')"))
            await connection.run_sync(_ensure_delegate_lineage_column)
            await connection.run_sync(_ensure_delegate_lineage_column)

            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"] for column in inspect(sync_connection).get_columns("sessions")
                }
            )
            value = (
                await connection.execute(
                    text("SELECT delegation_metadata FROM sessions WHERE session_id = 'legacy'")
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert "delegation_metadata" in columns
    assert value == "{}"
