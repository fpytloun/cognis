from __future__ import annotations

import asyncio
import json
import re
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

import cognis.core.context as context_module
from cognis.core.agent_profiles import resolve_conversation_agent_profile
from cognis.core.context import (
    ContextAssembler,
    _build_agent_work_context_info,
    _build_channel_context_info,
    _build_environment_info,
    _build_web_main_chat_context_info,
    _build_web_topic_context_info,
    _find_cache_breakpoint,
    _is_protected_context_message,
    _limit_history_native_attachment_replay,
    _load_project_instructions,
    events_to_messages,
)
from cognis.core.errors import ImmutablePrefixUnavailable
from cognis.core.executor_pool import ExecutorAvailability, ExecutorPool, ResolvedExecutorTarget
from cognis.core.followups import (
    FollowUpMode,
    FollowUpOriginKind,
    FollowUpRequiredAction,
    FollowUpStatus,
    TaskResultFollowUp,
)
from cognis.core.immutable_prefix import (
    ImmutablePrefixEntry,
    build_prefix_message_events,
)
from cognis.core.memory_aliases import MemoryAliasState
from cognis.core.message_markers import (
    AUDIT_METADATA,
    IMMUTABLE_PREFIX,
    LATEST_MEMORY_RECALL_CONTEXT,
    PROJECT_CONTEXT,
    TOKEN_ESTIMATE,
    TURN_BOUNDARY,
)
from cognis.core.project_context import ProjectContextEntry, build_project_instruction_message
from cognis.core.prompts import (
    PromptContext,
    build_follow_up_guidance,
    build_system_instructions,
)
from cognis.core.runtime import ExecutorEnvironmentSnapshot
from cognis.core.session_cache import (
    CachedEvent,
    CanonicalContextSnapshot,
    CanonicalHistoryUnavailable,
)
from cognis.core.step_profiles import (
    resolve_step_profile,
    step_profile_allows_tool,
    step_profile_visible_by_default,
)
from cognis.models.agent import (
    AgentCapabilities,
    AgentDefinition,
    AgentLLMConfig,
    AgentRuntimeProfile,
)
from cognis.models.config import ModelInfo
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    SessionEvent,
    SessionModel,
)
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolSource
from cognis.models.workflow import StepDefinition, StepProfileMode
from cognis.providers.memory.policy import resolve_memory_policy


class _CacheEntry:
    def __init__(self) -> None:
        self.last_compaction_summary = "summary"
        self.intention = "cached intention"
        self.events = []
        self.initialized = True
        self.last_event_seq = 0
        self.memory_policy_fingerprint: str | None = None
        self.memory_policy_mode: str | None = None


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_memory", [False, True])
@pytest.mark.parametrize("invalidation_phase", ["before_snapshot", "after_snapshot"])
async def test_assembly_never_silently_loses_invalidated_history(
    skip_memory: bool,
    invalidation_phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.core.context as context_module

    cache = _SessionCache()
    cache.history_events = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"content": "Implement the approved fix", "turn_id": "current-turn"},
        }
    ]
    original_refresh = cache.refresh
    original_run = context_module.run_cpu_bound

    def invalidate() -> None:
        cache.history_events.clear()
        cache.prefix_entries.clear()
        cache.entry.canonical_stale = True

    async def refresh(session: SessionModel) -> object:
        result = await original_refresh(session)
        if invalidation_phase == "before_snapshot":
            invalidate()
        return result

    async def run(function: object, *args: object, **kwargs: object) -> object:
        if getattr(function, "__name__", "") == "_prepare_prefix_budget":
            invalidate()
        return await original_run(function, *args, **kwargs)

    monkeypatch.setattr(cache, "refresh", refresh)
    if invalidation_phase == "after_snapshot":
        monkeypatch.setattr(context_module, "run_cpu_bound", run)
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    kwargs = dict(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Implement the approved fix",
        skip_memory=skip_memory,
        required_turn_id="current-turn",
        require_user_event=True,
    )
    if invalidation_phase == "before_snapshot":
        with pytest.raises(CanonicalHistoryUnavailable):
            await assembler.assemble(**kwargs)
    else:
        result = await assembler.assemble(**kwargs)
        assert any(
            "Implement the approved fix" in str(message.get("content"))
            for message in result.messages
        )
        assert result.degraded is False


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_memory", [False, True])
@pytest.mark.parametrize("source_present", [False, True])
async def test_retry_assembly_requires_durable_original_admission(
    skip_memory: bool,
    source_present: bool,
) -> None:
    cache = _SessionCache()
    cache.history_events = [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"turn_id": "original", "content": "Never deploy"},
        },
        {
            "seq": 2,
            "type": "system_message",
            "data": {
                "event": "system_notice",
                "kind": "model_recovery",
                "scope": "turn",
                "turn_id": "retry",
                "retry_source_turn_id": "original",
            },
        },
    ]
    if not source_present:
        cache.history_events.pop(0)
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    kwargs = dict(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Never deploy",
        skip_memory=skip_memory,
        required_turn_id="retry",
        require_user_event=True,
    )
    if source_present:
        result = await assembler.assemble(**kwargs)
        assert any("Never deploy" in str(message.get("content")) for message in result.messages)
        assert not result.degraded
    else:
        with pytest.raises(CanonicalHistoryUnavailable, match="Current user instruction"):
            await assembler.assemble(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_memory", [False, True])
async def test_real_cache_assembly_recovers_invalidation_during_provider_await(
    skip_memory: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.core.session_cache import SessionCache
    from cognis.models.session import EventAppendResult, EventReadResult

    class Guardrails(_Guardrails):
        def __init__(self) -> None:
            super().__init__()
            self.events = [
                {
                    "seq": 1,
                    "type": "user_message",
                    "data": {"content": "Preserve the admitted instruction", "turn_id": "current"},
                }
            ]

        async def read_events(self, *, after_seq: int = 0, **kwargs: object) -> EventReadResult:
            return EventReadResult(
                events=[event for event in self.events if event["seq"] > after_seq],
                last_seq=len(self.events),
                has_more=False,
            )

        async def record_events(self, **kwargs: object) -> EventAppendResult:
            first_seq = len(self.events) + 1
            for event in kwargs["events"]:
                self.events.append(
                    {"seq": len(self.events) + 1, "type": event.type, "data": event.data}
                )
            return EventAppendResult(
                ok=True,
                count=len(self.events) - first_seq + 1,
                first_seq=first_seq,
                last_seq=len(self.events),
            )

    guardrails = Guardrails()
    cache = SessionCache(guardrails)
    session = _session()
    memory = _Memory()
    llm = _LLM()
    refreshed = asyncio.Event()
    original_refresh = cache.refresh
    original_recall = memory.recall
    original_model_info = llm.get_model_info

    async def refresh(session: SessionModel) -> object:
        entry = await original_refresh(session)
        refreshed.set()
        return entry

    async def recall(**kwargs: object) -> object:
        await refreshed.wait()
        await cache.invalidate_canonical(session.session_id)
        return await original_recall(**kwargs)

    async def model_info(model_id: str) -> ModelInfo:
        await cache.invalidate_canonical(session.session_id)
        return await original_model_info(model_id)

    monkeypatch.setattr(cache, "refresh", refresh)
    if skip_memory:
        monkeypatch.setattr(llm, "get_model_info", model_info)
    else:
        monkeypatch.setattr(memory, "recall", recall)
    assembler = ContextAssembler(
        memory=memory,
        guardrails=guardrails,
        llm=llm,
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    result = await assembler.assemble(
        session=session,
        conversation=_conversation(),
        agent=_agent(),
        user_message="Preserve the admitted instruction",
        skip_memory=skip_memory,
        required_turn_id="current",
        require_user_event=True,
    )
    assert any(
        "Preserve the admitted instruction" in str(message.get("content"))
        for message in result.messages
    )
    assert not result.degraded


class _SessionCache:
    def __init__(
        self,
        fail_refresh: bool = False,
        cold: bool = False,
        model_override: str | None = None,
        model_override_provider_id: str | None = None,
    ) -> None:
        self.fail_refresh = fail_refresh
        self.cold = cold
        self.model_override = model_override
        self.model_override_provider_id = model_override_provider_id
        self.entry = None if cold else _CacheEntry()
        self.refresh_calls = 0
        self.prefix_entries: list[ImmutablePrefixEntry] = []
        self.prefix_entries_after_refresh: list[ImmutablePrefixEntry] = []
        self.policy_fingerprint_after_refresh: str | None = None
        self.history_events: list[dict[str, object]] = []
        self.prefix_repair_needed = False
        self.last_repair_attempt_at: float | None = None
        self.mark_prefix_repair_calls = 0
        self.project_contexts: dict[str, ProjectContextEntry] = {}
        self.memory_aliases = MemoryAliasState()
        self.projection_decisions: dict[str, list[dict[str, object]]] = {}

    async def refresh(self, session: SessionModel) -> object:
        del session
        self.refresh_calls += 1
        await asyncio.sleep(0.1)
        if self.fail_refresh:
            raise RuntimeError("event read failed")
        self.entry = self.entry or _CacheEntry()
        if self.prefix_entries_after_refresh:
            self.prefix_entries = list(self.prefix_entries_after_refresh)
        if self.policy_fingerprint_after_refresh is not None:
            self.entry.memory_policy_fingerprint = self.policy_fingerprint_after_refresh
        return self.entry

    def get_entry(self, session_id: str) -> object | None:
        del session_id
        return self.entry

    async def acquire_context_snapshot(self, session: SessionModel) -> CanonicalContextSnapshot:
        return self.get_context_snapshot(session.session_id)

    def get_context_snapshot(self, session_id: str) -> CanonicalContextSnapshot:
        from copy import deepcopy

        if (
            self.entry is None
            or not self.entry.initialized
            or getattr(self.entry, "canonical_stale", False)
        ):
            raise CanonicalHistoryUnavailable("Canonical context is stale")
        events = [
            CachedEvent(
                seq=int(event.get("seq", 0)),
                type=str(event["type"]),
                data=deepcopy(event.get("data", {})),
            )
            if isinstance(event, dict)
            else deepcopy(event)
            for event in self.get_events_since_compaction(session_id)
        ]
        return CanonicalContextSnapshot(
            events=events,
            prefix_entries=deepcopy(self.prefix_entries),
            last_compaction_summary=self.entry.last_compaction_summary,
            last_event_seq=self.entry.last_event_seq,
            last_compaction_seq=0,
            projection_revision=0,
            projection_decisions=deepcopy(self.projection_decisions),
        )

    def get_memory_aliases(self, session_id: str) -> MemoryAliasState:
        del session_id
        return self.memory_aliases

    def get_projection_decisions(self, session_id: str) -> dict[str, list[dict[str, object]]]:
        del session_id
        return {
            call_id: [dict(decision) for decision in decisions]
            for call_id, decisions in self.projection_decisions.items()
        }

    def get_intention(self, session_id: str) -> str | None:
        del session_id
        return "cached intention"

    async def update_intention(
        self, session_id: str, intention: str | None, **kwargs: object
    ) -> None:
        del session_id, kwargs
        if self.entry is not None:
            self.entry.intention = intention

    def get_events_since_compaction(self, session_id: str, types: list[str] | None = None) -> list:
        del session_id, types
        return list(self.history_events)

    def get_prefix_entries(self, session_id: str) -> list[ImmutablePrefixEntry]:
        del session_id
        return list(self.prefix_entries)

    def get_project_contexts(self, session_id: str) -> list[ProjectContextEntry]:
        del session_id
        return sorted(
            self.project_contexts.values(), key=lambda item: (item.seq, item.project_root)
        )

    def get_project_context(
        self, session_id: str, project_root: str | None
    ) -> ProjectContextEntry | None:
        del session_id
        return None if project_root is None else self.project_contexts.get(project_root)

    async def store_project_context(
        self, session_id: str, project_context: ProjectContextEntry
    ) -> ProjectContextEntry:
        del session_id
        self.project_contexts[project_context.project_root] = project_context
        return project_context

    def needs_prefix_repair(self, session_id: str) -> bool:
        del session_id
        return self.prefix_repair_needed

    def get_last_repair_attempt_at(self, session_id: str) -> float | None:
        del session_id
        return self.last_repair_attempt_at

    async def note_repair_attempt(self, session_id: str) -> None:
        del session_id
        self.last_repair_attempt_at = monotonic()

    async def append_recorded_events(
        self, session: SessionModel, events: list, result: object
    ) -> None:
        intaris_events: list[dict[str, object]] = []
        seq = int(getattr(result, "first_seq", 0))
        for event in events:
            intaris_event = {"seq": seq, "type": event.type, "data": event.data}
            intaris_events.append(intaris_event)
            seq += 1
            if event.type == "lifecycle" and event.data.get("event") == "context_projection":
                for decision in event.data.get("decisions", []):
                    if isinstance(decision, dict):
                        key = str(decision.get("call_id") or f"event:{decision.get('event_seq')}")
                        self.projection_decisions.setdefault(key, []).append(
                            {
                                "operation": decision.get("operation"),
                                "rendering_policy_version": decision.get(
                                    "rendering_policy_version"
                                ),
                                "parameters": dict(decision.get("parameters", {})),
                            }
                        )
            if event.type not in {"system_message", "developer_message"}:
                continue
            self.prefix_entries.append(
                ImmutablePrefixEntry(
                    role=str(event.data.get("role") or "developer"),
                    source=str(event.data.get("source") or ""),
                    content=str(event.data.get("content") or ""),
                    seq=seq - 1,
                )
            )
        projection_events = [
            event
            for event in intaris_events
            if event["type"] == "lifecycle" and event["data"].get("event") == "context_projection"
        ]
        self.history_events.extend(projection_events)
        if hasattr(self, "_events"):
            self._events.extend(projection_events)

    async def invalidate_canonical(self, session_id: str) -> None:
        del session_id
        self.entry = None

    async def store_prefix_snapshot(
        self,
        session_id: str,
        entries: list[ImmutablePrefixEntry],
        *,
        snapshot_seq: int,
        snapshot_source: str,
        memory_policy_fingerprint: str | None = None,
        memory_policy_mode: str | None = None,
    ) -> None:
        del session_id, snapshot_seq, snapshot_source
        self.prefix_entries = list(entries)
        self.prefix_repair_needed = False
        if self.entry is not None:
            self.entry.memory_policy_fingerprint = memory_policy_fingerprint
            self.entry.memory_policy_mode = memory_policy_mode

    async def mark_prefix_repair_needed(self, session_id: str) -> None:
        del session_id
        self.prefix_repair_needed = True
        self.mark_prefix_repair_calls += 1

    def get_compaction_summary(self, session_id: str) -> str | None:
        del session_id
        return "summary"

    def get_model_override(self, session_id: str) -> str | None:
        del session_id
        return self.model_override

    def get_model_override_provider_id(self, session_id: str) -> str | None:
        del session_id
        return self.model_override_provider_id

    def update_context_usage(
        self, session: object, *, prompt_tokens: int, max_context_tokens: int, model: str
    ) -> None:
        pass


class _HistorySessionCache(_SessionCache):
    def __init__(self, events: list[dict[str, object]]) -> None:
        super().__init__()
        self._events = list(events)
        for event in events:
            data = event.get("data")
            if (
                event.get("type") != "lifecycle"
                or not isinstance(data, dict)
                or data.get("event") != "context_projection"
            ):
                continue
            for raw_decision in data.get("decisions", []):
                if not isinstance(raw_decision, dict):
                    continue
                call_id = raw_decision.get("call_id")
                if not isinstance(call_id, str):
                    continue
                self.projection_decisions.setdefault(call_id, []).append(
                    {
                        "operation": raw_decision.get("operation"),
                        "rendering_policy_version": raw_decision.get("rendering_policy_version"),
                        "parameters": dict(raw_decision.get("parameters", {})),
                    }
                )

    def get_events_since_compaction(self, session_id: str, types: list[str] | None = None) -> list:
        del session_id, types
        return list(self._events)


class _Memory:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.search_modes: list[str] = []
        self.identity_calls: list[dict[str, object]] = []
        self.recall_calls: list[dict[str, object]] = []
        self.call_order: list[str] = []

    async def load_session_identity(self, **kwargs: object) -> dict[str, object]:
        self.call_order.append("identity")
        self.identity_calls.append(dict(kwargs))
        await asyncio.sleep(0.1)
        return {
            "session_id": "mem-1",
            "instructions": "Use memory tools carefully.",
            "core_memories": "prefers Python",
        }

    async def recall(self, **kwargs: object) -> dict[str, object]:
        self.call_order.append("recall")
        self.recall_calls.append(dict(kwargs))
        self.search_modes.append(str(kwargs["search_mode"]))
        await asyncio.sleep(0.1)
        if self.fail:
            raise RuntimeError("mnemory unavailable")
        return {
            "session_id": "mem-1",
            "core_memories": "prefers Python",
            "search_results": [{"memory": "Uses pytest", "score": 0.9}],
        }


class _Guardrails:
    def __init__(self) -> None:
        self.record_event_calls: list[dict[str, object]] = []

    async def get_session(self, session_id: str) -> object:
        del session_id
        await asyncio.sleep(0.1)
        return type(
            "IntarisSession",
            (),
            {
                "intention": "fresh intention",
                "title": None,
                "updated_at": None,
                "status": "active",
            },
        )()

    async def record_events(self, **kwargs: object) -> object:
        events = list(kwargs.get("events", []))
        self.record_event_calls.append(dict(kwargs))
        return type(
            "AppendResult",
            (),
            {
                "ok": True,
                "count": len(events),
                "first_seq": 1,
                "last_seq": len(events),
            },
        )()


class _TitleGuardrails(_Guardrails):
    async def get_session(self, session_id: str) -> object:
        del session_id
        await asyncio.sleep(0.1)
        return type(
            "IntarisSession",
            (),
            {
                "intention": "child intention",
                "status": "active",
                "title": "Child task title",
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
        )()


class _FailingSessionFactory:
    def __call__(self) -> _FailingSessionFactory:
        return self

    async def __aenter__(self) -> object:
        raise AssertionError("child session must not sync title to conversation state")

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _LLM:
    async def resolve_model(
        self, explicit_model: str | None = None, task_type: str = "default"
    ) -> str:
        del explicit_model, task_type
        return "test-model"

    async def get_model_info(self, model_id: str) -> ModelInfo:
        del model_id
        return ModelInfo(model_id="test-model", context_window=20000, max_output_tokens=256)

    def count_tokens(self, text: str, model: str) -> int:
        del model
        return max(1, len(text) // 4)

    def count_messages_tokens(self, messages: list[dict[str, object]], model: str) -> int:
        del model
        return sum(max(1, len(str(message.get("content", ""))) // 4) for message in messages)


@pytest.mark.parametrize("recoverable", [True, False])
@pytest.mark.parametrize("threshold", [0.85, 0.9])
def test_projection_uses_steady_not_legacy_compaction_threshold(
    recoverable: bool, threshold: float
) -> None:
    from cognis.core.message_markers import RECOVERY_CALL_ID

    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        compaction_threshold=threshold,
    )
    messages = [{"role": "user", "content": "Keep this instruction"}]
    for index in range(40):
        call_id = f"call-{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "data " * 4300,
                    **({RECOVERY_CALL_ID: call_id} if recoverable else {}),
                },
            ]
        )
    messages.append({"role": "user", "content": "Continue"})
    raw = _LLM().count_messages_tokens(messages, "test-model")
    assert 250240 * 0.85 < raw < 250240 * 0.88
    projected, _, _, _, _, tokens, _ = assembler._finalize_assembled_messages(
        messages=messages,
        resolved_model="test-model",
        max_context_tokens=400000,
        available_prompt_tokens=250240,
        max_prompt_tokens=237728,
        tool_schema_tokens=0,
    )
    if recoverable:
        assert tokens <= 250240 * 0.35
    else:
        assert tokens == raw
    assert projected[0]["content"] == "Keep this instruction"


@pytest.mark.parametrize("ratio", [1.0, 2.361, 3.0])
def test_cross_turn_projection_uses_calibrated_savings(ratio: float) -> None:
    import math

    from cognis.core.message_markers import RECOVERY_CALL_ID

    llm = _LLM()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    messages = [{"role": "user", "content": "Preserve this instruction"}]
    for i in range(40):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-{i}",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call-{i}",
                    RECOVERY_CALL_ID: f"call-{i}",
                    "content": "data " * 4800,
                },
            ]
        )
    messages.append({"role": "user", "content": "Now continue the next task"})
    raw = llm.count_messages_tokens(messages, "test-model") + 10000
    assert raw < 320000
    projected, _ = assembler._project_cross_turn_messages(
        messages=messages,
        resolved_model="test-model",
        max_context_tokens=1000000,
        available_prompt_tokens=839232,
        max_prompt_tokens=797270,
        tool_schema_tokens=10000,
        calibration_ratio=ratio,
    )
    remaining = llm.count_messages_tokens(projected, "test-model") + 10000
    assert projected[0]["content"] == "Preserve this instruction"
    if raw * ratio <= 839232 * 0.75:
        assert projected == messages
    else:
        assert math.ceil(remaining * ratio) <= 839232 * 0.35
        assert remaining < raw


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_memory", [False, True])
async def test_assembly_passes_compatible_calibration_to_worker(skip_memory: bool) -> None:
    import math
    from unittest.mock import Mock

    cache = _SessionCache()
    cache.apply_prompt_token_calibration = Mock(return_value=(3, {"applied_ratio": 2.361}))
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    assembler._finalize_assembled_messages = Mock(wraps=assembler._finalize_assembled_messages)
    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Keep the original instruction",
        skip_memory=skip_memory,
    )
    assert assembler._finalize_assembled_messages.call_args.kwargs["calibration_ratio"] == 2.361
    assert cache.apply_prompt_token_calibration.call_args.kwargs == {
        "raw_prompt_tokens": 1,
        "provider_id": None,
        "model": "test-model",
        "estimator_identity": "unknown:v1",
    }
    assert result.prompt_tokens >= math.ceil(
        _LLM().count_messages_tokens(result.messages, "test-model") * 2.361
    )


