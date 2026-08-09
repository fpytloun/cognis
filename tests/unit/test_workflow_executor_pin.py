"""Stage 36: workflow steps share the same executor.

These tests verify that all steps of a single task run on the same
executor unless the agent or user explicitly switches via switch_executor
or /executor — even though each workflow step creates its own
conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    active_executor_assigned_at: datetime | None = None
    active_executor_expires_at: datetime | None = None
    active_executor_source: str | None = None
    active_executor_generation: int = 0
    active_executor_unavailable_since: datetime | None = None


@dataclass
class _FakeConversation:
    conversation_id: str
    active_executor_id: str | None = None
    active_executor_assigned_at: datetime | None = None
    active_executor_expires_at: datetime | None = None
    active_executor_source: str | None = None
    active_executor_generation: int = 0
    active_executor_unavailable_since: datetime | None = None


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
    set_task_calls: list[tuple[str, str]] = field(default_factory=list)
    set_conv_calls: list[tuple[str, str]] = field(default_factory=list)


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

    async def _initialize_task_and_conversation(
        _session, *, task_id, conversation_id, active_executor_id, source="selector"
    ):
        fake_db.initialize_task_calls.append((task_id, active_executor_id))
        fake_db.initialize_conv_calls.append((conversation_id, active_executor_id))
        task = fake_db.tasks.get(task_id)
        conversation = fake_db.conversations.get(conversation_id)
        if task is None or conversation is None:
            return False
        initialized = task.active_executor_id is None
        if initialized:
            task.active_executor_id = active_executor_id
            task.active_executor_source = source
            task.active_executor_generation += 1
        conversation.active_executor_id = task.active_executor_id
        conversation.active_executor_assigned_at = task.active_executor_assigned_at
        conversation.active_executor_expires_at = task.active_executor_expires_at
        conversation.active_executor_source = task.active_executor_source
        conversation.active_executor_generation = task.active_executor_generation
        conversation.active_executor_unavailable_since = task.active_executor_unavailable_since
        return initialized

    async def _cas_failover(
        _session,
        *,
        conversation_id,
        task_id,
        expected_executor_id,
        new_executor_id,
        expected_generation,
        **_metadata,
    ):
        task = fake_db.tasks.get(task_id) if task_id else None
        conversation = fake_db.conversations.get(conversation_id) if conversation_id else None
        authority = task or conversation
        if (
            authority is None
            or authority.active_executor_id != expected_executor_id
            or authority.active_executor_generation != expected_generation
        ):
            return False, expected_generation, None
        authority.active_executor_id = new_executor_id
        authority.active_executor_generation += 1
        authority.active_executor_expires_at = None
        authority.active_executor_unavailable_since = None
        if task is not None and conversation is not None:
            conversation.active_executor_id = new_executor_id
            conversation.active_executor_generation = task.active_executor_generation
            conversation.active_executor_expires_at = None
            conversation.active_executor_unavailable_since = None
        return True, authority.active_executor_generation, "notice"

    async def _set_conv(_session, conversation_id, executor_id, **_metadata):
        fake_db.set_conv_calls.append((conversation_id, executor_id))
        conv = fake_db.conversations.get(conversation_id)
        if conv is None:
            return False
        conv.active_executor_id = executor_id
        return True

    async def _set_task(_session, task_id, executor_id, **_metadata):
        fake_db.set_task_calls.append((task_id, executor_id))
        task = fake_db.tasks.get(task_id)
        if task is None:
            return False
        task.active_executor_id = executor_id
        return True

    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(store_queries, "get_conversation", _get_conversation)
    monkeypatch.setattr(store_queries, "get_task", _get_task)
    monkeypatch.setattr(store_queries, "get_setting_value", _get_setting_value)
    monkeypatch.setattr(store_queries, "initialize_conversation_active_executor", _initialize_conv)
    monkeypatch.setattr(store_queries, "initialize_task_active_executor", _initialize_task)
    monkeypatch.setattr(
        store_queries,
        "initialize_task_and_conversation_active_executor",
        _initialize_task_and_conversation,
    )
    monkeypatch.setattr(store_queries, "cas_executor_failover", _cas_failover)
    monkeypatch.setattr(store_queries, "set_conversation_active_executor", _set_conv)
    monkeypatch.setattr(store_queries, "set_task_active_executor", _set_task)


async def _get_setting_value(_session, _key, default=None):
    return default


class _RuntimeSession:
    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _RuntimeSession:
        return self

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
async def test_initial_pin_loser_reloads_authoritative_winner_not_local_candidate(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    loser = _executor_row("exec-loser", labels={"tier": "primary"})
    winner = _executor_row("exec-winner", labels={"tier": "primary"})
    _patch_runtime_queries(monkeypatch, fake_db, [loser])

    async def _persist_winner(
        _session, *, task_id, conversation_id, active_executor_id, source="selector"
    ):
        del task_id, conversation_id, active_executor_id
        fake_db.tasks["task-1"].active_executor_id = "exec-winner"
        fake_db.tasks["task-1"].active_executor_source = source
        fake_db.tasks["task-1"].active_executor_generation = 1
        return False

    async def _get_authoritative_row(_session, executor_id, **_kwargs):
        return winner if executor_id == "exec-winner" else loser

    monkeypatch.setattr(
        store_queries,
        "initialize_task_and_conversation_active_executor",
        _persist_winner,
    )
    monkeypatch.setattr(store_queries, "get_executor_row", _get_authoritative_row)

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

    assert config["executor_id"] == "exec-winner"
    assert config["config"] == winner.config


@pytest.mark.asyncio
async def test_initial_pin_loser_fails_when_authoritative_winner_is_unusable(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    loser = _executor_row("exec-loser", labels={"tier": "primary"})
    winner = _executor_row("exec-winner", labels={"tier": "primary"}, runtime_state="stopped")
    winner.status = "stopped"
    _patch_runtime_queries(monkeypatch, fake_db, [loser])

    async def _persist_winner(
        _session, *, task_id, conversation_id, active_executor_id, source="selector"
    ):
        del task_id, conversation_id, active_executor_id
        fake_db.tasks["task-1"].active_executor_id = "exec-winner"
        fake_db.tasks["task-1"].active_executor_source = source
        fake_db.tasks["task-1"].active_executor_generation = 1
        return False

    async def _get_authoritative_row(_session, executor_id, **_kwargs):
        return winner if executor_id == "exec-winner" else loser

    monkeypatch.setattr(
        store_queries,
        "initialize_task_and_conversation_active_executor",
        _persist_winner,
    )
    monkeypatch.setattr(store_queries, "get_executor_row", _get_authoritative_row)

    with pytest.raises(RuntimeError, match="unavailable or unusable"):
        await runtime_support._resolve_eligible_executor_config(
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
        conversation_active_executor_expires_at=None,
        conversation_id=None,
        task_id=None,
        **_kwargs,
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

    monkeypatch.setattr(runtime_support, "_resolve_eligible_executor_config", _capture_resolve)
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
async def test_explicit_executor_cas_loser_uses_authoritative_winner(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    loser = _executor_row("exec-explicit-loser")
    winner = _executor_row("exec-explicit-winner")
    _patch_runtime_queries(monkeypatch, fake_db, [loser])
    calls = 0

    async def _get_executor_row(_session, executor_id, **_kwargs):
        nonlocal calls
        calls += 1
        return loser if calls == 1 and executor_id == "exec-explicit-loser" else winner

    async def _persist_winner(
        _session, *, task_id, conversation_id, active_executor_id, source="selector"
    ):
        del task_id, conversation_id, active_executor_id, source
        fake_db.tasks["task-1"].active_executor_id = "exec-explicit-winner"
        return False

    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(
        store_queries,
        "initialize_task_and_conversation_active_executor",
        _persist_winner,
    )

    config = await runtime_support._resolve_eligible_executor_config(
        SimpleNamespace(_session_factory=_runtime_session_factory),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "exec-explicit-loser"},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_id="conv-step-1",
        task_id="task-1",
    )

    assert config["executor_id"] == "exec-explicit-winner"


@pytest.mark.asyncio
async def test_initial_executor_persistence_failure_does_not_route_locally(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    _patch_runtime_queries(monkeypatch, fake_db, [_executor_row("exec-only")])
    rollback_called = False

    class _FailingSession(_RuntimeSession):
        async def commit(self) -> None:
            raise RuntimeError("commit failed")

        async def rollback(self) -> None:
            nonlocal rollback_called
            rollback_called = True

    with pytest.raises(RuntimeError, match="commit failed"):
        await runtime_support._resolve_eligible_executor_config(
            SimpleNamespace(_session_factory=lambda: _FailingSession()),
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
    assert rollback_called


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
    fake_db.tasks["task-1"].active_executor_source = "selector_primary"
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


@pytest.mark.asyncio
async def test_expired_additional_pin_falls_back_to_primary(
    monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
) -> None:
    rows = [
        _executor_row("exec-primary", labels={"tier": "primary"}),
        _executor_row("exec-add", labels={"role": "special"}),
    ]
    _patch_runtime_queries(monkeypatch, fake_db, rows)

    async def _setting_value(_session, key, default=None):
        return 0 if key.endswith("retry_seconds") else default

    monkeypatch.setattr(store_queries, "get_setting_value", _setting_value)

    expired = datetime.now(UTC) - timedelta(seconds=1)
    fake_db.tasks["task-1"].active_executor_id = "exec-add"
    fake_db.tasks["task-1"].active_executor_expires_at = expired
    fake_db.tasks["task-1"].active_executor_source = "additional_explicit"
    fake_db.tasks["task-1"].active_executor_generation = 1
    fake_db.conversations["conv-step-1"].active_executor_id = "exec-add"
    fake_db.conversations["conv-step-1"].active_executor_expires_at = expired
    fake_db.conversations["conv-step-1"].active_executor_source = "additional_explicit"
    fake_db.conversations["conv-step-1"].active_executor_generation = 1
    config = await runtime_support._resolve_eligible_executor_config(
        SimpleNamespace(
            _session_factory=_runtime_session_factory,
            executor=SimpleNamespace(
                websocket=SimpleNamespace(
                    get_connection=lambda executor_id: (
                        SimpleNamespace(connected=True) if executor_id == "exec-primary" else None
                    )
                )
            ),
        ),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={
                "executor_selector": {"tier": "primary"},
                "additional_executors": [{"executor_id": "exec-add"}],
            },
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_active_executor_id="exec-add",
        conversation_active_executor_expires_at=expired,
        conversation_active_executor_generation=1,
        conversation_id="conv-step-1",
        task_id="task-1",
    )

    assert config["executor_id"] == "exec-primary"
    assert fake_db.set_conv_calls == []
    assert fake_db.set_task_calls == []
    assert fake_db.tasks["task-1"].active_executor_id == "exec-primary"
    assert fake_db.conversations["conv-step-1"].active_executor_id == "exec-primary"
    assert fake_db.tasks["task-1"].active_executor_generation == 2
    assert "executor_pin_fallback_notice" not in config
