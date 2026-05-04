from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic

import pytest

from cognis.core.context import (
    ContextAssembler,
    _build_environment_info,
    _load_project_instructions,
)
from cognis.core.errors import ImmutablePrefixUnavailable
from cognis.core.followups import (
    FollowUpMode,
    FollowUpOriginKind,
    FollowUpRequiredAction,
    FollowUpStatus,
    TaskResultFollowUp,
)
from cognis.core.immutable_prefix import ImmutablePrefixEntry
from cognis.core.project_context import ProjectContextEntry, build_project_instruction_message
from cognis.core.prompts import PromptContext
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


class _SessionCache:
    def __init__(self, fail_refresh: bool = False, cold: bool = False) -> None:
        self.fail_refresh = fail_refresh
        self.cold = cold
        self.entry = None if cold else _CacheEntry()
        self.refresh_calls = 0
        self.prefix_entries: list[ImmutablePrefixEntry] = []
        self.prefix_entries_after_refresh: list[ImmutablePrefixEntry] = []
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

    async def update_intention(self, session_id: str, intention: str | None) -> None:
        del session_id
        if self.entry is not None:
            self.entry.intention = intention

    def get_events_since_compaction(self, session_id: str, types: list[str] | None = None) -> list:
        del session_id, types
        return []

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
        return None

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


class _VisionLLM(_LLM):
    async def get_model_info(self, model_id: str) -> ModelInfo:
        del model_id
        return ModelInfo(
            model_id="test-model",
            context_window=20000,
            max_output_tokens=256,
            supports_vision=True,
        )


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


def _conversation() -> ConversationModel:
    return ConversationModel(
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web", memory_labels={"project": "cognis"}),
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
    assert session_manager.attached == [("session-1", "mem-1")]
    assert any('trust="untrusted"' in str(message["content"]) for message in result.messages)


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


def test_project_instruction_loader_prefers_agents_over_nested_readme(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("# Project instructions\nUse pytest.\n")
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

    assert len(instructions) == 1
    assert "AGENTS.md" in instructions[0]
    assert "Use pytest" in instructions[0]
    assert "Nested overview" not in instructions[0]


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
async def test_context_assembler_raises_on_mnemory_failure() -> None:
    """Mnemory is a mandatory provider — failure must raise, not degrade."""
    assembler = ContextAssembler(
        memory=_Memory(fail=True),
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
        session_manager=_SessionManager(),
        max_context_tokens=4096,
        compaction_threshold=0.85,
    )

    with pytest.raises(RuntimeError, match="mnemory unavailable"):
        await assembler.assemble(
            session=_session(),
            conversation=_conversation(),
            agent=_agent(),
            user_message="should fail",
            tool_definitions=[],
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

    # With the new context structure, core memories are in the immutable prefix
    # and are never pruned. Only mutable recalled memories (search results)
    # should be pruned when the token budget is tight.
    assert not any(
        'trust="untrusted"' in str(message["content"])
        and "Recalled memories:" in str(message["content"])
        for message in result.messages
    )


def test_build_environment_info_contains_required_fields() -> None:
    """Environment info must include stable executor context, not wall-clock time."""
    info = _build_environment_info()
    assert "Home directory:" in info
    assert "Working directory:" in info
    assert "Platform:" in info
    assert "Hostname:" in info
    assert "System user:" in info
    assert "get_current_datetime" in info
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
    assert "IMPORTANT: If the task names a skill" in content
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
    assert "IMPORTANT: If the task names a skill" in content
    assert "This is a continuation from a previous session." in content
    assert "<continuation_summary>" in content
    assert "<memory_instructions>" not in content
    assert "Recalled memories:" not in content
    assert result.cache_breakpoint_index == 0
    assert "Home directory:" in str(result.messages[1]["content"])


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
