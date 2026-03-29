"""Context assembly for chat turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionModel
from cognis.models.tool import ToolDefinition
from cognis.runtime_context import scoped_runtime_context
from cognis.store.queries import get_setting_value

EVENT_TYPES_FOR_CONTEXT = [
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "delegation",
    "task_result",
    "task_failed",
    "task_cancelled",
]


class ContextAssemblyResult(BaseModel):
    """Fully assembled LLM context and metadata."""

    messages: list[dict[str, Any]]
    degraded: bool = False
    degraded_sources: list[str] = Field(default_factory=list)
    resolved_model: str
    static_tokens: int = 0
    dynamic_tokens: int = 0
    prompt_tokens: int = 0
    recommend_compaction: bool = False


class ContextAssembler:
    """Assemble LLM prompt context from cache, memory, and session state."""

    def __init__(
        self,
        *,
        memory: Any,
        guardrails: Any,
        llm: Any,
        session_cache: Any,
        session_manager: Any,
        max_context_tokens: int,
        compaction_threshold: float,
    ) -> None:
        self.memory = memory
        self.guardrails = guardrails
        self.llm = llm
        self.session_cache = session_cache
        self.session_manager = session_manager
        self.max_context_tokens = max_context_tokens
        self.compaction_threshold = compaction_threshold

    @classmethod
    async def from_session_factory(
        cls,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        memory: Any,
        guardrails: Any,
        llm: Any,
        session_cache: Any,
        session_manager: Any,
    ) -> ContextAssembler:
        """Create a context assembler with DB-backed settings."""

        async with session_factory() as db_session:
            max_context_tokens = await get_setting_value(
                db_session, "session.max_context_tokens", 128000
            )
            compaction_threshold = await get_setting_value(
                db_session, "session.compaction_threshold", 0.85
            )
        return cls(
            memory=memory,
            guardrails=guardrails,
            llm=llm,
            session_cache=session_cache,
            session_manager=session_manager,
            max_context_tokens=int(max_context_tokens)
            if isinstance(max_context_tokens, int)
            else 128000,
            compaction_threshold=float(compaction_threshold)
            if isinstance(compaction_threshold, (int, float))
            else 0.85,
        )

    async def assemble(
        self,
        *,
        session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        user_message: str,
        user_message_role: str = "user",
        tool_definitions: list[ToolDefinition] | None = None,
        active_delegations: list[dict[str, Any]] | None = None,
    ) -> ContextAssemblyResult:
        """Build the LLM message list for a single turn."""

        cached_intention = self.session_cache.get_intention(session.session_id)
        search_mode = "find" if session.mnemory_session_id is None else "search"

        with scoped_runtime_context(user_email=session.user_email, agent_id=session.agent_id):
            recall_task = self.memory.recall(
                query=user_message,
                session_id=session.mnemory_session_id,
                labels=conversation.context.memory_labels,
                context=cached_intention,
                search_mode=search_mode,
            )
            refresh_task = self.session_cache.refresh(session)
            intention_task = self.guardrails.get_session(
                session.intaris_session_id or session.session_id
            )
            gathered_results: tuple[Any, Any, Any] = await asyncio.gather(
                recall_task,
                refresh_task,
                intention_task,
                return_exceptions=True,
            )
        recall_result, cache_result, intention_result = gathered_results

        degraded_sources: list[str] = []

        if isinstance(cache_result, Exception):
            cache_entry = self.session_cache.get_entry(session.session_id)
            if cache_entry is None or not cache_entry.initialized:
                raise cache_result
            degraded_sources.append("events")
        else:
            cache_entry = cache_result

        if isinstance(intention_result, Exception):
            degraded_sources.append("intention")
        else:
            await self.session_cache.update_intention(
                session.session_id, intention_result.intention
            )
            cache_entry.intention = intention_result.intention

        memory_block = None
        if isinstance(recall_result, Exception):
            degraded_sources.append("memory")
            recall_payload: dict[str, Any] | None = None
        else:
            recall_payload = recall_result
            memory_block = _format_memory_context(recall_payload)
            recall_session_id = str(recall_payload.get("session_id") or "").strip()
            if session.mnemory_session_id is None and recall_session_id:
                updated = await self.session_manager.attach_mnemory_session(
                    session.session_id, recall_session_id
                )
                if updated:
                    session.mnemory_session_id = recall_session_id

        resolved_model = await self.llm.resolve_model(
            explicit_model=agent.llm_config.model if agent.llm_config else None,
            task_type="default",
        )
        model_info = await self.llm.get_model_info(resolved_model)
        if model_info.model_id == "unknown":
            degraded_sources.append("model_info")

        max_context_tokens = min(self.max_context_tokens, model_info.context_window)
        reserve_output_tokens = (
            agent.llm_config.max_tokens
            if agent.llm_config and agent.llm_config.max_tokens is not None
            else model_info.max_output_tokens
        )
        system_prompt_tokens, tool_schema_tokens = self._count_static_tokens(
            resolved_model=resolved_model,
            system_prompt=agent.system_prompt,
            tool_definitions=tool_definitions or [],
        )
        static_tokens = system_prompt_tokens + tool_schema_tokens
        dynamic_tokens = max(0, max_context_tokens - static_tokens - reserve_output_tokens)
        max_prompt_tokens = max(0, max_context_tokens - reserve_output_tokens)

        messages: list[dict[str, Any]] = []
        if agent.system_prompt:
            messages.append({"role": "system", "content": agent.system_prompt})
        compaction_summary = cache_entry.last_compaction_summary
        if compaction_summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"Compaction summary:\n{compaction_summary}",
                }
            )
        if memory_block is not None:
            messages.append({"role": "system", "content": memory_block})

        history_messages = self._events_to_messages(
            self.session_cache.get_events_since_compaction(
                session.session_id, EVENT_TYPES_FOR_CONTEXT
            )
        )
        messages.extend(history_messages)

        if active_delegations:
            messages.append(
                {"role": "system", "content": _format_active_delegations(active_delegations)}
            )
        messages.append({"role": user_message_role, "content": user_message})

        messages = self._prune_messages(
            messages=messages,
            resolved_model=resolved_model,
            max_prompt_tokens=max_prompt_tokens,
            system_prompt=agent.system_prompt,
            tool_schema_tokens=tool_schema_tokens,
        )
        prompt_tokens = (
            self.llm.count_messages_tokens(messages, resolved_model) + tool_schema_tokens
        )
        recommend_compaction = (
            max_context_tokens > 0
            and (prompt_tokens / max_context_tokens) >= self.compaction_threshold
        )
        return ContextAssemblyResult(
            messages=messages,
            degraded=bool(degraded_sources),
            degraded_sources=sorted(set(degraded_sources)),
            resolved_model=resolved_model,
            static_tokens=static_tokens,
            dynamic_tokens=dynamic_tokens,
            prompt_tokens=prompt_tokens,
            recommend_compaction=recommend_compaction,
        )

    def _count_static_tokens(
        self,
        *,
        resolved_model: str,
        system_prompt: str | None,
        tool_definitions: list[ToolDefinition],
    ) -> tuple[int, int]:
        system_prompt_tokens = 0
        tool_schema_tokens = 0
        if system_prompt:
            system_prompt_tokens += self.llm.count_tokens(system_prompt, resolved_model)
        if tool_definitions:
            schemas = json.dumps(
                [tool.model_dump(mode="json") for tool in tool_definitions],
                sort_keys=True,
            )
            tool_schema_tokens += self.llm.count_tokens(schemas, resolved_model)
        return system_prompt_tokens, tool_schema_tokens

    def _events_to_messages(self, events: list[Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for event in events:
            if event.type == "user_message":
                content = event.data.get("content")
                if isinstance(content, str):
                    messages.append({"role": "user", "content": content})
            elif event.type == "assistant_message":
                content = event.data.get("content")
                if isinstance(content, str):
                    messages.append({"role": "assistant", "content": content})
            elif event.type == "tool_result":
                output = event.data.get("output")
                if isinstance(output, str):
                    messages.append({"role": "system", "content": output})
            elif event.type == "tool_call":
                tool_name = event.data.get("tool_name")
                if isinstance(tool_name, str):
                    messages.append({"role": "assistant", "content": f"[Tool call: {tool_name}]"})
            elif event.type == "delegation":
                messages.append(
                    {
                        "role": "system",
                        "content": _format_delegation_status(event.data),
                    }
                )
            elif event.type in {"task_result", "task_failed", "task_cancelled"}:
                messages.append(
                    {
                        "role": "system",
                        "content": _format_task_update(event.type, event.data),
                    }
                )
        return messages

    def _prune_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        resolved_model: str,
        max_prompt_tokens: int,
        system_prompt: str | None,
        tool_schema_tokens: int,
    ) -> list[dict[str, Any]]:
        if max_prompt_tokens <= 0:
            return messages

        pruned_messages = list(messages)
        while (
            self.llm.count_messages_tokens(pruned_messages, resolved_model) + tool_schema_tokens
            > max_prompt_tokens
        ):
            memory_index = next(
                (
                    index
                    for index, message in enumerate(pruned_messages)
                    if message.get("role") == "system"
                    and isinstance(message.get("content"), str)
                    and '<memory_context trust="untrusted">' in str(message.get("content"))
                ),
                None,
            )
            if memory_index is not None:
                pruned_messages.pop(memory_index)
                continue

            removable_history_index = next(
                (
                    index
                    for index, message in enumerate(pruned_messages)
                    if index != len(pruned_messages) - 1
                    and not (
                        system_prompt is not None
                        and message.get("role") == "system"
                        and message.get("content") == system_prompt
                    )
                    and not (
                        message.get("role") == "system"
                        and isinstance(message.get("content"), str)
                        and str(message.get("content")).startswith("Compaction summary:\n")
                    )
                ),
                None,
            )
            if removable_history_index is None:
                break
            pruned_messages.pop(removable_history_index)
        return pruned_messages


def _format_memory_context(recall_payload: dict[str, Any]) -> str | None:
    parts: list[str] = []
    core_memories = recall_payload.get("core_memories")
    if isinstance(core_memories, str) and core_memories.strip():
        parts.append("Core memories:\n" + core_memories.strip())

    search_results = recall_payload.get("search_results")
    if isinstance(search_results, list) and search_results:
        lines = []
        for result in search_results:
            if not isinstance(result, dict):
                continue
            memory = result.get("memory")
            if not isinstance(memory, str) or not memory.strip():
                continue
            score = result.get("score")
            prefix = f"- ({score:.2f}) " if isinstance(score, (int, float)) else "- "
            lines.append(prefix + memory.strip())
        if lines:
            parts.append("Relevant memories:\n" + "\n".join(lines))

    if not parts:
        return None
    return '<memory_context trust="untrusted">\n' + "\n\n".join(parts) + "\n</memory_context>"


def _format_delegation_status(data: dict[str, Any]) -> str:
    child_session_id = data.get("child_session_id", "unknown")
    status = data.get("status", "unknown")
    result_summary = data.get("result_summary")
    summary = f"Delegation {child_session_id}: {status}"
    if isinstance(result_summary, str) and result_summary:
        summary = f"{summary}\nResult: {result_summary}"
    return summary


def _format_active_delegations(active_delegations: list[dict[str, Any]]) -> str:
    lines = ["Active delegations:"]
    for delegation in active_delegations:
        child_session_id = delegation.get("child_session_id", "unknown")
        status = delegation.get("status", "unknown")
        task = delegation.get("task")
        line = f"- {child_session_id}: {status}"
        if isinstance(task, str) and task:
            line = f"{line} ({task})"
        lines.append(line)
    return "\n".join(lines)


def _format_task_update(event_type: str, data: dict[str, Any]) -> str:
    title = data.get("title") or data.get("task_title") or data.get("task_id") or "Background task"
    result_summary = data.get("result_summary") or "No summary provided."
    status = {
        "task_result": "completed",
        "task_failed": "failed",
        "task_cancelled": "cancelled",
    }.get(event_type, "updated")
    return f"Task update: {title} {status}. Summary: {result_summary}"