@pytest.mark.parametrize("mismatch", [None, "provider_id", "model", "estimator_identity"])
def test_projection_calibration_is_scoped_to_runtime_identity(mismatch: str | None) -> None:
    from cognis.core.session_cache import CachedSessionState, SessionCache

    cache = SessionCache(None)
    entry = CachedSessionState(session_id="session", intaris_session_id="history")
    calibration = {
        "provider_id": "provider",
        "model": "test-model",
        "estimator_identity": "unknown:v1",
        "applied_ratio": 2.361,
    }
    if mismatch:
        calibration[mismatch] = "different"
    entry.context_metadata["prompt_token_calibration"] = calibration
    cache._entries["session"] = entry
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    assert assembler._projection_calibration_ratio("session", "test-model", "provider") == (
        2.361 if mismatch is None else 1.0
    )
    assert assembler._projection_calibration_ratio("other-session", "test-model", "provider") == 1.0


class _ScopedLLM(_LLM):
    def __init__(self) -> None:
        self.resolve_calls: list[dict[str, object]] = []
        self.info_calls: list[dict[str, object]] = []

    async def resolve_model_target(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
        acting_user_email: str | None = None,
    ) -> tuple[str, str | None]:
        self.resolve_calls.append(
            {
                "explicit_model": explicit_model,
                "task_type": task_type,
                "explicit_provider_id": explicit_provider_id,
                "acting_user_email": acting_user_email,
            }
        )
        return explicit_model or "test-model", explicit_provider_id

    async def get_model_info(
        self,
        model_id: str,
        provider_id: str | None = None,
        acting_user_email: str | None = None,
    ) -> ModelInfo:
        self.info_calls.append(
            {
                "model_id": model_id,
                "provider_id": provider_id,
                "acting_user_email": acting_user_email,
            }
        )
        return ModelInfo(model_id=model_id, context_window=20000, max_output_tokens=256)


class _VisionLLM(_LLM):
    async def get_model_info(self, model_id: str) -> ModelInfo:
        del model_id
        return ModelInfo(
            model_id="test-model",
            context_window=20000,
            max_output_tokens=256,
            supports_vision=True,
        )


class _FileLLM(_LLM):
    async def get_model_info(self, model_id: str) -> ModelInfo:
        del model_id
        return ModelInfo(
            model_id="test-model",
            context_window=20000,
            max_output_tokens=256,
            supports_pdf_input=True,
            supports_file_input=True,
        )


def test_cache_breakpoint_extends_through_project_context() -> None:
    messages = [
        {"role": "system", "content": "immutable", IMMUTABLE_PREFIX: True},
        {"role": "system", "content": "project", PROJECT_CONTEXT: True},
        {"role": "system", "content": "environment"},
        {"role": "user", "content": "hello"},
    ]

    assert _find_cache_breakpoint(messages) == 1


def test_only_latest_memory_recall_block_is_protected() -> None:
    stale_recall = {
        "role": "system",
        "content": "old recall",
        AUDIT_METADATA: {
            "context_injection": True,
            "replayable": True,
            "visibility": "agent_context",
        },
    }
    latest_recall = {**stale_recall, LATEST_MEMORY_RECALL_CONTEXT: True}

    assert not _is_protected_context_message(stale_recall)
    assert _is_protected_context_message(latest_recall)


def test_pruning_protects_current_turn_boundary_over_later_user_attachment() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    current_turn = {"role": "user", "content": "current instruction", TURN_BOUNDARY: True}
    messages = [
        {"role": "system", "content": "prefix", IMMUTABLE_PREFIX: True},
        {"role": "user", "content": "old history " * 200},
        current_turn,
        {"role": "user", "content": "attachment context " * 200},
        {"role": "system", "content": "tail reminder"},
    ]

    pruned = assembler._prune_messages(
        messages=messages,
        resolved_model="test-model",
        max_prompt_tokens=20,
        tool_schema_tokens=0,
    )

    assert current_turn in pruned
    assert all("old history" not in str(message.get("content", "")) for message in pruned)
    assert all("attachment context" not in str(message.get("content", "")) for message in pruned)


@pytest.mark.parametrize("group_count", [64, 512])
@pytest.mark.parametrize("underestimate", [False, True])
def test_pruning_visits_tool_groups_linearly(
    monkeypatch: pytest.MonkeyPatch, group_count: int, underestimate: bool
) -> None:
    from unittest.mock import Mock

    llm = _LLM()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    messages = [{"role": "system", "content": "prefix", IMMUTABLE_PREFIX: True}]
    for index in range(group_count):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"large.py"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call-{index}",
                    "content": "large output " * 1000,
                },
            ]
        )
    protected_result = {
        "role": "tool",
        "tool_call_id": "protected-call",
        "content": "protected evidence " * 1000,
        "_protected_tool_output": True,
    }
    messages.extend(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "protected-call", "function": {"name": "read"}}],
            },
            protected_result,
            {"role": "user", "content": "current instruction", TURN_BOUNDARY: True},
            {"role": "system", "content": "tail reminder"},
        ]
    )
    original = deepcopy(messages)
    expected = list(messages)
    for replacements in context_module._compact_droppable_tool_groups(expected):
        for index, message in replacements.items():
            expected[index] = message
    budget = llm.count_messages_tokens(expected, "test-model") + 100
    if underestimate:
        monkeypatch.setattr(assembler, "_message_token_estimate", lambda *_: 1)
    protection_check = Mock(wraps=context_module._is_protected_context_message)
    monkeypatch.setattr(context_module, "_is_protected_context_message", protection_check)

    pruned = assembler._prune_messages(
        messages=messages,
        resolved_model="test-model",
        max_prompt_tokens=budget,
        tool_schema_tokens=100,
    )

    assert pruned == expected
    assert messages == original
    assert protected_result in pruned
    assert all(pruned[2 + 2 * index]["_projected_compacted"] for index in range(group_count))
    assert protection_check.call_count <= 3 * len(messages)
    assert llm.count_messages_tokens(pruned, "test-model") + 100 <= budget


def test_pruning_stops_compacting_when_budget_is_met() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "old-call", "function": {"name": "read"}}]},
        {"role": "tool", "tool_call_id": "old-call", "content": "old evidence " * 1000},
        {"role": "assistant", "tool_calls": [{"id": "new-call", "function": {"name": "read"}}]},
        {"role": "tool", "tool_call_id": "new-call", "content": "new evidence " * 1000},
        {"role": "user", "content": "current instruction", TURN_BOUNDARY: True},
    ]
    pruned = assembler._prune_messages(
        messages=messages,
        resolved_model="test-model",
        max_prompt_tokens=4000,
        tool_schema_tokens=0,
    )

    assert len(pruned) == len(messages)
    assert pruned[1]["_projected_compacted"]
    assert pruned[3] is messages[3]


def test_pruning_refreshes_cached_assistant_estimate_with_unchanged_arguments() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "cached-call",
                    "function": {"name": "read", "arguments": '{"path":"large.py"}'},
                }
            ],
            TOKEN_ESTIMATE: 100_000,
        },
        {"role": "tool", "tool_call_id": "cached-call", "content": "large evidence " * 1000},
        {"role": "user", "content": "current instruction", TURN_BOUNDARY: True},
    ]

    pruned = assembler._prune_messages(
        messages=messages,
        resolved_model="test-model",
        max_prompt_tokens=1000,
        tool_schema_tokens=0,
    )

    assert len(pruned) == len(messages)
    assert pruned[0]["tool_calls"] == messages[0]["tool_calls"]
    assert TOKEN_ESTIMATE not in pruned[0]
    assert pruned[1]["_projected_compacted"]
    assert messages[0][TOKEN_ESTIMATE] == 100_000


class _SessionManager:
    def __init__(self) -> None:
        self.attached: list[tuple[str, str]] = []

    async def attach_mnemory_session(self, session_id: str, mnemory_session_id: str) -> bool:
        self.attached.append((session_id, mnemory_session_id))
        return True


def _agent() -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        system_prompt="You are helpful.",
        llm_config=AgentLLMConfig(model="test-model", max_tokens=128),
    )


def _default_memory_policy_fingerprint() -> str:
    agent = _agent()
    return resolve_memory_policy(
        agent,
        resolve_conversation_agent_profile(agent, _session(), _conversation()),
    ).policy_fingerprint


def _agent_with_personality() -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        system_prompt="Be helpful.",
        personality={
            "purpose": "research specialist",
            "tone": "formal, precise",
            "temperament": "patient, methodical",
            "behavioral_rules": ["Always cite sources"],
        },
        llm_config=AgentLLMConfig(model="test-model", max_tokens=128),
    )


def _agent_with_skills() -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        system_prompt="You are helpful.",
        skills={
            "_available_skills_metadata": (
                "<available_skills>\n"
                "  <skill>\n"
                "    <name>Release Helper</name>\n"
                "    <tools>tag_release</tools>\n"
                "  </skill>\n"
                "</available_skills>"
            )
        },
        llm_config=AgentLLMConfig(model="test-model", max_tokens=128),
    )


