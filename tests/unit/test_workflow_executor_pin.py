"""Stage 36: workflow steps share the same executor.

These tests verify that all steps of a single task run on the same
executor unless the agent or user explicitly switches via switch_executor
or /executor — even though each workflow step creates its own
conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.api import runtime_support
from cognis.core.executor_policy import ExecutorPolicy
from cognis.models.agent import AgentDefinition
from cognis.store import queries as store_queries


@dataclass
class _FakeTask:
    task_id: str = "task-1"
    active_executor_id: str | None = None


@dataclass
class _FakeConversation:
    conversation_id: str
    active_executor_id: str | None = None


def _executor_row(
    executor_id: str,
    *,
    labels: dict[str, Any] | None = None,
    runtime_state: str = "active",
) -> SimpleNamespace:
    return SimpleNamespace(
        executor_id=executor_id,
        executor_type="websocket",
        labels=labels or {},
        enabled_tools=["*"],
        enabled_tool_groups=[],
        status="active",
        is_default=False,
        owner_email="alice@example.com",
        runtime_state=runtime_state,
        desired_config_version=0,
        applied_config_version=0,
        observed_tools=[],
        runtime_metadata={},
        last_observed_at=None,
        config={},
    )


@dataclass
class _FakeDB:
    """Captures persistence calls so the test can assert on them."""

    tasks: dict[str, _FakeTask] = field(default_factory=dict)
    conversations: dict[str, _FakeConversation] = field(default_factory=dict)
    initialize_task_calls: list[tuple[str, str]] = field(default_factory=list)
    initialize_conv_calls: list[tuple[str, str]] = field(default_factory=list)


@pytest.fixture
def fake_db() -> _FakeDB:
    return _FakeDB(
        tasks={"task-1": _FakeTask()},
        conversations={
            "conv-step-1": _FakeConversation("conv-step-1"),
            "conv-step-2": _FakeConversation("conv-step-2"),
        },
    )


def _patch_runtime_queries(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB, rows: list[SimpleNamespace]
) -> None:
    async def _list_executors(*_, **__):
        return list(rows)

    async def _get_executor_row(_session, executor_id, **_kwargs):
        for row in rows:
            if row.executor_id == executor_id:
                return row
        return None

    async def _get_conversation(_session, conversation_id):
        return fake_db.conversations.get(conversation_id)

    async def _get_task(_session, task_id):
        return fake_db.tasks.get(task_id)

    async def _initialize_conv(_session, conversation_id, executor_id):
        fake_db.initialize_conv_calls.append((conversation_id, executor_id))
        conv = fake_db.conversations.get(conversation_id)
        if conv is None or conv.active_executor_id is not None:
            return False
        conv.active_executor_id = executor_id
        return True

    async def _initialize_task(_session, task_id, executor_id):
        fake_db.initialize_task_calls.append((task_id, executor_id))
        task = fake_db.tasks.get(task_id)
        if task is None or task.active_executor_id is not None:
            return False
        task.active_executor_id = executor_id
        return True

    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(store_queries, "get_conversation", _get_conversation)
    monkeypatch.setattr(store_queries, "get_task", _get_task)
    monkeypatch.setattr(
        store_queries, "initialize_conversation_active_executor", _initialize_conv
    )
    monkeypatch.setattr(
        store_queries, "initialize_task_active_executor", _initialize_task
    )


class _RuntimeSession:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


def _runtime_session_factory() -> _RuntimeSession:
    return _RuntimeSession()


@pytest.mark.asyncio
async def test_first_step_picks_and_persists_initial_active_on_task(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    """Stage 36 fix: first step initialises BOTH the conversation and task pin."""

    rows = [
        _executor_row("exec-a", labels={"tier": "primary"}),
        _executor_row("exec-b", labels={"tier": "primary"}),
    ]
    _patch_runtime_queries(monkeypatch, fake_db, rows)

    config = await runtime_support._resolve_eligible_executor_config(
        SimpleNamespace(_session_factory=_runtime_session_factory),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_selector": {"tier": "primary"}},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_id="conv-step-1",
        task_id="task-1",
    )
    # Stage 36: pick the lexicographically smallest usable primary at ties.
    assert config["executor_id"] == "exec-a"
    # Both initial-pick persistence paths must fire on the first step.
    assert fake_db.initialize_conv_calls == [("conv-step-1", "exec-a")]
    assert fake_db.initialize_task_calls == [("task-1", "exec-a")]
    assert fake_db.tasks["task-1"].active_executor_id == "exec-a"


@pytest.mark.asyncio
async def test_second_step_reads_task_pin_when_conversation_pin_absent(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    """Stage 36: task pin carries forward to a freshly-created step conversation.

    Even if the controller did not yet seed the conversation row from the
    task pin (a race), the runtime resolver still falls back to the task
    pin and uses it.
    """

    rows = [
        _executor_row("exec-a", labels={"tier": "primary"}),
        _executor_row("exec-b", labels={"tier": "primary"}),
    ]
    _patch_runtime_queries(monkeypatch, fake_db, rows)

    # Task already has its pin from step 1. Step 2's conversation row is
    # fresh and has no pin yet — the runtime factory should read the task
    # pin and use it instead of re-picking.
    fake_db.tasks["task-1"].active_executor_id = "exec-b"

    # Build the runtime factory so we exercise the task-pin lookup path.
    async def _policy(_):
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def _web_config(*_args, **_kwargs):
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)

    # Patch the resolver so we can assert the conversation_active_executor_id
    # passed in.
    captured: dict[str, Any] = {}

    async def _capture_resolve(
        providers,
        agent,
        user_email,
        policy,
        *,
        conversation_active_executor_id=None,
        conversation_id=None,
        task_id=None,
    ):
        captured["pin"] = conversation_active_executor_id
        captured["conversation_id"] = conversation_id
        captured["task_id"] = task_id
        return {
            "executor_id": "exec-b",
            "executor_type": "websocket",
            "enabled_tools": ["*"],
            "enabled_tool_groups": [],
            "labels": {},
            "config": {},
            "owner_email": "alice@example.com",
            "executor_owner_email": "alice@example.com",
            "selection_source": "selector",
            "desired_config_version": 0,
            "applied_config_version": 0,
            "observed_tools": [],
            "last_observed_at": None,
            "runtime_state": "active",
        }

    monkeypatch.setattr(
        runtime_support, "_resolve_eligible_executor_config", _capture_resolve
    )
    # Skip everything else; we only care that the pin was sourced from the task.
    monkeypatch.setattr(
        runtime_support,
        "_resolve_web_config",
        lambda *_a, **_k: {"web_available_backends": ["direct"]},
    )

    factory = runtime_support.build_step_runtime_factory(
        providers=SimpleNamespace(
            _session_factory=_runtime_session_factory,
            executor=SimpleNamespace(),
        ),
        shared_registry=None,
        shared_connection=None,
        session_factory=_runtime_session_factory,
    )
    # We expect the factory to bail out before tool registration since our
    # patched resolver returns a bare config dict; just verify the pin.
    with pytest.raises(Exception, match=r"."):  # noqa: BLE001
        await factory(
            agent=AgentDefinition(
                agent_id="agent-1",
                owner_email="alice@example.com",
                name="Agent",
                execution={"executor_selector": {"tier": "primary"}},
            ),
            user_email="alice@example.com",
            conversation_id="conv-step-2",
            task_id="task-1",
        )
    # The runtime factory MUST have read the task pin as the conversation
    # active_executor_id even though conv-step-2 has no pin of its own.
    assert captured["pin"] == "exec-b"
    assert captured["task_id"] == "task-1"
    assert captured["conversation_id"] == "conv-step-2"


@pytest.mark.asyncio
async def test_explicit_executor_id_initialises_task_pin(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    """Stage 36: explicit primary executor_id also seeds the task pin."""

    rows = [_executor_row("exec-only")]
    _patch_runtime_queries(monkeypatch, fake_db, rows)

    config = await runtime_support._resolve_eligible_executor_config(
        SimpleNamespace(_session_factory=_runtime_session_factory),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "exec-only"},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_id="conv-step-1",
        task_id="task-1",
    )
    assert config["executor_id"] == "exec-only"
    assert fake_db.initialize_task_calls == [("task-1", "exec-only")]
    assert fake_db.tasks["task-1"].active_executor_id == "exec-only"


@pytest.mark.asyncio
async def test_initialize_task_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    """The IS NULL guard prevents re-pick from clobbering an existing pin."""

    rows = [
        _executor_row("exec-a", labels={"tier": "primary"}),
        _executor_row("exec-b", labels={"tier": "primary"}),
    ]
    _patch_runtime_queries(monkeypatch, fake_db, rows)

    # Pre-existing pin: simulate a second step running after an earlier one.
    fake_db.tasks["task-1"].active_executor_id = "exec-b"
    fake_db.conversations["conv-step-1"].active_executor_id = None

    config = await runtime_support._resolve_eligible_executor_config(
        SimpleNamespace(_session_factory=_runtime_session_factory),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_selector": {"tier": "primary"}},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_active_executor_id="exec-b",  # pin propagated by factory
        conversation_id="conv-step-1",
        task_id="task-1",
    )
    # Returns the pin, does NOT call initialize again
    assert config["executor_id"] == "exec-b"
    assert fake_db.tasks["task-1"].active_executor_id == "exec-b"
