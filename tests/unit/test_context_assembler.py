from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import pytest

from cognis.core.context import (
    ContextAssembler,
    _build_agent_work_context_info,
    _build_channel_context_info,
    _build_environment_info,
    _build_web_main_chat_context_info,
    _build_web_topic_context_info,
    _find_cache_breakpoint,
    _is_protected_context_message,
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
from cognis.core.immutable_prefix import ImmutablePrefixEntry
from cognis.core.message_markers import (
    AUDIT_METADATA,
    IMMUTABLE_PREFIX,
    LATEST_MEMORY_RECALL_CONTEXT,
    PROJECT_CONTEXT,
    TURN_BOUNDARY,
)
from cognis.core.project_context import ProjectContextEntry, build_project_instruction_message
from cognis.core.prompts import (
    PromptContext,
    build_follow_up_guidance,
    build_system_instructions,
)
from cognis.core.runtime import ExecutorEnvironmentSnapshot
from cognis.core.step_profiles import (
    resolve_step_profile,
    step_profile_allows_tool,
    step_profile_visible_by_default,
)
from cognis.models.agent import AgentDefinition, AgentLLMConfig
from cognis.models.config import ModelInfo
from cognis.models.session import ConversationContext, ConversationModel, SessionModel
from cognis.models.tool import ToolDefinition, ToolSource
from cognis.models.workflow import StepDefinition, StepProfileMode


class _CacheEntry:
    def __init__(self) -> None:
        self.last_compaction_summary = "summary"
        self.intention = "cached intention"
        self.events = []
        self.initialized = True
        self.last_event_seq = 0


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
        self.history_events: list[dict[str, object]] = []
        self.prefix_repair_needed = False
        self.last_repair_attempt_at: float | None = None
        self.mark_prefix_repair_calls = 0
        self.project_contexts: dict[str, ProjectContextEntry] = {}

    async def refresh(self, session: SessionModel) -> object:
        del session
        self.refresh_calls += 1
        await asyncio.sleep(0.1)
        if self.fail_refresh:
            raise RuntimeError("event read failed")
        self.entry = self.entry or _CacheEntry()
        if self.prefix_entries_after_refresh:
            self.prefix_entries = list(self.prefix_entries_after_refresh)
        return self.entry

    def get_entry(self, session_id: str) -> object | None:
        del session_id
        return self.entry

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
        del session
        seq = int(getattr(result, "first_seq", 0))
        for event in events:
            if event.type not in {"system_message", "developer_message"}:
                continue
            self.prefix_entries.append(
                ImmutablePrefixEntry(
                    role=str(event.data.get("role") or "developer"),
                    source=str(event.data.get("source") or ""),
                    content=str(event.data.get("content") or ""),
                    seq=seq,
                )
            )
            seq += 1

    async def store_prefix_snapshot(
        self,
        session_id: str,
        entries: list[ImmutablePrefixEntry],
        *,
        snapshot_seq: int,
        snapshot_source: str,
    ) -> None:
        del session_id, snapshot_seq, snapshot_source
        self.prefix_entries = list(entries)
        self.prefix_repair_needed = False

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
            {"intention": "fresh intention", "title": None, "updated_at": None},
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
    assert "Do not optimize for finishing the whole job inside the parent turn" in content
    assert "parent chat as the command bridge" in content
    assert "delegate(wait=false) for bounded, non-interactive worker-style lookup" in content
    assert "prefer reusing an existing relevant managed conversation" in content
    assert "Use agent_conversation_send(wait=false) to continue it" in content
    assert (
        "agent_conversation_create(wait=false) only for a new visible iterative work loop"
        in content
    )
    assert "continue the same managed conversation" in content
    assert "instead of creating a duplicate" in content
    assert 'chat_mode="plan"' in content
    assert 'chat_mode="build"' in content
    assert "wait=false means fire-and-follow-up, not fire-and-duplicate" in content
    assert "follow-up/resume notification" in content
    assert "create_task for durable workflow-shaped work with lifecycle" in content


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
    assert "delegate(wait=false)" in content
    assert "delegate(wait=true)" in content
    assert "prefer reusing an existing relevant managed conversation" in content
    assert "Use agent_conversation_send(wait=false) to continue it" in content
    assert "agent_conversation_create(wait=false)" in content
    assert "only for a new visible implementation" in content
    assert "continue the same managed conversation" in content
    assert "instead of creating a duplicate" in content
    assert 'chat_mode="plan"' in content
    assert 'chat_mode="build"' in content
    assert "fire-and-follow-up" in content
    assert "stop the parent turn" in content
    assert "create_task" in content

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
    assert "Do not start background managed conversations" in content
    assert "managed-conversation tools are available without a wait parameter" in content
    assert "agent_conversation_create(wait=false)" in content
    assert "reserved for main/channel chat surfaces" in content
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
    assert "Use delegate for specialist child work" in content
    assert "Avoid asynchronous delegation from managed conversations" in content
    assert "Do not create tasks or workflows" in content
    assert "Implement assigned coding/debugging work directly" in content


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
    assert any(
        "Do not optimize for finishing the whole job inside the parent turn" in message
        for message in signal_messages
    )
    assert any("parent chat as the command bridge" in message for message in signal_messages)
    assert any(
        "delegate(wait=false) for bounded, non-interactive worker-style lookup" in message
        for message in signal_messages
    )
    assert any(
        "agent_conversation_create(wait=false) only for a new visible iterative work loop"
        in message
        for message in signal_messages
    )
    assert any(
        "Use agent_conversation_send(wait=false) to continue it" in message
        for message in signal_messages
    )
    assert any(
        'chat_mode="plan"' in message and 'chat_mode="build"' in message
        for message in signal_messages
    )
    assert any(
        "create_task for durable workflow-shaped work with lifecycle" in message
        for message in signal_messages
    )

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
    assert any("delegate(wait=false)" in message for message in direct_context_messages)
    assert any("delegate(wait=true)" in message for message in direct_context_messages)
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
    assert any("agent_conversation_create(wait=false)" in message for message in web_main_messages)
    assert any(
        "Use agent_conversation_send(wait=false) to continue it" in message
        for message in web_main_messages
    )

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
    assert any(
        "Do not start background managed conversations" in message for message in web_messages
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
        session_cache=_SessionCache(model_override="claude-sonnet-5"),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )
    agent = _agent().model_copy(
        update={"llm_config": AgentLLMConfig(model="gpt-5", provider_id="codex")}
    )

    await assembler.assemble(
        session=_session(),
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
async def test_context_assembler_audits_recalled_memories_as_replayable_developer_context() -> None:
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
    }


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
    assert result.messages[-1]["content"] == "warm failure"


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
    assert "do not use them as fallback capacity" in reminder
    assert "only when the task explicitly requires that specific machine" in reminder
    assert "switch back to a primary executor before unrelated or generic" in reminder


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
        if call.get("idempotency_key")
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
    assert "If the task names a skill" in content
    assert "Skills are managed exclusively through Cognis-provided skill tools" in content
    assert "not filesystem SKILL.md files or other filesystem skill manifests" in content
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
    assert "Use skill_write to create or update skills for future use" in content
    assert "task reveals reusable workflow, tool, safety, or style guidance" in content
    assert "use skill_asset_write for reusable references, templates, or scripts" in content
    assert "do not create SKILL.md files instead" in content


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
    assert "is not already marked as loaded" in content
    assert "unless the skill is already marked as loaded" in content


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
    assert "If the task names a skill" in content
    assert "Skills are managed exclusively through Cognis-provided skill tools" in content
    assert "task teaches a durable reusable procedure" in content
    assert "Prefer updating an existing relevant skill over creating a new one" in content
    assert "recurring class-level workflows" in content
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
    assert any("system-initiated follow-up" in content for content in contents)
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
    assert "follow-up result" not in immutable_contents

    suffix_contents = "\n".join(
        str(message.get("content", "")) for message in result.messages[breakpoint + 1 :]
    )
    assert "follow-up result" in suffix_contents


def test_follow_up_prompt_guidance_is_suffix_only() -> None:
    integrate_prefix = build_system_instructions(PromptContext.FOLLOW_UP_INTEGRATE)
    notify_prefix = build_system_instructions(PromptContext.FOLLOW_UP_NOTIFY)

    assert integrate_prefix == notify_prefix
    assert integrate_prefix is not None
    assert "follow-up result" not in integrate_prefix
    assert "system-initiated follow-up" not in integrate_prefix
    assert "follow-up result" in (build_follow_up_guidance(PromptContext.FOLLOW_UP_INTEGRATE) or "")
    assert "system-initiated follow-up" in (
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
    reminder_index = contents.index(reminder)
    attachment_index = contents.index("Attachment notice")
    user_index = contents.index("Implement auth")

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
    assert "call_id 'call-1'" in str(tool_messages[0]["content"])
    assert tool_messages[1]["content"] == "recent-1"
    assert tool_messages[2]["content"] == "recent-2"


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
async def test_context_assembler_clears_follow_up_markers_in_skip_memory_path() -> None:
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

    assert all(not any(str(key).startswith("_") for key in message) for message in result.messages)


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