def _agent_with_auto_loaded_skill() -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        system_prompt="You are helpful.",
        skills={
            "_available_skills_metadata": (
                "<available_skills>\n"
                "  <skill>\n"
                "    <name>Coding</name>\n"
                "    <loaded>true</loaded>\n"
                "  </skill>\n"
                "</available_skills>"
            ),
            "_auto_loaded_skill_contexts": [
                (
                    "<loaded_skill>\n"
                    "<skill_id>coding</skill_id>\n"
                    "<name>Coding</name>\n"
                    "<instructions>\nUse careful implementation discipline.\n</instructions>\n"
                    "</loaded_skill>"
                )
            ],
        },
        llm_config=AgentLLMConfig(model="test-model", max_tokens=128),
    )


def _conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web", memory_labels={"project": "cognis"}),
    )


def _web_main_conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(
            type="web",
            ref="web:user:user@example.com:default",
            memory_labels={"project": "cognis"},
        ),
    )


def _signal_conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(
            type="signal",
            ref="signal:acct:+420",
            platform_data={
                "channel_type": "signal",
                "chat_type": "group",
                "chat_name": "Ops",
                "thread_id": "thread-1",
            },
        ),
    )


def _agent_direct_conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(
            type="web",
            ref="web:agent_direct:user@example.com:agent-1",
            platform_data={"kind": "agent_direct"},
        ),
    )


def _session(mnemory_session_id: str | None = None) -> SessionModel:
    return SessionModel(
        session_id="session-1",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id="session-1",
        mnemory_session_id=mnemory_session_id,
    )


def _child_session() -> SessionModel:
    return SessionModel(
        session_id="child-1",
        conversation_id="conv-1",
        parent_session_id="parent-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id="child-1",
    )


@pytest.mark.asyncio
async def test_context_assembler_does_not_sync_child_session_title() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_TitleGuardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
        session_factory=_FailingSessionFactory(),
    )

    result = await assembler.assemble(
        session=_child_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="review this",
        tool_definitions=[],
    )

    assert result.messages


def test_build_channel_context_info_is_channel_only() -> None:
    assert _build_channel_context_info(ConversationContext(type="web")) is None
    assert (
        _build_channel_context_info(
            ConversationContext(type="web", platform_data={"assistant_delivery_mode": "final_only"})
        )
        is None
    )
    assert _build_channel_context_info(ConversationContext(type="task", ref="task-1")) is None
    assert _build_channel_context_info(ConversationContext(type="direct")) is None

    content = _build_channel_context_info(
        ConversationContext(
            type="signal",
            ref="signal:acct:+420",
            platform_data={
                "channel_type": "signal",
                "chat_type": "group",
                "chat_name": "Ops",
                "thread_id": "thread-1",
            },
        )
    )

    assert content is not None
    assert "Conversation channel context:" in content
    assert "- Channel: signal" in content
    assert "- Chat type: group" in content
    assert "- Chat name: Ops" in content
    assert "- Thread-bound: yes" in content
    assert "Keep the channel unblocked" in content
    assert "asynchronous action" in content
    assert "delegate" not in content
    assert "agent_conversation" not in content
    assert "create_task" not in content


def test_build_channel_context_info_includes_final_only_delivery_guidance() -> None:
    content = _build_channel_context_info(
        ConversationContext(
            type="signal",
            ref="signal:acct:+420",
            platform_data={
                "channel_type": "signal",
                "assistant_delivery_mode": "final_only",
            },
        )
    )

    assert content is not None
    assert "Assistant delivery mode:" in content
    assert "- Mode: final_only." in content
    assert "only the final assistant message" in content
    assert "Do not write intermediate assistant messages" in content
    assert "self-contained enough for the request" in content
    assert "Do not replay routine progress or filler" in content


def test_build_channel_context_info_includes_concatenated_delivery_guidance() -> None:
    content = _build_channel_context_info(
        ConversationContext(
            type="signal",
            ref="signal:acct:+420",
            platform_data={
                "channel_type": "signal",
                "assistant_delivery_mode": "concatenated",
            },
        )
    )

    assert content is not None
    assert "Assistant delivery mode:" in content
    assert "- Mode: concatenated." in content
    assert "batched together at the end, not as live progress" in content
    assert "Treat the delivered batch as one final channel reply" in content
    assert "avoid repetitive status notes and pre-tool filler" in content
    assert "Use judgment" in content


def test_build_web_main_chat_context_info_is_web_main_only() -> None:
    assert _build_web_main_chat_context_info(ConversationContext(type="web")) is None
    assert _build_web_main_chat_context_info(ConversationContext(type="signal")) is None

    content = _build_web_main_chat_context_info(
        ConversationContext(
            type="web",
            ref="web:agent_direct:user@example.com:agent-1",
            platform_data={"kind": "agent_direct"},
        )
    )

    assert content is not None
    assert "Web main chat context:" in content
    assert "- Channel: web" in content
    assert "- Main web chat: yes" in content
    assert "DM-like main chat" in content
    assert "Keep this chat responsive" in content
    assert "asynchronous action" in content
    assert "delegate" not in content
    assert "agent_conversation" not in content
    assert "create_task" not in content

    web_main_content = _build_web_main_chat_context_info(
        ConversationContext(type="web", ref="web:user:user@example.com:default")
    )
    assert web_main_content is not None
    assert "Web main chat context:" in web_main_content


def test_build_web_topic_context_info_is_topic_only() -> None:
    assert _build_web_topic_context_info(ConversationContext(type="signal")) is None
    assert (
        _build_web_topic_context_info(
            ConversationContext(type="web", ref="web:user:user@example.com:default")
        )
        is None
    )
    assert (
        _build_web_topic_context_info(
            ConversationContext(
                type="web",
                ref="web:agent_direct:user@example.com:agent-1",
                platform_data={"kind": "agent_direct"},
            )
        )
        is None
    )

    content = _build_web_topic_context_info(ConversationContext(type="web"))

    assert content is not None
    assert "Web topic chat context:" in content
    assert "- Main web chat: no" in content
    assert "normal topic chat, not the DM-like main chat" in content
    assert "Do not apply responsiveness or nonblocking guidance" in content
    assert "Joined execution and proportionate waiting are normal" in content
    assert "delegate" not in content
    assert "managed conversation" not in content
    assert "wait=false" not in content
    assert "Keep this chat responsive" not in content


def test_build_agent_work_context_info_lists_only_valid_managed_options() -> None:
    content = _build_agent_work_context_info(
        ConversationContext(
            type="agent_work",
            platform_data={
                "kind": "agent_work",
                "controller_agent_id": "riker",
                "controller_conversation_id": "conv-controller",
                "controller_session_id": "sess-controller",
            },
        )
    )

    assert content is not None
    assert "Agent work context:" in content
    assert "delegate(wait=false)" not in content
    assert "agent_conversation_create(wait=false)" not in content
    assert "delegate" not in content
    assert "tasks or workflows" not in content


@pytest.mark.asyncio
async def test_context_assembler_injects_channel_context_only_for_channel_conversations() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    signal_result = await assembler.assemble(
        session=_session(),
        conversation=_signal_conversation(),
        agent=_agent(),
        user_message="please help",
        tool_definitions=[],
    )
    signal_messages = [str(message.get("content", "")) for message in signal_result.messages]

    assert any("Conversation channel context:" in message for message in signal_messages)
    assert any("Keep the channel unblocked" in message for message in signal_messages)
    assert not any("delegate(wait=" in message for message in signal_messages)
    assert not any("agent_conversation_create" in message for message in signal_messages)

    direct_result = await assembler.assemble(
        session=_session(),
        conversation=_agent_direct_conversation(),
        agent=_agent(),
        user_message="please help",
        tool_definitions=[],
    )
    direct_messages = [str(message.get("content", "")) for message in direct_result.messages]
    direct_context_messages = [
        message for message in direct_messages if "Web main chat context:" in message
    ]

    assert any("Web main chat context:" in message for message in direct_messages)
    assert any("Keep this chat responsive" in message for message in direct_context_messages)
    assert not any("delegate(wait=" in message for message in direct_context_messages)
    assert not any("Conversation channel context:" in message for message in direct_messages)

    web_main_result = await assembler.assemble(
        session=_session(),
        conversation=_web_main_conversation(),
        agent=_agent(),
        user_message="please help",
        tool_definitions=[],
    )
    web_main_messages = [str(message.get("content", "")) for message in web_main_result.messages]
    assert any("Web main chat context:" in message for message in web_main_messages)
    assert any("Keep this chat responsive" in message for message in web_main_messages)
    assert not any("agent_conversation_create" in message for message in web_main_messages)

    web_result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="please help",
        tool_definitions=[],
    )
    web_messages = [str(message.get("content", "")) for message in web_result.messages]

    assert not any("Conversation channel context:" in message for message in web_messages)
    assert not any("Web main chat context:" in message for message in web_messages)
    assert any("Web topic chat context:" in message for message in web_messages)
    assert any("normal topic chat" in message for message in web_messages)
    assert any(
        "Do not apply responsiveness or nonblocking guidance" in message for message in web_messages
    )
    assert any(
        "Joined execution and proportionate waiting are normal" in message
        for message in web_messages
    )
    assert not any("Keep the channel unblocked" in message for message in web_messages)
    assert not any("Keep this direct chat responsive" in message for message in web_messages)
    assert not any(
        "keep the conversation asynchronously responsive" in message for message in web_messages
    )


@pytest.mark.asyncio
async def test_context_assembler_passes_actor_scope_to_model_resolution() -> None:
    llm = _ScopedLLM()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    agent = _agent().model_copy(
        update={"llm_config": AgentLLMConfig(model=None, provider_id="user-claude")}
    )

    await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=agent,
        user_message="please help",
        tool_definitions=[],
    )

    assert llm.resolve_calls[0]["explicit_provider_id"] == "user-claude"
    assert llm.resolve_calls[0]["acting_user_email"] == "user@example.com"
    assert llm.info_calls[0]["provider_id"] == "user-claude"
    assert llm.info_calls[0]["acting_user_email"] == "user@example.com"


@pytest.mark.asyncio
async def test_context_assembler_model_override_does_not_inherit_agent_provider() -> None:
    llm = _ScopedLLM()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=llm,
        session_cache=_SessionCache(model_override="stale-cache-model"),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    agent = _agent().model_copy(
        update={"llm_config": AgentLLMConfig(model="gpt-5", provider_id="codex")}
    )

    await assembler.assemble(
        session=_session().model_copy(update={"model_override": "claude-sonnet-5"}),
        conversation=_conversation(),
        agent=agent,
        user_message="please help",
        tool_definitions=[],
    )

    assert llm.resolve_calls[0]["explicit_model"] == "claude-sonnet-5"
    assert llm.resolve_calls[0]["explicit_provider_id"] is None
    assert llm.info_calls[0]["provider_id"] is None


@pytest.mark.asyncio
async def test_context_assembler_runs_fetches_in_parallel_and_attaches_memory_session() -> None:
    memory = _Memory()
    session_manager = _SessionManager()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=session_manager,
        max_context_tokens=4096,
        compaction_threshold=0.85,
        recall_ttl_seconds=123,
    )

    started_at = monotonic()
    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="please help",
        tool_definitions=[],
    )
    elapsed = monotonic() - started_at

    assert elapsed < 0.5
    assert memory.call_order == ["identity", "recall"]
    assert memory.search_modes == ["search"]
    assert memory.identity_calls == [
        {
            "session_id": None,
            "labels": {"project": "cognis"},
            "context": "cached intention",
        }
    ]
    assert memory.recall_calls[0]["session_id"] == "mem-1"
    assert memory.recall_calls[0]["ttl"] == 123
    assert session_manager.attached == [("session-1", "mem-1")]
    assert any('trust="untrusted"' in str(message["content"]) for message in result.messages)


@pytest.mark.asyncio
async def test_context_assembler_skips_disabled_artifact_urls() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_FileLLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    blocked_url = "https://cognis.fpy.cz/api/v1/artifacts/content/documents/doc_1/report.pdf"

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Read this",
        user_attachments=[
            {
                "artifact_id": "art_1",
                "kind": "pdf",
                "mime_type": "application/pdf",
                "filename": "report.pdf",
                "url": blocked_url,
            }
        ],
        disabled_artifact_urls={blocked_url},
        tool_definitions=[],
    )

    serialized = json.dumps(result.messages)
    assert blocked_url not in serialized
    assert "report.pdf" in serialized


@pytest.mark.asyncio
async def test_context_assembler_skips_disabled_artifact_urls_from_history() -> None:
    cache = _SessionCache()
    blocked_url = "https://cognis.fpy.cz/api/v1/artifacts/content/documents/doc_1/report.pdf"
    cache.history_events = [
        {
            "type": "user_message",
            "data": {
                "content": "Read this",
                "attachments": [
                    {
                        "artifact_id": "art_1",
                        "kind": "pdf",
                        "mime_type": "application/pdf",
                        "filename": "report.pdf",
                        "url": blocked_url,
                    }
                ],
            },
        }
    ]
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_FileLLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Read this",
        disabled_artifact_urls={blocked_url},
        tool_definitions=[],
    )

    serialized = json.dumps(result.messages)
    assert blocked_url not in serialized
    assert "report.pdf" in serialized


def test_history_native_attachment_replay_keeps_newest_images_within_budget() -> None:
    events: list[dict[str, object]] = []
    for index in range(40):
        events.extend(
            [
                {
                    "type": "user_message",
                    "data": {
                        "content": f"Image {index}",
                        "attachments": [
                            {
                                "artifact_id": f"img_{index}",
                                "kind": "image",
                                "mime_type": "image/png",
                                "filename": f"image-{index}.png",
                                "url": f"https://cognis.example/artifacts/img_{index}",
                            }
                        ],
                    },
                },
                {
                    "type": "assistant_message",
                    "data": {"content": f"Response {index}"},
                },
            ]
        )

    bounded, replay_positions = _limit_history_native_attachment_replay(events)

    assert replay_positions == {(index, 0) for index in range(64, 80, 2)}
    serialized = json.dumps(bounded)
    assert "https://cognis.example/artifacts/img_39" in serialized
    assert "https://cognis.example/artifacts/img_32" in serialized
    assert "https://cognis.example/artifacts/img_31" not in serialized
    assert '"artifact_id": "img_0"' in serialized
    assert '"filename": "image-0.png"' in serialized


def test_history_native_attachment_replay_budgets_repeated_artifact_ids_by_position() -> None:
    attachments = [
        {
            "artifact_id": "img_repeated",
            "kind": "image",
            "mime_type": "image/png",
            "filename": f"copy-{index}.png",
            "url": f"https://cognis.example/artifacts/img_repeated?copy={index}",
        }
        for index in range(10)
    ]
    events = [
        {
            "type": "user_message",
            "data": {"content": "Repeated image", "attachments": attachments},
        }
    ]

    bounded, replay_positions = _limit_history_native_attachment_replay(events)

    assert replay_positions == {(0, index) for index in range(2, 10)}
    bounded_attachments = bounded[0]["data"]["attachments"]
    assert "url" not in bounded_attachments[0]
    assert "url" not in bounded_attachments[1]
    assert bounded_attachments[2]["url"].endswith("copy=2")
    assert bounded_attachments[9]["url"].endswith("copy=9")


def test_history_native_attachment_replay_partially_selects_oversized_newest_event() -> None:
    old_event = {
        "type": "user_message",
        "data": {
            "content": "Old image",
            "attachments": [
                {
                    "artifact_id": "img_old",
                    "kind": "image",
                    "mime_type": "image/png",
                    "filename": "old.png",
                    "url": "https://cognis.example/artifacts/img_old",
                }
            ],
        },
    }
    newest_attachments = [
        {
            "artifact_id": f"img_new_{index}",
            "kind": "image",
            "mime_type": "image/png",
            "filename": f"new-{index}.png",
            "url": f"https://cognis.example/artifacts/img_new_{index}",
        }
        for index in range(10)
    ]
    events = [
        old_event,
        {
            "type": "user_message",
            "data": {"content": "Newest images", "attachments": newest_attachments},
        },
    ]

    bounded, replay_positions = _limit_history_native_attachment_replay(events)

    assert replay_positions == {(1, index) for index in range(2, 10)}
    assert "url" not in bounded[0]["data"]["attachments"][0]
    assert "url" not in bounded[1]["data"]["attachments"][1]
    assert bounded[1]["data"]["attachments"][2]["url"].endswith("img_new_2")
    assert bounded[1]["data"]["attachments"][9]["url"].endswith("img_new_9")


