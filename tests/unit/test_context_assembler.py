from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic

import pytest

from cognis.core.context import ContextAssembler, _build_environment_info
from cognis.core.followups import (
    FollowUpMode,
    FollowUpOriginKind,
    FollowUpRequiredAction,
    FollowUpStatus,
    TaskResultFollowUp,
)
from cognis.core.prompts import PromptContext
from cognis.core.runtime import ExecutorEnvironmentSnapshot
from cognis.models.agent import AgentDefinition, AgentLLMConfig
from cognis.models.config import ModelInfo
from cognis.models.session import ConversationContext, ConversationModel, SessionModel
from cognis.models.tool import ToolDefinition, ToolSource


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
        self._cached_instructions: str | None = None
        self._cached_core: str | None = None

    async def refresh(self, session: SessionModel) -> object:
        del session
        self.refresh_calls += 1
        await asyncio.sleep(0.1)
        if self.fail_refresh:
            raise RuntimeError("event read failed")
        self.entry = self.entry or _CacheEntry()
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

    def get_cached_memory(
        self, session_id: str, ttl_seconds: float = 1800.0
    ) -> tuple[str | None, str | None, bool]:
        del session_id, ttl_seconds
        if self._cached_instructions is not None or self._cached_core is not None:
            return self._cached_instructions, self._cached_core, True
        return None, None, False

    def get_cached_memory_details(
        self, session_id: str, ttl_seconds: float = 1800.0
    ) -> tuple[str | None, str | None, bool, bool]:
        del session_id, ttl_seconds
        if self._cached_instructions is not None or self._cached_core is not None:
            return self._cached_instructions, self._cached_core, True, True
        return None, None, False, False

    async def cache_memory(
        self, session_id: str, instructions: str | None, core_memories: str | None
    ) -> None:
        del session_id
        # Merge semantics: None means "not returned", preserve existing.
        if instructions is not None:
            self._cached_instructions = instructions
        if core_memories is not None:
            self._cached_core = core_memories

    def get_model_override(self, session_id: str) -> str | None:
        del session_id
        return None

    def update_context_usage(
        self, session: object, *, prompt_tokens: int, max_context_tokens: int, model: str
    ) -> None:
        pass


class _Memory:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.search_modes: list[str] = []

    async def recall(self, **kwargs: object) -> dict[str, object]:
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

    assert elapsed < 0.25
    assert memory.search_modes == ["find"]
    assert session_manager.attached == [("session-1", "mem-1")]
    assert any('trust="untrusted"' in str(message["content"]) for message in result.messages)


@pytest.mark.asyncio
async def test_context_assembler_loads_root_project_instructions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Project instructions\nUse pytest.\n")
    (tmp_path / "README.md").write_text("# Readme\nHelpful overview.\n")

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
        "Instructions from:" in content and "AGENTS.md" in content for content in system_messages
    )
    assert not any(
        "Instructions from:" in content and "README.md" in content for content in system_messages
    )
    assert "Instructions from:" in str(result.messages[0]["content"])
    assert "AGENTS.md" in str(result.messages[0]["content"])


@pytest.mark.asyncio
async def test_context_assembler_uses_search_mode_for_follow_up_turns() -> None:
    memory = _Memory()
    assembler = ContextAssembler(
        memory=memory,
        guardrails=_Guardrails(),
        llm=_LLM(),
        session_cache=_SessionCache(),
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
    assembler = ContextAssembler(
        memory=_Memory(),
        guardrails=_Guardrails(),
        llm=_LLM(),
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
    """Environment info must include home dir, cwd, platform, and date."""
    info = _build_environment_info()
    assert "Home directory:" in info
    assert "Working directory:" in info
    assert "Platform:" in info
    assert "Date:" in info
    assert "Hostname:" in info
    assert "System user:" in info
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
        async def recall(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            return {
                "session_id": "mem-1",
                "instructions": "Use remember tool to store durable facts.",
                "core_memories": "Prefers Python and pytest.",
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
    assert "This is a continuation from a previous session." in content
    assert "<continuation_summary>" in content
    assert "<memory_instructions>" not in content
    assert "Recalled memories:" not in content
    assert result.cache_breakpoint_index == 0
    assert "Home directory:" in str(result.messages[1]["content"])


@pytest.mark.asyncio
async def test_context_assembler_preserves_core_memories_on_partial_refresh() -> None:
    """When Mnemory returns instructions but not core_memories (e.g. TTL
    refresh on a subsequent call), the previously cached core_memories
    must survive — they are part of the immutable prefix.
    """

    call_count = 0

    class _PartialMemory:
        """First call returns both; second returns only instructions."""

        search_modes: list[str] = []

        async def recall(self, **kwargs: object) -> dict[str, object]:
            nonlocal call_count
            call_count += 1
            self.search_modes.append(str(kwargs["search_mode"]))
            await asyncio.sleep(0.01)
            if call_count == 1:
                return {
                    "session_id": "mem-1",
                    "instructions": "Use remember tool to store facts.",
                    "core_memories": "## Agent Identity\n- I am a helpful assistant",
                    "search_results": [{"memory": "Uses pytest", "score": 0.9}],
                }
            # Subsequent call: instructions refreshed, core_memories absent
            return {
                "session_id": "mem-1",
                "instructions": "Use remember tool to store facts.",
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

    # First call: both instructions and core_memories returned and cached
    result1 = await assembler.assemble(
        session=_session(),
        conversation=_conversation(),
        agent=_agent(),
        user_message="hello",
        tool_definitions=[],
    )
    core_in_first = [m for m in result1.messages if "Agent Identity" in str(m.get("content", ""))]
    assert len(core_in_first) == 1, "Core memories should be in first turn context"

    # Second call: instructions returned but NOT core_memories.
    # The cached core_memories must still appear in the context.
    result2 = await assembler.assemble(
        session=_session(mnemory_session_id="mem-1"),
        conversation=_conversation(),
        agent=_agent(),
        user_message="follow up",
        tool_definitions=[],
    )
    core_in_second = [m for m in result2.messages if "Agent Identity" in str(m.get("content", ""))]
    assert len(core_in_second) == 1, (
        "Core memories must survive partial recall — they are part of the immutable prefix"
    )


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