def test_history_native_attachment_replay_preserves_cached_event_provenance() -> None:
    event = CachedEvent(
        seq=7,
        type="user_message",
        data={
            "content": "Historic image",
            "attachments": [
                {
                    "artifact_id": "img_old",
                    "kind": "image",
                    "mime_type": "image/png",
                    "filename": "old.png",
                    "url": "https://cognis.example/artifacts/img_old",
                }
            ],
        },
        source="intaris",
        ts="2026-08-09T18:00:00Z",
    )

    bounded, replay_positions = _limit_history_native_attachment_replay(
        [
            event,
            {
                "type": "user_message",
                "data": {
                    "content": "New image",
                    "attachments": [
                        {
                            "artifact_id": "img_new",
                            "kind": "image",
                            "mime_type": "image/png",
                            "filename": "new.png",
                            "url": "https://cognis.example/artifacts/img_new",
                        }
                    ],
                },
            },
        ]
    )

    assert replay_positions == {(0, 0), (1, 0)}
    assert isinstance(bounded[0], CachedEvent)
    assert bounded[0].seq == 7
    assert bounded[0].source == "intaris"
    assert bounded[0].ts == "2026-08-09T18:00:00Z"
    assert bounded[0].data["attachments"][0]["url"].endswith("img_old")


def test_history_native_attachment_replay_preserves_newest_file_turn() -> None:
    events = [
        {
            "type": "user_message",
            "data": {
                "content": "Old file",
                "attachments": [
                    {
                        "artifact_id": "doc_old",
                        "kind": "pdf",
                        "mime_type": "application/pdf",
                        "filename": "old.pdf",
                        "url": "https://cognis.example/artifacts/doc_old",
                    }
                ],
            },
        },
        {
            "type": "user_message",
            "data": {
                "content": "New file",
                "attachments": [
                    {
                        "artifact_id": "doc_new",
                        "kind": "pdf",
                        "mime_type": "application/pdf",
                        "filename": "new.pdf",
                        "url": "https://cognis.example/artifacts/doc_new",
                    }
                ],
            },
        },
    ]

    bounded, replay_positions = _limit_history_native_attachment_replay(events)

    assert replay_positions == set()
    assert "url" not in bounded[0]["data"]["attachments"][0]
    assert bounded[1]["data"]["attachments"][0]["url"].endswith("doc_new")


@pytest.mark.asyncio
async def test_context_assembler_prioritizes_current_attachments_over_bounded_history() -> None:
    cache = _SessionCache()
    cache.history_events = []
    for index in range(5):
        cache.history_events.extend(
            [
                {
                    "type": "user_message",
                    "data": {
                        "content": f"Image {index}",
                        "attachments": [
                            {
                                "artifact_id": f"img_{index}",
                                "kind": "image",
                                "mime_type": "image/png",
                                "filename": f"image-{index}.png",
                                "size_bytes": 10 * 1024 * 1024,
                                "url": f"https://cognis.example/artifacts/img_{index}",
                            }
                        ],
                    },
                },
                {
                    "type": "assistant_message",
                    "data": {"content": f"Response {index}"},
                },
            ]
        )
    current_url = "https://cognis.example/artifacts/img_current"
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_VisionLLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=20000,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Newest image",
        user_attachments=[
            {
                "artifact_id": "img_current",
                "kind": "image",
                "mime_type": "image/png",
                "filename": "current.png",
                "url": current_url,
            }
        ],
        tool_definitions=[],
    )

    serialized = json.dumps(result.messages)
    assert current_url in serialized
    assert "https://cognis.example/artifacts/img_4" in serialized
    assert "https://cognis.example/artifacts/img_2" in serialized
    assert "https://cognis.example/artifacts/img_1" not in serialized
    assert "artifact_id=img_0" in serialized
    assert "removed from native model input" not in serialized


@pytest.mark.asyncio
async def test_context_assembler_skips_disabled_artifact_ids_from_history() -> None:
    cache = _SessionCache()
    artifact_url = "https://cognis.fpy.cz/api/v1/artifacts/content/documents/doc_1/report.pdf"
    cache.history_events = [
        {
            "type": "user_message",
            "data": {
                "content": "Read this",
                "attachments": [
                    {
                        "artifact_id": "doc_1",
                        "kind": "pdf",
                        "mime_type": "application/pdf",
                        "filename": "report.pdf",
                        "url": artifact_url,
                    }
                ],
            },
        }
    ]
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_FileLLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Read this",
        disabled_artifact_ids={"doc_1"},
        tool_definitions=[],
    )

    serialized = json.dumps(result.messages)
    assert artifact_url not in serialized
    assert "report.pdf" in serialized


@pytest.mark.asyncio
async def test_context_assembler_skips_disabled_artifact_ids_from_current_turn() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_FileLLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    artifact_url = "https://cognis.fpy.cz/api/v1/artifacts/content/documents/doc_1/report.pdf"

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Read this",
        user_attachments=[
            {
                "artifact_id": "doc_1",
                "kind": "pdf",
                "mime_type": "application/pdf",
                "filename": "report.pdf",
                "url": artifact_url,
            }
        ],
        disabled_artifact_ids={"doc_1"},
        tool_definitions=[],
    )

    serialized = json.dumps(result.messages)
    assert artifact_url not in serialized
    assert "report.pdf" in serialized


@pytest.mark.asyncio
async def test_context_assembler_loads_root_project_instructions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Project instructions\nUse pytest.\n")
    (tmp_path / "README.md").write_text("# Readme\nHelpful overview.\n")
    cache = _SessionCache()
    cache.project_contexts[str(tmp_path)] = ProjectContextEntry(
        project_root=str(tmp_path),
        source_path=str(tmp_path / "AGENTS.md"),
        content=build_project_instruction_message(
            project_root=str(tmp_path),
            source_path=str(tmp_path / "AGENTS.md"),
            content="# Project instructions\nUse pytest.",
            working_directory=str(tmp_path),
        ),
        content_hash="hash",
        working_directory=str(tmp_path),
        seq=20,
    )

    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="implement this",
        tool_definitions=[],
        workspace_root=str(tmp_path),
        effective_working_directory=str(tmp_path),
        executor_environment=ExecutorEnvironmentSnapshot(
            available=True,
            executor_id="exec-1",
            executor_type="in_process",
            cwd=str(tmp_path),
            home=str(tmp_path),
        ),
    )

    system_messages = [
        str(message.get("content", ""))
        for message in result.messages
        if message.get("role") == "system"
    ]
    assert any(
        "Instructions for project at" in content and "AGENTS.md" in content
        for content in system_messages
    )
    assert not any("README.md" in content for content in system_messages)
    assert "Instructions for project at" in str(result.messages[1]["content"])
    assert "AGENTS.md" in str(result.messages[1]["content"])
    assert result.cache_breakpoint_index == 1


def test_project_instruction_loader_merges_agent_files_over_nested_readme(tmp_path: Path) -> None:
    intermediate = tmp_path / "src"
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("# Project instructions\nUse pytest.\n")
    (intermediate / "CLAUDE.md").write_text("# Source instructions\nUse type hints.\n")
    (nested / "README.md").write_text("# Feature readme\nNested overview.\n")

    instructions = _load_project_instructions(
        workspace_root=str(tmp_path),
        effective_working_directory=str(nested),
        executor_environment=ExecutorEnvironmentSnapshot(
            available=True,
            executor_id="exec-1",
            executor_type="in_process",
            cwd=str(nested),
            home=str(tmp_path),
        ),
    )

    assert len(instructions) == 2
    assert "AGENTS.md" in instructions[0]
    assert "Use pytest" in instructions[0]
    assert "CLAUDE.md" in instructions[1]
    assert "Use type hints" in instructions[1]
    assert all("Nested overview" not in instruction for instruction in instructions)


def test_project_instruction_loader_marks_truncation(tmp_path: Path) -> None:
    nested = tmp_path / "src"
    nested.mkdir()
    (tmp_path / "AGENTS.md").write_text("A" * 40000)

    instructions = _load_project_instructions(
        workspace_root=str(tmp_path),
        effective_working_directory=str(nested),
        executor_environment=ExecutorEnvironmentSnapshot(
            available=True,
            executor_id="exec-1",
            executor_type="in_process",
            cwd=str(nested),
            home=str(tmp_path),
        ),
    )

    assert len(instructions) == 1
    assert instructions[0].endswith("[truncated]")


@pytest.mark.asyncio
async def test_context_assembler_uses_search_mode_for_follow_up_turns() -> None:
    memory = _Memory()
    cache = _SessionCache()
    cache.prefix_entries = [
        ImmutablePrefixEntry(
            role="system",
            source="identity",
            content="Agent identity",
            seq=1,
        )
    ]
    cache.entry.memory_policy_fingerprint = _default_memory_policy_fingerprint()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    await assembler.assemble(
        session=_session(mnemory_session_id="mem-1"),
        conversation=_conversation(),
        agent=_agent(),
        user_message="follow up",
        tool_definitions=[],
    )

    assert memory.search_modes == ["search"]
    assert memory.identity_calls == []


@pytest.mark.asyncio
async def test_context_assembler_rebuilds_prefix_from_refresh_before_retry_bootstrap() -> None:
    memory = _Memory()
    cache = _SessionCache(cold=True)
    cache.prefix_entries_after_refresh = [
        ImmutablePrefixEntry(
            role="system",
            source="identity",
            content="Recovered identity",
            seq=2,
        ),
        ImmutablePrefixEntry(
            role="developer",
            source="core_memories",
            content="Recovered core memories",
            seq=3,
        ),
    ]
    cache.policy_fingerprint_after_refresh = _default_memory_policy_fingerprint()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(mnemory_session_id="mem-existing"),
        conversation=_conversation(),
        agent=_agent(),
        user_message="retry this step",
        tool_definitions=[],
    )

    assert cache.refresh_calls == 1
    assert memory.identity_calls == []
    assert memory.recall_calls[0]["session_id"] == "mem-existing"
    assert "Recovered identity" in str(result.messages[0]["content"])


@pytest.mark.asyncio
async def test_context_assembler_repairs_existing_mnemory_session_with_fresh_session_when_core_missing() -> (
    None
):
    class _RepairingMemory(_Memory):
        async def load_session_identity(self, **kwargs: object) -> dict[str, object]:
            self.call_order.append("identity")
            self.identity_calls.append(dict(kwargs))
            if kwargs.get("session_id") == "mem-existing":
                return {
                    "session_id": "mem-existing",
                    "instructions": "Use memory tools carefully.",
                    "core_memories": None,
                }
            return {
                "session_id": "mem-repaired",
                "instructions": "Use memory tools carefully.",
                "core_memories": "restored core memories",
            }

        async def recall(self, **kwargs: object) -> dict[str, object]:
            self.call_order.append("recall")
            self.recall_calls.append(dict(kwargs))
            self.search_modes.append(str(kwargs["search_mode"]))
            return {
                "session_id": str(kwargs.get("session_id") or "mem-repaired"),
                "search_results": [{"memory": "Uses pytest", "score": 0.9}],
            }

    memory = _RepairingMemory()
    cache = _SessionCache(cold=True)
    session_manager = _SessionManager()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=session_manager,
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    session = _session(mnemory_session_id="mem-existing")

    result = await assembler.assemble(
        session=session,
        conversation=_conversation(),
        agent=_agent(),
        user_message="retry this step",
        tool_definitions=[],
    )

    assert cache.refresh_calls == 1
    assert memory.identity_calls == [
        {
            "session_id": "mem-existing",
            "labels": {"project": "cognis"},
            "context": "cached intention",
        },
        {"session_id": None, "labels": {"project": "cognis"}, "context": "cached intention"},
    ]
    assert session_manager.attached == [("session-1", "mem-repaired")]
    assert session.mnemory_session_id == "mem-repaired"
    assert "restored core memories" in str(result.messages[0]["content"])


@pytest.mark.asyncio
async def test_context_assembler_adopts_forged_per_turn_mnemory_session_and_marks_repair() -> None:
    class _ForgedRecallMemory(_Memory):
        async def recall(self, **kwargs: object) -> dict[str, object]:
            self.call_order.append("recall")
            self.recall_calls.append(dict(kwargs))
            self.search_modes.append(str(kwargs["search_mode"]))
            return {
                "session_id": "mem-forged",
                "search_results": [{"memory": "Uses pytest", "score": 0.9}],
                "_session_forged": True,
            }

    memory = _ForgedRecallMemory()
    cache = _SessionCache()
    cache.prefix_entries = [
        ImmutablePrefixEntry(role="system", source="identity", content="Agent identity", seq=1)
    ]
    cache.entry.memory_policy_fingerprint = _default_memory_policy_fingerprint()
    session_manager = _SessionManager()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=session_manager,
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    session = _session(mnemory_session_id="mem-existing")

    await assembler.assemble(
        session=session,
        conversation=_conversation(),
        agent=_agent(),
        user_message="follow up",
        tool_definitions=[],
    )

    assert session_manager.attached == [("session-1", "mem-forged")]
    assert session.mnemory_session_id == "mem-forged"
    assert cache.mark_prefix_repair_calls == 1


@pytest.mark.asyncio
async def test_context_assembler_rebuilds_mnemory_session_after_rotation_with_cached_prefix() -> (
    None
):
    memory = _Memory()
    cache = _SessionCache()
    cache.prefix_entries = [
        ImmutablePrefixEntry(role="system", source="identity", content="Stale identity", seq=1),
        ImmutablePrefixEntry(
            role="developer",
            source="memory_instructions",
            content="Stale memory instructions",
            seq=2,
        ),
        ImmutablePrefixEntry(
            role="developer", source="core_memories", content="stale core memories", seq=3
        ),
    ]
    session_manager = _SessionManager()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=session_manager,
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    session = _session(mnemory_session_id=None)

    result = await assembler.assemble(
        session=session,
        conversation=_conversation(),
        agent=_agent(),
        user_message="after compaction turn",
        tool_definitions=[],
    )

    assert memory.identity_calls == [
        {"session_id": None, "labels": {"project": "cognis"}, "context": "cached intention"}
    ]
    assert memory.recall_calls[0]["session_id"] == "mem-1"
    assert session_manager.attached == [("session-1", "mem-1")]
    assert session.mnemory_session_id == "mem-1"
    assert "prefers Python" in str(result.messages[0]["content"])
    assert "stale core memories" not in str(result.messages[0]["content"])


@pytest.mark.asyncio
async def test_context_assembler_degrades_on_mnemory_failure() -> None:
    """Mnemory recall failure must degrade gracefully, not abort the turn.

    The result should mark the assembly as degraded with source "memory",
    include a visible system notice, and contain no recalled-memory block.
    """
    assembler = ContextAssembler(
        memory=_Memory(fail=True),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="should degrade",
        tool_definitions=[],
    )

    assert result.degraded is True
    assert "memory" in result.degraded_sources
    assert result.system_notices
    assert any("recall" in n.lower() or "memory" in n.lower() for n in result.system_notices)
    # No mutable recalled-memories block (search results) should appear.
    # The immutable prefix may still contain core_memories from the identity
    # bootstrap, but the per-turn search-results block must be absent.
    mutable_search_block_present = any(
        isinstance(m.get("content"), str)
        and "Recalled memories:" in m["content"]
        and "memory_context" in m["content"]
        for m in result.messages
    )
    assert not mutable_search_block_present


@pytest.mark.asyncio
async def test_intaris_status_overlaps_identity_but_recall_uses_adopted_memory_session() -> None:
    status_started = asyncio.Event()
    identity_finished = asyncio.Event()

    class Memory(_Memory):
        async def load_session_identity(self, **kwargs: object) -> dict[str, object]:
            await status_started.wait()
            result = await super().load_session_identity(**kwargs)
            identity_finished.set()
            return result

        async def recall(self, **kwargs: object) -> dict[str, object]:
            assert identity_finished.is_set()
            assert kwargs["session_id"] == "mem-1"
            return await super().recall(**kwargs)

    class Guardrails(_Guardrails):
        async def get_session(self, session_id: str) -> object:
            status_started.set()
            return await super().get_session(session_id)

    memory = Memory()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(cold=True),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    result = await asyncio.wait_for(
        assembler.assemble(
            session=_session(),
            conversation=_conversation(),
            agent=_agent(),
            user_message="hello",
            tool_definitions=[],
        ),
        3,
    )
    assert memory.recall_calls
    assert "prefers Python" in str(result.messages[0]["content"])


@pytest.mark.asyncio
async def test_cancelled_context_preparation_joins_prefix_and_status_reads() -> None:
    identity_started, status_started = asyncio.Event(), asyncio.Event()
    identity_stopped, status_stopped = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()

    class Memory(_Memory):
        async def load_session_identity(self, **kwargs: object) -> dict[str, object]:
            identity_started.set()
            try:
                await release.wait()
                return {}
            finally:
                identity_stopped.set()

    class Guardrails(_Guardrails):
        async def get_session(self, session_id: str) -> object:
            status_started.set()
            try:
                await release.wait()
                return None
            finally:
                status_stopped.set()

    assembler = ContextAssembler(
        memory=Memory(),
        guardrails=Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(cold=True),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    pending = asyncio.create_task(
        assembler.assemble(
            session=_session(),
            conversation=_conversation(),
            agent=_agent(),
            user_message="hello",
            tool_definitions=[],
        )
    )
    await asyncio.wait_for(asyncio.gather(identity_started.wait(), status_started.wait()), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert identity_stopped.is_set()
    assert status_stopped.is_set()


@pytest.mark.asyncio
async def test_optional_recall_timeout_does_not_limit_core_identity_bootstrap() -> None:
    class _SlowOptionalRecallMemory(_Memory):
        def __init__(self) -> None:
            super().__init__()
            self.recall_cancelled = asyncio.Event()

        async def load_session_identity(self, **kwargs: object) -> dict[str, object]:
            await asyncio.sleep(0.05)
            return await super().load_session_identity(**kwargs)

        async def recall(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            try:
                await asyncio.sleep(60)
            finally:
                self.recall_cancelled.set()
            return {"search_results": []}

    memory = _SlowOptionalRecallMemory()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(cold=True),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
        optional_recall_timeout_seconds=0.01,
    )

    result = await assembler.assemble(
        session=_session(mnemory_session_id=None),
        conversation=_conversation(),
        agent=_agent(),
        user_message="bounded optional recall",
        tool_definitions=[],
    )

    assert memory.identity_calls
    assert memory.recall_cancelled.is_set()
    assert result.degraded is True
    assert result.system_notices == [
        "Optional memory recall timed out for this turn; continuing without recalled memories."
    ]
    assert "prefers Python" in str(result.messages[0]["content"])


@pytest.mark.asyncio
async def test_context_assembler_audits_recalled_memories_as_replayable_developer_context() -> None:
    class _MemoryWithRecord(_Memory):
        async def recall(self, **kwargs: object) -> dict[str, object]:
            result = await super().recall(**kwargs)
            result["search_results"] = [
                {
                    "id": "01234567-89ab-4def-8123-456789abcdef",
                    "memory": "Uses pytest",
                    "score": 0.9,
                    "metadata": {},
                }
            ]
            return result

    assembler = ContextAssembler(
        memory=_MemoryWithRecord(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="remembered context?",
        tool_definitions=[],
    )

    audit = next(item for item in result.audit_messages if item["source"] == "memory_search")
    assert audit["role"] == "developer"
    assert "Uses pytest" in audit["content"]
    assert audit["metadata"] == {
        "context_injection": True,
        "replayable": True,
        "replay_scope": "same_session",
        "visibility": "agent_context",
        "model_role": "system",
        "trust": "untrusted",
        "memory_aliases": {
            "version": 1,
            "bindings": [
                {
                    "alias": "m1",
                    "memory_id": "01234567-89ab-4def-8123-456789abcdef",
                    "revision": audit["metadata"]["memory_aliases"]["bindings"][0]["revision"],
                }
            ],
            "invalidated": [],
        },
    }
    assert "[m1]" in audit["content"]


def test_events_to_messages_replays_only_marked_developer_context_injections() -> None:
    events = [
        {
            "type": "developer_message",
            "data": {
                "role": "developer",
                "source": "memory_search",
                "content": '<memory_context trust="untrusted">\nRecalled memories:\n- Uses pytest\n</memory_context>',
                "context_injection": True,
                "replayable": True,
                "visibility": "agent_context",
                "model_role": "system",
            },
        },
        {
            "type": "developer_message",
            "data": {
                "role": "developer",
                "source": "routing_reminder",
                "content": "Do not replay ordinary audit messages.",
            },
        },
    ]

    messages = events_to_messages(events)

    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert (
        messages[0]["content"]
        == '<memory_context trust="untrusted">\nRecalled memories:\n- Uses pytest\n</memory_context>'
    )


def test_events_to_messages_preserves_accepted_tool_json_and_result_metadata() -> None:
    accepted_arguments = '{"path": "café",  "line": 3}'
    events = [
        {
            "type": "tool_call",
            "data": {
                "name": "read",
                "call_id": "call-accepted-json",
                "arguments": {"path": "café", "line": 3},
                "provider_arguments_json": accepted_arguments,
            },
        },
        {
            "type": "tool_result",
            "data": {
                "name": "read",
                "call_id": "call-accepted-json",
                "result": "read failed",
                "is_error": True,
                "recovery_call_id": "recovery-1",
                "has_full_output": True,
            },
        },
    ]

    messages = events_to_messages(events)

    assert messages[0]["tool_calls"][0]["function"]["arguments"] == accepted_arguments
    assert messages[1]["_tool_is_error"] is True
    assert messages[1]["_recovery_call_id"] == "recovery-1"
    assert messages[1]["_has_full_output"] is True


def test_event_omission_replay_removes_synthetic_orphan_with_owning_tool_call() -> None:
    messages = events_to_messages(
        [
            {
                "seq": 41,
                "type": "tool_call",
                "data": {
                    "name": "read",
                    "call_id": "call-interrupted",
                    "arguments": {"path": "large.txt"},
                },
            },
            {
                "seq": 42,
                "type": "assistant_message",
                "data": {"content": "The turn resumed after interruption."},
            },
        ],
        projection_decisions={
            "event:41": [
                {
                    "operation": "omit_event",
                    "rendering_policy_version": 1,
                    "parameters": {"event_seq": 41},
                }
            ]
        },
    )

    assert not any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call-interrupted"
        for message in messages
    )
    assert not any(message.get("tool_calls") for message in messages)
    assert any(
        message.get("content") == "The turn resumed after interruption." for message in messages
    )


@pytest.mark.asyncio
async def test_projection_delta_is_durable_and_replayed_without_a_second_write() -> None:
    events = [
        {
            "seq": 11,
            "type": "tool_call",
            "data": {
                "name": "read",
                "call_id": "call-projected",
                "arguments": {"path": "large.txt"},
            },
        },
        {
            "seq": 12,
            "type": "tool_result",
            "data": {
                "name": "read",
                "call_id": "call-projected",
                "result": "large result",
                "is_error": False,
                "has_full_output": True,
                "recovery_call_id": "recovery-projected",
            },
        },
    ]
    cache = _HistorySessionCache(events)
    guardrails = _Guardrails()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    decision = {
        "call-projected": [
            {
                "operation": "compact_result",
                "rendering_policy_version": 1,
                "parameters": {},
            }
        ]
    }

    await assembler._persist_projection_decisions(
        session=_session(),
        agent=_agent(),
        decisions=decision,
    )
    stored_events = guardrails.record_event_calls[0]["events"]
    assert len(stored_events) == 1
    assert stored_events[0].type == "lifecycle"
    assert stored_events[0].data["event"] == "context_projection"
    assert cache.get_projection_decisions(_session().session_id) == decision

    replayed = await assembler._events_to_messages(
        cache.get_events_since_compaction(_session().session_id),
        model_info=ModelInfo(model_id="test-model", context_window=200_000, max_output_tokens=256),
        owner_email="user@example.com",
        projection_decisions=cache.get_projection_decisions(_session().session_id),
    )
    result_message = next(message for message in replayed if message.get("role") == "tool")
    assert result_message["content"] != "large result"
    assert result_message["_projected_compacted"] is True
    import httpx

    from cognis.providers.llm.message_projection import project_messages_for_provider
    from cognis.providers.llm.responses_bridge import messages_to_responses_input

    anthropic_request = project_messages_for_provider(
        replayed,
        provider=type("Provider", (), {"config": {"preset": "anthropic"}})(),
        llm_api="messages",
    )
    responses_request = project_messages_for_provider(
        replayed,
        provider=None,
        llm_api="responses",
    )
    assert (
        next(message for message in responses_request.messages if message["role"] == "tool")[
            "content"
        ]
        != "large result"
    )
    responses_input = messages_to_responses_input(responses_request.messages)
    assert anthropic_request.messages
    anthropic_request_bytes = len(
        httpx.Request(
            "POST",
            "http://provider.invalid/messages",
            json={"messages": anthropic_request.messages},
        ).content
    )
    responses_request_bytes = len(
        httpx.Request(
            "POST",
            "http://provider.invalid/responses",
            json={"input": responses_input},
        ).content
    )
    assert anthropic_request_bytes < 64 * 1024
    assert responses_request_bytes < 64 * 1024

    new_decision = assembler._new_projection_decisions(
        decision,
        cache.get_projection_decisions(_session().session_id),
    )
    await assembler._persist_projection_decisions(
        session=_session(),
        agent=_agent(),
        decisions=new_decision,
    )
    assert len(guardrails.record_event_calls) == 1


@pytest.mark.asyncio
async def test_projection_decision_event_stays_within_encoded_request_bounds() -> None:
    import httpx

    cache = _HistorySessionCache([])
    guardrails = _Guardrails()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    decisions = {
        f"call-{index:04d}": [
            {
                "operation": "clear_arguments",
                "rendering_policy_version": 1,
                "parameters": {"threshold": 24_000},
            }
        ]
        for index in range(150)
    }

    await assembler._persist_projection_decisions(
        session=_session(),
        agent=_agent(),
        decisions=decisions,
    )

    assert [len(call["events"]) for call in guardrails.record_event_calls] == [1]
    assert len(guardrails.record_event_calls[0]["events"][0].data["decisions"]) == 150
    request_sizes = [
        len(
            httpx.Request(
                "POST",
                "http://intaris.invalid/events",
                json=[event.model_dump() for event in call["events"]],
            ).content
        )
        for call in guardrails.record_event_calls
    ]
    assert max(request_sizes) < 256 * 1024


@pytest.mark.asyncio
async def test_large_projection_delta_uses_bounded_independently_replayable_batches() -> None:
    cache = _HistorySessionCache([])
    guardrails = _Guardrails()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    decisions = {
        f"{'c' * 240}{index:04d}": [
            {
                "operation": "clear_arguments",
                "rendering_policy_version": 1,
                "parameters": {"threshold": 24_000},
            }
        ]
        for index in range(1_000)
    }

    await assembler._persist_projection_decisions(
        session=_session(),
        agent=_agent(),
        decisions=decisions,
    )
    assert len(guardrails.record_event_calls) > 1
    recorded = []
    for call in guardrails.record_event_calls:
        assert len(call["events"]) == 1
        event = call["events"][0]
        assert event.data["event"] == "context_projection"
        assert len(json.dumps([event.model_dump()], ensure_ascii=False).encode()) < 256 * 1024
        recorded.extend(event.data["decisions"])
    assert len(recorded) == len(decisions)
    assert {item["call_id"] for item in recorded} == set(decisions)


@pytest.mark.asyncio
async def test_pressured_detached_handoff_snapshot_never_persists_projection() -> None:
    cache = _SessionCache()
    cache.prefix_entries = [
        ImmutablePrefixEntry(role="system", source="identity", content="Agent identity", seq=1)
    ]
    for index in range(40):
        call_id = f"call_snapshot_read_{index}"
        cache.history_events.extend(
            [
                {
                    "seq": 2 * index + 2,
                    "type": "tool_call",
                    "data": {
                        "call_id": call_id,
                        "name": "read",
                        "arguments": {"path": "/repo"},
                    },
                },
                {
                    "seq": 2 * index + 3,
                    "type": "tool_result",
                    "data": {
                        "call_id": call_id,
                        "name": "read",
                        "result": "evidence " * 2500,
                        "recovery_call_id": call_id,
                    },
                },
            ]
        )
    guardrails = _Guardrails()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    before_events = deepcopy(cache.history_events)
    result = await assembler.assemble_snapshot(
        snapshot=cache.get_context_snapshot(_session().session_id),
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        instruction="Summarize this history for a handoff.",
        tool_schema_tokens=0,
    )
    assert result.projection_compacted_tool_group_anchors
    assert not guardrails.record_event_calls
    assert cache.history_events == before_events


@pytest.mark.asyncio
async def test_projection_event_retry_reuses_idempotency_key_after_append_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _HistorySessionCache([])
    guardrails = _Guardrails()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    keys: list[str] = []
    attempt = 0

    async def record_events(**kwargs: object) -> object:
        nonlocal attempt
        attempt += 1
        keys.append(str(kwargs["idempotency_key"]))
        if attempt == 1:
            raise RuntimeError("temporary Intaris failure")
        return SimpleNamespace(ok=True, first_seq=3, last_seq=3)

    monkeypatch.setattr(guardrails, "record_events", record_events)
    decisions = {
        "call-retry": [
            {
                "operation": "compact_result",
                "rendering_policy_version": 1,
                "parameters": {},
            }
        ]
    }

    with pytest.raises(RuntimeError, match="temporary Intaris failure"):
        await assembler._persist_projection_decisions(
            session=_session(),
            agent=_agent(),
            decisions=decisions,
        )
    assert cache.entry is None

    await assembler._persist_projection_decisions(
        session=_session(),
        agent=_agent(),
        decisions=decisions,
    )

    assert len(keys) == 2
    assert keys[0] == keys[1]


def test_events_to_messages_replays_managed_policy_as_developer_once() -> None:
    policy = {
        "type": "developer_message",
        "data": {
            "role": "developer",
            "source": "managed_channel_policy",
            "content": "Immutable managed-channel policy.",
            "context_injection": True,
            "replayable": True,
            "replay_scope": "conversation",
            "visibility": "agent_context",
            "model_role": "developer",
            "trust": "trusted",
            "context_key": "managed_channel_policy:conversation-1",
        },
    }

    messages = events_to_messages([policy])

    assert len(messages) == 1
    assert messages[0]["role"] == "developer"
    assert messages[0]["content"] == "Immutable managed-channel policy."
    assert messages[0]["_audit_metadata"]["replay_scope"] == "conversation"


def test_rotated_prefix_preserves_managed_policy_replay_metadata() -> None:
    events = build_prefix_message_events(
        [
            ImmutablePrefixEntry(
                source="managed_channel_policy",
                role="developer",
                content="Immutable managed-channel policy.",
            )
        ]
    )

    assert len(events) == 1
    assert events[0].data["context_injection"] is True
    assert events[0].data["replay_scope"] == "conversation"
    assert events[0].data["visibility"] == "agent_context"
    assert events[0].data["model_role"] == "developer"
    replayed = events_to_messages([events[0]])
    assert len(replayed) == 1
    assert replayed[0]["role"] == "developer"
    assert replayed[0]["content"] == "Immutable managed-channel policy."


@pytest.mark.asyncio
async def test_managed_policy_is_visible_first_reload_and_rotated_prefix() -> None:
    policy_event = SessionEvent(
        type="developer_message",
        data={
            "role": "developer",
            "source": "managed_channel_policy",
            "content": "Immutable managed-channel policy.",
            "context_injection": True,
            "replayable": True,
            "replay_scope": "conversation",
            "visibility": "agent_context",
            "model_role": "developer",
            "trust": "trusted",
            "context_key": "managed_channel_policy:conversation-1",
        },
    )

    async def assemble(cache: _SessionCache) -> list[dict[str, object]]:
        assembler = ContextAssembler(
            memory=_Memory(),
            guardrails=_Guardrails(),
            llm=_LLM(),
            session_cache=cache,
            session_manager=_SessionManager(),
            max_context_tokens=4096,
            compaction_threshold=0.85,
        )
        return (
            await assembler.assemble(
                session=_session(),
                conversation=_conversation(),
                agent=_agent(),
                user_message="Continue",
                tool_definitions=[],
            )
        ).messages

    first_cache = _SessionCache()
    first_cache.history_events = [policy_event]
    first = await assemble(first_cache)

    reload_cache = _SessionCache(cold=True)
    reload_cache.history_events = [policy_event]
    reloaded = await assemble(reload_cache)

    rotated_cache = _SessionCache()
    rotated_cache.prefix_entries = [
        ImmutablePrefixEntry(
            source="managed_channel_policy",
            role="developer",
            content="Immutable managed-channel policy.",
            seq=7,
        )
    ]
    rotated_cache.history_events = [
        {
            "seq": 7,
            "type": policy_event.type,
            "data": dict(policy_event.data),
        }
    ]
    rotated = await assemble(rotated_cache)

    for messages in (first, reloaded, rotated):
        policy_messages = [
            message
            for message in messages
            if "Immutable managed-channel policy." in str(message.get("content", ""))
        ]
        assert len(policy_messages) == 1, messages
        assert policy_messages[0]["role"] in {"developer", "system"}


def test_events_to_messages_uses_agent_context_for_visible_system_notice() -> None:
    messages = events_to_messages(
        [
            {
                "type": "lifecycle",
                "data": {
                    "event": "system_notice",
                    "kind": "agent_profile_changed",
                    "notice_id": "agent-profile-switch:turn-1:call-1",
                    "message": "Agent profile changed: developer → senior.",
                    "agent_context": (
                        "<agent_profile_change>\nActive profile: senior\n</agent_profile_change>"
                    ),
                },
            }
        ]
    )

    assert messages == [
        {
            "role": "system",
            "content": ("<agent_profile_change>\nActive profile: senior\n</agent_profile_change>"),
        }
    ]


@pytest.mark.asyncio
async def test_reasoning_only_profile_switch_preserves_immutable_prefix() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        compaction_threshold=0.85,
    )
    agent = _agent().model_copy(
        update={
            "agent_profiles": {
                "junior": AgentRuntimeProfile(
                    profile_id="junior",
                    description="Bounded routine work.",
                    model="test-model",
                    reasoning_effort="low",
                    agent_switchable=True,
                ),
                "senior": AgentRuntimeProfile(
                    profile_id="senior",
                    description="Complex high-risk work.",
                    model="test-model",
                    reasoning_effort="high",
                    agent_switchable=True,
                ),
            },
            "default_agent_profile_id": "junior",
        }
    )
    session = _session()
    conversation = _conversation()
    session.agent_profile_id = "junior"
    conversation.agent_profile_id = "junior"

    junior = await assembler.assemble(
        session=session,
        conversation=conversation,
        agent=agent,
        user_message="Implement the change.",
        tool_definitions=[],
    )
    session.agent_profile_id = "senior"
    conversation.agent_profile_id = "senior"
    senior = await assembler.assemble(
        session=session,
        conversation=conversation,
        agent=agent,
        user_message="Implement the change.",
        tool_definitions=[],
    )
    assembler.session_cache.history_events = [
        {
            "seq": index,
            "type": kind,
            "data": {
                "turn_id": "profile-turn",
                "call_id": "profile-call",
                "kind": "agent_profile_changed",
                "name": "switch_agent_profile",
                "content": "Implement the change.",
                "result": "switched",
            },
        }
        for index, kind in enumerate(("user_message", "tool_call", "tool_result", "lifecycle"), 1)
    ]
    memoryless = await assembler.assemble(
        session=session,
        conversation=conversation,
        agent=agent,
        user_message="Implement the change.",
        tool_definitions=[],
        skip_memory=True,
        skip_user_message=True,
        required_turn_id="profile-turn",
        require_user_event=True,
        attachment_notice="Attachment format required extraction.",
        attachment_context="Extracted attachment content.",
        routing_reminder="Continue the existing request under the new profile.",
    )

    assert junior.cache_breakpoint_index == senior.cache_breakpoint_index
    assert junior.cache_breakpoint_index is not None
    breakpoint = junior.cache_breakpoint_index
    assert junior.messages[: breakpoint + 1] == senior.messages[: breakpoint + 1]
    junior_suffix = junior.messages[breakpoint + 1 :]
    senior_suffix = senior.messages[breakpoint + 1 :]
    assert any("Profile: junior" in str(message.get("content")) for message in junior_suffix)
    assert any("Profile: senior" in str(message.get("content")) for message in senior_suffix)
    assert any(
        "senior (current): Complex high-risk work." in str(message.get("content"))
        for message in memoryless.messages
    )
    assert any(
        message.get("role") == "user"
        and "Extracted attachment content." in str(message.get("content"))
        and str(message.get("content")).endswith("</message>")
        for message in memoryless.messages
    )
    assert any(
        message.get("content") == "Attachment format required extraction."
        for message in memoryless.messages
    )
    assert any(
        message.get("_audit_source") == "routing_reminder"
        and str(message.get("content")).endswith(
            "Continue the existing request under the new profile."
        )
        and message.get(AUDIT_METADATA, {}).get("replayable") is True
        for message in memoryless.messages
    )
    assert not any(
        message.get("role") == "user" and message.get("content") == "Implement the change."
        for message in memoryless.messages
    )


@pytest.mark.asyncio
async def test_context_assembler_does_not_prune_recalled_memories_first() -> None:
    class _TinyLLM(_LLM):
        def count_messages_tokens(self, messages: list[dict[str, object]], model: str) -> int:
            del model
            return sum(len(str(message.get("content", ""))) for message in messages)

    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_TinyLLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=900,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="x" * 1200,
        tool_definitions=[],
    )

    assert any(
        isinstance(message.get("content"), str)
        and "Recalled memories:" in message["content"]
        and "Uses pytest" in message["content"]
        for message in result.messages
    )


@pytest.mark.asyncio
async def test_context_assembler_fails_before_search_when_bootstrap_identity_lacks_core() -> None:
    class _MissingCoreMemory(_Memory):
        async def load_session_identity(self, **kwargs: object) -> dict[str, object]:
            self.call_order.append("identity")
            self.identity_calls.append(dict(kwargs))
            return {
                "session_id": "mem-1",
                "instructions": "Use memory tools carefully.",
                "core_memories": None,
            }

    memory = _MissingCoreMemory()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    with pytest.raises(ImmutablePrefixUnavailable, match="Core memories are unavailable"):
        await assembler.assemble(
            session=_session(),
            conversation=_conversation(),
            agent=_agent(),
            user_message="should fail",
            tool_definitions=[],
        )

    assert memory.call_order == ["identity"]
    assert memory.recall_calls == []


@pytest.mark.asyncio
async def test_context_assembler_raises_on_cold_cache_event_failure() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(fail_refresh=True, cold=True),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    with pytest.raises(RuntimeError, match="event read failed"):
        await assembler.assemble(
            session=_session(),
            conversation=_conversation(),
            agent=_agent(),
            user_message="cold failure",
            tool_definitions=[],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_memory", [False, True])
async def test_known_stale_cache_cannot_mask_refresh_failure(skip_memory: bool) -> None:
    cache = _SessionCache(fail_refresh=True, cold=False)
    cache.entry.canonical_stale = True
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    kwargs = dict(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="continue",
        tool_definitions=[],
        skip_memory=skip_memory,
    )
    with pytest.raises(RuntimeError):
        await assembler.assemble(**kwargs)
    # A later successful canonical refresh can restore execution.
    cache.fail_refresh = False
    cache.entry.canonical_stale = False
    result = await assembler.assemble(**kwargs)
    assert "events" not in result.degraded_sources


@pytest.mark.asyncio
async def test_context_assembler_skip_memory_degrades_on_warm_cache_event_failure() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(fail_refresh=True, cold=False),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="warm failure",
        tool_definitions=[],
        skip_memory=True,
    )

    assert "events" in result.degraded_sources
    assert result.messages[-1]["content"].endswith(">warm failure</message>")


@pytest.mark.asyncio
async def test_context_assembler_accounts_for_tool_schema_budget() -> None:
    class _SmallWindowLLM(_LLM):
        async def get_model_info(self, model_id: str) -> ModelInfo:
            del model_id
            return ModelInfo(model_id="test-model", context_window=400, max_output_tokens=128)

    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_SmallWindowLLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=400,
        compaction_threshold=0.85,
    )
    large_tool = ToolDefinition(
        name="filesystem/read_file",
        description="Read file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "x" * 800}},
        },
        source=ToolSource(type="builtin"),
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="please help",
        tool_definitions=[large_tool],
    )

    # Managed recalled memories are already part of Mnemory's session-level
    # dedupe state, so they must not be silently pruned after recall.
    assert any(
        'trust="untrusted"' in str(message["content"])
        and "Recalled memories:" in str(message["content"])
        for message in result.messages
    )


def test_build_environment_info_contains_required_fields() -> None:
    """Environment info must include stable executor context and day-granularity date."""
    info = _build_environment_info()
    assert "Home directory:" in info
    assert "Working directory:" in info
    assert "Date:" in info
    assert "Git repository:" in info
    assert "Platform:" in info
    assert "Hostname:" in info
    assert "System user:" in info
    assert "get_current_datetime" in info
    assert "Assigned executors" not in info
    # Must contain the actual home directory, not a generic placeholder
    assert str(Path.home()) in info
    # Must instruct the LLM about ~ expansion
    assert "~" in info


def test_build_environment_info_uses_executor_snapshot() -> None:
    info = _build_environment_info(
        ExecutorEnvironmentSnapshot(
            available=True,
            executor_id="exec-1",
            executor_type="websocket",
            user="remote-user",
            home="/remote/home",
            cwd="/remote/work",
            hostname="remote-host",
            platform_os="linux",
            platform_arch="x86_64",
        )
    )
    assert "exec-1" in info
    assert "/remote/home" in info
    assert "/remote/work" in info
    assert "tools default to the effective working directory" in info


def test_build_environment_info_marks_unavailable_remote_environment() -> None:
    info = _build_environment_info(
        ExecutorEnvironmentSnapshot(
            available=False,
            executor_id="exec-2",
            executor_type="websocket",
        )
    )
    assert "exec-2" in info
    assert "unavailable" in info
    assert "Do not guess controller paths" in info
    assert "get_current_datetime" in info


@pytest.mark.asyncio
async def test_context_assembler_appends_executor_pool_tail_reminder() -> None:
    pool = ExecutorPool(
        primary=[
            ResolvedExecutorTarget(
                executor_id="exec-primary",
                executor_type="websocket",
                is_primary=True,
                selection_source="explicit",
                description=None,
                state=ExecutorAvailability.USABLE,
            )
        ],
        additional=[
            ResolvedExecutorTarget(
                executor_id="raspi-camera",
                executor_type="websocket",
                is_primary=False,
                selection_source="additional_explicit",
                description="camera host",
                state=ExecutorAvailability.USABLE,
            )
        ],
    )

    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="hello",
        tool_definitions=[],
        executor_environment=ExecutorEnvironmentSnapshot(
            available=True, executor_id="exec-primary"
        ),
        executor_pool=pool,
        active_executor_id="exec-primary",
    )

    environment_message = next(
        str(message.get("content", ""))
        for message in result.messages
        if str(message.get("content", "")).startswith("Environment:")
    )
    assert "Assigned executors" not in environment_message
    contents = [str(message.get("content", "")) for message in result.messages]
    reminder = next(content for content in contents if "Assigned executors" in content)
    assert "Use primary executors for normal work" in reminder
    assert "not fallback capacity" in reminder


@pytest.mark.asyncio
async def test_context_assembler_includes_environment_info() -> None:
    """Assembled context must contain an environment info system message."""
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="hello",
        tool_definitions=[],
        executor_environment=ExecutorEnvironmentSnapshot(
            available=True,
            executor_id="exec-1",
            executor_type="websocket",
            user="remote-user",
            home="/remote/home",
            cwd="/remote/cwd",
            hostname="remote-host",
            platform_os="linux",
            platform_arch="arm64",
        ),
    )

    env_messages = [
        m
        for m in result.messages
        if m.get("role") == "system" and "Home directory:" in str(m.get("content", ""))
    ]
    assert len(env_messages) == 1, "Expected exactly one environment info message"
    content = env_messages[0]["content"]
    assert "/remote/home" in content
    assert "/remote/cwd" in content
    assert "linux" in content


@pytest.mark.asyncio
async def test_context_assembler_refreshes_environment_between_turns() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    first = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="hello",
        tool_definitions=[],
        executor_environment=ExecutorEnvironmentSnapshot(
            available=True,
            executor_id="exec-a",
            executor_type="websocket",
            home="/home/a",
            cwd="/cwd/a",
        ),
    )
    second = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="hello",
        tool_definitions=[],
        executor_environment=ExecutorEnvironmentSnapshot(
            available=True,
            executor_id="exec-b",
            executor_type="websocket",
            home="/home/b",
            cwd="/cwd/b",
        ),
    )

    first_env = [
        m
        for m in first.messages
        if m.get("role") == "system" and "Home directory:" in str(m.get("content", ""))
    ][0]
    second_env = [
        m
        for m in second.messages
        if m.get("role") == "system" and "Home directory:" in str(m.get("content", ""))
    ][0]
    assert "/home/a" in str(first_env["content"])
    assert "/home/b" in str(second_env["content"])


@pytest.mark.asyncio
async def test_context_assembler_includes_artifact_ids_with_native_image_blocks() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_VisionLLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Please edit this image",
        user_attachments=[
            {
                "artifact_id": "att_1",
                "kind": "image",
                "mime_type": "image/png",
                "filename": "photo.png",
                "size_bytes": 123,
                "url": "https://example.test/photo.png",
            }
        ],
        tool_definitions=[],
    )

    user_messages = [message for message in result.messages if message.get("role") == "user"]
    current_turn = user_messages[-1]
    assert isinstance(current_turn["content"], list)
    assert current_turn["content"][0]["type"] == "text"
    assert "artifact_id=att_1" in current_turn["content"][0]["text"]
    assert current_turn["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_context_assembler_does_not_project_svg_as_native_image() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_VisionLLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Please inspect this diagram",
        user_attachments=[
            {
                "artifact_id": "att_svg",
                "kind": "image",
                "mime_type": "image/svg+xml",
                "filename": "diagram.svg",
                "size_bytes": 123,
                "url": "https://example.test/diagram.svg",
            }
        ],
        tool_definitions=[],
    )

    user_messages = [message for message in result.messages if message.get("role") == "user"]
    current_turn = user_messages[-1]
    assert isinstance(current_turn["content"], str)
    assert "artifact_id=att_svg" in current_turn["content"]
    assert "diagram.svg" in current_turn["content"]
    assert "image_url" not in current_turn["content"]


@pytest.mark.asyncio
async def test_context_assembler_keeps_native_attachments_on_user_role_for_system_turns() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_VisionLLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Continue the interrupted turn.",
        user_message_role="system",
        user_attachments=[
            {
                "artifact_id": "att_1",
                "kind": "image",
                "mime_type": "image/png",
                "filename": "photo.png",
                "size_bytes": 123,
                "url": "https://example.test/photo.png",
            }
        ],
        tool_definitions=[],
    )

    system_messages = [
        message
        for message in result.messages
        if message.get("role") == "system"
        and message.get("content") == "Continue the interrupted turn."
    ]
    assert system_messages

    attachment_messages = [
        message
        for message in result.messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
    ]
    assert attachment_messages
    current_turn = attachment_messages[-1]
    assert current_turn["content"][0]["type"] == "text"
    assert "artifact_id=att_1" in current_turn["content"][0]["text"]
    assert current_turn["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_context_assembler_includes_composed_identity_prompt() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent_with_personality(),
        user_message="hello",
        tool_definitions=[],
    )

    first_message = result.messages[0]
    assert first_message["role"] == "system"
    content = str(first_message["content"])
    assert "<identity>" in content
    assert "Purpose: research specialist" in content
    assert "Tone: formal, precise" in content
    assert "Temperament: patient, methodical" in content
    assert "- Always cite sources" in content
    assert "Be helpful." in content
    assert "<instructions>" in content
    assert "## Behavior" in content
    assert result.cache_breakpoint_index == 0


@pytest.mark.asyncio
async def test_immutable_prefix_idempotency_key_includes_counter_and_content_hash() -> None:
    cache = _SessionCache()
    assert cache.entry is not None
    cache.entry.last_event_seq = 42
    guardrails = _Guardrails()
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=guardrails,
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="hello",
        tool_definitions=[],
    )

    keys = [
        str(call.get("idempotency_key"))
        for call in guardrails.record_event_calls
        if ":immutable_prefix:" in str(call.get("idempotency_key"))
    ]
    assert any(":immutable_prefix:bootstrap:42:" in key for key in keys)
    suffix_parts = [key.split(":")[-4:] for key in keys]
    assert all(parts[0] == "bootstrap" for parts in suffix_parts)
    assert all(parts[1] == "42" for parts in suffix_parts)
    assert all(
        len(parts[2]) == 16 and all(char in "0123456789abcdef" for char in parts[2])
        for parts in suffix_parts
    )
    assert {parts[3] for parts in suffix_parts} == {"messages", "snapshot"}


@pytest.mark.asyncio
async def test_context_assembler_consolidates_immutable_prefix_into_first_message() -> None:
    class _MemoryWithInstructions:
        async def load_session_identity(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            return {
                "session_id": "mem-1",
                "instructions": "Use remember tool to store durable facts.",
                "core_memories": "Prefers Python and pytest.",
            }

        async def recall(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            return {
                "session_id": "mem-1",
                "search_results": [{"memory": "Mutable recalled memory", "score": 0.9}],
            }

    assembler = ContextAssembler(
        memory=_MemoryWithInstructions(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent_with_skills(),
        user_message="hello",
        tool_definitions=[],
        prompt_context=PromptContext.CHAT,
    )

    first_message = result.messages[0]
    assert first_message["role"] == "system"
    content = str(first_message["content"])
    assert "<identity>" in content
    assert "You are helpful." in content
    assert "<instructions>" in content
    assert "## Behavior" in content
    assert "<memory_instructions>" in content
    assert "Use remember tool to store durable facts." in content
    assert '<memory_context trust="untrusted">' in content
    assert "Prefers Python and pytest." in content
    assert "<available_skills>" in content
    assert "Release Helper" in content
    assert "<skills_guidance>" in content
    assert "skill_write" not in content
    assert "<critical_rules>" in content
    assert "managed only through Cognis skill tools" in content
    assert "This is a continuation from a previous session." in content
    assert "<continuation_summary>" in content
    assert "Mutable recalled memory" not in content
    assert result.cache_breakpoint_index == 0

    assert "Home directory:" in str(result.messages[1]["content"])
    recalled_messages = [
        str(message.get("content", ""))
        for message in result.messages
        if "Recalled memories:" in str(message.get("content", ""))
    ]
    assert recalled_messages == [
        '<memory_context trust="untrusted">\nRecalled memories:\n- (0.90) Mutable recalled memory\n</memory_context>'
    ]


@pytest.mark.asyncio
async def test_context_assembler_mentions_skill_write_only_when_visible() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent_with_skills(),
        user_message="hello",
        tool_definitions=[_profile_tool("skill_write", category="skills", read_only=False)],
        skip_memory=True,
        prompt_context=PromptContext.CHAT,
    )

    content = str(result.messages[0]["content"])
    assert "<skills_guidance>" in content
    assert "skill_write" in content
    assert "skill_asset_write" in content


@pytest.mark.asyncio
async def test_context_assembler_includes_auto_loaded_skill_context() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent_with_auto_loaded_skill(),
        user_message="hello",
        tool_definitions=[],
        skip_memory=True,
        prompt_context=PromptContext.CHAT,
    )

    content = str(result.messages[0]["content"])
    assert "<loaded_skills>" in content
    assert "<loaded_skill>" in content
    assert "Use careful implementation discipline." in content
    assert "not already loaded" in content


@pytest.mark.asyncio
async def test_context_assembler_skip_memory_path_uses_consolidated_immutable_prefix() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent_with_skills(),
        user_message="hello",
        tool_definitions=[],
        skip_memory=True,
        prompt_context=PromptContext.TASK_STEP,
    )

    first_message = result.messages[0]
    assert first_message["role"] == "system"
    content = str(first_message["content"])
    assert "<identity>" in content
    assert "You are helpful." in content
    assert "<instructions>" in content
    assert "## Behavior" in content
    assert "## Step execution" in content
    assert "<available_skills>" in content
    assert "Release Helper" in content
    assert "<skills_guidance>" in content
    assert "<critical_rules>" in content
    assert "managed only through Cognis skill tools" in content
    assert "This is a continuation from a previous session." in content
    assert "<continuation_summary>" in content
    assert "<memory_instructions>" not in content
    assert "Recalled memories:" not in content
    assert result.cache_breakpoint_index == 0
    assert "Home directory:" in str(result.messages[1]["content"])


@pytest.mark.asyncio
async def test_context_assembler_skip_memory_ignores_forked_owner_prefix() -> None:
    cache = _SessionCache()
    cache.prefix_entries = [
        ImmutablePrefixEntry(
            role="system",
            source="identity",
            content="You are Riker, the main owner agent.",
            seq=1,
        ),
        ImmutablePrefixEntry(
            role="developer",
            source="core_memories",
            content="## Agent Identity\n- Riker owner identity",
            seq=2,
        ),
    ]
    memory = _Memory()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session().model_copy(update={"agent_id": "system:architect"}),
        conversation=_conversation().model_copy(update={"agent_id": "system:architect"}),
        agent=AgentDefinition(
            agent_id="system:architect",
            owner_email="system@cognis.local",
            name="Architect",
            system_prompt="Review architecture risks only.",
            agent_type="secondary",
            is_system=True,
            llm_config=AgentLLMConfig(model="test-model", max_tokens=128),
        ),
        user_message="review the plan",
        tool_definitions=[],
        skip_memory=True,
        prompt_context=PromptContext.TASK_STEP,
    )

    content = str(result.messages[0]["content"])
    assert "Review architecture risks only." in content
    assert "You are Riker" not in content
    assert "Riker owner identity" not in content
    assert memory.identity_calls == []


@pytest.mark.asyncio
async def test_context_assembler_ignores_per_turn_identity_fields_after_bootstrap() -> None:
    """Per-turn recall must not mutate the immutable prefix after bootstrap."""

    call_count = 0

    class _PartialMemory:
        """Bootstrap identity stays fixed even if per-turn recall disagrees later."""

        search_modes: list[str] = []

        async def load_session_identity(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            return {
                "session_id": "mem-1",
                "instructions": "Use remember tool to store facts.",
                "core_memories": "## Agent Identity\n- I am a helpful assistant",
            }

        async def recall(self, **kwargs: object) -> dict[str, object]:
            nonlocal call_count
            call_count += 1
            self.search_modes.append(str(kwargs["search_mode"]))
            await asyncio.sleep(0.01)
            return {
                "session_id": "mem-1",
                "instructions": "DO NOT USE THIS NEW INSTRUCTION",
                "core_memories": "DO NOT REPLACE EXISTING CORE",
                "search_results": [],
            }

    memory = _PartialMemory()
    cache = _SessionCache()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result1 = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="hello",
        tool_definitions=[],
    )
    core_in_first = [m for m in result1.messages if "Agent Identity" in str(m.get("content", ""))]
    assert len(core_in_first) == 1, "Core memories should be in first turn context"

    result2 = await assembler.assemble(
        session=_session(mnemory_session_id="mem-1"),
        conversation=_conversation(),
        agent=_agent(),
        user_message="follow up",
        tool_definitions=[],
    )
    core_in_second = [m for m in result2.messages if "Agent Identity" in str(m.get("content", ""))]
    assert len(core_in_second) == 1
    prefix = str(result2.messages[0]["content"])
    assert "DO NOT USE THIS NEW INSTRUCTION" not in prefix
    assert "DO NOT REPLACE EXISTING CORE" not in prefix
    assert memory.search_modes == ["search", "search"]


@pytest.mark.asyncio
async def test_context_assembler_renders_follow_up_boundary_and_active_block() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Daily brief",
        source_type="scheduler",
        delivery_mode="same_conversation",
        result_summary="Daily summary is ready.",
        description="Daily schedule",
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="",
        user_message_role="system",
        follow_up=follow_up,
        tool_definitions=[],
    )

    contents = [str(message.get("content", "")) for message in result.messages]
    assert any("historical conversation context" in content for content in contents)
    assert any('<follow_up_event mode="notify"' in content for content in contents)
    assert any("## Follow-up notification" in content for content in contents)
    assert all(message.get("content") != "" for message in result.messages)


@pytest.mark.asyncio
async def test_context_assembler_keeps_follow_up_data_out_of_immutable_prefix() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.INTEGRATE,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="same_thread",
        required_action=FollowUpRequiredAction.INTEGRATE_RESULT,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Implement auth",
        source_type="chat",
        delivery_mode="same_conversation",
        result_summary="Refresh token support implemented.",
        description="Auth work",
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="",
        user_message_role="system",
        follow_up=follow_up,
        tool_definitions=[],
        prompt_context=PromptContext.FOLLOW_UP_INTEGRATE,
    )

    breakpoint = result.cache_breakpoint_index
    assert breakpoint is not None
    immutable_contents = "\n".join(
        str(message.get("content", "")) for message in result.messages[: breakpoint + 1]
    )
    assert "Implement auth" not in immutable_contents
    assert "Refresh token support implemented." not in immutable_contents
    assert "## Follow-up integration" not in immutable_contents

    suffix_contents = "\n".join(
        str(message.get("content", "")) for message in result.messages[breakpoint + 1 :]
    )
    assert "## Follow-up integration" in suffix_contents


def test_follow_up_prompt_guidance_is_suffix_only() -> None:
    integrate_prefix = build_system_instructions(PromptContext.FOLLOW_UP_INTEGRATE)
    notify_prefix = build_system_instructions(PromptContext.FOLLOW_UP_NOTIFY)

    assert integrate_prefix == notify_prefix
    assert integrate_prefix is not None
    assert "## Follow-up" not in integrate_prefix
    assert "## Follow-up integration" in (
        build_follow_up_guidance(PromptContext.FOLLOW_UP_INTEGRATE) or ""
    )
    assert "## Follow-up notification" in (
        build_follow_up_guidance(PromptContext.FOLLOW_UP_NOTIFY) or ""
    )


@pytest.mark.asyncio
async def test_context_assembler_injects_routing_reminder_before_attachment_notice_and_user_message() -> (
    None
):
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    reminder = "Routing hint: this request looks like non-trivial implementation work."
    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Implement auth",
        attachment_notice="Attachment notice",
        routing_reminder=reminder,
        tool_definitions=[],
    )

    contents = [str(message.get("content", "")) for message in result.messages]
    reminder_index = next(index for index, content in enumerate(contents) if reminder in content)
    attachment_index = contents.index("Attachment notice")
    user_index = next(
        index
        for index, content in enumerate(contents)
        if content.endswith(">Implement auth</message>")
    )

    assert reminder_index < attachment_index < user_index


@pytest.mark.asyncio
async def test_context_assembler_preserves_current_turn_native_attachments_when_message_is_in_history() -> (
    None
):
    attachment = {
        "artifact_id": "att-1",
        "kind": "image",
        "mime_type": "image/png",
        "filename": "muchi.png",
        "size_bytes": 12,
        "url": "https://example.com/muchi.png",
    }
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_VisionLLM(),
        session_cache=_HistorySessionCache(
            [
                {
                    "type": "user_message",
                    "data": {
                        "content": "User attached an image file.",
                        "attachments": [attachment],
                    },
                }
            ]
        ),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="User attached an image file.",
        user_attachments=[attachment],
        tool_definitions=[],
    )

    image_messages = [
        message
        for message in result.messages
        if message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in message.get("content", [])
        )
    ]

    assert image_messages
    text_parts = [
        str(part.get("text") or "")
        for part in image_messages[0]["content"]
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    assert any("artifact_id=att-1" in text for text in text_parts)


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_memory", [False, True])
async def test_recorded_attachment_context_replaces_replayed_text_without_duplicates(
    skip_memory: bool,
) -> None:
    attachment = {
        "artifact_id": "att-history",
        "kind": "image",
        "mime_type": "image/png",
        "filename": "context.png",
        "size_bytes": 12,
        "url": "https://example.com/context.png",
    }
    metadata = {
        "ts": "2026-08-01T10:15:00Z",
        "channel": "matrix",
        "sender": "Alice",
        "untrusted": True,
    }
    contextual_messages = [
        {
            "content": "Thread root context",
            "message_metadata": {
                "ts": "2026-08-01T10:10:00Z",
                "channel": "matrix",
                "sender": "Bob",
                "untrusted": True,
            },
        }
    ]
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_VisionLLM(),
        session_cache=_HistorySessionCache(
            [
                {
                    "type": "user_message",
                    "data": {
                        "content": "Primary request",
                        "message_metadata": metadata,
                        "context_messages": contextual_messages,
                        "attachments": [attachment],
                    },
                }
            ]
        ),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Primary request",
        user_message_metadata=metadata,
        contextual_messages=contextual_messages,
        user_attachments=[attachment],
        attachment_context="Extracted attachment context",
        tool_definitions=[],
        skip_memory=skip_memory,
    )

    user_messages = [message for message in result.messages if message.get("role") == "user"]
    text_blocks = [
        str(part.get("text") or "")
        for message in user_messages
        for part in (
            message.get("content", [])
            if isinstance(message.get("content"), list)
            else [{"type": "text", "text": message.get("content", "")}]
        )
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    model_text = "\n".join(text_blocks)
    native_blocks = [
        part
        for message in user_messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]

    assert model_text.count("Thread root context") == 1
    assert model_text.count("Extracted attachment context") == 1
    assert model_text.count("Primary request") == 1
    assert model_text.count("artifact_id=att-history") == 1
    assert model_text.count("<message ") == model_text.count("</message>") == 3
    assert model_text.index("Thread root context") < model_text.index(
        "Extracted attachment context"
    )
    assert model_text.index("Extracted attachment context") < model_text.index("Primary request")
    assert model_text.rstrip().endswith("</message>")
    assert len(native_blocks) == 1


@pytest.mark.asyncio
async def test_context_assembler_keeps_small_tool_groups_below_steady_target() -> None:
    events: list[dict[str, object]] = []
    for index in range(4):
        call_id = f"call-{index}"
        events.extend(
            [
                {
                    "type": "tool_call",
                    "data": {
                        "name": "read",
                        "call_id": call_id,
                        "arguments": {"path": f"{index}.py"},
                    },
                },
                {
                    "type": "tool_result",
                    "data": {
                        "call_id": call_id,
                        "name": "read",
                        "result": f"result-{index}",
                        "output_size": 8,
                        "recovery_call_id": call_id,
                    },
                },
            ]
        )
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_HistorySessionCache(events),
        session_manager=_SessionManager(),
        max_context_tokens=20_000,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="compare all four results",
        tool_definitions=[],
    )

    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert [message["content"] for message in tool_messages] == [
        "result-0",
        "result-1",
        "result-2",
        "result-3",
    ]
    assert result.projection_compacted_tool_group_anchors == []
    assert result.projection_mutable_start_index == 0


@pytest.mark.asyncio
async def test_context_assembler_projects_older_tool_groups_into_stable_placeholders() -> None:
    events = [
        {
            "type": "tool_call",
            "data": {"name": "bash", "call_id": "call-1", "arguments": {"command": "ls"}},
        },
        {
            "type": "tool_result",
            "data": {
                "call_id": "call-1",
                "name": "bash",
                "result": "A" * 240_000,
                "output_size": 240_000,
                "recovery_call_id": "call-1",
            },
        },
        {
            "type": "tool_call",
            "data": {"name": "read", "call_id": "call-2", "arguments": {"path": "a.py"}},
        },
        {
            "type": "tool_result",
            "data": {
                "call_id": "call-2",
                "name": "read",
                "result": "recent-1",
                "output_size": 8,
                "recovery_call_id": "call-2",
            },
        },
        {
            "type": "tool_call",
            "data": {"name": "grep", "call_id": "call-3", "arguments": {"pattern": "needle"}},
        },
        {
            "type": "tool_result",
            "data": {
                "call_id": "call-3",
                "name": "grep",
                "result": "recent-2",
                "output_size": 8,
                "recovery_call_id": "call-3",
            },
        },
    ]
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_HistorySessionCache(events),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="continue",
        tool_definitions=[],
    )

    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert "Tool output omitted from prompt." in str(tool_messages[0]["content"])
    assert "call_id='call-1'" in str(tool_messages[0]["content"])
    assert tool_messages[1]["content"] == "recent-1"
    assert tool_messages[2]["content"] == "recent-2"
    assert len(result.projection_compacted_tool_group_anchors) == 1
    assert result.projection_mutable_start_index > 0


@pytest.mark.asyncio
async def test_context_assembler_keeps_routing_reminder_out_of_immutable_prefix() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    reminder = "Routing hint: this request looks like code review or audit work."
    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="Review this diff",
        routing_reminder=reminder,
        tool_definitions=[],
    )

    breakpoint = result.cache_breakpoint_index
    assert breakpoint is not None
    immutable_contents = "\n".join(
        str(message.get("content", "")) for message in result.messages[: breakpoint + 1]
    )
    assert reminder not in immutable_contents


@pytest.mark.asyncio
async def test_provider_boundary_clears_follow_up_markers_in_skip_memory_path() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Daily brief",
        source_type="scheduler",
        delivery_mode="same_conversation",
        result_summary="Done",
        description="daily schedule",
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="",
        user_message_role="system",
        follow_up=follow_up,
        tool_definitions=[],
        skip_memory=True,
    )

    # Assembly retains rendering inputs for subsequent within-turn projection;
    # no controller-only fields may escape the actual provider boundary.
    from cognis.providers.llm.message_projection import project_messages_for_provider

    provider_messages = project_messages_for_provider(
        result.messages, provider=None, llm_api="responses"
    ).messages
    assert all(
        not any(str(key).startswith("_") for key in message) for message in provider_messages
    )
    from types import SimpleNamespace

    from cognis.providers.llm.message_projection import project_messages_for_provider

    projected = project_messages_for_provider(
        result.messages,
        provider=SimpleNamespace(config={"preset": "anthropic"}),
        llm_api="chat_completions",
    ).messages
    assert all(not any(str(key).startswith("_") for key in message) for message in projected)


def _profile_tool(name: str, *, category: str, read_only: bool) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category=category,
        read_only=read_only,
    )


def test_direct_default_profile_keeps_delegate_visible() -> None:
    profile = resolve_step_profile(
        StepDefinition(
            name="direct",
            type="run",
            prompt="",
            step_profile_id="system:direct-default",
        )
    )

    assert step_profile_visible_by_default(
        _profile_tool("delegate", category="orchestration", read_only=False),
        profile,
    )
    assert step_profile_visible_by_default(
        _profile_tool("create_task", category="orchestration", read_only=False),
        profile,
    )
    assert not step_profile_visible_by_default(
        _profile_tool("bash", category="shell", read_only=False),
        profile,
    )
    assert step_profile_allows_tool(
        _profile_tool("bash", category="shell", read_only=False),
        profile,
    )
    assert step_profile_visible_by_default(
        _profile_tool("image_edit", category="image", read_only=True),
        profile,
    )
    assert not step_profile_visible_by_default(
        _profile_tool("get_status", category="system", read_only=True),
        profile,
    )


def test_general_task_profile_uses_matrix_for_visible_tools_but_keeps_broad_search() -> None:
    profile = resolve_step_profile(
        StepDefinition(
            name="task",
            type="run",
            prompt="",
            step_profile_id="system:general-task",
        )
    )

    assert step_profile_visible_by_default(
        _profile_tool("memory_search", category="memory", read_only=True),
        profile,
    )
    assert step_profile_visible_by_default(
        _profile_tool("skill_load", category="skill", read_only=True),
        profile,
    )
    assert step_profile_visible_by_default(
        _profile_tool("read_tool_output", category="context", read_only=True),
        profile,
    )
    assert step_profile_visible_by_default(
        _profile_tool("mcp_googleworkspace__search_messages", category="mcp", read_only=True),
        profile,
    )
    assert not step_profile_visible_by_default(
        _profile_tool("get_status", category="system", read_only=True),
        profile,
    )
    assert not step_profile_visible_by_default(
        _profile_tool("browser_snapshot", category="browser", read_only=True),
        profile,
    )
    assert step_profile_allows_tool(
        _profile_tool("browser_snapshot", category="browser", read_only=True),
        profile,
    )


def test_hard_profile_hides_and_removes_out_of_matrix_tools() -> None:
    profile = resolve_step_profile(
        StepDefinition(
            name="direct",
            type="run",
            prompt="",
            step_profile_id="system:direct-default",
            step_profile_mode=StepProfileMode.HARD,
        )
    )

    assert not step_profile_visible_by_default(
        _profile_tool("bash", category="shell", read_only=False),
        profile,
    )
    assert not step_profile_allows_tool(
        _profile_tool("bash", category="shell", read_only=False),
        profile,
    )


@pytest.mark.asyncio
async def test_immutable_prefix_stays_identical_across_turns_when_recall_changes() -> None:
    class _VaryingMemory(_Memory):
        def __init__(self) -> None:
            super().__init__()
            self._recall_count = 0

        async def recall(self, **kwargs: object) -> dict[str, object]:
            self._recall_count += 1
            base = await super().recall(**kwargs)
            return {
                **base,
                "search_results": [
                    {
                        "memory": f"Dynamic recalled memory {self._recall_count}",
                        "score": 0.9,
                    }
                ],
            }

    assembler = ContextAssembler(
        memory=_VaryingMemory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    first = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent_with_skills(),
        user_message="hello",
        tool_definitions=[],
        prompt_context=PromptContext.CHAT,
    )
    second = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent_with_skills(),
        user_message="hello again",
        tool_definitions=[],
        prompt_context=PromptContext.CHAT,
    )

    assert first.messages[0]["content"] == second.messages[0]["content"]
    assert any(
        "Dynamic recalled memory 1" in str(message.get("content", "")) for message in first.messages
    )
    assert any(
        "Dynamic recalled memory 2" in str(message.get("content", ""))
        for message in second.messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "identity_calls", "recall_calls", "has_core"),
    [
        ("full_auto", 1, 1, True),
        ("proactive", 1, 0, True),
        ("on_demand", 0, 0, False),
    ],
)
async def test_context_assembler_applies_frozen_memory_mode_policy(
    mode: str,
    identity_calls: int,
    recall_calls: int,
    has_core: bool,
) -> None:
    memory = _Memory()
    agent = _agent().model_copy(
        update={
            "capabilities": AgentCapabilities(
                memory_backend_options={"mode": mode},
            )
        }
    )
    session = _session()
    conversation = _conversation()
    policy = resolve_memory_policy(
        agent,
        resolve_conversation_agent_profile(agent, session, conversation),
    )
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=session,
        conversation=conversation,
        agent=agent,
        user_message="remember this",
        tool_definitions=[],
        memory_policy=policy,
    )

    assert len(memory.identity_calls) == identity_calls
    assert len(memory.recall_calls) == recall_calls
    prompt = "\n".join(str(message.get("content") or "") for message in result.messages)
    assert ("prefers Python" in prompt) is has_core
    if mode == "proactive":
        assert "core memories are already loaded" in prompt
    if mode == "on_demand":
        assert "available on demand" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True], ids=["warm-l1", "cold-reconstruction"])
@pytest.mark.parametrize(
    ("backend", "mode", "expected_instruction", "expects_core"),
    [
        ("mnemory", "proactive", "core memories are already loaded", True),
        ("mnemory", "on_demand", "available on demand", False),
        ("none", "", None, False),
    ],
)
async def test_policy_fingerprint_transition_replaces_stale_memory_prefix(
    cold: bool,
    backend: str,
    mode: str,
    expected_instruction: str | None,
    expects_core: bool,
) -> None:
    cache = _SessionCache(cold=cold)
    stale_entries = [
        ImmutablePrefixEntry(
            role="developer",
            source="memory_instructions",
            content="STALE FULL AUTO MEMORY INSTRUCTIONS",
            seq=10,
        ),
        ImmutablePrefixEntry(
            role="developer",
            source="core_memories",
            content="STALE CORE MEMORY",
            seq=11,
        ),
        ImmutablePrefixEntry(
            role="developer",
            source="managed_channel_policy",
            content="PERSISTED MANAGED CHANNEL POLICY",
            seq=12,
        ),
    ]
    if cold:
        cache.prefix_entries_after_refresh = stale_entries
        cache.policy_fingerprint_after_refresh = "old-full-auto-fingerprint"
    else:
        cache.prefix_entries = stale_entries
        assert cache.entry is not None
        cache.entry.memory_policy_fingerprint = "old-full-auto-fingerprint"

    options = {"mode": mode} if mode else {}
    agent = _agent().model_copy(
        update={
            "capabilities": AgentCapabilities(
                memory_backend=backend,
                memory_backend_options=options,
            )
        }
    )
    session = _session("mem-1")
    conversation = _conversation()
    policy = resolve_memory_policy(
        agent,
        resolve_conversation_agent_profile(agent, session, conversation),
    )
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=session,
        conversation=conversation,
        agent=agent,
        user_message="hello",
        tool_definitions=[],
        memory_policy=policy,
    )

    prompt = "\n".join(str(message.get("content") or "") for message in result.messages)
    assert "STALE FULL AUTO MEMORY INSTRUCTIONS" not in prompt
    assert "STALE CORE MEMORY" not in prompt
    assert prompt.count("PERSISTED MANAGED CHANNEL POLICY") == 1
    if expected_instruction:
        assert expected_instruction in prompt
    else:
        assert "Mnemory is" not in prompt
    assert ("prefers Python" in prompt) is expects_core
    assert cache.entry is not None
    assert cache.entry.memory_policy_fingerprint == policy.policy_fingerprint
    assert [
        entry.source for entry in cache.prefix_entries if entry.source == "managed_channel_policy"
    ] == ["managed_channel_policy"]


@pytest.mark.asyncio
async def test_persisted_message_metadata_prevents_timestamp_boundary_duplicate() -> None:
    metadata = {"ts": "2026-08-01T10:15:00Z"}
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_HistorySessionCache(
            [
                {
                    "type": "user_message",
                    "ts": "2026-08-01T10:15:01Z",
                    "data": {"content": "workflow prompt", "message_metadata": metadata},
                }
            ]
        ),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="workflow prompt",
        user_message_metadata=metadata,
        tool_definitions=[],
    )

    matching = [
        message
        for message in result.messages
        if message.get("role") == "user" and "workflow prompt" in str(message.get("content"))
    ]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_current_untrusted_attachment_text_is_inside_envelope() -> None:
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    metadata = {
        "ts": "2026-08-01T10:15:00Z",
        "channel": "signal",
        "sender": "Alice",
        "untrusted": True,
    }

    result = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="",
        user_message_metadata=metadata,
        attachment_context="Extracted attachment text",
        user_attachments=[
            {
                "artifact_id": "att_1",
                "kind": "file",
                "mime_type": "application/pdf",
                "filename": "report.pdf",
                "size_bytes": 123,
            }
        ],
        tool_definitions=[],
    )

    user_text = "\n".join(
        str(message.get("content", ""))
        for message in result.messages
        if message.get("role") == "user"
    )
    assert 'untrusted="true"' in user_text
    assert "Extracted attachment text" in user_text
    assert user_text.rstrip().endswith("</message>")


class _SlowCountingLLM(_LLM):
    """Simulates a multi-second exact count; must not block the event loop."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    def count_messages_tokens(self, messages: list[dict[str, object]], model: str) -> int:
        self.calls += 1
        time.sleep(self.delay)
        return super().count_messages_tokens(messages, model)


@pytest.mark.asyncio
async def test_assemble_keeps_event_loop_responsive_during_slow_token_counts() -> None:
    slow_llm = _SlowCountingLLM(delay=0.25)
    cache = _SessionCache()
    cache.history_events = [
        {"type": "user_message", "data": {"content": f"question {index} " * 50}}
        for index in range(20)
    ]
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=slow_llm,
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=200_000,
        compaction_threshold=0.85,
    )
    baseline = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=cache,
        session_manager=_SessionManager(),
        max_context_tokens=200_000,
        compaction_threshold=0.85,
    )

    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await assembler.assemble(
            session=_session(),
            conversation=_conversation(),
            agent=_agent(),
            user_message="next",
            tool_definitions=[],
        )
    finally:
        stop.set()
        await ticker_task

    expected = await baseline.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="next",
        tool_definitions=[],
    )

    def _without_timestamps(messages: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                key: (re.sub(r'ts="[^"]*"', 'ts=""', value) if isinstance(value, str) else value)
                for key, value in message.items()
            }
            for message in messages
        ]

    assert slow_llm.calls >= 1
    assert max(gaps) < 0.1, f"event loop stalled for {max(gaps):.3f}s during assembly"
    assert _without_timestamps(result.messages) == _without_timestamps(expected.messages)
    assert result.prompt_tokens == expected.prompt_tokens
    assert result.cache_breakpoint_index == expected.cache_breakpoint_index
